//! Vehicle telemetry, arming, modes, and manual-input controls.
use crate::app::MissionControl;
use rerun::external::egui;
use serde_json::json;
use std::time::Instant;

pub(super) const AMBER: egui::Color32 = egui::Color32::from_rgb(255, 216, 74);
pub(super) const GREEN: egui::Color32 = egui::Color32::from_rgb(184, 237, 207);
pub(super) const RED: egui::Color32 = egui::Color32::from_rgb(255, 184, 174);

pub(super) fn accent_button(
    label: &str,
    color: egui::Color32,
    fill: egui::Color32,
) -> egui::Button<'_> {
    egui::Button::new(egui::RichText::new(label).color(color))
        .fill(fill)
        .stroke(egui::Stroke::new(1.0, color.gamma_multiply(0.45)))
}

impl MissionControl {
    /// Live telemetry belongs to the window chrome, outside the workspace tabs.
    pub(crate) fn telemetry_strip(&self, ui: &mut egui::Ui) {
        let replay = self.recording_busy
            || self.viewer.recording_db().is_some_and(|db| {
                db.application_id().to_string() != "mission-control-live"
                    || self.state["live_recording_id"].as_str()
                        != Some(db.recording_id().to_string().as_str())
            });
        let code = self.state["sdk"]["control"]["status"] == "active";
        let live = self.ready && self.flag("connected");
        let value = |name: &str, precision: usize, unit: &str| {
            if live {
                self.state[name]
                    .as_f64()
                    .map(|n| format!("{n:.precision$}{unit}"))
            } else {
                None
            }
            .unwrap_or_else(|| "—".into())
        };
        ui.horizontal_wrapped(|ui| {
            egui::Frame::new()
                .fill(if replay {
                    egui::Color32::from_rgb(45, 76, 112)
                } else if code {
                    egui::Color32::from_rgb(92, 63, 150)
                } else {
                    egui::Color32::from_rgb(180, 38, 48)
                })
                .corner_radius(4)
                .inner_margin(egui::Margin::symmetric(8, 3))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 5.0;
                        if replay {
                            let (rect, _) =
                                ui.allocate_exact_size(egui::vec2(14.0, 8.0), egui::Sense::hover());
                            for offset in [0.0, 7.0] {
                                let left = rect.left() + offset;
                                ui.painter().add(egui::Shape::convex_polygon(
                                    vec![
                                        egui::pos2(left, rect.top()),
                                        egui::pos2(left + 6.0, rect.center().y),
                                        egui::pos2(left, rect.bottom()),
                                    ],
                                    egui::Color32::WHITE,
                                    egui::Stroke::NONE,
                                ));
                            }
                        } else {
                            let (rect, _) =
                                ui.allocate_exact_size(egui::vec2(8.0, 8.0), egui::Sense::hover());
                            ui.painter()
                                .circle_filled(rect.center(), 3.0, egui::Color32::WHITE);
                        }
                        ui.label(
                            egui::RichText::new(if replay {
                                "REPLAY"
                            } else if code {
                                "CODE MODE"
                            } else {
                                "LIVE"
                            })
                                .small()
                                .strong()
                                .color(egui::Color32::WHITE),
                        );
                    });
                })
                .response
                .on_hover_text(if replay {
                    "Viewing a recording. The vehicle readings alongside remain live."
                } else if code {
                    "A programmatic controller owns the vehicle. Kill switch and Esc remain available."
                } else {
                    "Viewing the live recording."
                });
            ui.small("LIVE VEHICLE");
            ui.separator();
            ui.label("Speed");
            ui.monospace(value("speed", 1, " m/s"));
            ui.separator();
            ui.label("Heading");
            ui.monospace(value("heading", 0, "°"));
            ui.separator();
            let gps = live && self.number("fix") >= 2.0;
            ui.colored_label(
                if gps { GREEN } else { AMBER },
                if gps { "GPS" } else { "GPS unavailable" },
            );
            if gps {
                ui.monospace(format!(
                    "{:.6}, {:.6}",
                    self.number("lat"),
                    self.number("lon")
                ));
            }
            ui.small(format!(
                "Fix {} · {} satellites",
                value("fix", 0, ""),
                value("satellites", 0, "")
            ));
            ui.separator();
            ui.small(format!("Battery {}", value("battery_v", 1, " V")));
        });
    }

    pub(crate) fn execution_banner(&self, ui: &mut egui::Ui) {
        if self.flag("emergency_stopped") {
            egui::Frame::new()
                .fill(egui::Color32::from_rgb(67, 25, 30))
                .inner_margin(8.0)
                .show(ui, |ui| {
                    ui.colored_label(RED, "KILL SWITCH ENGAGED · Drive commands blocked");
                    ui.small("MANUAL + neutral requested. Reset to enable new commands.");
                    ui.small(format!(
                        "Vehicle-reported mode: {} · {}",
                        self.state["vehicle_confirmed"]["mode"]
                            .as_str()
                            .unwrap_or("unconfirmed"),
                        if self.flag("connected") {
                            "link online"
                        } else {
                            "link offline · stop delivery unavailable"
                        }
                    ));
                });
            return;
        }
        let execution = &self.state["sdk"]["execution"];
        if !execution.is_object() {
            return;
        }
        egui::Frame::new()
            .fill(egui::Color32::from_rgb(38, 31, 58))
            .inner_margin(8.0)
            .show(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.strong("CODE MODE");
                    ui.label(if execution["mode"] == "simulation" {
                        "SIMULATION"
                    } else {
                        "LIVE BOAT"
                    });
                    ui.separator();
                    ui.label(execution["label"].as_str().unwrap_or("Running"));
                    ui.small(format!(
                        "{:.1} s elapsed",
                        execution["elapsed_s"].as_f64().unwrap_or(0.0)
                    ));
                    if let Some(fraction) = execution["fraction"].as_f64() {
                        ui.add(
                            egui::ProgressBar::new(fraction as f32)
                                .desired_width(200.0)
                                .show_percentage(),
                        );
                    } else {
                        ui.spinner();
                    }
                });
            });
    }

    /// Operational controls remain visible when the detail tab changes.
    pub(crate) fn vehicle_controls(&mut self, ui: &mut egui::Ui) {
        let mode = self.state["mode"].as_str().unwrap_or("STARTING").to_owned();
        ui.horizontal_wrapped(|ui| {
            ui.colored_label(
                if self.flag("connected") { GREEN } else { RED },
                if self.flag("connected") {
                    "ONLINE"
                } else {
                    "OFFLINE"
                },
            );
            ui.colored_label(
                if self.flag("armed") {
                    AMBER
                } else {
                    ui.visuals().text_color()
                },
                if self.flag("armed") {
                    "ARMED"
                } else {
                    "DISARMED"
                },
            );
            ui.strong(&mode);
        });
        if self.flag("demo") || self.flag("simulating") {
            ui.colored_label(AMBER, "SIMULATION · no live vehicle in demo mode");
        }
        ui.separator();
        ui.add_enabled_ui(self.ready, |ui| {
            let armed = self.flag("armed");
            let arm_button = if armed {
                accent_button("Disarm…", RED, egui::Color32::from_rgb(67, 38, 38))
            } else {
                accent_button("Arm…", AMBER, egui::Color32::from_rgb(58, 50, 30))
            };
            if ui.add(arm_button).clicked() {
                self.pause();
                self.confirm = Some(if armed { "disarm" } else { "arm" }.into());
            }
            ui.horizontal(|ui| {
                for mode in ["MANUAL", "HOLD", "AUTO"] {
                    let response = if self.state["mode"].as_str() == Some(mode) {
                        ui.add(accent_button(
                            mode,
                            AMBER,
                            egui::Color32::from_rgb(58, 50, 30),
                        ))
                    } else {
                        ui.button(mode)
                    };
                    if response.clicked() {
                        self.pause();
                        self.command("mode", json!({"mode":mode}));
                    }
                }
            });
            ui.separator();
            let manual_toggle = ui
                .scope(|ui| {
                    if self.manual {
                        ui.visuals_mut().selection.bg_fill = egui::Color32::from_rgb(44, 78, 60);
                        ui.visuals_mut().selection.stroke = egui::Stroke::new(1.0, GREEN);
                    }
                    let color = if self.manual {
                        GREEN
                    } else {
                        ui.visuals().text_color()
                    };
                    ui.checkbox(
                        &mut self.manual,
                        egui::RichText::new("Enable joystick").color(color),
                    )
                })
                .inner;
            if manual_toggle.changed() {
                manual_toggle.surrender_focus();
                if self.manual {
                    self.manual_requested_at = Some(Instant::now());
                    self.command("manual", json!({}));
                } else {
                    self.pause();
                }
            }
            ui.small("Left stick: throttle · right stick: steering. Esc stops. Focus loss pauses.");
        });
    }

    pub(crate) fn controls(&mut self, ui: &mut egui::Ui) {
        ui.heading("Joystick");
        let connected = self.joystick["connected"].as_bool() == Some(true);
        ui.colored_label(
            if connected { GREEN } else { AMBER },
            if connected {
                "Connected"
            } else {
                "No joystick input"
            },
        );
        if let Some(error) = self.joystick["error"]
            .as_str()
            .filter(|error| !error.is_empty())
        {
            ui.colored_label(AMBER, error);
        }
        let calibrating = self.joystick["calibration"]["pending"].as_bool() == Some(true);
        if ui
            .add_enabled(
                connected && !calibrating,
                egui::Button::new("Recalibrate sticks"),
            )
            .clicked()
        {
            self.pause();
            self.command("calibrate_joystick", json!({}));
        }
        if let Some(status) = self.joystick["calibration"]["status"].as_str() {
            ui.small(status);
        }
        ui.small("Release both sticks to calibrate. Enable joystick again when finished.");
        ui.horizontal(|ui| {
            for (stick, label) in [("left", "Left · throttle"), ("right", "Right · steering")] {
                ui.vertical(|ui| {
                    ui.small(label);
                    let (rect, _) =
                        ui.allocate_exact_size(egui::vec2(120.0, 120.0), egui::Sense::hover());
                    let center = rect.center();
                    let radius = 50.0;
                    let painter = ui.painter();
                    let stroke = egui::Stroke::new(1.0, ui.visuals().weak_text_color());
                    painter.circle_stroke(center, radius, stroke);
                    painter.line_segment(
                        [
                            center - egui::vec2(radius, 0.0),
                            center + egui::vec2(radius, 0.0),
                        ],
                        stroke,
                    );
                    painter.line_segment(
                        [
                            center - egui::vec2(0.0, radius),
                            center + egui::vec2(0.0, radius),
                        ],
                        stroke,
                    );
                    if connected {
                        let x = self.joystick[format!("{stick}_x")].as_f64().unwrap_or(0.0) as f32;
                        let y = self.joystick[format!("{stick}_y")].as_f64().unwrap_or(0.0) as f32;
                        let raw_point = center
                            + egui::vec2(
                                x * (1.0 - y * y / 2.0).sqrt(),
                                -y * (1.0 - x * x / 2.0).sqrt(),
                            ) * radius;
                        painter.circle_stroke(raw_point, 4.0, stroke);
                        let corrected = &self.joystick["calibration"]["corrected"];
                        let x = corrected[format!("{stick}_x")].as_f64().unwrap_or(x as f64) as f32;
                        let y = corrected[format!("{stick}_y")].as_f64().unwrap_or(y as f64) as f32;
                        // Square HID ranges map into the circle without clipping diagonal positions.
                        let point = center
                            + egui::vec2(
                                x * (1.0 - y * y / 2.0).sqrt(),
                                -y * (1.0 - x * x / 2.0).sqrt(),
                            ) * radius;
                        painter.line_segment([center, point], egui::Stroke::new(2.0, GREEN));
                        painter.circle_filled(point, 5.0, GREEN);
                    }
                });
            }
        });
        ui.small("Green: filtered input · outline: raw stick position");
        ui.horizontal(|ui| {
            let throttle = self.state["manual"]["throttle"].as_f64().unwrap_or(0.0) / 10.0;
            let steering = self.state["manual"]["yaw"].as_f64().unwrap_or(0.0) / 10.0;
            ui.monospace(format!(
                "Throttle {throttle:+.0}% · Steering {steering:+.0}%"
            ));
        });
        ui.collapsing("Control diagnostics", |ui| {
            let output = &self.state["control_output"];
            if output["sent"].as_bool() == Some(true) {
                ui.small(format!(
                    "Last sent: throttle {} · steering {} · RC ch1 {} / ch3 {}",
                    output["throttle"], output["yaw"], output["chan1_raw"], output["chan3_raw"]
                ));
                ui.small("Written to transport; vehicle response is not acknowledged.");
            } else if output.is_object() {
                ui.colored_label(AMBER, output["error"].as_str().unwrap_or("Unavailable"));
            } else {
                ui.small("No control output attempted yet");
            }
            ui.small("Outgoing protocol fields are available in Recordings & layers → Sent.");
        });
        ui.separator();
        ui.heading("Backend health");
        if let Some(age) = self.state["health"]["heartbeat_age"].as_f64() {
            ui.label(format!("Vehicle heartbeat: {age:.1} s ago · timeout 3 s"));
        } else {
            ui.label("Vehicle heartbeat: waiting");
        }
        ui.label(format!(
            "Receive errors: {}",
            self.state["health"]["receive_errors"].as_u64().unwrap_or(0)
        ));
        if let Some(error) = self.state["health"]["recording_error"]
            .as_str()
            .filter(|error| !error.is_empty())
        {
            ui.colored_label(
                egui::Color32::LIGHT_RED,
                format!("Recording error: {error}"),
            );
        }
        ui.separator();
        egui::ScrollArea::vertical()
            .max_height(160.0)
            .show(ui, |ui| {
                if let Some(logs) = self.state["logs"].as_array() {
                    for log in logs.iter().rev().take(12) {
                        ui.small(log.as_str().unwrap_or(""));
                    }
                }
            });
    }
}
