//! Moves inline images out of a v4 block. Each base64 raster data URI becomes an `image:v4` reference, and its
//! decoded bytes go to the flush's image pack.
//!
//! The scan is one `memmem` pass for `data:image/`. A data URI looks the same in an attribute, a CSS `url()`, a
//! `srcset` candidate or stylesheet text, so the scan needs no knowledge of where it sits. Every other byte is
//! copied as it is. Nothing here protects privacy, so an image the scan cannot take stays inline in the sealed block.

use std::borrow::Cow;
use std::collections::HashSet;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use memchr::memmem;

use crate::refs::{image_ref, reference_hash};

const PREFIX: &[u8] = b"data:image/";
const BASE64_MARKER: &[u8] = b";base64,";

#[derive(Debug, Clone, Copy)]
pub struct ImageLimits {
    pub max_image_bytes: usize,
    pub max_images: usize,
    pub max_total_bytes: usize,
}

impl Default for ImageLimits {
    /// The per-image and per-message caps of the v3 collector.
    fn default() -> Self {
        Self {
            max_image_bytes: 900 * 1024,
            max_images: 4096,
            max_total_bytes: 64 * 1024 * 1024,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Image {
    pub reference: String,
    pub mime: &'static str,
    pub bytes: Vec<u8>,
}

/// Collects the images of one message. One sink per message keeps the caps per message, as in v3.
pub struct ImageSink<'k> {
    content_key: &'k [u8; 32],
    team_id: i64,
    key_day: &'k str,
    limits: ImageLimits,
    seen: HashSet<String>,
    total_bytes: usize,
    pub images: Vec<Image>,
    /// Data URIs left inline: over a cap, not decodable, or not a supported raster type.
    pub left_inline: u32,
}

impl<'k> ImageSink<'k> {
    pub fn new(
        content_key: &'k [u8; 32],
        team_id: i64,
        key_day: &'k str,
        limits: ImageLimits,
    ) -> Self {
        Self {
            content_key,
            team_id,
            key_day,
            limits,
            seen: HashSet::new(),
            total_bytes: 0,
            images: Vec::new(),
            left_inline: 0,
        }
    }

    fn take(&mut self, mime: &'static str, payload: &[u8]) -> Option<String> {
        if self.images.len() >= self.limits.max_images
            || payload.len() > self.limits.max_image_bytes.div_ceil(3) * 4
        {
            return None;
        }
        let bytes = STANDARD.decode(payload).ok()?;
        if bytes.len() > self.limits.max_image_bytes
            || self.total_bytes + bytes.len() > self.limits.max_total_bytes
        {
            return None;
        }
        let reference = image_ref(
            self.team_id,
            self.key_day,
            &reference_hash(self.content_key, &bytes),
        );
        // An image repeated in one message is stored once and referenced at every place it appears.
        if self.seen.insert(reference.clone()) {
            self.total_bytes += bytes.len();
            self.images.push(Image {
                reference: reference.clone(),
                mime,
                bytes,
            });
        }
        Some(reference)
    }
}

fn raster_mime(subtype: &[u8]) -> Option<&'static str> {
    match subtype.to_ascii_lowercase().as_slice() {
        b"png" => Some("image/png"),
        b"jpeg" | b"jpg" => Some("image/jpeg"),
        b"gif" => Some("image/gif"),
        b"webp" => Some("image/webp"),
        _ => None,
    }
}

fn is_base64(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'=')
}

/// Replaces every inline raster image in `bytes` with its reference. Returns the input unchanged when it holds none.
pub fn extract_images<'b>(bytes: &'b [u8], sink: &mut ImageSink<'_>) -> Cow<'b, [u8]> {
    let mut out: Option<Vec<u8>> = None;
    let mut copied_to = 0;
    for start in memmem::find_iter(bytes, PREFIX) {
        if start < copied_to {
            continue;
        }
        let subtype_start = start + PREFIX.len();
        let Some(subtype_length) = bytes[subtype_start..].iter().position(|&b| b == b';') else {
            break;
        };
        let marker = subtype_start + subtype_length;
        let payload_start = marker + BASE64_MARKER.len();
        let taken = raster_mime(&bytes[subtype_start..marker])
            .filter(|_| bytes[marker..].starts_with(BASE64_MARKER))
            .and_then(|mime| {
                let length = bytes[payload_start..]
                    .iter()
                    .position(|&b| !is_base64(b))
                    .unwrap_or(bytes.len() - payload_start);
                // An escaped slash (`\/` or `\u002F`) continues the payload, which a plain run cannot decode. Any
                // other escape, such as the `\"` that closes a quoted CSS url, ends it.
                let end = payload_start + length;
                if bytes.get(end) == Some(&b'\\') && matches!(bytes.get(end + 1), Some(b'/' | b'u'))
                {
                    return None;
                }
                sink.take(mime, &bytes[payload_start..payload_start + length])
                    .map(|reference| (reference, payload_start + length))
            });
        match taken {
            Some((reference, end)) => {
                let out = out.get_or_insert_with(|| Vec::with_capacity(bytes.len()));
                out.extend_from_slice(&bytes[copied_to..start]);
                out.extend_from_slice(reference.as_bytes());
                copied_to = end;
            }
            None => sink.left_inline += 1,
        }
    }
    match out {
        Some(mut out) => {
            out.extend_from_slice(&bytes[copied_to..]);
            Cow::Owned(out)
        }
        None => Cow::Borrowed(bytes),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: [u8; 32] = [3; 32];

    fn sink(limits: ImageLimits) -> ImageSink<'static> {
        ImageSink::new(&KEY, 7, "2026-10-15", limits)
    }

    fn data_uri(mime: &str, bytes: &[u8]) -> String {
        format!("data:{mime};base64,{}", STANDARD.encode(bytes))
    }

    /// Puts each image back in place of its reference, as a reader of the dataset does.
    fn reinline(bytes: &[u8], images: &[Image]) -> Vec<u8> {
        let mut text = String::from_utf8(bytes.to_vec()).unwrap();
        for image in images {
            text = text.replace(&image.reference, &data_uri(image.mime, &image.bytes));
        }
        text.into_bytes()
    }

    #[test]
    fn moves_images_out_of_every_slot_and_back_without_changing_a_byte() {
        let png = data_uri("image/png", b"png bytes");
        let jpeg = data_uri("image/JPEG", b"jpeg bytes");
        // The stylesheet text sits inside a JSON string, so its quotes arrive escaped.
        let event = format!(
            r#"{{"type":2,"data":{{"node":{{"attributes":{{"src":"{png}","srcset":"{jpeg} 1x, {png} 2x","style":"background:url({jpeg})"}},"childNodes":[{{"textContent":".a{{background:url(\"{png}\")}}"}}]}}}}}}"#
        );
        let mut images = sink(ImageLimits::default());
        let out = extract_images(event.as_bytes(), &mut images);

        assert!(!String::from_utf8_lossy(&out).contains("base64"));
        assert_eq!(images.images.len(), 2, "each distinct image is stored once");
        assert_eq!(images.left_inline, 0);
        assert_eq!(
            reinline(&out, &images.images),
            event.replace("image/JPEG", "image/jpeg").into_bytes()
        );
    }

    #[test]
    fn leaves_what_it_cannot_take_inline() {
        let big = vec![1u8; 1000];
        let cases = [
            (data_uri("image/svg+xml", b"<svg/>"), ImageLimits::default()),
            (data_uri("image/bmp", b"BM"), ImageLimits::default()),
            ("data:image/png;utf8,abc".to_owned(), ImageLimits::default()),
            (
                "data:image/png;base64,iVBOR\\u002Fw".to_owned(),
                ImageLimits::default(),
            ),
            (
                data_uri("image/png", &big),
                ImageLimits {
                    max_image_bytes: 999,
                    ..ImageLimits::default()
                },
            ),
            (
                data_uri("image/png", &big),
                ImageLimits {
                    max_images: 0,
                    ..ImageLimits::default()
                },
            ),
        ];
        for (uri, limits) in cases {
            let event = format!(r#"{{"src":"{uri}"}}"#);
            let mut images = sink(limits);
            assert!(
                matches!(
                    extract_images(event.as_bytes(), &mut images),
                    Cow::Borrowed(_)
                ),
                "{uri}"
            );
            assert_eq!((images.images.len(), images.left_inline), (0, 1), "{uri}");
        }
    }
}
