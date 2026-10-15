# FIU adaptation of Rerun re_view_map 0.38.1

Source: https://github.com/rerun-io/rerun/tree/0.38.1/crates/viewer/re_view_map
Licensed under upstream MIT OR Apache-2.0; the MIT notice is included here.

This local Cargo patch retains Rerun's GeoPoints/GeoLineStrings visualizers,
picking, time-range queries, and geographic pan/zoom. It replaces the fixed
online tile providers with Mission Control's authenticated loopback FIU source.
`fiu_source.rs` contains the provider and its metadata-driven configuration.
`map_view.rs` wires it into tile initialization, initial center, and the
selection panel. This application exposes only the FIU basemap.

Keep this crate aligned with the exact Rerun SDK/viewer version when upgrading.

Mission Control adds a planning UI hook. Toolbar and waypoint actions live in frontend/ui/map_controls.rs; the map supplies its geographic projector and live recording identity. Existing GeoPoints/GeoLineStrings renderers are retained.
