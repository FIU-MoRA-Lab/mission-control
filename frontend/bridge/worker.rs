//! Own the Python child process, and poll vehicle state.
use super::http::Api;
use super::{Event, LiveJoystick, Request, UiLease};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader},
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

pub(crate) fn run_backend(
    url: String,
    requests: mpsc::Receiver<Request>,
    events: mpsc::Sender<Event>,
    ui_lease: UiLease,
    live_joystick: LiveJoystick,
    args: Vec<String>,
) {
    let result = (|| -> Result<(), String> {
        let python = std::env::var("MISSION_CONTROL_PYTHON").unwrap_or("python3".into());
        let root = std::env::var("MISSION_CONTROL_ROOT").unwrap_or_else(|_| "..".into());
        let mut child = Command::new(python)
            .args(["-m", "backend.bridge", "--rerun-url", &url])
            .args(args)
            .current_dir(root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("Python backend: {e}"))?;
        let mut reader =
            BufReader::new(child.stdout.take().ok_or("Backend stdout is unavailable")?);
        let mut line = String::new();
        reader.read_line(&mut line).map_err(|e| e.to_string())?;
        let ready: Value =
            serde_json::from_str(&line).map_err(|e| format!("Backend did not start: {e}"))?;
        // This reader runs independently of HTTP commands, telemetry polling and Rerun.
        std::thread::spawn(move || {
            for line in reader.lines() {
                let Ok(line) = line else {
                    break;
                };
                let Ok(packet) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if packet["event"] == "joystick" && packet["data"].is_object() {
                    live_joystick.publish(packet["data"].clone());
                }
            }
            live_joystick
                .publish(json!({"connected":false,"error":"Joystick display feed disconnected"}));
        });
        let api = Api {
            root: format!("http://127.0.0.1:{}", ready["port"]),
            token: ready["token"]
                .as_str()
                .ok_or("Missing backend token")?
                .into(),
            agent: ureq::Agent::config_builder()
                .http_status_as_error(false)
                .timeout_global(Some(Duration::from_secs(2)))
                .build()
                .into(),
        };
        let diagnostic_log = ready["diagnostic_log"]
            .as_str()
            .unwrap_or("the session backend log")
            .to_owned();
        let _ = events.send(Event::Ready(ready));
        // One executor preserves checkbox order without delaying manual-input polling.
        let (layouts_tx, layouts_rx) = mpsc::channel::<Value>();
        // Replay layout updates flush through Rerun and may exceed the 2 s
        // state/vehicle-command timeout, especially just after a large load.
        let layout_api = Api {
            agent: ureq::Agent::config_builder()
                .http_status_as_error(false)
                .timeout_global(Some(Duration::from_secs(15)))
                .build()
                .into(),
            ..api.clone()
        };
        let layout_events = events.clone();
        std::thread::spawn(move || {
            for data in layouts_rx {
                match layout_api.call("/command", Some(data)) {
                    Ok(_) => {
                        let _ =
                            layout_events.send(Event::Notice("Layer visibility updated".into()));
                    }
                    Err(error) => {
                        let _ = layout_events.send(Event::Error(format!("Layers: {error}")));
                    }
                }
            }
        });
        let mut last_poll = Instant::now() - Duration::from_secs(1);
        let mut ui_expired = false;
        loop {
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                return Err(format!(
                    "Python backend exited ({status}). Check the terminal and {diagnostic_log}"
                ));
            }
            match requests.try_recv() {
                Ok(Request::Command(action, mut data)) => {
                    if action == "configure_plots" {
                        data["action"] = json!(action);
                        let _ = layouts_tx.send(data);
                    } else if action == "recording_catalog" {
                        data["action"] = json!(action);
                        let api = Api {
                            agent: ureq::Agent::config_builder()
                                .http_status_as_error(false)
                                .timeout_global(Some(Duration::from_secs(15)))
                                .build()
                                .into(),
                            ..api.clone()
                        };
                        let events = events.clone();
                        let target = (
                            data["application_id"]
                                .as_str()
                                .unwrap_or_default()
                                .to_owned(),
                            data["recording_id"].as_str().unwrap_or_default().to_owned(),
                        );
                        // Catalog scans must not block vehicle polling while an RRD loads.
                        std::thread::spawn(move || match api.call("/command", Some(data)) {
                            Ok(value) => {
                                let _ = events.send(Event::ViewerCatalog(value));
                            }
                            Err(error) => {
                                let _ = events.send(Event::ViewerCatalogError(target, error));
                            }
                        });
                    } else if action == "load_mcap"
                        || action == "load_recording"
                        || action == "filter_readings"
                    {
                        let api = api.clone();
                        let events = events.clone();
                        std::thread::spawn(move || {
                            data["action"] = json!(action);
                            // File conversion can take longer than command round trips.
                            let api = Api {
                                agent: ureq::Agent::config_builder()
                                    .http_status_as_error(false)
                                    .timeout_global(Some(Duration::from_secs(120)))
                                    .build()
                                    .into(),
                                ..api
                            };
                            match api.call("/command", Some(data)) {
                                Ok(result) => {
                                    let _ = events.send(Event::PlotCatalog(result.clone()));
                                    if action == "load_mcap" || action == "load_recording" {
                                        let _ = events.send(Event::Recording(result.clone()));
                                    }
                                    let _ = events.send(Event::Notice(format!(
                                        "Loaded {} · {} {} · {} streams",
                                        result["filename"],
                                        result["messageCount"],
                                        if result["format"] == "rrd" {
                                            "rows"
                                        } else {
                                            "messages"
                                        },
                                        result["streams"]
                                    )));
                                }
                                Err(e) => {
                                    let _ = events
                                        .send(Event::RecordingError(format!("Recording: {e}")));
                                }
                            }
                        });
                    } else {
                        data["action"] = json!(action);
                        if let Err(e) = api.call("/command", Some(data)) {
                            let _ = events.send(Event::Error(e));
                        }
                    }
                }
                Ok(Request::PrepareQuit) => match api.call("/prepare-quit", Some(json!({}))) {
                    Ok(state) => {
                        let _ =
                            events.send(Event::QuitCheck(state["armed"].as_bool().unwrap_or(true)));
                    }
                    Err(e) => {
                        let _ =
                            events.send(Event::Error(format!("Cannot confirm vehicle state: {e}")));
                        let _ = events.send(Event::QuitCheck(true));
                    }
                },
                Ok(Request::Shutdown(confirmed)) => {
                    match api.call("/shutdown", Some(json!({"confirmed_armed": confirmed}))) {
                        Ok(_) => {
                            break;
                        }
                        Err(e) => {
                            let _ = events.send(Event::Error(format!("Shutdown: {e}")));
                            let _ = events.send(Event::QuitCheck(true));
                        }
                    }
                }
                Ok(Request::ParentExit) => break,
                Err(mpsc::TryRecvError::Disconnected) => break,
                Err(mpsc::TryRecvError::Empty) => {}
            }
            let lease_expired = ui_lease.expired(Instant::now());
            if !lease_expired {
                ui_expired = false;
            }
            if !ui_expired && lease_expired {
                if let Err(error) = api.call(
                    "/command",
                    Some(json!({"action":"manual_active", "active":false})),
                ) {
                    let _ = events.send(Event::Error(error));
                }
                ui_expired = true;
            }
            if last_poll.elapsed() >= Duration::from_millis(100) {
                match api.call("/state", None) {
                    Ok(state) => {
                        if events.send(Event::State(state)).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        let _ = events.send(Event::StateError(format!("Vehicle bridge: {e}")));
                    }
                }
                last_poll = Instant::now();
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        drop(child.stdin.take()); // EOF triggers neutral output and MCAP flush, even after GUI failure.
        let _ = events.send(Event::Notice("Saving recording…".into()));
        // Closed RRDs are compacted and checked before the backend exits.
        let deadline = Instant::now() + Duration::from_secs(300);
        while Instant::now() < deadline {
            if child.try_wait().map_err(|e| e.to_string())?.is_some() {
                // Keep the viewer receiver alive until Python flushes and disconnects.
                let _ = events.send(Event::Closed);
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
        let _ = events.send(Event::Closed);
        Ok(())
    })();
    if let Err(error) = result {
        let _ = events.send(Event::Unavailable(error));
    }
}
