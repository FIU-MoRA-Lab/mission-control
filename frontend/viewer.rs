//! Rerun host setup and its loopback telemetry receiver; custom panels live in ui/.
use rerun::external::{eframe, re_grpc_server, re_log_channel, re_viewer};

pub(crate) fn create(
    cc: &eframe::CreationContext<'_>,
    token: re_viewer::MainThreadToken,
) -> Result<re_viewer::App, Box<dyn std::error::Error + Send + Sync>> {
    re_viewer::customize_eframe_and_setup_renderer(cc)?;
    let mut viewer = re_viewer::App::new(
        token,
        re_viewer::build_info(),
        re_viewer::AppEnvironment::Custom("Mission Control".into()),
        re_viewer::StartupOptions::default(),
        cc,
        None,
        re_viewer::AsyncRuntimeHandle::from_current_tokio_runtime_or_wasmbindgen()?,
    );
    viewer.add_view_class::<crate::custom_map::FiuMap>(
        re_sdk_types::reflection::ViewReflection {
            applicability: re_sdk_types::reflection::ViewApplicability::Archetypes(vec![
                <re_sdk_types::archetypes::GeoPoints as re_sdk_types::Archetype>::name(),
                <re_sdk_types::archetypes::GeoLineStrings as re_sdk_types::Archetype>::name(),
            ]),
        },
    )?;
    Ok(viewer)
}

/// Owns the local Rerun data connection, separate from the command HTTP bridge.
pub(crate) struct TelemetryReceiver {
    pub(crate) messages: re_log_channel::LogReceiver,
    pub(crate) server: re_grpc_server::MessageProxyHandle,
    pub(crate) url: String,
}

pub(crate) fn telemetry_receiver() -> Result<TelemetryReceiver, Box<dyn std::error::Error>> {
    let probe = std::net::TcpListener::bind("127.0.0.1:0")?;
    let address = probe.local_addr()?;
    drop(probe);
    let (messages, server) = re_grpc_server::spawn_with_recv(
        address,
        Default::default(),
        re_grpc_server::shutdown::never(),
    );
    Ok(TelemetryReceiver {
        messages,
        server,
        url: format!("rerun+http://{address}/proxy"),
    })
}

/// Snapshot the selected recording without reopening it or changing viewer selection.
pub(crate) fn chart_entities(
    db: &rerun::external::re_entity_db::EntityDb,
) -> Vec<serde_json::Value> {
    use re_sdk_types::external::arrow::datatypes::DataType;
    use serde_json::json;
    let engine = db.storage_engine();
    let schema = engine.store().schema();
    let mut entities = Vec::new();
    for path in db.sorted_entity_paths() {
        let mut dtype = None;
        for component in schema.all_components_for_entity(path).into_iter().flatten() {
            if let Some((component_type, arrow_type)) =
                schema.lookup_component_type(path, *component)
            {
                let type_name = component_type
                    .map(|name| name.to_string())
                    .unwrap_or_default();
                if type_name == "rerun.components.Scalar" && dtype.is_none() {
                    dtype = Some("number");
                }
                if component.to_string() == "StateChange:state" {
                    dtype = Some("state");
                }
                if type_name == "rerun.components.LatLon" {
                    dtype = Some("geo");
                }
                if component.to_string() == "value" {
                    match arrow_type {
                        DataType::Boolean => dtype = Some("bool"),
                        DataType::Utf8 | DataType::LargeUtf8 | DataType::Utf8View => {
                            dtype = Some("text")
                        }
                        _ => {}
                    }
                }
            }
        }
        if let Some(dtype) = dtype {
            entities.push(json!({"path":path.to_string(),"data_type":dtype}));
        }
    }
    entities
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn selected_recording_catalog_uses_its_own_scalar_state_and_map_entities() {
        let (rec, storage) = rerun::RecordingStreamBuilder::new("directly-opened-recording")
            .memory()
            .unwrap();
        rec.log_static(
            "mavlink/RC_CHANNELS/chan3_raw",
            &rerun::Scalars::new([1650.0]),
        )
        .unwrap();
        rec.log_static(
            "vehicle/mode",
            &rerun::StateChange::new().with_state(["AUTO"]),
        )
        .unwrap();
        rec.log_static(
            "geo/vehicle",
            &rerun::GeoPoints::from_lat_lon([[25.0, -80.0]]),
        )
        .unwrap();
        let messages = storage.take();
        let mut db = rerun::external::re_entity_db::EntityDb::new(messages[0].store_id().clone());
        for message in messages {
            db.add_log_msg(&message).unwrap();
        }
        let entities = chart_entities(&db);
        assert!(
            entities
                .contains(&json!({"path":"/mavlink/RC_CHANNELS/chan3_raw","data_type":"number"}))
        );
        assert!(entities.contains(&json!({"path":"/vehicle/mode","data_type":"state"})));
        assert!(entities.contains(&json!({"path":"/geo/vehicle","data_type":"geo"})));
        assert_eq!(entities.len(), 3);
    }
}
