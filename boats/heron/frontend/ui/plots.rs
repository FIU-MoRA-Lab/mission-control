//! Independent field visibility with an observed, schema-backed layer catalog.
use crate::app::MissionControl;
use rerun::external::egui;
use serde_json::json;
use std::collections::BTreeMap;

impl MissionControl {
    pub(crate) fn cache_plot_catalog(&mut self, value: serde_json::Value) {
        if let (Some(app), Some(recording)) = (
            value["application_id"].as_str(),
            value["recording_id"].as_str(),
        ) {
            let target = (app.to_owned(), recording.to_owned());
            if self.plot_target.as_ref() == Some(&target) {
                self.plot_catalog_retry_at = None;
                self.select_plot_catalog(&value);
            }
            self.plot_catalogs.insert(target, value);
        }
    }

    fn select_plot_catalog(&mut self, value: &serde_json::Value) {
        self.replay_preset = "fields".into();
        self.replay_fields = value["visible_fields"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|field| field.as_str().map(str::to_owned))
            .collect();
        self.plot_recording = value.clone();
    }

    fn sync_plot_recording(&mut self) {
        let Some(db) = self.viewer.recording_db() else {
            self.plot_target = None;
            self.plot_recording = serde_json::Value::Null;
            return;
        };
        let target = (
            db.application_id().to_string(),
            db.recording_id().to_string(),
        );
        if target.0 == "mission-control-live"
            && self.state["live_recording_id"].as_str() == Some(target.1.as_str())
        {
            self.plot_source = "live".into();
            self.plot_target = Some(target);
            return;
        }
        self.plot_source = "replay".into();
        let entity_count = db.sorted_entity_paths().count();
        if self.plot_target.as_ref() == Some(&target)
            && (self.plot_recording.is_null()
                || self.plot_recording["entity_count"]
                    .as_u64()
                    .is_none_or(|count| count == entity_count as u64))
        {
            return;
        }
        if let Some(value) = self.plot_catalogs.get(&target).cloned() {
            if value["entity_count"]
                .as_u64()
                .is_none_or(|count| count == entity_count as u64)
            {
                self.plot_target = Some(target);
                self.plot_catalog_retry_at = None;
                self.select_plot_catalog(&value);
                return;
            }
        }
        // The loader returns the complete catalog. Scanning a partially received
        // RRD can race that response and block on the backend's replay lock.
        if !self.ready
            || self.recording_busy
            || self
                .plot_catalog_retry_at
                .is_some_and(|at| std::time::Instant::now() < at)
        {
            return;
        }
        let entities = crate::viewer::chart_entities(db);
        if entities.is_empty() && db.num_physical_chunks() == 0 {
            return;
        }
        self.plot_recording = serde_json::Value::Null;
        self.plot_catalog_retry_at = None;
        self.plot_target = Some(target.clone());
        self.command("recording_catalog", json!({"application_id":target.0,"recording_id":target.1,"entities":entities,"entity_count":entity_count}));
    }

    pub(crate) fn plots(&mut self, ui: &mut egui::Ui) {
        self.sync_plot_recording();
        ui.heading("Data layers");
        ui.label("Choose a chart preset or individual fields. Geographic readings also appear on the map.");
        ui.small(if self.plot_source == "live" {
            "Editing the selected live recording"
        } else {
            "Editing the selected recording"
        });
        if self.plot_target.is_none()
            || (self.plot_source == "replay" && self.plot_recording.is_null())
        {
            ui.label("Reading the selected recording’s fields…");
            return;
        }
        let schema = if self.plot_source == "live" {
            &self.state["field_catalog"]
        } else {
            &self.plot_recording["field_catalog"]
        }
        .as_array()
        .cloned()
        .unwrap_or_default();
        let source_presets = if self.plot_source == "live" {
            &self.state["chart_presets"]
        } else {
            &self.plot_recording["chart_presets"]
        };
        let presets = available_presets(source_presets, &self.state["chart_presets"], &schema);
        let preset = if self.plot_source == "live" {
            &mut self.live_preset
        } else {
            &mut self.replay_preset
        };
        let previous_preset = preset.clone();
        ui.small("Chart preset");
        if presets.is_empty() {
            ui.colored_label(
                egui::Color32::LIGHT_RED,
                "Preset metadata is unavailable. Restart the app to load the updated backend.",
            );
        }
        let preset_label = presets
            .iter()
            .find(|item| item["id"].as_str() == Some(preset.as_str()))
            .and_then(|item| item["label"].as_str())
            .unwrap_or("Individual fields");
        ui.add_enabled_ui(self.ready && !self.recording_busy && !self.quitting, |ui| {
            egui::ComboBox::from_id_salt("chart-preset")
                .selected_text(preset_label)
                .show_ui(ui, |ui| {
                    for item in &presets {
                        if let (Some(id), Some(label)) =
                            (item["id"].as_str(), item["label"].as_str())
                        {
                            ui.selectable_value(preset, id.to_owned(), label);
                        }
                    }
                });
        });
        if matches!(preset.as_str(), "controls" | "throttle" | "steering") {
            ui.small("Overlay scale: PWM µs. Requested and MANUAL_CONTROL inputs use 1500 + value × 0.5. Servo 1/3 are output commands, not measured thrust; left/right assignment is unverified. Raw fields remain available below.");
        } else if preset == "thruster_feedback" {
            ui.small("Reported RPM indicates motor rotation when feedback is available. Current indicates electrical load; voltage indicates supply power. Battery current includes other equipment. Servo 1/3 are commands, not measured motion or thrust. Feedback charts appear only for available telemetry; ESC/RPM channel mapping must be verified.");
        } else if preset == "state_changes" {
            ui.small("State lanes show ARMED/DISARMED, MANUAL/AUTO/HOLD and mission transitions. Vehicle lanes come from received messages; GCS lanes show local controller state. Unknown values create gaps. Older RRDs may lack state lanes.");
        } else if matches!(preset.as_str(), "states" | "activity") {
            ui.small("Booleans use categorical state lanes. Armed/mode state comes from vehicle heartbeats; local mission flags describe the ground station.");
        }
        ui.add(
            egui::TextEdit::singleline(&mut self.layer_search)
                .hint_text("Search fields, units, sources, or tags…"),
        );
        let search = self.layer_search.to_lowercase();
        let selected = if self.plot_source == "live" {
            &mut self.live_fields
        } else {
            &mut self.replay_fields
        };
        let mut changed = previous_preset != *preset;
        if previous_preset != *preset && preset != "fields" {
            *selected = presets
                .iter()
                .find(|item| item["id"].as_str() == Some(preset.as_str()))
                .and_then(|item| item["visible_fields"].as_array())
                .into_iter()
                .flatten()
                .filter_map(|field| field.as_str().map(str::to_owned))
                .collect();
        }
        ui.horizontal(|ui| {
            ui.label(format!("{} fields visible", selected.len()));
            if ui.button("Hide all").clicked() {
                selected.clear();
                *preset = "fields".into();
                changed = true;
            }
            if ui.button("Essentials").clicked() {
                *preset = "fields".into();
                *selected = schema
                    .iter()
                    .filter(|field| field["default"].as_bool() == Some(true))
                    .filter_map(|field| field["id"].as_str().map(str::to_owned))
                    .collect();
                changed = true;
            }
        });
        ui.separator();
        let mut groups = BTreeMap::<String, Vec<&serde_json::Value>>::new();
        for field in &schema {
            let text = format!(
                "{} {} {} {} {} {} {} {}",
                field["label"],
                field["unit"],
                field["group"],
                field["source"],
                field["category"],
                field["tags"],
                field["description"],
                field["display_label"]
            )
            .to_lowercase();
            if search.is_empty() || text.contains(&search) {
                groups
                    .entry(field["category"].as_str().unwrap_or("Other").to_owned())
                    .or_default()
                    .push(field);
            }
        }
        if schema.is_empty() {
            ui.label("Waiting for vehicle fields or a recording…");
        } else if groups.is_empty() {
            ui.label("No matching fields.");
        }
        for (group, mut fields) in groups {
            fields.sort_by_key(|field| {
                let stage = match field["stage"].as_str().unwrap_or("") {
                    "requested" | "display" => 0,
                    "sent" => 1,
                    _ => 2,
                };
                (stage, field["label"].as_str().unwrap_or(""))
            });
            let enabled = fields
                .iter()
                .filter(|field| selected.contains(field["id"].as_str().unwrap_or("")))
                .count();
            let group_label = group
                .split_once(" · ")
                .map(|(_, label)| label)
                .unwrap_or(&group);
            egui::CollapsingHeader::new(format!("{group_label} · {enabled}/{}", fields.len()))
                .id_salt((self.plot_source.as_str(), group.as_str(), search.is_empty()))
                .default_open(
                    group.starts_with("04")
                        || fields.iter().any(|field| field["kind"] == "telemetry")
                        || !search.is_empty(),
                )
                .show(ui, |ui| {
                    if let Some(help) = fields
                        .first()
                        .and_then(|field| field["category_help"].as_str())
                        .filter(|help| !help.is_empty())
                    {
                        ui.small(help);
                    }
                    ui.horizontal(|ui| {
                        if ui.small_button("Show available").clicked() {
                            for field in &fields {
                                if field["available"] != false {
                                    if let Some(id) = field["id"].as_str() {
                                        selected.insert(id.to_owned());
                                    }
                                }
                            }
                            changed = true;
                        }
                        if ui.small_button("Hide group").clicked() {
                            for field in &fields {
                                if let Some(id) = field["id"].as_str() {
                                    selected.remove(id);
                                }
                            }
                            changed = true;
                        }
                    });
                    for field in fields {
                        let id = field["id"].as_str().unwrap_or("");
                        let label = field["display_label"]
                            .as_str()
                            .or_else(|| field["label"].as_str())
                            .unwrap_or(id);
                        let unit = field["unit"].as_str().unwrap_or("");
                        let text = if unit.is_empty() {
                            label.to_owned()
                        } else {
                            format!("{label} · {unit}")
                        };
                        let text = if field["kind"] == "telemetry" || field["kind"] == "diagnostic"
                        {
                            text
                        } else {
                            let direction = field["direction"].as_str().unwrap_or("raw");
                            let availability = if field["available"] == false {
                                " · waiting for data"
                            } else {
                                ""
                            };
                            format!("{direction} · {text}{availability}")
                        };
                        let mut visible = selected.contains(id);
                        let mut details = format!(
                            "{}\nSource: {}\nType: {}\nTags: {}",
                            field["description"].as_str().unwrap_or(""),
                            field["source"].as_str().unwrap_or(""),
                            field["data_type"].as_str().unwrap_or("number"),
                            field["tags"]
                                .as_array()
                                .map(|tags| tags
                                    .iter()
                                    .filter_map(|tag| tag.as_str())
                                    .collect::<Vec<_>>()
                                    .join(", "))
                                .unwrap_or_default()
                        );
                        if let Some(enumeration) = field["enum"].as_str() {
                            details.push_str(&format!("\nEnum: {enumeration}"));
                        }
                        if field["map_path"].is_string() {
                            details.push_str("\nIncludes a geographic reading layer");
                        }
                        if ui
                            .add_enabled(
                                self.ready
                                    && !self.recording_busy
                                    && !self.quitting
                                    && (field["available"] != false || visible),
                                egui::Checkbox::new(&mut visible, text),
                            )
                            .on_hover_text(details)
                            .changed()
                        {
                            if visible {
                                selected.insert(id.to_owned());
                            } else {
                                selected.remove(id);
                            }
                            changed = true;
                        }
                    }
                });
        }
        if changed && self.ready && !self.recording_busy && !self.quitting {
            let fields: Vec<_> = selected.iter().cloned().collect();
            let preset = preset.clone();
            let mut data =
                json!({"source":self.plot_source,"visible_fields":fields,"preset":preset});
            if self.plot_source == "replay" {
                data["application_id"] = self.plot_recording["application_id"].clone();
                data["recording_id"] = self.plot_recording["recording_id"].clone();
            }
            self.command("configure_plots", data);
        }
        ui.separator();
        ui.small("Related fields are grouped together: requested input, sent packets and received telemetry. ‘Received’ means received by the ground station, not acknowledgment of a command. Waiting fields become selectable when data arrives.");
        ui.small("MAVLink units come from the vehicle dialect. Throttle, steering and active thruster outputs are enabled by default. Unknown sensor units are left unspecified. Visibility changes preserve recorded data.");
    }
}

/// Older/cached replay catalogs may lack preset metadata. Reuse the backend's
/// current definitions, retaining only fields that exist in this recording.
fn available_presets(
    source: &serde_json::Value,
    fallback: &serde_json::Value,
    schema: &[serde_json::Value],
) -> Vec<serde_json::Value> {
    if let Some(presets) = source.as_array().filter(|presets| !presets.is_empty()) {
        return presets.clone();
    }
    let fields: std::collections::BTreeSet<_> = schema
        .iter()
        .filter_map(|field| field["id"].as_str())
        .collect();
    fallback
        .as_array()
        .into_iter()
        .flatten()
        .cloned()
        .map(|mut preset| {
            let selected: Vec<_> = preset["visible_fields"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|field| field.as_str().is_some_and(|field| fields.contains(field)))
                .cloned()
                .collect();
            preset["visible_fields"] = json!(selected);
            preset
        })
        .collect()
}

#[cfg(test)]
mod preset_tests {
    use super::*;

    #[test]
    fn replay_without_metadata_keeps_presets_and_filters_live_only_fields() {
        let definitions = json!([
            {"id":"controls","label":"Control chain","visible_fields":["/shared", "/live-only"]},
            {"id":"state_changes","label":"State changes","visible_fields":[]}
        ]);
        let schema = vec![json!({"id":"/shared"})];
        for missing in [serde_json::Value::Null, json!([])] {
            let presets = available_presets(&missing, &definitions, &schema);
            assert_eq!(presets.len(), 2);
            assert_eq!(presets[0]["visible_fields"], json!(["/shared"]));
            assert_eq!(presets[1]["id"], "state_changes");
        }
        let replay = json!([{"id":"fields","label":"Individual fields","visible_fields":[]}]);
        assert_eq!(
            available_presets(&replay, &definitions, &schema),
            replay.as_array().unwrap().clone()
        );
    }
}
