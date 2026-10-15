//! Application-owned FIU tile provider; coordinates are geographic, never image pixels.
use std::sync::{OnceLock, RwLock};
use walkers::{
    Position, TileId,
    sources::{Attribution, TileSource},
};

#[derive(Clone)]
pub(crate) struct FiuSource {
    root: String,
    token: String,
    center_lat: f64,
    center_lon: f64,
}
static FIU: OnceLock<RwLock<Option<FiuSource>>> = OnceLock::new();

/// Set from the Python readiness record. No external tile server is contacted.
pub fn configure_fiu(port: u16, token: String, center_lat: f64, center_lon: f64) {
    *FIU.get_or_init(|| RwLock::new(None)).write().unwrap() = Some(FiuSource {
        root: format!("http://127.0.0.1:{port}"),
        token,
        center_lat,
        center_lon,
    });
}
pub(crate) fn source() -> Option<FiuSource> {
    FIU.get_or_init(|| RwLock::new(None))
        .read()
        .unwrap()
        .clone()
}
pub(crate) fn center() -> Option<Position> {
    source().map(|source| walkers::lat_lon(source.center_lat, source.center_lon))
}
impl TileSource for FiuSource {
    fn tile_url(&self, tile: TileId) -> String {
        format!(
            "{}/tiles/fiu/{}/{}/{}.png?token={}",
            self.root, tile.zoom, tile.x, tile.y, self.token
        )
    }
    fn attribution(&self) -> Attribution {
        Attribution {
            text: "FIU · Vivid Standard imagery",
            url: "https://www.fiu.edu/",
            logo_light: None,
            logo_dark: None,
        }
    }
    fn max_zoom(&self) -> u8 {
        22
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fiu_tiles_use_only_the_local_authenticated_server() {
        let source = FiuSource {
            root: "http://127.0.0.1:4321".into(),
            token: "test-token".into(),
            center_lat: 25.75,
            center_lon: -80.37,
        };
        assert_eq!(
            source.tile_url(TileId {
                x: 145093,
                y: 223298,
                zoom: 19
            }),
            "http://127.0.0.1:4321/tiles/fiu/19/145093/223298.png?token=test-token"
        );
        assert_eq!(source.attribution().text, "FIU · Vivid Standard imagery");
    }
}
