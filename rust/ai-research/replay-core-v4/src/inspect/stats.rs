//! One row of the v4 block index. Every value covers its block only, so a session row is a fold of its block rows.

/// The block index columns from `replay-data.md`, apart from the location columns the writer adds.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct BlockStats {
    pub first_ts_ms: Option<i64>,
    pub last_ts_ms: Option<i64>,
    pub event_count: u64,
    pub byte_size: u64,
    pub host: Option<String>,

    pub full_snapshots: u32,
    pub events_before_first_full_snapshot: u32,
    pub orphan_mutations: u32,
    pub max_gap_ms: i64,
    pub dom_nodes: u32,
    pub dom_text_chars: u64,
    pub remote_stylesheets: u32,
    pub inline_stylesheets: u32,
    pub iframes: u32,
    pub canvases: u32,
    pub videos: u32,
    pub shadow_roots: u32,
    pub cold_start: bool,
    pub node_cap_reached: bool,

    pub active_ms: i64,
    pub clicks: u32,
    pub rage_clicks: u32,
    pub dead_clicks: u32,
    pub keypresses: u32,
    pub scrolls: u32,
    pub mouse_moves: u32,
    pub touches: u32,
    pub selections: u32,
    pub media_events: u32,
    pub viewport_resizes: u32,
    pub navigations: u32,
    pub console_errors: u32,
    pub console_warnings: u32,
    pub network_requests: u32,
    pub network_failures: u32,

    pub viewport_width: Option<u32>,
    pub viewport_height: Option<u32>,
    pub touch_input: bool,

    pub images_extracted: u32,
    pub image_bytes: u64,
    pub images_inline: u32,
    pub image_urls: u32,
}

impl BlockStats {
    /// Folds a later block into this one: sums, minimum and maximum times, the first host and the last viewport.
    pub fn merge(&mut self, later: &BlockStats) {
        self.first_ts_ms = min_option(self.first_ts_ms, later.first_ts_ms);
        self.last_ts_ms = self.last_ts_ms.max(later.last_ts_ms);
        self.host = self.host.take().or_else(|| later.host.clone());
        self.max_gap_ms = self.max_gap_ms.max(later.max_gap_ms);
        self.dom_nodes = self.dom_nodes.max(later.dom_nodes);
        self.dom_text_chars = self.dom_text_chars.max(later.dom_text_chars);
        self.cold_start |= later.cold_start;
        self.node_cap_reached |= later.node_cap_reached;
        self.touch_input |= later.touch_input;
        self.viewport_width = later.viewport_width.or(self.viewport_width);
        self.viewport_height = later.viewport_height.or(self.viewport_height);
        // A session's events before its first full snapshot all sit in the blocks before that snapshot.
        if self.full_snapshots == 0 {
            self.events_before_first_full_snapshot += later.events_before_first_full_snapshot;
        }
        macro_rules! sum {
            ($($field:ident),*) => { $( self.$field += later.$field; )* };
        }
        sum!(
            event_count,
            byte_size,
            full_snapshots,
            orphan_mutations,
            remote_stylesheets,
            inline_stylesheets,
            iframes,
            canvases,
            videos,
            shadow_roots,
            active_ms,
            clicks,
            rage_clicks,
            dead_clicks,
            keypresses,
            scrolls,
            mouse_moves,
            touches,
            selections,
            media_events,
            viewport_resizes,
            navigations,
            console_errors,
            console_warnings,
            network_requests,
            network_failures,
            images_extracted,
            image_bytes,
            images_inline,
            image_urls
        );
    }
}

fn min_option(a: Option<i64>, b: Option<i64>) -> Option<i64> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    }
}
