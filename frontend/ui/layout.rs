//! Window panels, workspace tabs, status messages, and confirmation dialogs.
use super::vehicle::{RED, accent_button};
use crate::app::MissionControl;
use crate::bridge::Request;
use re_ui::ContextExt as _;
use rerun::external::eframe::App as _;
use rerun::external::{eframe, egui};
use serde_json::json;

impl MissionControl {
    pub(crate) fn render_layout(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        let top_bar_style = ui.top_bar_style(frame, false);
        egui::Panel::top("mission-control-header").show(ui, |ui| {
            ui.horizontal(|ui| {
                // Share the title bar with macOS controls using their measured size.
                ui.set_min_height(top_bar_style.height);
                ui.add_space(top_bar_style.indent);
                ui.heading("MISSION CONTROL");
                ui.label("HERON");
                if ui
                    .add_enabled(
                        self.ready,
                        accent_button(
                            "STOP VEHICLE · Esc",
                            RED,
                            egui::Color32::from_rgb(67, 38, 38),
                        ),
                    )
                    .clicked()
                {
                    self.pause();
                    self.command("stop", json!({}));
                }
            });
            ui.separator();
            self.telemetry_strip(ui);
        });
        egui::Panel::bottom("mission-control-status").show(ui, |ui| {
            if self.quitting && self.notice == "Saving recording…" {
                ui.horizontal(|ui| {
                    ui.spinner();
                    ui.label(&self.notice);
                });
            } else if !self.error.is_empty() {
                ui.colored_label(egui::Color32::LIGHT_RED, &self.error);
            } else if let Some(error) = &self.state_error {
                ui.colored_label(egui::Color32::LIGHT_RED, error);
            } else {
                let status = self.state["status"].as_str().unwrap_or("Ready");
                if self.flag("status_error") {
                    ui.colored_label(egui::Color32::LIGHT_RED, status);
                } else {
                    ui.label(status);
                }
            }
        });
        egui::Panel::right("mission-control-panel")
            .default_size(360.0)
            .min_size(320.0)
            .show(ui, |ui| {
                self.vehicle_controls(ui);
                ui.separator();
                self.camera_controls(ui);
                ui.separator();
                ui.horizontal(|ui| {
                    ui.selectable_value(&mut self.tab, 0, "Vehicle");
                    ui.selectable_value(&mut self.tab, 2, "Recordings & layers");
                });
                ui.separator();
                egui::ScrollArea::vertical().show(ui, |ui| match self.tab {
                    0 => self.controls(ui),
                    _ => self.recordings(ui),
                });
            });
        self.map_actions();
        self.viewer.ui(ui, frame);
        if let Some(action) = self.confirm.clone() {
            egui::Window::new("Confirm action")
                .collapsible(false)
                .resizable(false)
                .anchor(egui::Align2::CENTER_CENTER, egui::Vec2::ZERO)
                .show(ui.ctx(), |ui| {
                    ui.label(match action.as_str() {
                        "quit" => "Vehicle is armed. Are you sure you want to quit?",
                        "arm" => "Arm the vehicle?",
                        "disarm" => "Disarm the vehicle?",
                        _ => "Upload the route and start the mission?",
                    });
                    if action == "start" {
                        ui.label(format!(
                            "Target mission speed: {:.1} m/s",
                            self.state["mission_speed"].as_f64().unwrap_or(1.0)
                        ));
                    }
                    ui.horizontal(|ui| {
                        if ui.button("Cancel").clicked() {
                            self.confirm = None;
                            self.quitting = false;
                        }
                        if ui.button("Confirm").clicked() {
                            self.confirm = None;
                            if action == "quit" {
                                let _ = self.requests.send(Request::Shutdown(true));
                            } else {
                                self.command(&action, json!({}));
                            }
                        }
                    });
                });
        }
    }
}
