//! Camera reception is independent of vehicle connection and manual control.
use super::vehicle::{AMBER, GREEN};
use crate::app::MissionControl;
use rerun::external::egui;
use serde_json::json;

impl MissionControl {
    pub(crate) fn camera_controls(&mut self, ui: &mut egui::Ui) {
        let connected = self.state["camera"]["connected"].as_bool() == Some(true);
        let enabled = self.state["camera"]["enabled"].as_bool() == Some(true);
        ui.horizontal(|ui| {
            ui.strong("Camera");
            ui.colored_label(
                if connected { GREEN } else { AMBER },
                self.state["camera"]["status"]
                    .as_str()
                    .unwrap_or("Starting…"),
            )
            .on_hover_text(self.state["camera"]["error"].as_str().unwrap_or(""));
        });
        ui.add_enabled_ui(self.ready, |ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.camera_source)
                    .hint_text("Camera stream URL")
                    .desired_width(f32::INFINITY),
            );
            ui.horizontal(|ui| {
                if ui
                    .button(if enabled { "Reconnect" } else { "Connect" })
                    .clicked()
                {
                    self.command(
                        "configure_camera",
                        json!({"enabled":true,"source":self.camera_source}),
                    );
                }
                if ui
                    .add_enabled(enabled, egui::Button::new("Disconnect"))
                    .clicked()
                {
                    self.command("configure_camera", json!({"enabled":false}));
                }
                if connected {
                    ui.small("640 × 480 · 10 fps");
                }
            });
        });
    }
}
