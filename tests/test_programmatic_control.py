"""Exercise control expiry, ownership, geometry, scoped logs, and the real SDK boundary."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from mcap.reader import make_reader
from backend.controller.runtime import ControllerRuntime
from backend.controller.vehicle_control import VehicleController
from backend.logger.session_recorder import SessionRecorder
from backend.mapping.constraints import OperatingArea, waypoint, xy
from backend.control.planning import Waypoint, Plan, Leg, LinearThrottleRamp, ConstantThrottle
from backend.control.adapter import Adapter, PARAMETERS
from backend.bridge.http_server import make_server
from backend.sdk import MissionControl
from backend.sdk.discovery import publish_endpoint

ORIGIN = Waypoint(25.758,-80.374)


def point(x,y): return waypoint((x,y),ORIGIN)


def lake(outline=None, fountains=()):
    outline = outline or [(-50,-50),(50,-50),(50,50),(-50,50),(-50,-50)]
    coords = [[point(x,y).lon,point(x,y).lat] for x,y in outline]
    return {"type":"FeatureCollection","padding_m":2,"features":[
        {"properties":{"role":"shoreline"},"geometry":{"type":"Polygon","coordinates":[coords]}},
        *[{"properties":{"role":"fountain_center"},"geometry":{"type":"Point","coordinates":[point(x,y).lon,point(x,y).lat]}} for x,y in fountains],
    ]}


class GeometryTests(unittest.TestCase):
    def test_entire_segment_excludes_fountain_even_with_valid_endpoints(self):
        area = OperatingArea(lake(fountains=[(0,0)]))
        plan = Plan([Leg(point(-10,0),point(10,0),ConstantThrottle(.2))])
        self.assertFalse(area.validate(plan)["valid"])
        self.assertEqual(area.validate(plan)["issues"][0]["reason"],"fountain_exclusion")

    def test_concave_shoreline_cannot_be_shortcut(self):
        outline = [(-20,-20),(20,-20),(20,20),(5,20),(5,-5),(-5,-5),(-5,20),(-20,20),(-20,-20)]
        area = OperatingArea(lake(outline))
        self.assertTrue(area.inside(xy(point(-10,10),area.origin)))
        self.assertIsNotNone(area.segment_issue(point(-10,10),point(10,10)))

    def test_padding_and_missing_geometry_fail_closed(self):
        area = OperatingArea(lake())
        self.assertEqual(area.segment_issue(point(49,0),point(49,10)),"shoreline_clearance")
        with self.assertRaises(ValueError): OperatingArea({"features":[]})


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ,{"MISSION_CONTROL_DATA_DIR":self.temp.name})
        self.env.start()
        self.source = SessionRecorder(Path(self.temp.name)/"session.mcap",formats=("mcap","jsonl"))
        self.source.open()
        self.controller = VehicleController(recorder=self.source)
        self.controller.lat,self.controller.lon = ORIGIN.lat,ORIGIN.lon
        tiles = SimpleNamespace(last_error=None,close=lambda:None)
        self.runtime = ControllerRuntime(self.controller,tiles,inputs=False)
        self.runtime.geometry = SimpleNamespace(lake_geojson=lambda:lake(),safety_padding_m=2)
        self.service = self.runtime.programmatic

    def tearDown(self):
        with self.runtime._commands:
            self.service.takeover("Test teardown")
            self.service.end_recording()
        for worker in self.service.finalizers: worker.join(5)
        self.source.close()
        self.env.stop()
        self.temp.cleanup()

    def acquire(self,**kwargs):
        return self.service.acquire({"start":{"lat":ORIGIN.lat,"lon":ORIGIN.lon},**kwargs})

    def test_expiry_neutralizes_and_revokes_without_ui_polling(self):
        info = self.acquire()
        self.service.set_target({"id":info["id"],"target":{"throttle":.5,"steering":0}})
        self.service.tick(time.monotonic()+.01)
        self.assertGreater(self.service.adapter.speed,0)
        self.service.tick(time.monotonic()+1)
        self.assertFalse(self.service.busy)
        self.assertEqual(self.service.adapter.speed,0)
        with self.assertRaises(ValueError):
            self.service.set_target({"id":info["id"],"target":{"throttle":.5,"steering":0}})

    def test_ownership_is_exclusive_and_ui_stop_revokes(self):
        info = self.acquire()
        with self.assertRaises(ValueError): self.acquire()
        self.assertTrue(self.runtime.session.manual.suspended)
        self.runtime.command("stop",{})
        self.assertFalse(self.service.busy)
        self.assertFalse(self.runtime.session.manual.suspended)
        with self.assertRaises(ValueError): self.service.require(info["id"])

    def test_kill_switch_blocks_reacquisition_until_explicit_reset(self):
        self.acquire()
        self.runtime.command("stop", {})
        self.assertTrue(self.runtime.state()["emergency_stopped"])
        self.assertIsNone(self.service.state()["execution"])
        with self.assertRaisesRegex(ValueError, "Kill switch"):
            self.acquire()
        for action, data in [("manual", {}), ("arm", {}), ("start", {}),
                             ("manual_active", {"active": True})]:
            with self.assertRaisesRegex(ValueError, "Kill switch"):
                self.runtime.command(action, data)
        # A worker that was already starting must finish before reset is allowed.
        self.controller.is_starting_mission = True
        with self.assertRaisesRegex(ValueError, "workers"):
            self.runtime.command("reset_stop", {})
        self.controller.is_starting_mission = False
        deadline = time.monotonic() + 1
        while self.controller._stop_in_progress and time.monotonic() < deadline:
            time.sleep(.01)
        self.runtime.command("reset_stop", {})
        self.assertFalse(self.runtime._manual_active)
        self.assertFalse(self.service.busy)
        self.acquire()

    def test_execution_banner_reports_route_profile_and_open_ended_progress(self):
        info = self.acquire()
        execution = self.service.state()["execution"]
        self.assertEqual(execution["label"], "Direct control")
        self.assertIsNone(execution["fraction"])
        self.service.play({"id": info["id"], "keyframes": [
            (0, {"throttle": 0, "steering": 0}),
            (10, {"throttle": .2, "steering": 0})]})
        self.service.profile_started = time.monotonic() - 5
        self.assertAlmostEqual(self.service.state()["execution"]["fraction"], .5, places=2)
        self.service.release()
        plan = Plan([Leg(ORIGIN, point(0,6), ConstantThrottle(.5))])
        info = self.acquire()
        self.service.follow({"id": info["id"], "plan": plan.to_dict()})
        self.service.owner["progress"] = .4
        execution = self.service.state()["execution"]
        self.assertEqual(execution["label"], "Route · leg 1/1")
        self.assertAlmostEqual(execution["fraction"], .4)
        self.service.release()
        self.assertIsNone(self.service.state()["execution"])

    def test_opposite_thrusters_rotate_without_translation_in_simulation(self):
        info = self.acquire(kind="thrusters")
        self.service.set_target({"id":info["id"],"target":{"left":.3,"right":-.3}})
        self.service.tick(time.monotonic()+.1)
        self.assertEqual(self.service.adapter.position,ORIGIN)
        self.assertGreater(self.service.adapter.heading,0)
        self.assertEqual(self.service.adapter.speed,0)

    def test_plan_executes_without_client_and_publishes_its_own_files(self):
        plan = Plan([Leg(ORIGIN,point(0,6),ConstantThrottle(.5))],arrival_radius_m=.5)
        info = self.service.execute({"name":"example","plan":plan.to_dict()})
        now = time.monotonic()
        for index in range(100):
            self.service.tick(now+index*.1)
            if not self.service.busy: break
        self.assertFalse(self.service.busy)
        self.assertEqual(self.service.last["status"],"completed")
        self.service.finalizers[-1].join(5)
        self.assertFalse(self.service.finalizers[-1].is_alive())
        directory = Path(info["directory"])
        self.assertEqual(json.loads((directory/"manifest.json").read_text())["status"],"completed")
        self.assertTrue((directory/"plan.json").exists())
        self.assertTrue((directory/"area.geojson").exists())
        with (directory/"recording.mcap").open("rb") as stream:
            records = list(make_reader(stream,validate_crcs=True).iter_messages())
        self.assertGreater(len(records),5)
        self.assertTrue(all(channel.topic == "/input/CONTROL" for _,channel,_ in records))

    def test_streaming_replacement_validates_before_installing_and_expires(self):
        info = self.acquire()
        first = Plan([Leg(ORIGIN,point(0,10),ConstantThrottle(.2))])
        self.service.follow({"id":info["id"],"plan":first.to_dict(),"valid_for_s":.5})
        follower = self.service.follower
        outside = Plan([Leg(ORIGIN,point(80,0),ConstantThrottle(.2))])
        with self.assertRaises(ValueError):
            self.service.follow({"id":info["id"],"plan":outside.to_dict()})
        self.assertIs(self.service.follower,follower)
        self.service.set_target({"id":info["id"],"target":{"throttle":0,"steering":.2}})
        self.assertIsNone(self.service.follower)
        self.service.tick(time.monotonic()+1)
        self.assertFalse(self.service.busy)

    def test_live_thrusters_send_raw_control_without_parameter_reads(self):
        c = self.controller
        c.connected = c._confirmed_armed = True
        c.fix_type = 3
        c._confirmed_mode = c.mode_str = "MANUAL"
        c._last_vehicle_heartbeat = c._last_position_at = c._last_heading_at = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        c._control_parameters = {}
        with patch.object(c.transport,"send") as send:
            info = self.acquire(mode="live",kind="thrusters")
            send.reset_mock()
            self.service.set_target({"id":info["id"],"target":{"left":.4,"right":-.4}})
            self.service.tick(time.monotonic()+.02)
            send.assert_called_once_with("manual_control",1,0,400,0,0,0)

    def test_joystick_meaning_is_preserved_for_verified_two_paddle_input(self):
        c = self.controller
        c.connected = True
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        c._control_parameters["PILOT_STEER_TYPE"] = (1,time.monotonic())
        with patch.object(c.transport,"send") as send:
            c.send_manual_control(200,300)
            self.assertEqual(send.call_args_list[0].args,("manual_control",1,0,500,-100,0,0))
            self.assertEqual(send.call_args_list[1].args,("rc_channels_override",1,1,1750,0,1450,0,0,0,0,0))

    def test_scoped_logs_copy_raw_traffic_without_bleeding_across_runs(self):
        from pymavlink import mavutil
        message = mavutil.mavlink.MAVLink_heartbeat_message(2,3,0,0,3,3)
        self.source.log_message(message)
        self.source.flush()
        info = self.service.begin_recording({"name":"raw"})
        self.source.log_message(message)
        self.service.end_recording()
        self.service.finalizers[-1].join(5)
        with Path(info["recording"]).open("rb") as stream:
            messages = list(make_reader(stream).iter_messages())
        self.assertEqual(sum(channel.topic == "/mavlink/HEARTBEAT" for _,channel,_ in messages),1)
        other = ControllerRuntime(self.controller,self.runtime.tiles,inputs=False)
        self.assertEqual(other.programmatic.command("status",{"id":info["id"]})["status"],"completed")

    def test_approaching_fountain_padding_requests_neutral_before_next_command(self):
        self.runtime.geometry = SimpleNamespace(lake_geojson=lambda:lake(fountains=[(0,0)]),safety_padding_m=2)
        start = point(0,5)
        info = self.service.acquire({'start':{'lat':start.lat,'lon':start.lon}})
        self.service.set_target({'id':info['id'],'target':{'throttle':.2,'steering':0}})
        self.service.adapter.position = point(0,2.2)
        self.service.adapter.speed = 1.0
        self.service.tick()
        self.assertFalse(self.service.busy)
        self.assertEqual(self.service.adapter.speed,0)
        self.assertIn('clearance',self.service.last['reason'])

    def test_invalid_plan_creates_no_run_and_does_not_acquire(self):
        plan = Plan([Leg(ORIGIN,point(70,0),ConstantThrottle(.2))])
        with self.assertRaises(ValueError): self.service.execute({"plan":plan.to_dict()})
        self.assertFalse(self.service.busy)
        self.assertFalse((Path(self.temp.name)/"runs").exists())

    def test_live_requires_verified_mapping_and_never_silently_simulates(self):
        with self.assertRaises(ValueError): self.acquire(mode="live")
        self.assertFalse(self.service.busy)
        self.assertFalse(self.controller.is_armed)

    def test_routes_survive_new_runtime_and_snapshot_is_independent(self):
        points = [{"lat":ORIGIN.lat,"lon":ORIGIN.lon},{"lat":point(0,8).lat,"lon":point(0,8).lon}]
        self.service.command("route_save",{"name":"test","waypoints":points})
        other = ControllerRuntime(self.controller,self.runtime.tiles,inputs=False)
        restored = other.programmatic.command("route_get",{"name":"test"})
        self.assertEqual(restored["waypoints"],points)

    def test_fractional_ramp_survives_initial_neutral_in_plot_snapshots(self):
        from backend.logger.rrd_logger import RrdLogger
        from backend.rerun.live import RerunTelemetry
        from backend.rerun.viewer_stream import ViewerStream
        from rerun.chunk import RrdReader
        import pyarrow as pa
        path = Path(self.temp.name)/"fractional.rrd"
        writer = RrdLogger(path)
        writer.open()
        stream = ViewerStream(path,disk_writer=writer)
        telemetry = RerunTelemetry(stream,{"crs":"EPSG:4326","bounds":[0,0,1,1],"center":[ORIGIN.lat,ORIGIN.lon]})
        telemetry.state(self.runtime.state(refresh_ui=False),0)
        info = self.acquire(kind="thrusters")
        for index,level in enumerate((.1234,.5678,.9999)):
            self.service.set_target({"id":info["id"],"target":{"left":level,"right":level}})
            self.service.tick()
            self.assertEqual(self.service.owner["throttle"],level)
            telemetry.state(self.runtime.state(refresh_ui=False),(index+1)*.1)
        writer.close()
        samples = []
        for chunk in RrdReader(path).stream():
            if chunk.entity_path != "/commands/input": continue
            batch = chunk.to_record_batch()
            if "requested_throttle" in batch.schema.names:
                column = batch.column("requested_throttle")
                self.assertEqual(column.type.value_type,pa.float64())
                samples.extend(value[0] for value in column.to_pylist())
        self.assertEqual(len(samples),4)
        for actual,expected in zip(samples,[0.0,123.4,567.8,999.9]):
            self.assertAlmostEqual(actual,expected)

    def test_acceleration_and_four_straight_cases_reach_east_before_they_complete(self):
        from experiments.motion._common import HOME, END
        from backend.batch import discover
        entries = discover([Path(__file__).resolve().parents[1]/'experiments'])[:5]
        service = self.service
        class Handle:
            def wait_until_finished(inner):
                now = service._started
                peak = 0
                for index in range(20000):
                    service.tick(now+index*.05)
                    if not service.busy: break
                    peak = max(peak, service.owner['throttle'])
                self.assertEqual(service.last['status'],'completed',service.last['reason'])
                self.assertAlmostEqual(peak, expected_peak, delta=.02)
                actual = Waypoint(service.last['lat'],service.last['lon'])
                self.assertLessEqual(math.hypot(*xy(actual,END)),1.01)
                service.finalizers[-1].join(5)
        class Client:
            @contextmanager
            def armed_manual(inner): yield inner
            def set_manual(inner): pass
            def arm(inner): pass
            def disarm(inner): pass
            def navigate(inner, destination, **kwargs): pass
            def run(inner, name, plan):
                service.execute({'name':name, 'plan':plan.to_dict()})
                return Handle()
        for entry, expected_peak in zip(entries, (1, .25, .5, .75, 1)):
            service.last = None  # Explicit independent simulation fixture at HOME.
            entry.function(Client())

    def test_backend_profile_is_evaluated_every_tick_and_home_transfer_does_not_teleport(self):
        info = self.acquire(kind="thrusters")
        self.service.play({"id":info["id"],"keyframes":[[0,{"left":0,"right":0}],[1,{"left":.4,"right":.4}],[2,{"left":0,"right":0}]]})
        began = self.service.profile_started
        self.service.tick(began+.123456)
        self.assertGreater(self.service.owner["left"],0)
        self.assertLess(self.service.owner["left"],.4)
        self.service.tick(began+1)
        displaced = self.service.adapter.position
        self.service.release(info["id"])
        self.assertNotEqual(displaced,ORIGIN)
        info = self.service.navigate({"destination":{"lat":ORIGIN.lat,"lon":ORIGIN.lon},"heading_deg":90})
        self.assertEqual(self.service.adapter.position,displaced)
        now = time.monotonic()
        for i in range(1000):
            self.service.tick(now+i*.05)
            if not self.service.busy: break
        self.assertEqual(self.service.last["status"],"completed")
        self.assertLess(abs((self.service.last["heading"]-90+180)%360-180),3.1)

    def advance_movement(self, seconds):
        began = self.service.motion_started
        for i in range(1, round(seconds / .05) + 1):
            self.service.tick(began + i * .05)
            if not self.service.busy or self.service.motion is None:
                break

    def test_timed_move_tracks_speed_and_stops_without_client_updates(self):
        info = self.acquire()
        self.service.movement("move", {"id":info["id"], "speed_mps":1, "duration_s":10})
        self.advance_movement(8)
        self.assertAlmostEqual(self.service.adapter.speed, 1, delta=.08)
        self.assertAlmostEqual(self.service.adapter.heading, 0)
        self.service.tick(self.service.motion_started + 10)
        self.assertEqual(self.service.owner["motion_status"], "completed")
        self.assertEqual(self.service.adapter.speed, 0)
        self.assertEqual(self.service.target["throttle"], 0)
        # Backend completion does not need a connected client to neutralize outputs.
        self.service.tick(self.service.motion_started + 13)
        self.assertFalse(self.service.busy)

    def test_heading_goal_takes_shortest_turn_and_finishes_with_zero_translation(self):
        info = self.acquire(kind="thrusters", heading_deg=350)
        self.service.movement("turn_to", {"id":info["id"], "heading_deg":10})
        self.advance_movement(10)
        self.assertEqual(self.service.owner["motion_status"], "completed")
        self.assertLess(abs((self.service.adapter.heading-10+180)%360-180), 3)
        self.assertEqual(self.service.adapter.position, ORIGIN)
        self.assertEqual(self.service.target["left"], 0)
        self.assertEqual(self.service.target["right"], 0)

    def test_position_goal_reaches_destination_and_neutralizes(self):
        info = self.acquire()
        destination = point(5, 6)
        self.service.movement("go_to", {"id":info["id"], "destination":asdict(destination), "speed_mps":1})
        self.advance_movement(40)
        self.assertEqual(self.service.owner["motion_status"], "completed")
        self.assertLessEqual(math.hypot(*xy(self.service.adapter.position, destination)), 1)
        self.assertEqual(self.service.adapter.speed, 0)

    def test_movement_validation_preserves_active_intent_and_replacement_is_identifiable(self):
        info = self.acquire()
        command = {"id":info["id"], "speed_mps":.5, "duration_s":5}
        first = self.service.movement("move", command)
        motion = self.service.motion
        for invalid in ({"speed_mps":float("nan")}, {"duration_s":0},
                        {"heading_deg":90, "turn_rate_deg_s":10}):
            with self.assertRaises(ValueError):
                self.service.movement("move", {**command, **invalid})
            self.assertIs(self.service.motion, motion)
        with self.assertRaisesRegex(ValueError, "constraint"):
            self.service.movement("go_to", {"id":info["id"], "destination":asdict(point(80, 0))})
        self.assertIs(self.service.motion, motion)
        second = self.service.movement("move", command)
        self.assertNotEqual(first["motion_id"], second["motion_id"])
        self.service.stop_movement({"id":info["id"], "motion_id":first["motion_id"]})
        self.assertIsNotNone(self.service.motion)
        self.service.stop_movement({"id":info["id"]})
        self.assertIsNone(self.service.motion)
        self.assertEqual(self.service.owner["motion_status"], "cancelled")

    def test_unreached_goal_times_out_and_raw_command_replaces_movement(self):
        info = self.acquire()
        self.service.movement("turn_to", {"id":info["id"], "heading_deg":180, "timeout_s":.1})
        self.advance_movement(.2)
        self.assertFalse(self.service.busy)
        self.assertIn("timed out", self.service.last["reason"])
        self.assertEqual(self.service.last["motion_status"], "failed")
        info = self.acquire()
        self.service.movement("move", {"id":info["id"], "speed_mps":1, "duration_s":5})
        self.service.set_target({"id":info["id"], "target":{"throttle":.2, "steering":0}})
        self.assertIsNone(self.service.motion)
        self.assertEqual(self.service.owner["motion_status"], "replaced")

    def test_no_gps_rejects_movement_goals_instead_of_using_throttle_as_speed(self):
        self.runtime.testing_without_gps = True
        info = self.acquire()
        for operation in ("move", "turn_to", "go_to"):
            with self.assertRaisesRegex(ValueError, "feedback"):
                self.service.movement(operation, {"id":info["id"]})

    def test_live_movement_requires_fresh_speed_feedback(self):
        # Use the simulated adapter as a state source to isolate speed freshness.
        info = self.acquire()
        self.service.owner["mode"] = "live"
        command = {"id":info["id"], "speed_mps":1, "duration_s":5}
        with self.assertRaisesRegex(ValueError, "ground-speed"):
            self.service.movement("move", command)
        self.controller._last_speed_at = time.monotonic()
        self.service.movement("move", command)
        self.controller._last_speed_at -= 2
        self.service.tick()
        self.assertFalse(self.service.busy)
        self.assertIn("ground-speed", self.service.last["reason"])

    def test_real_client_waits_for_backend_timed_movement_and_can_reuse_scope(self):
        server = make_server(self.runtime, "test-token")
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.runtime.worker.start()
        try:
            mc = MissionControl(f"http://127.0.0.1:{server.server_port}", "test-token")
            with mc:
                with mc.record("movement") as run:
                    with mc.control(start=ORIGIN) as control:
                        result = control.move(speed_mps=.5, heading_deg=0, duration_s=.15)
                        self.assertEqual(result["motion_status"], "completed")
                        self.assertEqual(result["throttle"], 0)
                        result = control.turn_to(heading_deg=0)
                        self.assertEqual(result["motion_status"], "completed")
                        result = control.go_to(ORIGIN)
                        self.assertEqual(result["motion_status"], "completed")
                        control.stop()
                self.assertEqual(run.status()["status"], "completed")
                self.assertTrue((run.directory / "recording.mcap").exists())
        finally:
            self.runtime._stop.set()
            self.runtime.worker.join(2)
            server.shutdown()
            server.server_close()

    def test_live_home_transfer_uses_auto_and_restores_manual_without_arming(self):
        c = self.controller
        c.connected = c._confirmed_armed = True
        c.fix_type = 3
        c.mode_str = c._confirmed_mode = "MANUAL"
        c._last_vehicle_heartbeat = c._last_position_at = c._last_heading_at = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        def mode(value):
            c.mode_str = c._confirmed_mode = value
            return True
        with patch.object(c,'upload_mission',return_value=True), \
             patch.object(c,'set_mode',side_effect=mode) as set_mode, patch.object(c,'set_arm') as arm, patch.object(c.transport,'send') as send:
            dest = point(0,6)
            self.service.navigate({'mode':'live','destination':{'lat':dest.lat,'lon':dest.lon}})
            self.service.auto_workers[-1].join(2)
            self.service.tick()
            c.lat,c.lon = dest.lat,dest.lon
            for _ in range(4): self.service.tick()
            self.assertEqual(self.service.last['status'],'completed')
            self.assertEqual([call.args[0] for call in set_mode.call_args_list],['AUTO','MANUAL'])
            arm.assert_not_called()
            self.assertTrue(any(call.args[0] == 'mission_set_current' for call in send.call_args_list))

    def test_real_client_can_record_direct_controls_without_renewing_ui_lease(self):
        server = make_server(self.runtime,"test-token")
        serving = threading.Thread(target=server.serve_forever,daemon=True)
        serving.start()
        self.runtime.worker.start()
        try:
            with publish_endpoint(server.server_port,"test-token"):
                with MissionControl.connect() as mc:
                    before = self.runtime._last_ui
                    mc.state()
                    self.assertEqual(self.runtime._last_ui,before)
                    with mc.record("direct",metadata={"sample":1}) as run:
                        with mc.control(kind="thrusters",start=ORIGIN) as control:
                            control.hold(.15,left=.2,right=-.2)
                    self.assertEqual(run.status()["status"],"completed")
                    self.assertTrue((run.directory/"recording.mcap").exists())
                    with mc.record("direct") as second:
                        mc.run_thruster("left", throttle_pct=20, duration_s=.15, start=ORIGIN)
                        state = mc.state()["sdk"]["control"]
                        self.assertEqual((state["left"], state["right"]), (0, 0))
                        mc.run_thruster("right", throttle_pct=20, duration_s=.15, start=ORIGIN)
                        state = mc.state()["sdk"]["control"]
                        self.assertEqual((state["left"], state["right"]), (0, 0))
                    self.assertNotEqual(run.directory,second.directory)
        finally:
            self.runtime._stop.set()
            self.runtime.worker.join(2)
            server.shutdown()
            server.server_close()

    def test_live_scopes_never_request_parameters_even_after_cache_expiry(self):
        c = self.controller
        c.connected = c._confirmed_armed = True
        c._confirmed_mode = 'MANUAL'
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        self.runtime.testing_without_gps = True
        c._control_parameters = {}
        clock = [time.monotonic()]
        mc = MissionControl('http://127.0.0.1:1234','test-token',mode='live')
        with mc, patch('time.monotonic',side_effect=lambda:clock[0]), \
             patch.object(c,'check_vehicle_link',return_value=True), \
             patch.object(c.transport,'send') as send, \
             patch.object(mc,'state',side_effect=lambda:{'sdk':self.service.state()}), \
             patch.object(mc,'_call',side_effect=lambda operation,**data:self.service.command(operation,data)):
            for elapsed in (0,61,122):
                clock[0] += elapsed
                with mc.control(kind='thrusters'):
                    pass
            self.assertTrue(send.call_args_list)
            self.assertTrue(all(call.args[0] == 'manual_control' for call in send.call_args_list))
            self.assertEqual(c._control_parameters,{})

    def test_explicit_vehicle_actions_use_normal_arming_and_do_not_enable_joystick(self):
        c = self.controller
        c.connected = True
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        c._confirmed_mode = 'MANUAL'
        with patch.object(c.transport,'send') as send, patch.object(c,'set_mode',return_value=True) as mode:
            self.service.command('vehicle_action',{'command':'set_manual'})
            mode.assert_called_once_with('MANUAL')
            self.assertFalse(self.runtime._manual_active)
            self.service.command('vehicle_action',{'command':'arm'})
            arm = send.call_args.args
            self.assertEqual(arm[0],'command_long')
            self.assertEqual(arm[5:7],(1.0,0.0))  # Normal arm; no force-arm magic.
            self.assertFalse(c._confirmed_armed)  # Transmission is not confirmation.
            c.emergency_stopped = True
            with self.assertRaisesRegex(ValueError,'Kill switch'):
                self.service.command('vehicle_action',{'command':'arm'})
            self.service.command('vehicle_action',{'command':'disarm'})
            self.assertEqual(send.call_args.args[5:7],(0.0,0.0))

    def test_parameter_requests_preserve_unrequested_cached_values(self):
        c = self.controller
        c.connected = True
        c.master = SimpleNamespace(target_system=1,target_component=1)
        c._control_parameters = {'RC1_MIN':(1100,time.monotonic()),'RC3_MIN':(1100,time.monotonic())}
        with patch.object(c.transport,'send') as send:
            result = self.service.command('parameters',{'request':True,'names':['RC1_MIN']})
        self.assertEqual(result,{'RC3_MIN':1100})
        self.assertEqual(send.call_count,1)
        self.assertEqual(send.call_args.args[-2],b'RC1_MIN')


if __name__ == "__main__": unittest.main()


class RecordingCloseRaceTests(unittest.TestCase):
    def test_late_source_sample_after_close_does_not_write_closed_destination(self):
        from backend.control.recording import RunRecording
        from backend.logger.records import input_record
        with tempfile.TemporaryDirectory() as tmp:
            source = SessionRecorder(Path(tmp)/'source.mcap',formats=('mcap',))
            source.open()
            try:
                recording = RunRecording(Path(tmp)/'run',source)
                sample = input_record({'requested_throttle':10})
                recording.ended_ns = sample.receive_timestamp_ns+1
                recording.copy(sample)
                self.assertEqual(recording.close(),[])
                # A delayed source callback can still carry a timestamp inside the run.
                recording.copy(sample)
                self.assertEqual(recording.recorder.errors,[])
            finally:
                source.close()


if __name__ == "__main__": unittest.main()
