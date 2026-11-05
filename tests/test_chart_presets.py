"""Verify overlay scales, transitions, nulls and durable replay projections."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import rerun as rr
from rerun.chunk import RrdReader
from mcap.writer import Writer
from boats.heron.backend.logger.session_recorder import SessionRecorder
from boats.heron.visualization.diagnostics import RecordDiagnostics, chart_fields, control_values, diagnostic_catalog
from boats.heron.visualization.presets import PRESETS, preset_fields, raw
from boats.heron.visualization.schema import field_catalog
from boats.heron.visualization.blueprint import blueprint
from boats.heron.visualization.replay import export_mcap
from boats.heron.visualization.rrd import open_rrd
from boats.heron.backend.recordings.mcap_reader import load_recording
from boats.heron.visualization.live import RerunTelemetry


def chunks(path):
    reader = RrdReader(path)
    return list(reader.stream(store=reader.recordings()[0]))


def values(path, entity, component=b"rerun.components.Scalar"):
    result = []
    for chunk in chunks(path):
        if chunk.entity_path != entity: continue
        batch = chunk.to_record_batch()
        for index, field in enumerate(batch.schema):
            if field.metadata and field.metadata.get(b"rerun:component_type") == component:
                result.extend(batch.column(index).to_pylist())
    return result


def lane_values(path, entity):
    import pyarrow as pa
    samples = []
    for chunk in chunks(path):
        if chunk.entity_path != entity: continue
        batch = chunk.to_record_batch()
        times = batch.column("mission").cast(pa.int64()).to_pylist() if "mission" in batch.schema.names else list(range(batch.num_rows))
        for index, field in enumerate(batch.schema):
            if (field.metadata or {}).get(b"rerun:component") == b"StateChange:state":
                samples.extend((time, value) for time, value in zip(times, batch.column(index).to_pylist()) if value is not None)
    return [value for _, value in sorted(samples, key=lambda item: item[0])]


class ChartPresetTests(unittest.TestCase):
    def test_thruster_feedback_groups_received_feedback_by_units(self):
        schema = field_catalog({
            "/mavlink/ESC_STATUS": {"rpm[0]", "current[0]", "voltage[0]"},
            "/mavlink/ESC_TELEMETRY_1_TO_4": {"rpm[0]", "current[0]", "voltage[0]"},
            "/commands/ESC_TELEMETRY_1_TO_4": {"rpm[0]"},
            "/mavlink/RPM": {"rpm1", "rpm2"},
            "/mavlink/BATTERY_STATUS": {"current_battery", "voltages[0]"},
            "/mavlink/SERVO_OUTPUT_RAW": {"servo1_raw", "servo3_raw"},
        }, telemetry=[])
        selected = preset_fields(schema, "thruster_feedback")
        self.assertNotIn(raw("ESC_TELEMETRY_1_TO_4", "rpm[0]", True), selected)
        layout = blueprint(catalog=schema, visible_fields=selected, preset="thruster_feedback")
        views = [view for row in layout.root_container.contents[1:-1] for view in row.contents]
        groups = {str(view.name): view.contents for view in views}
        self.assertIn(raw("ESC_STATUS", "rpm[0]"), groups["Motor feedback · RPM"])
        self.assertIn(raw("ESC_TELEMETRY_1_TO_4", "rpm[0]"), groups["Motor feedback · RPM"])
        self.assertIn("/mavlink/RPM/rpm1", groups["Motor feedback · RPM"])
        self.assertEqual(groups["ESC current · A"], [raw("ESC_STATUS", "current[0]")])
        self.assertEqual(groups["ESC current · cA (100 = 1 A)"], [raw("ESC_TELEMETRY_1_TO_4", "current[0]")])
        self.assertIn(raw("BATTERY_STATUS", "voltages[0]"), groups["Battery supply voltage · mV"])

    def test_thruster_feedback_without_esc_data_keeps_available_commands(self):
        schema = field_catalog({"/mavlink/SERVO_OUTPUT_RAW": {"servo1_raw", "servo3_raw"}}, telemetry=[])
        selected = preset_fields(schema, "thruster_feedback")
        layout = blueprint(catalog=schema, visible_fields=selected, preset="thruster_feedback")
        views = layout.root_container.contents[1].contents
        self.assertEqual(len(views), 1)
        self.assertEqual(str(views[0].name), "Thruster commands · PWM µs")

    def test_state_changes_preset_uses_native_categorical_view(self):
        layout = blueprint(catalog=[], visible_fields=[], preset="state_changes")
        view = layout.root_container.contents[1]
        self.assertEqual(view.class_identifier, "StateTimeline")
        self.assertIn("/state_changes/**", view.contents)
        self.assertNotEqual(view.id, blueprint(catalog=[]).root_container.contents[-1].id)

    def test_state_change_lanes_use_labels_deduplicate_and_reset_unknown(self):
        stream = Mock()
        diagnostics = RecordDiagnostics()
        def heartbeat(base, mode):
            diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", sys_id=3, comp_id=1,
                            data=dict(type=11, autopilot=3, base_mode=base, custom_mode=mode))
        heartbeat(1, 0)
        heartbeat(1, 0)
        heartbeat(129, 10)
        heartbeat(129, 4)
        def states(path):
            return [call.args[1].state.as_arrow_array().to_pylist() for call in stream.log.call_args_list
                    if call.args[0] == path and isinstance(call.args[1], rr.StateChange)]
        self.assertEqual(states("/state_changes/vehicle/3_1/HEARTBEAT/armed"), [["DISARMED"], ["ARMED"]])
        self.assertEqual(states("/state_changes/vehicle/3_1/HEARTBEAT/mode"), [["MANUAL"], ["AUTO"], ["HOLD"]])
        diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", sys_id=3, comp_id=1,
                        data=dict(type=11, autopilot=3, base_mode=None, custom_mode=None))
        self.assertEqual(states("/state_changes/vehicle/3_1/HEARTBEAT/armed")[-1], [""])
        self.assertEqual(states("/state_changes/vehicle/3_1/HEARTBEAT/mode")[-1], [""])
        for active in (True, None, False):
            diagnostics.log(stream, topic="/input/JOYSTICK", message_type="JOYSTICK", direction="input", data={"active": active})
        self.assertEqual(states("/input/JOYSTICK/active"), [["ENABLED"], [""], ["PAUSED"]])

    def test_local_snapshot_state_changes_are_separate_from_vehicle_lanes(self):
        stream = Mock()
        adapter = RerunTelemetry(stream, dict(crs="EPSG:4326", bounds=[], center=[]))
        state = dict(simulating=False, manual=dict(throttle=0, yaw=0, source="NO JOYSTICK", active=False),
                     armed=False, mode="MANUAL", connected=False, lon=0, lat=0, waypoints=[],
                     lake={"features": []}, status="", status_error=False, uploaded=False, started=False)
        adapter.state(state, 0)
        adapter.state(state, 1)
        adapter.state(state | dict(armed=True, mode="AUTO", started=True), 2)
        states = [call.args[1].state.as_arrow_array().to_pylist() for call in stream.log.call_args_list
                  if call.args[0] == "/state_changes/gcs/mode"]
        self.assertEqual(states, [["MANUAL"], ["AUTO"]])
        self.assertFalse(any(call.args[0].startswith("/state_changes/vehicle/") for call in stream.log.call_args_list))

    def test_input_scale_matches_actual_rc_override_and_skips_unavailable(self):
        output = control_values("MANUAL_CONTROL", {"x": 600, "r": -200}, "tx")
        self.assertEqual(output, {"/diagnostics/controls/sent_throttle": 1800,
                                  "/diagnostics/controls/sent_steering": 1400})
        self.assertEqual(control_values("MANUAL_CONTROL", {"x": None, "r": 32767}, "rx"), {})
        self.assertEqual(chart_fields({"active": True, "inactive": False, "unknown": None}),
                     {})

    def test_rover_raw_y_z_axes_reach_control_charts(self):
        values = control_values("MANUAL_CONTROL", {"x":0,"r":0,"y":-200,"z":600}, "tx")
        self.assertEqual(values, {"/diagnostics/controls/sent_throttle":1800,
                                  "/diagnostics/controls/sent_steering":1400})

    def test_heartbeat_modes_armed_and_transition_deduplication(self):
        stream = Mock()
        diagnostics = RecordDiagnostics()
        data = dict(type=11, autopilot=3, base_mode=1, custom_mode=0, system_status=3)
        first = diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", data=data)
        self.assertEqual(first["/diagnostics/state/armed"], 0)
        self.assertEqual(first["/diagnostics/state/manual"], 1)
        events = lambda: [call for call in stream.log.call_args_list if call.args[0].startswith("/events/state_changes/")]
        self.assertEqual(len(events()), 3)
        diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", data=data)
        self.assertEqual(len(events()), 3)
        data.update(base_mode=129, custom_mode=10)
        result = diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", data=data)
        self.assertEqual(result["/diagnostics/state/armed"], 1)
        self.assertEqual(result["/diagnostics/state/manual"], 0)
        self.assertEqual(result["/diagnostics/state/auto"], 1)
        self.assertEqual(len(events()), 5)
        for direction, invalid in (("tx", data), ("rx", data | {"type": 6}), ("rx", {"base_mode": 128})):
            self.assertEqual(diagnostics.log(stream, topic="/mavlink/HEARTBEAT", message_type="HEARTBEAT", data=invalid, direction=direction), {})

    def test_error_severity_ack_in_progress_and_health_recovery(self):
        stream = Mock()
        diagnostics = RecordDiagnostics()
        def log(kind, data):
            diagnostics.log(stream, topic="/mavlink/" + kind, message_type=kind, data=data)
        log("STATUSTEXT", {"severity": 3, "text": "Motor fault"})
        log("STATUSTEXT", {"severity": 6, "text": "Ready"})
        log("COMMAND_ACK", {"command": 400, "result": 5})
        log("COMMAND_ACK", {"command": 400, "result": 2})
        log("SYS_STATUS", dict(onboard_control_sensors_present=3, onboard_control_sensors_enabled=3, onboard_control_sensors_health=1))
        log("SYS_STATUS", dict(onboard_control_sensors_present=3, onboard_control_sensors_enabled=3, onboard_control_sensors_health=1))
        log("SYS_STATUS", dict(onboard_control_sensors_present=3, onboard_control_sensors_enabled=3, onboard_control_sensors_health=3))
        errors = [call for call in stream.log.call_args_list if call.args[0].startswith("/events/errors/")]
        self.assertEqual(len(errors), 3)
        text = [str(call.args[1]) for call in stream.log.call_args_list if call.args[0] == "/events/messages"]
        self.assertTrue(any("recovered" in message for message in text))

    def test_presets_overlay_related_sources_and_keep_map_identity(self):
        schema = field_catalog({"/commands/RC_CHANNELS_OVERRIDE": {"chan3_raw", "chan1_raw"},
                                "/mavlink/RC_CHANNELS": {"chan3_raw", "chan1_raw"},
                                "/mavlink/SERVO_OUTPUT_RAW": {"servo1_raw", "servo3_raw"}}) + diagnostic_catalog()
        normal = blueprint(catalog=schema)
        grouped = blueprint(catalog=schema, visible_fields=preset_fields(schema, "controls"), preset="controls")
        self.assertEqual(normal.root_container.contents[0].id, grouped.root_container.contents[0].id)
        row = grouped.root_container.contents[1]
        self.assertEqual(len(row.contents), 2)
        throttle = row.contents[0]
        self.assertIn("/diagnostics/controls/requested_throttle", throttle.contents)
        self.assertIn("/commands/RC_CHANNELS_OVERRIDE/chan3_raw", throttle.contents)
        self.assertIn("/mavlink/SERVO_OUTPUT_RAW/servo1_raw", throttle.contents)
        self.assertNotIn("/telemetry/manual/throttle", throttle.contents)
        with self.assertRaises(ValueError): blueprint(preset="invalid")

    def test_null_boolean_clears_chart_without_recording_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with SessionRecorder(Path(tmp)/"input", formats=("rrd",)) as recorder:
                recorder.log_input({"active": True})
                recorder.log_input({"active": None})
                recorder.log_input({"active": False})
            samples = values(recorder.rrd_path, "/input/JOYSTICK/active")
            self.assertEqual(samples, [])
            self.assertEqual(values(recorder.rrd_path, "/diagnostics/state/input_active"), [])
            self.assertEqual(lane_values(recorder.rrd_path, "/input/JOYSTICK/active"), [["ENABLED"], [""], ["PAUSED"]])
            self.assertEqual(lane_values(recorder.rrd_path, "/diagnostics/state/input_active"), [["ENABLED"], [""], ["PAUSED"]])
            reopened = open_rrd(recorder.rrd_path, destination=Path(tmp)/"copy.rrd")
            fields = {field["id"]: field for field in reopened["field_catalog"]}
            self.assertEqual(fields["/input/JOYSTICK/active"]["data_type"], "bool")
            self.assertIn("/diagnostics/state/input_active", fields)

    def test_boolean_fields_use_state_timeline_in_all_presets(self):
        schema = field_catalog({"/input/JOYSTICK": {"active"}}, telemetry=set(), types={("/input/JOYSTICK", "active"): "bool"})
        def all_views(item):
            if hasattr(item, "class_identifier"): return [item]
            return [view for child in item.contents for view in all_views(child)]
        for preset in PRESETS:
            layout = blueprint(catalog=schema, visible_fields=["/input/JOYSTICK/active"], preset=preset)
            views = all_views(layout.root_container)
            selected = [view for view in views if "/input/JOYSTICK/active" in view.contents]
            self.assertTrue(selected, preset)
            self.assertTrue(all(view.class_identifier in {"StateTimeline", "Dataframe"} for view in selected), preset)

    def test_older_rrd_boolean_values_and_nulls_get_categorical_lanes(self):
        import pyarrow as pa
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"old.rrd"
            stream = rr.RecordingStream("old-booleans")
            stream.save(source)
            for second, active in enumerate((True, None, False)):
                stream.set_time("mission", duration=second)
                components = dict(state=pa.array(["null" if active is None else "present"]))
                if active is not None: components["value"] = pa.array([active], type=pa.bool_())
                stream.log("/input/JOYSTICK/active", rr.AnyValues(**components))
                stream.log("/diagnostics/local/armed", rr.Scalars([] if active is None else int(active)))
            stream.flush(); stream.disconnect()
            dest = Path(tmp)/"converted.rrd"
            info = open_rrd(source, destination=dest)
            self.assertEqual(lane_values(dest, "/input/JOYSTICK/active"), [["ENABLED"], [""], ["PAUSED"]])
            self.assertEqual(lane_values(dest, "/diagnostics/local/armed"), [["ARMED"], [""], ["DISARMED"]])
            self.assertEqual(values(dest, "/diagnostics/local/armed"), values(source, "/diagnostics/local/armed"))
            types = {field["id"]:field["data_type"] for field in info["field_catalog"]}
            self.assertEqual(types["/diagnostics/local/armed"], "bool")

    def test_rrd_with_only_native_state_changes_can_use_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"native-states.rrd"
            stream = rr.RecordingStream("native-states")
            stream.save(source)
            for second, mode in enumerate(("MANUAL", "AUTO", "HOLD")):
                stream.set_time("mission", duration=second)
                stream.log("/vehicle/mode", rr.StateChange(state=mode))
            stream.flush(); stream.disconnect()
            info = open_rrd(source, destination=Path(tmp)/"copy.rrd")
            native = next(field for field in info["field_catalog"] if field["path"] == "/vehicle/mode")
            self.assertEqual(native["data_type"], "state")
            selected = preset_fields(info["field_catalog"], "state_changes")
            self.assertIn("/vehicle/mode", selected)
            layout = blueprint(catalog=info["field_catalog"], visible_fields=selected, preset="state_changes")
            self.assertIn("/vehicle/mode", layout.root_container.contents[1].contents)

    def test_control_only_mcap_replay_has_diagnostics_text_and_boolean_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/"controls.mcap"
            with source.open("wb") as handle:
                writer = Writer(handle); writer.start()
                for i, (topic, message_type, data) in enumerate([
                    ("/input/JOYSTICK", "JOYSTICK", {"active": True, "connected": True, "requested_throttle": 600, "requested_yaw": -200}),
                    ("/commands/MANUAL_CONTROL", "MANUAL_CONTROL", {"x": 600, "r": -200}),
                    ("/mavlink/SERVO_OUTPUT_RAW", "SERVO_OUTPUT_RAW", {"servo1_raw": 1700, "servo3_raw": 1600}),
                    ("/mavlink/STATUSTEXT", "STATUSTEXT", {"severity": 3, "text": "Motor fault"}),
                    ("/mavlink/HEARTBEAT", "HEARTBEAT", {"type": 11, "autopilot": 3, "base_mode": 1, "custom_mode": 0, "system_status": 3}),
                    ("/mavlink/HEARTBEAT", "HEARTBEAT", {"type": 11, "autopilot": 3, "base_mode": 129, "custom_mode": 10, "system_status": 4}),
                    ("/mavlink/HEARTBEAT", "HEARTBEAT", {"type": 11, "autopilot": 3, "base_mode": 129, "custom_mode": 4, "system_status": 4}),
                ]):
                    channel = writer.register_channel(topic=topic, message_encoding="json", schema_id=0)
                    writer.add_message(channel_id=channel, log_time=1_000_000_000 + i*100_000_000, publish_time=0,
                                       data=json.dumps(dict(msg_type=message_type, data=data)).encode())
                writer.finish()
            self.assertEqual(load_recording(source)["track"], [])
            output = Path(tmp)/"replay.rrd"
            basemap = dict(crs="EPSG:4326", bounds=[-81,25,-80,26], center=[-80.3,25.7])
            info = export_mcap(source, output, basemap, {"type": "FeatureCollection", "features": []})
            self.assertEqual(values(output, "/diagnostics/controls/requested_throttle"), [[1800.0]])
            self.assertEqual(values(output, "/diagnostics/controls/sent_throttle"), [[1800.0]])
            self.assertEqual(values(output, "/mavlink/SERVO_OUTPUT_RAW/servo1_raw"), [[1700.0]])
            paths = {chunk.entity_path for chunk in chunks(output)}
            self.assertIn("/events/errors/messages", paths)
            self.assertTrue(any(path.startswith("/events/state_changes/") for path in paths))
            mode_path = "/state_changes/vehicle/0_0/HEARTBEAT/mode"
            def lane_states(filename):
                samples = []
                for chunk in chunks(filename):
                    if chunk.entity_path != mode_path: continue
                    batch = chunk.to_record_batch()
                    for index, field in enumerate(batch.schema):
                        if field.metadata and field.metadata.get(b"rerun:component") == b"StateChange:state":
                            samples.extend(batch.column(index).to_pylist())
                return samples
            self.assertEqual(lane_states(output), [["MANUAL"], ["AUTO"], ["HOLD"]])
            types = {field["id"]: field["data_type"] for field in info["field_catalog"]}
            self.assertEqual(types["/input/JOYSTICK/active"], "bool")
            self.assertEqual(types["/mavlink/STATUSTEXT/text"], "text")
            self.assertEqual({p["id"] for p in info["chart_presets"]}, set(PRESETS))
            reopened = open_rrd(output, destination=Path(tmp)/"copy.rrd")
            ids = {field["id"] for field in reopened["field_catalog"]}
            self.assertIn("/diagnostics/controls/sent_throttle", ids)
            self.assertIn("/mavlink/STATUSTEXT/text", ids)
            self.assertEqual(lane_states(Path(tmp)/"copy.rrd"), [["MANUAL"], ["AUTO"], ["HOLD"]])

    def test_legacy_raw_paths_are_included_in_control_overlays(self):
        schema = field_catalog({"RC_CHANNELS": {"chan3_raw"}, "SERVO_OUTPUT_RAW": {"servo1_raw"}}, telemetry=set())
        selected = preset_fields(schema, "throttle")
        self.assertIn("/sensors/RC_CHANNELS/chan3_raw", selected)
        grouped = blueprint(catalog=schema, visible_fields=selected, preset="throttle")
        chart = grouped.root_container.contents[1].contents[0]
        self.assertIn("/sensors/RC_CHANNELS/chan3_raw", chart.contents)
        self.assertIn("/sensors/SERVO_OUTPUT_RAW/servo1_raw", chart.contents)
