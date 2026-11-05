//! Frontend side of the Python/Rust bridge; no drawing or MAVLink implementation.
mod http;
mod protocol;
mod worker;

pub(crate) use protocol::{Event, LiveJoystick, Request, UiLease};
pub(crate) use worker::run_backend;
