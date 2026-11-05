//! Recording loading and shared replay layer controls.
use crate::app::MissionControl;
use rerun::external::egui;

impl MissionControl {
    pub(crate) fn recordings(&mut self, ui: &mut egui::Ui) {
        if ui
            .add_enabled(
                self.ready && !self.recording_busy,
                egui::Button::new("Load recording…"),
            )
            .clicked()
        {
            self.files("load_recording");
        }
        ui.small("Live vehicle state remains in this panel while Rerun displays a replay. Recorded data never sends vehicle commands.");
        if !self.notice.is_empty() {
            ui.label(&self.notice);
        }
        egui::CollapsingHeader::new("Session details & help").show(ui, |ui| {
            if let Some(path) = self.state["session_directory"].as_str() {
                ui.small(format!("Live session folder: {path}"));
                ui.small("MCAP, RRD, and JSONL are saved together. Active files use .part names; completed files become available when the session closes.");
            }
            if let Some(path) = self.recording["rerun_path"].as_str() {
                ui.small(format!("Latest replay: {path}"));
            }
            ui.small("The replay timeline synchronizes maps, scalar plots, and readings. Add views, inspect entities, and configure visible time ranges in the Blueprint panel.");
            ui.small("Use Rerun’s File menu to save a recording as RRD or save your layout as a blueprint.");
        });
        ui.separator();
        ui.add_enabled_ui(self.ready && !self.recording_busy && !self.quitting, |ui| {
            self.plots(ui);
        });
    }
}
