//! Route tools rendered inside the existing geographic Rerun FIU map.
use crate::app::MissionControl;
use rerun::external::egui;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex, mpsc};

pub(crate) type MapState = Arc<Mutex<(Value, bool)>>;
pub(crate) type MapAction = (String, Value);

pub(crate) fn install() -> (MapState, mpsc::Receiver<MapAction>) {
    install_with(crate::custom_map::install_planning_ui)
}

fn install_with(
    register: impl FnOnce(crate::custom_map::PlanningUi),
) -> (MapState, mpsc::Receiver<MapAction>) {
    let snapshot = Arc::new(Mutex::new((Value::Null, false)));
    let state = snapshot.clone();
    let (sender, receiver) = mpsc::channel();
    let mut edit = false;
    let mut dragging: Option<usize> = None;
    let mut lat = 25.758;
    let mut lon = -80.374;
    register(Box::new(move |ui, map, live, follow_live| {
        let (state, ready) = state.lock().unwrap().clone();
        let flag = |key: &str| state[key].as_bool().unwrap_or(false);
        let available = ready && live;
        let editable = available
            && !flag("armed")
            && !flag("started")
            && !flag("starting")
            && !flag("uploading");
        let points: Vec<(f64, f64)> = state["waypoints"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|p| Some((p["lat"].as_f64()?, p["lon"].as_f64()?)))
            .collect();
        let send = |action: &str, data| {
            let _ = sender.send((action.to_owned(), data));
        };
        if !editable {
            edit = false;
            dragging = None;
        }
        let Some((projector, response)) = map else {
            ui.horizontal_wrapped(|ui| {
                ui.add_enabled(editable, egui::Checkbox::new(&mut edit, "Edit route"));
                ui.label("Mission speed");
                let mut speed = state["mission_speed"].as_f64().unwrap_or(1.0);
                if ui.add_enabled(editable, egui::DragValue::new(&mut speed)
                    .range(0.1..=10.0).speed(0.1).fixed_decimals(1).suffix(" m/s"))
                    .on_hover_text("Target speed for the next waypoint mission. Change while disarmed, before starting.")
                    .changed()
                {
                    send("mission_speed", json!({"meters_per_second":speed}));
                }
                for (label, action) in [("Load route", "load"), ("Save route", "save")] {
                    if ui
                        .add_enabled(available, egui::Button::new(label))
                        .clicked()
                    {
                        send(action, json!({}));
                    }
                }
                for (label, action, enabled) in [
                    (
                        "Close loop",
                        "close_loop",
                        editable && flag("close_available"),
                    ),
                    ("Clear", "clear", editable && flag("clear_available")),
                    (
                        "Start mission…",
                        "start",
                        available && !points.is_empty() && !flag("starting") && !flag("uploading"),
                    ),
                ] {
                    if ui.add_enabled(enabled, egui::Button::new(label)).clicked() {
                        send(action, json!({}));
                    }
                }
                ui.menu_button(format!("Route · {} points", points.len()), |ui| {
                    ui.add_enabled_ui(editable, |ui| {
                        ui.horizontal(|ui| {
                            ui.label("Lat");
                            ui.add(
                                egui::DragValue::new(&mut lat)
                                    .speed(0.00001)
                                    .max_decimals(6),
                            );
                            ui.label("Lon");
                            ui.add(
                                egui::DragValue::new(&mut lon)
                                    .speed(0.00001)
                                    .max_decimals(6),
                            );
                            if ui.button("Add").clicked() {
                                send("add_waypoint", json!({"lat":lat,"lon":lon}));
                            }
                        });
                    });
                    egui::ScrollArea::vertical()
                        .max_height(200.0)
                        .show(ui, |ui| {
                            for (index, (lat, lon)) in points.iter().enumerate() {
                                ui.horizontal(|ui| {
                                    ui.monospace(format!("{:02}  {lat:.6}, {lon:.6}", index + 1));
                                    if ui.add_enabled(editable, egui::Button::new("×")).clicked() {
                                        send("remove_waypoint", json!({"index":index}));
                                    }
                                });
                            }
                        });
                });
                ui.menu_button("Lake", |ui| {
                    let mut padding = state["padding"].as_f64().unwrap_or(0.0);
                    if ui
                        .add_enabled(
                            available && flag("padding_enabled"),
                            egui::Slider::new(&mut padding, 0.0..=10.0).text("Padding (m)"),
                        )
                        .changed()
                    {
                        send("padding", json!({"meters":padding}));
                        *follow_live = true;
                    }
                    if ui
                        .add_enabled(available, egui::Button::new("Export geometry"))
                        .clicked()
                    {
                        send("export_lake", json!({}));
                    }
                });
            });
            if edit {
                ui.small("Click to add · drag a waypoint to move · right-click to delete · middle drag to pan");
            } else if !live {
                ui.small("Recording playback · route editing is available in the live recording");
            }
            return edit;
        };
        if !edit {
            return false;
        }
        let painter = ui.painter_at(response.rect);
        let screen: Vec<_> = points
            .iter()
            .map(|&(lat, lon)| projector.project(walkers::lat_lon(lat, lon)).to_pos2())
            .collect();
        let nearest = |pos: egui::Pos2| {
            screen
                .iter()
                .enumerate()
                .filter(|(_, p)| p.distance(pos) <= 14.0)
                .min_by(|(_, a), (_, b)| a.distance(pos).total_cmp(&b.distance(pos)))
                .map(|(index, _)| index)
        };
        if response.contains_pointer()
            && ui.input(|i| i.pointer.button_pressed(egui::PointerButton::Primary))
        {
            dragging = ui.input(|i| i.pointer.press_origin()).and_then(nearest);
        }
        if ui.input(|i| i.pointer.button_released(egui::PointerButton::Primary)) {
            if let (Some(index), Some(pos)) =
                (dragging.take(), ui.input(|i| i.pointer.latest_pos()))
            {
                let position = projector.unproject(pos.to_vec2());
                send(
                    "move_waypoint",
                    json!({"index":index,"lat":position.y(),"lon":position.x()}),
                );
            }
        }
        if response.clicked() {
            if let Some(pos) = response
                .interact_pointer_pos()
                .filter(|pos| nearest(*pos).is_none())
            {
                let position = projector.unproject(pos.to_vec2());
                send(
                    "add_waypoint",
                    json!({"lat":position.y(),"lon":position.x()}),
                );
            }
        }
        if response.secondary_clicked() {
            if let Some(index) = response.interact_pointer_pos().and_then(nearest) {
                send("remove_waypoint", json!({"index":index}));
            }
        }
        for (index, mut pos) in screen.into_iter().enumerate() {
            if dragging == Some(index) {
                if let Some(pointer) = ui.input(|i| i.pointer.latest_pos()) {
                    pos = pointer;
                }
            }
            painter.circle_filled(pos, 10.0, egui::Color32::from_rgb(255, 210, 65));
            painter.text(
                pos,
                egui::Align2::CENTER_CENTER,
                (index + 1).to_string(),
                egui::FontId::proportional(11.0),
                egui::Color32::BLACK,
            );
        }
        true
    }));
    (snapshot, receiver)
}

impl MissionControl {
    pub(crate) fn map_actions(&mut self) {
        while let Ok((action, data)) = self.map_actions.try_recv() {
            match action.as_str() {
                "load" | "save" | "export_lake" => self.files(&action),
                "start" => {
                    self.pause();
                    self.confirm = Some("start".into());
                }
                _ => self.command(&action, data),
            }
        }
        *self.map_state.lock().unwrap() = (
            planning_snapshot(&self.state),
            self.ready && self.confirm.is_none(),
        );
    }
}

/// Map tools need route state, not the full telemetry and field catalog.
fn planning_snapshot(state: &Value) -> Value {
    let mut snapshot = serde_json::Map::new();
    for key in [
        "armed",
        "started",
        "starting",
        "uploading",
        "waypoints",
        "mission_speed",
        "close_available",
        "clear_available",
        "padding",
        "padding_enabled",
    ] {
        snapshot.insert(key.to_owned(), state[key].clone());
    }
    Value::Object(snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::custom_map::PlanningUi;
    use walkers::{Map, MapMemory, Projector};

    fn frame(
        ctx: &egui::Context,
        callback: &mut PlanningUi,
        memory: &mut MapMemory,
        events: Vec<egui::Event>,
        live: bool,
    ) -> (bool, egui::Pos2) {
        let mut result = (false, egui::Pos2::ZERO);
        let mut output = ctx.run_ui(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(700.0, 500.0),
                )),
                events,
                ..Default::default()
            },
            |root_ui| {
                egui::CentralPanel::default().show(root_ui, |ui| {
                    let mut follow = false;
                    let editing = callback(ui, None, live, &mut follow);
                    let center = walkers::lat_lon(25.758, -80.374);
                    let response = ui.add(
                        Map::new(None, memory, center)
                            .drag_pan_buttons(egui::DragPanButtons::MIDDLE),
                    );
                    let projector = Projector::new(response.rect, memory, center);
                    result = (editing, projector.project(center).to_pos2());
                    callback(ui, Some((&projector, &response)), live, &mut follow);
                });
            },
        );
        output.textures_delta.clear();
        result
    }
    fn pointer(pos: egui::Pos2, pressed: bool, button: egui::PointerButton) -> Vec<egui::Event> {
        vec![
            egui::Event::PointerMoved(pos),
            egui::Event::PointerButton {
                pos,
                button,
                pressed,
                modifiers: Default::default(),
            },
        ]
    }
    #[test]
    fn planning_snapshot_excludes_telemetry_catalog_and_keeps_route_controls() {
        let state = json!({"armed":true,"mission_speed":1.5,"padding":2.0,"padding_enabled":false,"waypoints":[{"lat":1,"lon":2}], "field_catalog":[{"id":"large catalog"}], "logs":["diagnostics"]});
        let snapshot = planning_snapshot(&state);
        assert_eq!(snapshot["waypoints"], state["waypoints"]);
        assert_eq!(snapshot["armed"], true);
        assert_eq!(snapshot["padding"], 2.0);
        assert_eq!(snapshot["mission_speed"], 1.5);
        assert!(snapshot.get("field_catalog").is_none());
        assert!(snapshot.get("logs").is_none());
    }

    #[test]
    fn waypoint_drag_uses_projected_position_and_replay_blocks_edits() {
        let mut callback = None;
        let (state, actions) = install_with(|cb| callback = Some(cb));
        let mut callback = callback.unwrap();
        *state.lock().unwrap() = (json!({"waypoints":[{"lat":25.758,"lon":-80.374}]}), true);
        let ctx = egui::Context::default();
        let mut memory = MapMemory::default();
        memory.set_zoom(19.0).unwrap();
        frame(&ctx, &mut callback, &mut memory, vec![], true);
        let checkbox = egui::pos2(16.0, 18.0);
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(checkbox, true, egui::PointerButton::Primary),
            true,
        );
        let (editing, marker) = frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(checkbox, false, egui::PointerButton::Primary),
            true,
        );
        assert!(editing);
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(marker, true, egui::PointerButton::Primary),
            true,
        );
        let destination = marker + egui::vec2(35.0, 20.0);
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            vec![egui::Event::PointerMoved(destination)],
            true,
        );
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(destination, false, egui::PointerButton::Primary),
            true,
        );
        let (action, data) = actions.try_recv().expect("drag must send a waypoint move");
        assert_eq!(action, "move_waypoint");
        assert_eq!(data["index"], 0);
        assert!((data["lat"].as_f64().unwrap() - 25.758).abs() > 0.000001);
        assert!((data["lon"].as_f64().unwrap() + 80.374).abs() > 0.000001);
        assert!(
            actions.try_recv().is_err(),
            "drag must not also add a waypoint"
        );
        assert!(!frame(&ctx, &mut callback, &mut memory, vec![], false).0);
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(marker, true, egui::PointerButton::Secondary),
            false,
        );
        frame(
            &ctx,
            &mut callback,
            &mut memory,
            pointer(marker, false, egui::PointerButton::Secondary),
            false,
        );
        assert!(
            actions.try_recv().is_err(),
            "replay must not send route edits"
        );
    }
}
