//! Poll vehicle state, optionally owning the Python child process.
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
        let attached = std::env::var("MISSION_CONTROL_ATTACH").ok();
        let mut child = None;
        let (api, ready) = if let Some(path) = &attached {
            let descriptor: Value = serde_json::from_slice(
                &std::fs::read(path).map_err(|e| format!("Backend descriptor: {e}"))?,
            )
            .map_err(|e| e.to_string())?;
            let root = descriptor["url"].as_str().ok_or("Missing backend URL")?;
            let address: std::net::SocketAddr = root
                .strip_prefix("http://")
                .ok_or("Expected local HTTP backend")?
                .parse()
                .map_err(|_| "Invalid backend address")?;
            if !address.ip().is_loopback() {
                return Err("Only loopback backend attachment is supported".into());
            }
            let api = make_api(
                root.to_owned(),
                descriptor["token"]
                    .as_str()
                    .ok_or("Missing token")?
                    .to_owned(),
            );
            let mut ready = api.call(
                "/command",
                Some(json!({"action":"attach_viewer", "url":url})),
            )?;
            ready["port"] = json!(address.port());
            ready["token"] = descriptor["token"].clone();
            (api, ready)
        } else {
            let ready = {
                let python = std::env::var("MISSION_CONTROL_PYTHON").unwrap_or("python3".into());
                let root = std::env::var("MISSION_CONTROL_ROOT").unwrap_or_else(|_| "..".into());
                let mut process = Command::new(python)
                    .args(["-m", "backend.bridge", "--rerun-url", &url])
                    .args(args)
                    .current_dir(root)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::inherit())
                    .spawn()
                    .map_err(|e| format!("Python backend: {e}"))?;
                let mut reader = BufReader::new(
                    process
                        .stdout
                        .take()
                        .ok_or("Backend stdout is unavailable")?,
                );
                let mut line = String::new();
                reader.read_line(&mut line).map_err(|e| e.to_string())?;
                let ready: Value = serde_json::from_str(&line)
                    .map_err(|e| format!("Backend did not start: {e}"))?;
                // This reader runs independently of HTTP commands, telemetry polling and Rerun.
                let display = live_joystick.clone();
                std::thread::spawn(move || {
                    for line in reader.lines() {
                        let Ok(line) = line else {
                            break;
                        };
                        let Ok(packet) = serde_json::from_str::<Value>(&line) else {
                            continue;
                        };
                        if packet["event"] == "joystick" && packet["data"].is_object() {
                            display.publish(packet["data"].clone());
                        }
                    }
                    display.publish(
                        json!({"connected":false,"error":"Joystick display feed disconnected"}),
                    );
                });
                child = Some(process);
                ready
            };
            let api = make_api(
                format!("http://127.0.0.1:{}", ready["port"]),
                ready["token"]
                    .as_str()
                    .ok_or("Missing backend token")?
                    .to_owned(),
            );
            (api, ready)
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
        let mut state_failures = 0;
        loop {
            if let Some(status) = child
                .as_mut()
                .map(|process| process.try_wait())
                .transpose()
                .map_err(|e| e.to_string())?
                .flatten()
            {
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
                Ok(Request::PrepareQuit) if attached.is_some() => {
                    let _ = events.send(Event::QuitCheck(false));
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
                Ok(Request::Shutdown(_)) if attached.is_some() => break,
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
                        state_failures = 0;
                        if attached.is_some() {
                            live_joystick.publish(state["joystick"].clone());
                        }
                        if events.send(Event::State(state)).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        state_failures += 1;
                        if attached.is_some() && state_failures >= 3 {
                            break;
                        }
                        let _ = events.send(Event::StateError(format!("Vehicle bridge: {e}")));
                    }
                }
                last_poll = Instant::now();
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        if attached.is_some() {
            let _ = api.call(
                "/command",
                Some(json!({"action":"manual_active", "active":false})),
            );
            let _ = api.call("/command", Some(json!({"action":"detach_viewer"})));
            let _ = events.send(Event::Closed);
            return Ok(());
        }
        let mut child = child.ok_or("Missing owned backend process")?;
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

fn make_api(root: String, token: String) -> Api {
    Api {
        root,
        token,
        agent: ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_secs(2)))
            .build()
            .into(),
    }
}
