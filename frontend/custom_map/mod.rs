//! Opt-in FIU map using public Rerun view/query APIs and standard walkers.
mod data;
mod planning;
mod tiles;

use re_viewer_context::*;
use rerun::external::egui;
use walkers::{HttpTiles, Map, MapMemory, Projector};

pub(crate) fn enabled() -> bool {
    std::env::var("MISSION_CONTROL_CUSTOM_MAP").as_deref() == Ok("1")
}

pub(crate) fn configure_fiu(port: u16, token: String, lat: f64, lon: f64) {
    tiles::configure_fiu(port, token.clone(), lat, lon);
    re_view_map::configure_fiu(port, token, lat, lon);
}

pub(crate) type PlanningUi = Box<
    dyn FnMut(&mut egui::Ui, Option<(&Projector, &egui::Response)>, bool, &mut bool) -> bool + Send,
>;

pub(crate) fn install_planning_ui(callback: PlanningUi) {
    if enabled() {
        planning::install_planning_ui(callback);
    } else {
        re_view_map::install_planning_ui(callback);
    }
}

#[derive(Default)]
pub(crate) struct FiuMap;
struct State {
    tiles: Option<HttpTiles>,
    memory: MapMemory,
}
impl Default for State {
    fn default() -> Self {
        let mut memory = MapMemory::default();
        let _ = memory.set_zoom(19.0);
        Self {
            tiles: None,
            memory,
        }
    }
}
impl ViewState for State {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
    fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
        self
    }
    fn heap_size_bytes(&self) -> u64 {
        // walkers keeps its tile cache private; estimate the allocated tile textures.
        self.tiles.as_ref().map_or(0, |tiles| {
            (256 * 256 * 4 * (256usize.saturating_sub(tiles.stats().in_progress))) as u64
        })
    }
}
impl ViewClass for FiuMap {
    fn identifier() -> re_sdk_types::ViewClassIdentifier {
        "FiuMap".into()
    }
    fn display_name(&self) -> &'static str {
        "FIU custom map (prototype)"
    }
    fn icon(&self) -> &'static re_ui::Icon {
        &re_ui::icons::VIEW_MAP
    }
    fn help(&self, _: egui::os::OperatingSystem) -> re_ui::Help {
        re_ui::Help::new("FIU offline map")
    }
    fn on_register(
        &self,
        registry: &mut ViewSystemRegistrator<'_>,
    ) -> Result<(), ViewClassRegistryError> {
        registry.register_visualizer::<data::Points>()?;
        registry.register_visualizer::<data::Lines>()?;
        Ok(())
    }
    fn new_state(&self) -> Box<dyn ViewState> {
        Box::<State>::default()
    }
    fn layout_priority(&self) -> ViewClassLayoutPriority {
        ViewClassLayoutPriority::High
    }
    fn supports_visible_time_range(&self) -> bool {
        true
    }
    fn spawn_heuristics(
        &self,
        _: &ViewerContext<'_>,
        _: &dyn Fn(&rerun::external::re_log_types::EntityPath) -> bool,
    ) -> ViewSpawnHeuristics {
        ViewSpawnHeuristics::empty()
    }
    fn ui(
        &self,
        ctx: &ViewerContext<'_>,
        _: &MissingChunkReporter,
        ui: &mut egui::Ui,
        state: &mut dyn ViewState,
        _: &ViewQuery<'_>,
        output: SystemExecutionOutput,
    ) -> Result<ViewClassUiOutput, ViewSystemExecutionError> {
        let state = state.downcast_mut::<State>()?;
        let live = ctx
            .active_recording_store_view_context()
            .db
            .application_id()
            .to_string()
            == "mission-control-live";
        let editing = planning::draw(ctx, ui, None, live);
        let Some(source) = tiles::source() else {
            ui.label("Waiting for offline FIU imagery…");
            return Ok(Default::default());
        };
        let center = tiles::center().unwrap();
        let tiles = state
            .tiles
            .get_or_insert_with(|| HttpTiles::new(source, ui.ctx().clone()));
        let buttons = if editing {
            egui::DragPanButtons::MIDDLE
        } else {
            egui::DragPanButtons::PRIMARY | egui::DragPanButtons::MIDDLE
        };
        let response =
            ui.add(Map::new(Some(tiles), &mut state.memory, center).drag_pan_buttons(buttons));
        if !editing && response.double_clicked() {
            state.memory.follow_my_position();
            let _ = state.memory.set_zoom(19.0);
        }
        let projector = Projector::new(response.rect, &state.memory, center);
        let painter = ui.painter_at(response.rect);
        let lines =
            output.visualizer_data_or_default::<data::Geometry>(data::Lines::identifier())?;
        let points =
            output.visualizer_data_or_default::<data::Geometry>(data::Points::identifier())?;
        for geometry in [&lines, &points] {
            for shape in &geometry.shapes {
                let positions: Vec<_> = shape
                    .positions
                    .iter()
                    .map(|p| projector.project(*p).to_pos2())
                    .collect();
                let radius = shape
                    .radius
                    .ui_points()
                    .or_else(|| {
                        shape
                            .radius
                            .scene_units()
                            .map(|r| r * projector.scale_pixel_per_meter(shape.positions[0]))
                    })
                    .unwrap_or(3.0);
                if shape.line {
                    if positions.len() >= 2 {
                        painter.add(egui::Shape::line(
                            positions,
                            egui::Stroke::new(2.0 * radius, shape.color),
                        ));
                    }
                } else {
                    for pos in positions {
                        painter.circle_filled(pos, radius, shape.color);
                    }
                }
            }
        }
        // Keep the boat visible above all reading and route layers.
        for shape in points.shapes.iter().filter(|shape| shape.vehicle) {
            for position in &shape.positions {
                let center = projector.project(*position).to_pos2();
                painter.circle_filled(center, 11.0, egui::Color32::WHITE);
                painter.circle_filled(center, 9.0, egui::Color32::BLACK);
                painter.circle_filled(center, 7.0, egui::Color32::from_rgb(255, 45, 55));
            }
        }
        planning::draw(ctx, ui, Some((&projector, &response)), live);
        painter.text(
            response.rect.left_bottom() + egui::vec2(5.0, -5.0),
            egui::Align2::LEFT_BOTTOM,
            "FIU · Vivid Standard imagery",
            egui::FontId::proportional(10.0),
            egui::Color32::WHITE,
        );
        Ok(Default::default())
    }
}
