"""Exercise control expiry, ownership, geometry, scoped logs, and the real SDK boundary."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from mcap.reader import make_reader
from backend.controller.runtime import ControllerRuntime
from backend.controller.vehicle_control import VehicleController
from backend.logger.session_recorder import SessionRecorder
from backend.mapping.constraints import OperatingArea, waypoint, xy
from backend.control.planning import Waypoint, Plan, Leg, Linear, Constant
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
        plan = Plan([Leg(point(-10,0),point(10,0),Constant(.2))])
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

    def test_opposite_thrusters_rotate_without_translation_in_simulation(self):
        info = self.acquire(kind="thrusters")
        self.service.set_target({"id":info["id"],"target":{"left":.3,"right":-.3}})
        self.service.tick(time.monotonic()+.1)
        self.assertEqual(self.service.adapter.position,ORIGIN)
        self.assertGreater(self.service.adapter.heading,0)
        self.assertEqual(self.service.adapter.speed,0)

    def test_plan_executes_without_client_and_publishes_its_own_files(self):
        plan = Plan([Leg(ORIGIN,point(0,6),Constant(.5))],arrival_radius_m=.5)
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
        first = Plan([Leg(ORIGIN,point(0,10),Constant(.2))])
        self.service.follow({"id":info["id"],"plan":first.to_dict(),"valid_for_s":.5})
        follower = self.service.follower
        outside = Plan([Leg(ORIGIN,point(80,0),Constant(.2))])
        with self.assertRaises(ValueError):
            self.service.follow({"id":info["id"],"plan":outside.to_dict()})
        self.assertIs(self.service.follower,follower)
        self.service.set_target({"id":info["id"],"target":{"throttle":0,"steering":.2}})
        self.assertIsNone(self.service.follower)
        self.service.tick(time.monotonic()+1)
        self.assertFalse(self.service.busy)

    def test_live_paddles_encode_separate_rc_inputs_and_verify_mapping(self):
        c = self.controller
        c.connected = c._confirmed_armed = True
        c.fix_type = 3
        c._confirmed_mode = c.mode_str = "MANUAL"
        c._last_vehicle_heartbeat = c._last_position_at = c._last_heading_at = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        expected = {"PILOT_STEER_TYPE":1,"SERVO1_FUNCTION":73,"SERVO3_FUNCTION":74,
                    "RCMAP_ROLL":1,"RCMAP_THROTTLE":3,"RC1_MIN":1000,"RC1_TRIM":1500,
                    "RC1_MAX":2000,"RC1_REVERSED":0,"RC3_MIN":1000,"RC3_TRIM":1500,"RC3_MAX":2000,"RC3_REVERSED":0}
        c._control_parameters = {key:(value,time.monotonic()) for key,value in expected.items()}
        with patch.object(c.transport,"send") as send:
            info = self.acquire(mode="live",kind="thrusters")
            send.reset_mock()
            self.service.set_target({"id":info["id"],"target":{"left":.4,"right":-.4}})
            self.service.tick(time.monotonic()+.02)
            send.assert_called_once_with("rc_channels_override",1,1,1700,0,1300,0,0,0,0,0)
            self.service.takeover("test")
            c._control_parameters["SERVO1_FUNCTION"] = (74,time.monotonic())
            with self.assertRaises(ValueError): self.acquire(mode="live",kind="thrusters")

    def test_joystick_meaning_is_preserved_for_verified_two_paddle_input(self):
        c = self.controller
        c.connected = True
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        c._control_parameters["PILOT_STEER_TYPE"] = (1,time.monotonic())
        with patch.object(c.transport,"send") as send:
            c.send_manual_control(200,300)
            self.assertEqual(send.call_args_list[0].args,("manual_control",1,-100,0,0,500,0))
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
        plan = Plan([Leg(ORIGIN,point(70,0),Constant(.2))])
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

    def test_all_straight_cases_reach_east_before_they_complete(self):
        from experiments._common import HOME, END
        from backend.batch import discover
        entries = discover([Path(__file__).resolve().parents[1]/'experiments'])[:4]
        service = self.service
        class Handle:
            def wait_until_finished(inner):
                now = time.monotonic()
                for index in range(20000):
                    service.tick(now+index*.05)
                    if not service.busy: break
                self.assertEqual(service.last['status'],'completed',service.last['reason'])
                actual = Waypoint(service.last['lat'],service.last['lon'])
                self.assertLessEqual(math.hypot(*xy(actual,END)),1.01)
                service.finalizers[-1].join(5)
        class Client:
            def run(inner,name,plan):
                service.execute({'name':name,'plan':plan.to_dict()})
                return Handle()
        for entry in entries:
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
        with patch('backend.control.adapter.Adapter.parameters'), patch.object(c,'upload_mission',return_value=True), \
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
                        pass
                    self.assertNotEqual(run.directory,second.directory)
        finally:
            self.runtime._stop.set()
            self.runtime.worker.join(2)
            server.shutdown()
            server.server_close()


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
