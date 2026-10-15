//! Hook for Mission Control's map toolbar and geographic waypoint interaction.
use rerun::external::egui;
use std::sync::{Mutex, OnceLock};
use walkers::Projector;
type PlanningUi = Box<
    dyn FnMut(&mut egui::Ui, Option<(&Projector, &egui::Response)>, bool, &mut bool) -> bool + Send,
>;
static UI: OnceLock<Mutex<Option<PlanningUi>>> = OnceLock::new();
pub fn install_planning_ui(callback: PlanningUi) {
    *UI.get_or_init(Default::default).lock().unwrap() = Some(callback);
}
pub(crate) fn draw(
    ctx: &re_viewer_context::ViewerContext<'_>,
    ui: &mut egui::Ui,
    map: Option<(&Projector, &egui::Response)>,
    live: bool,
) -> bool {
    let mut follow_live = false;
    let editing = UI
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .as_mut()
        .is_some_and(|callback| callback(ui, map, live, &mut follow_live));
    if live && follow_live {
        // Planning controls read current backend state. Bring the recording's
        // time cursor forward too, so the map shows the resulting geometry.
        ctx.send_time_commands([re_viewer_context::TimeControlCommand::SetPlayState(
            re_sdk_types::blueprint::components::PlayState::Following,
        )]);
    }
    editing
}
