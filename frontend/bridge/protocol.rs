//! Commands and responses at the process boundary.
use serde_json::Value;
use std::{
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// Only the latest rendered-frame time matters; never queue heartbeats with commands.
#[derive(Clone)]
pub(crate) struct UiLease(Arc<Mutex<Instant>>);
impl UiLease {
    pub(crate) fn new() -> Self {
        Self(Arc::new(Mutex::new(Instant::now())))
    }
    pub(crate) fn refresh(&self) {
        *self.0.lock().unwrap() = Instant::now();
    }
    pub(crate) fn expired(&self, now: Instant) -> bool {
        now.saturating_duration_since(*self.0.lock().unwrap()) > Duration::from_millis(250)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frame_lease_expires_without_queued_events() {
        let lease = UiLease::new();
        let worker = lease.clone();
        let stamp = *lease.0.lock().unwrap();
        assert!(!worker.expired(stamp + Duration::from_millis(249)));
        assert!(worker.expired(stamp + Duration::from_millis(251)));
        *lease.0.lock().unwrap() = stamp + Duration::from_secs(1);
        assert!(!worker.expired(stamp + Duration::from_secs(1)));
    }
}

/// HID display feed overwrites old samples, so paused rendering cannot build a backlog.
#[derive(Clone, Default)]
pub(crate) struct LiveJoystick(Arc<Mutex<Option<(Value, Instant)>>>);
impl LiveJoystick {
    pub(crate) fn publish(&self, value: Value) {
        *self.0.lock().unwrap() = Some((value, Instant::now()));
    }
    pub(crate) fn snapshot(&self) -> Value {
        let guard = self.0.lock().unwrap();
        let Some((value, received)) = guard.as_ref() else {
            return Value::Null;
        };
        let mut value = value.clone();
        if received.elapsed() > Duration::from_millis(400) {
            value["connected"] = Value::Bool(false);
        }
        value
    }
}

#[cfg(test)]
mod live_joystick_tests {
    use super::*;
    #[test]
    fn display_keeps_newest_input_and_expires_stalled_feed() {
        let feed = LiveJoystick::default();
        feed.publish(serde_json::json!({"left_x":0.1,"connected":true}));
        feed.publish(serde_json::json!({"left_x":0.9,"connected":true}));
        assert_eq!(feed.snapshot()["left_x"], 0.9);
        let mut guard = feed.0.lock().unwrap();
        guard.as_mut().unwrap().1 = Instant::now() - Duration::from_secs(1);
        drop(guard);
        assert_eq!(feed.snapshot()["connected"], false);
    }
}

pub(crate) enum Request {
    Command(String, Value),
    PrepareQuit,
    Shutdown(bool),
    ParentExit,
}
pub(crate) enum Event {
    Ready(Value),
    Recording(Value),
    PlotCatalog(Value),
    ViewerCatalog(Value),
    ViewerCatalogError((String, String), String),
    State(Value),
    Error(String),
    RecordingError(String),
    Unavailable(String),
    Notice(String),
    QuitCheck(bool),
    Closed,
}
