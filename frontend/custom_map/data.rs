//! Query geospatial archetypes through Rerun's visible-history API.
use re_sdk_types::{
    Archetype as _,
    archetypes::{GeoLineStrings, GeoPoints},
    components::{GeoLineString, LatLon, Radius},
};
use re_view::{DataResultQuery as _, VisualizerInstructionQueryResults};
use re_viewer_context::*;
use rerun::external::egui;

#[derive(Clone)]
pub(super) struct Shape {
    pub positions: Vec<walkers::Position>,
    pub color: egui::Color32,
    pub radius: Radius,
    pub line: bool,
    pub vehicle: bool,
}
#[derive(Default, Clone)]
pub(super) struct Geometry {
    pub shapes: Vec<Shape>,
}

fn color(value: u32) -> egui::Color32 {
    let [r, g, b, a] = value.to_be_bytes();
    egui::Color32::from_rgba_unmultiplied(r, g, b, a)
}

macro_rules! visualizer {
    ($name:ident, $id:literal, $arch:ident, $component:ty, $descriptor:ident, $slice:ty, $positions:expr, $line:expr) => {
        #[derive(Default)]
        pub(super) struct $name;
        impl IdentifiedViewSystem for $name {
            fn identifier() -> ViewSystemIdentifier {
                $id.into()
            }
        }
        impl VisualizerSystem for $name {
            fn visualizer_query_info(&self, _: &AppOptions) -> VisualizerQueryInfo {
                VisualizerQueryInfo::single_required_component::<$component>(
                    &$arch::$descriptor(),
                    &$arch::all_components(),
                )
            }
            fn execute(
                &self,
                ctx: &ViewContext<'_>,
                query: &ViewQuery<'_>,
                _: &ViewContextCollection,
            ) -> Result<VisualizerExecutionOutput, ViewSystemExecutionError> {
                let output = VisualizerExecutionOutput::default();
                let mut geometry = Geometry::default();
                for (result, instruction) in
                    query.iter_visualizer_instruction_for(Self::identifier())
                {
                    let history =
                        result.query_archetype_with_history::<$arch>(ctx, query, instruction, None);
                    let results =
                        VisualizerInstructionQueryResults::new(instruction, &history, &output);
                    let positions = results.iter_required($arch::$descriptor().component);
                    let colors = results.iter_optional($arch::descriptor_colors().component);
                    let radii = results.iter_optional($arch::descriptor_radii().component);
                    for (_, positions, colors, radii) in re_query::range_zip_1x2(
                        positions.slice::<$slice>(),
                        colors.slice::<u32>(),
                        radii.slice::<f32>(),
                    ) {
                        for (i, position) in positions.iter().enumerate() {
                            let positions: Vec<walkers::Position> = ($positions)(position);
                            if positions.is_empty() {
                                continue;
                            }
                            let colors = colors.unwrap_or(&[]);
                            let radii = radii.unwrap_or(&[]);
                            geometry.shapes.push(Shape {
                                positions,
                                color: color(
                                    colors
                                        .get(i)
                                        .or(colors.last())
                                        .copied()
                                        .unwrap_or(0x60c8ffff),
                                ),
                                radius: Radius(
                                    radii
                                        .get(i)
                                        .or(radii.last())
                                        .copied()
                                        .unwrap_or(-3.0)
                                        .into(),
                                ),
                                line: $line,
                                vehicle: result.entity_path.to_string() == "/geo/vehicle",
                            });
                        }
                    }
                }
                Ok(output.with_visualizer_data(geometry))
            }
        }
    };
}
visualizer!(
    Points,
    "FiuPoints",
    GeoPoints,
    LatLon,
    descriptor_positions,
    [f64; 2],
    |p: &[f64; 2]| vec![walkers::lat_lon(p[0], p[1])],
    false
);
visualizer!(
    Lines,
    "FiuLines",
    GeoLineStrings,
    GeoLineString,
    descriptor_line_strings,
    &[[f64; 2]],
    |line: &&[[f64; 2]]| line.iter().map(|p| walkers::lat_lon(p[0], p[1])).collect(),
    true
);
