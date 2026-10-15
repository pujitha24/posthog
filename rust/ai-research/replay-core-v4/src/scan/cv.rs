//! `cv` events: posthog-js compresses a full snapshot's data, and a mutation's `adds`, `texts`, `attributes` and
//! `removes`, as gzip stored one byte per latin-1 codepoint in a JSON string. The anonymizer re-emits them as zstd.

use std::borrow::Cow;

use posthog_replay_anonymizer::compression;
use posthog_replay_anonymizer::scan::{self, Span};

use super::images::{extract_images, ImageSink};
use super::message::Event;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CvError {
    #[error("cv string is not latin-1")]
    NotLatin1,
    #[error("cv payload does not decompress: {0}")]
    Decompress(String),
    #[error("malformed cv string: {0}")]
    Malformed(&'static str),
}

/// Decodes the JSON string at `span` to the JSON it compresses.
pub fn decode(bytes: &[u8], span: Span) -> Result<Vec<u8>, CvError> {
    let text = scan::unescape(bytes, span).map_err(|e| CvError::Malformed(e.0))?;
    let mut compressed = Vec::with_capacity(text.len());
    for c in text.chars() {
        compressed.push(u8::try_from(u32::from(c)).map_err(|_| CvError::NotLatin1)?);
    }
    compression::decompress_by_magic(&compressed).map_err(|e| CvError::Decompress(format!("{e:#}")))
}

/// Compresses JSON as zstd and writes it as a quoted latin-1 JSON string.
pub fn encode(json: &[u8]) -> Result<Vec<u8>, CvError> {
    let compressed =
        compression::compress_cv(json).map_err(|e| CvError::Decompress(format!("{e:#}")))?;
    let text: String = compressed.iter().map(|&b| char::from(b)).collect();
    let mut out = Vec::with_capacity(text.len() + 2);
    scan::write_json_string(&text, &mut out);
    Ok(out)
}

/// The compressed strings in an event: `data` itself, or the string fields at the top level of a `data` object.
pub fn compressed_spans(inner: &[u8], event: &Event) -> Vec<Span> {
    let Some(data) = event.data.filter(|_| event.compressed) else {
        return Vec::new();
    };
    if scan::is_string(inner, data) {
        return vec![data];
    }
    let Ok(entries) = scan::object_entries(inner, data) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .map(|entry| entry.value)
        .filter(|&value| scan::is_string(inner, value))
        .collect()
}

/// Moves the images out of one event, inside its compressed strings too. Returns `None` when nothing changed.
///
/// A compressed string that does not decode stays as it is. Its images stay inline, sealed like the rest.
pub fn extract_event_images(
    inner: &[u8],
    event: &Event,
    sink: &mut ImageSink<'_>,
) -> Option<Vec<u8>> {
    let bytes = &inner[event.span.0..event.span.1];
    let compressed = compressed_spans(inner, event);
    if compressed.is_empty() {
        return match extract_images(bytes, sink) {
            Cow::Borrowed(_) => None,
            Cow::Owned(out) => Some(out),
        };
    }
    let mut out: Option<Vec<u8>> = None;
    let mut copied_to = event.span.0;
    for span in compressed {
        let Ok(json) = decode(inner, span) else {
            continue;
        };
        let Cow::Owned(extracted) = extract_images(&json, sink) else {
            continue;
        };
        let Ok(encoded) = encode(&extracted) else {
            continue;
        };
        let out = out.get_or_insert_with(|| Vec::with_capacity(bytes.len()));
        out.extend_from_slice(&inner[copied_to..span.0]);
        out.extend_from_slice(&encoded);
        copied_to = span.1;
    }
    out.map(|mut out| {
        out.extend_from_slice(&inner[copied_to..event.span.1]);
        out
    })
}

#[cfg(test)]
mod tests {
    use base64::engine::general_purpose::STANDARD;
    use base64::Engine;
    use serde_json::{json, Value};

    use super::*;
    use crate::scan::images::ImageLimits;
    use crate::scan::message::read_message;

    const KEY: [u8; 32] = [3; 32];

    /// The posthog-js wire form: gzip, one byte per latin-1 codepoint.
    fn sdk_cv(value: &Value) -> String {
        compression::gzip(value.to_string().as_bytes())
            .unwrap()
            .iter()
            .map(|&b| char::from(b))
            .collect()
    }

    fn message_with(events: Value) -> Vec<u8> {
        let inner = json!({"event": "$snapshot_items", "properties": {"$session_id": "s", "$snapshot_items": events}});
        json!({"distinct_id": "d", "data": inner.to_string()})
            .to_string()
            .into_bytes()
    }

    fn decoded(event: &[u8], field: &[&str]) -> Value {
        let event: Value = serde_json::from_slice(event).unwrap();
        let mut value = &event["data"];
        for name in field {
            value = &value[name];
        }
        let compressed: Vec<u8> = value.as_str().unwrap().chars().map(|c| c as u8).collect();
        serde_json::from_slice(&compression::decompress_by_magic(&compressed).unwrap()).unwrap()
    }

    #[test]
    fn moves_images_out_of_compressed_snapshots_and_mutations() {
        let image = format!("data:image/png;base64,{}", STANDARD.encode(b"png bytes"));
        let snapshot = json!({"node": {"attributes": {"src": image}}});
        let adds = json!([{"parentId": 1, "node": {"attributes": {"src": image}}}]);
        let texts = sdk_cv(&json!([{"id": 2, "value": "kept"}]));
        let message = read_message(message_with(json!([
            {"type": 2, "timestamp": 1, "data": sdk_cv(&snapshot), "cv": "2024-10"},
            {"type": 3, "timestamp": 2, "data": {"source": 0, "adds": sdk_cv(&adds), "texts": texts}, "cv": "2024-10"},
        ])))
        .unwrap();
        let mut sink = ImageSink::new(&KEY, 7, "2026-10-15", ImageLimits::default());

        let full = extract_event_images(&message.inner, &message.events[0], &mut sink).unwrap();
        let reference = decoded(&full, &[])["node"]["attributes"]["src"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(reference.starts_with("image:v4:7:2026-10-15:"));

        let mutation = extract_event_images(&message.inner, &message.events[1], &mut sink).unwrap();
        assert_eq!(
            decoded(&mutation, &["adds"])[0]["node"]["attributes"]["src"],
            reference.as_str()
        );
        let mutation: Value = serde_json::from_slice(&mutation).unwrap();
        assert_eq!(
            mutation["data"]["texts"],
            texts.as_str(),
            "a string without images keeps its bytes"
        );
        assert_eq!(sink.images.len(), 1);
    }

    #[test]
    fn leaves_a_compressed_string_that_does_not_decode() {
        let message = read_message(message_with(json!([
            {"type": 2, "timestamp": 1, "data": "not gzip data:image/png;base64,AAAA", "cv": "2024-10"},
        ])))
        .unwrap();
        let mut sink = ImageSink::new(&KEY, 7, "2026-10-15", ImageLimits::default());
        assert_eq!(
            extract_event_images(&message.inner, &message.events[0], &mut sink),
            None
        );
        assert!(sink.images.is_empty());
    }
}
