//! Shared application state and eframe lifecycle: events, focus, input, and shutdown.
use crate::bridge::{Event, LiveJoystick, Request, UiLease};
use rerun::external::{eframe, egui, re_viewer};
use serde_json::{Value, json};
use std::{
    sync::mpsc,
    time::{Duration, Instant},
};

pub(crate) struct MissionControl {
    pub(crate) viewer: re_viewer::App,
    pub(crate) requests: mpsc::Sender<Request>,
    pub(crate) events: mpsc::Receiver<Event>,
    ui_lease: UiLease,
    live_joystick: LiveJoystick,
    pub(crate) joystick: Value,
    pub(crate) state: Value,
    pub(crate) ready: bool,
    pub(crate) error: String,
    pub(crate) notice: String,
    pub(crate) manual: bool,
    pub(crate) tab: u8,
    pub(crate) confirm: Option<String>,
    pub(crate) quitting: bool,
    pub(crate) closed: bool,
    pub(crate) basemap: Value,
    pub(crate) map_state: crate::ui::map_controls::MapState,
    pub(crate) map_actions: mpsc::Receiver<crate::ui::map_controls::MapAction>,
    pub(crate) recording_busy: bool,
    pub(crate) manual_requested_at: Option<Instant>,
    pub(crate) recording: Value,
    pub(crate) plot_source: String,
    pub(crate) live_preset: String,
    pub(crate) replay_preset: String,
    pub(crate) layer_search: String,
    pub(crate) live_fields: std::collections::BTreeSet<String>,
    live_fields_initialized: bool,
    pub(crate) replay_fields: std::collections::BTreeSet<String>,
    pub(crate) plot_recording: Value,
    pub(crate) plot_catalogs: std::collections::BTreeMap<(String, String), Value>,
    pub(crate) plot_target: Option<(String, String)>,
    pub(crate) plot_catalog_retry_at: Option<Instant>,
}
impl MissionControl {
    pub(crate) fn new(
        viewer: re_viewer::App,
        requests_tx: mpsc::Sender<Request>,
        events_rx: mpsc::Receiver<Event>,
        ui_lease: UiLease,
        live_joystick: LiveJoystick,
        opening_recording: bool,
    ) -> Self {
        let (map_state, map_actions) = crate::ui::map_controls::install();
        Self {
            viewer,
            requests: requests_tx,
            events: events_rx,
            ui_lease,
            live_joystick,
            joystick: Value::Null,
            state: Value::Null,
            ready: false,
            error: "Starting Python controller…".into(),
            notice: String::new(),
            manual: false,
            tab: if opening_recording { 2 } else { 0 },
            confirm: None,
            quitting: false,
            closed: false,
            basemap: Value::Null,
            map_state,
            map_actions,
            recording_busy: opening_recording,
            manual_requested_at: None,
            recording: Value::Null,
            plot_source: "live".into(),
            live_preset: "controls".into(),
            replay_preset: "fields".into(),
            layer_search: String::new(),
            live_fields: Default::default(),
            live_fields_initialized: false,
            replay_fields: Default::default(),
            plot_recording: Value::Null,
            plot_catalogs: Default::default(),
            plot_target: None,
            plot_catalog_retry_at: None,
        }
    }
    pub(crate) fn command(&mut self, action: &str, data: Value) {
        if self
            .requests
            .send(Request::Command(action.into(), data))
            .is_err()
        {
            self.error = "Controller process is unavailable".into();
        }
    }
    pub(crate) fn pause(&mut self) {
        self.manual = false;
        self.command("manual_active", json!({"active":false}));
    }
    pub(crate) fn flag(&self, name: &str) -> bool {
        self.state[name].as_bool().unwrap_or(false)
    }
    pub(crate) fn number(&self, name: &str) -> f64 {
        self.state[name].as_f64().unwrap_or(0.0)
    }
    pub(crate) fn files(&mut self, action: &str) {
        self.pause();
        let file = if action == "save" || action == "export_lake" {
            rfd::FileDialog::new()
                .add_filter("GeoJSON", &["geojson"])
                .save_file()
        } else if action == "load_mcap" || action == "load_recording" {
            rfd::FileDialog::new()
                .add_filter("Recordings", &["mcap", "rrd"])
                .pick_file()
        } else {
            rfd::FileDialog::new()
                .add_filter("Mission", &["geojson", "json", "csv"])
                .pick_file()
        };
        if let Some(path) = file {
            if action == "load_mcap" || action == "load_recording" {
                self.recording_busy = true;
                self.notice = "Opening recording…".into();
            }
            self.command(action, json!({"path":path.to_string_lossy()}));
        }
    }
}

impl eframe::App for MissionControl {
    fn save(&mut self, storage: &mut dyn eframe::Storage) {
        self.viewer.save(storage);
    }
    fn logic(&mut self, ctx: &egui::Context, frame: &mut eframe::Frame) {
        if stop_shortcut(ctx) {
            self.pause();
            self.command("stop", json!({}));
        }
        self.viewer.logic(ctx, frame);
        self.ui_lease.refresh();
        self.joystick = self.live_joystick.snapshot();
        while let Ok(event) = self.events.try_recv() {
            match event {
                Event::PlotCatalog(value) => {
                    self.recording_busy = false;
                    self.cache_plot_catalog(value);
                }
                Event::ViewerCatalog(value) => {
                    self.cache_plot_catalog(value);
                }
                Event::ViewerCatalogError(target, error) => {
                    if self.plot_target.as_ref() == Some(&target) && self.plot_recording.is_null() {
                        // Release the pending target so a failed request can be retried.
                        self.plot_target = None;
                        self.plot_catalog_retry_at = Some(Instant::now() + Duration::from_secs(2));
                        self.error = format!("Recording layers: {error}. Retrying…");
                    }
                }
                Event::Recording(value) => {
                    self.recording_busy = false;
                    self.recording = value;
                }
                Event::Ready(value) => {
                    self.basemap = value["basemap"].clone();
                    if let (Some(port), Some(token)) =
                        (value["port"].as_u64(), value["token"].as_str())
                    {
                        let center = &self.basemap["center"];
                        crate::custom_map::configure_fiu(
                            port as u16,
                            token.to_owned(),
                            center[0].as_f64().unwrap_or(0.0),
                            center[1].as_f64().unwrap_or(0.0),
                        );
                    }
                    self.ready = true;
                    self.error.clear();
                }
                Event::State(value) => {
                    if !self.live_fields_initialized {
                        if let Some(presets) = value["chart_presets"].as_array() {
                            self.live_fields = presets
                                .iter()
                                .find(|preset| {
                                    preset["id"].as_str() == Some(self.live_preset.as_str())
                                })
                                .and_then(|preset| preset["visible_fields"].as_array())
                                .into_iter()
                                .flatten()
                                .filter_map(|field| field.as_str().map(str::to_owned))
                                .collect();
                            self.live_fields_initialized = true;
                        }
                    }
                    self.state = value;
                    if self.manual
                        && (self.state["mode"] != "MANUAL"
                            || self.state["manual"]["active"] != true)
                    {
                        // Allow the enable command to reach the backend before treating a snapshot as rejection.
                        if self
                            .manual_requested_at
                            .is_some_and(|t| t.elapsed() > Duration::from_millis(500))
                        {
                            self.pause();
                        }
                    }
                }
                Event::Unavailable(error) => {
                    self.error = error;
                    self.ready = false;
                    self.closed = true;
                    self.pause();
                }
                Event::Error(error) => {
                    self.error = error;
                }
                Event::RecordingError(error) => {
                    self.error = error;
                    self.recording_busy = false;
                }
                Event::Notice(notice) => {
                    self.notice = notice;
                    self.error.clear();
                }
                Event::QuitCheck(armed) => {
                    if armed {
                        self.confirm = Some("quit".into());
                    } else {
                        let _ = self.requests.send(Request::Shutdown(false));
                    }
                }
                Event::Closed => {
                    self.closed = true;
                    ctx.send_viewport_cmd(egui::ViewportCommand::Close);
                }
            }
        }
        if ctx.input(|i| i.viewport().close_requested()) && !self.closed {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            if !self.quitting {
                self.pause();
                self.quitting = true;
                let _ = self.requests.send(Request::PrepareQuit);
            }
        }
        let focused = ctx.input(|i| i.viewport().focused.unwrap_or(false));
        if self.manual && (!focused || self.confirm.is_some()) {
            self.pause();
        }
        ctx.request_repaint_after(Duration::from_millis(16));
    }
    fn ui(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        self.render_layout(ui, frame);
    }
}

// macOS Quit and event-loop failures can bypass the cancellable window-close
// event. Keep the viewer (and its log receiver) alive until the child releases
// its telemetry sinks, using the same EOF cleanup as a lost parent.
impl Drop for MissionControl {
    fn drop(&mut self) {
        if self.closed {
            return;
        }
        if self.requests.send(Request::ParentExit).is_err() {
            return;
        }
        let deadline = Instant::now() + Duration::from_secs(302);
        while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
            match self.events.recv_timeout(remaining) {
                Ok(Event::Closed | Event::Unavailable(_)) | Err(_) => break,
                Ok(_) => {}
            }
        }
    }
}

/// Reserve Escape for the vehicle before the viewer processes keyboard shortcuts.
fn stop_shortcut(ctx: &egui::Context) -> bool {
    ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Escape))
}

#[cfg(test)]
mod shortcut_tests {
    use super::*;
    #[test]
    fn escape_stops_vehicle_and_space_remains_for_timeline() {
        for (key, expected) in [(egui::Key::Escape, true), (egui::Key::Space, false)] {
            let ctx = egui::Context::default();
            let mut output = ctx.run_ui(
                egui::RawInput {
                    events: vec![egui::Event::Key {
                        key,
                        physical_key: None,
                        pressed: true,
                        repeat: false,
                        modifiers: egui::Modifiers::NONE,
                    }],
                    ..Default::default()
                },
                |_| {
                    assert_eq!(stop_shortcut(&ctx), expected);
                    assert_eq!(ctx.input(|i| i.key_pressed(key)), !expected);
                },
            );
            output.textures_delta.clear();
        }
    }
}
