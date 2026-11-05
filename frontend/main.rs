//! Launch the native frontend: Rerun host, Python bridge, and custom egui application.
#[path = "../boats/heron/frontend/app.rs"]
mod app;
#[path = "../boats/heron/frontend/bridge/mod.rs"]
mod bridge;
mod custom_map;
mod ui;
mod viewer;

use app::MissionControl;
use bridge::{LiveJoystick, Request, UiLease};
use rerun::external::{eframe, re_log, re_memory, re_viewer, tokio};
use serde_json::json;
use std::sync::mpsc;

// Track viewer allocations so Rerun can enforce its memory limit accurately.
#[global_allocator]
static ALLOCATOR: re_memory::AccountingAllocator<std::alloc::System> =
    re_memory::AccountingAllocator::new(std::alloc::System);

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = Vec::new();
    let mut recording = None;
    let mut input = std::env::args().skip(1);
    while let Some(arg) = input.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                println!(
                    "Mission Control · native Rerun ground station\n\nUsage: mission-control [--boat heron] [--demo] [--connection MAVLINK_URL] [--mission-file PATH] [--fiu-root PATH] [--recording RECORDING_PATH]\n\nOne boat per app instance. SeaRobotics Surveyor integration is pending. --demo opens no vehicle socket. --recording opens an MCAP or RRD replay at startup."
                );
                return Ok(());
            }
            "--recording" => {
                recording = Some(input.next().ok_or("--recording requires an MCAP path")?);
            }
            "--boat" => match input.next().as_deref() {
                Some("heron") => {}
                Some("surveyor") => {
                    return Err(
                        "SeaRobotics Surveyor integration pending; use --boat heron.".into(),
                    );
                }
                _ => return Err("--boat requires heron or surveyor".into()),
            },
            "--demo" => args.push(arg),
            "--connection" | "--mission-file" | "--fiu-root" => {
                let value = input.next().ok_or("Missing option value")?;
                args.extend([arg, value]);
            }
            _ => return Err(format!("Unknown option: {arg}. Use --help.").into()),
        }
    }
    re_log::setup_logging();
    let token = re_viewer::MainThreadToken::i_promise_i_am_on_the_main_thread();
    let telemetry = viewer::telemetry_receiver()?;
    let receiver = telemetry.messages;
    let _server = telemetry.server;
    let url = telemetry.url;
    let (requests_tx, requests_rx) = mpsc::channel();
    let (events_tx, events_rx) = mpsc::channel();
    let ui_lease = UiLease::new();
    let worker_lease = ui_lease.clone();
    let live_joystick = LiveJoystick::default();
    let worker_joystick = live_joystick.clone();
    if let Some(path) = &recording {
        requests_tx.send(Request::Command(
            "load_recording".into(),
            json!({"path":path}),
        ))?;
    }
    let worker = std::thread::spawn(move || {
        bridge::run_backend(
            url,
            requests_rx,
            events_tx,
            worker_lease,
            worker_joystick,
            args,
        )
    });
    let mut options = re_viewer::native::eframe_options(None);
    options.viewport = options
        .viewport
        .with_app_id("mission-control")
        .with_inner_size([1540.0, 980.0]);
    let result = eframe::run_native(
        "Mission Control · Heron",
        options,
        Box::new(move |cc| {
            let mut viewer = viewer::create(cc, token)?;
            viewer.add_log_receiver(receiver);
            Ok(Box::new(MissionControl::new(
                viewer,
                requests_tx,
                events_rx,
                ui_lease,
                live_joystick,
                recording.is_some(),
            )))
        }),
    );
    let _ = worker.join();
    result?;
    Ok(())
}
