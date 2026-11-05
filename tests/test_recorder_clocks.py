"""Cross-format source clocks and passive QGC-compatible recorder integration."""
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from mcap.reader import make_reader
from pymavlink import mavutil
from rerun.chunk import RrdReader
from boats.heron.backend.logger.records import mavlink_record, input_record
from boats.heron.backend.logger.session_recorder import SessionRecorder
from boats.heron.backend.logger.mcap_logger import McapLogger
from boats.heron.backend.logger.jsonl_logger import JsonlLogger
from boats.heron.backend.logger.rrd_logger import RrdLogger

ROOT = Path(__file__).resolve().parents[1]

class RecorderClockTests(unittest.TestCase):
    def setUp(self):
        self.encoder = mavutil.mavlink.MAVLink(None, srcSystem=3, srcComponent=1)

    def packet(self, message):
        message.pack(self.encoder)
        return message

    def test_source_clock_is_not_replaced_by_reception_or_disk_write_time(self):
        received = 1_791_900_000_123_000_000
        attitude = self.packet(self.encoder.attitude_encode(12345, 0, 0, 0, 0, 0, 0))
        gps_boot = self.packet(self.encoder.gps_raw_int_encode(23456000, 3, 257500000, -803000000, 0, 0, 0, 0, 0, 10))
        gps_utc = self.packet(self.encoder.gps_raw_int_encode(received//1000, 3, 257500000, -803000000, 0, 0, 0, 0, 0, 10))
        heartbeat = self.packet(self.encoder.heartbeat_encode(11, 3, 0, 0, 4))
        records = [mavlink_record(message, receive_timestamp_ns=received+i) for i,message in enumerate([attitude,gps_boot,gps_utc,heartbeat])]
        records.append(input_record({'left_x':.2,'left_y':.3,'right_x':.4,'right_y':.5},timestamp_ns=received+10))
        self.assertEqual([r.clock for r in records], ['vehicle_boot','vehicle_boot','vehicle_unix','unavailable','ground_station'])
        self.assertEqual(records[0].timestamp_ns,12_345_000_000)
        self.assertEqual(records[1].timestamp_ns,23_456_000_000)
        self.assertIsNone(records[3].timestamp_ns)
        self.assertIsNone(records[0].payload['timestamp_iso'])
        with tempfile.TemporaryDirectory() as tmp:
            prefix=Path(tmp)/'session'
            with SessionRecorder(prefix) as recorder:
                for record in records: recorder.write(record)
                recorder.flush()
                self.assertEqual(recorder.errors, [])
            with prefix.with_suffix('.mcap').open('rb') as stream:
                messages=list(make_reader(stream,validate_crcs=True).iter_messages())
            payloads=[json.loads(message.data) for _,_,message in messages]
            self.assertEqual(payloads,[record.payload for record in records])
            self.assertEqual([json.loads(line) for line in prefix.with_suffix('.jsonl').read_text().splitlines()],payloads)
            self.assertEqual(messages[0][2].log_time,received)
            self.assertEqual(messages[0][2].publish_time,12_345_000_000)
            # MCAP requires publish_time=log_time when unavailable; source stays null.
            self.assertEqual(messages[3][2].publish_time,received+3)
            self.assertIsNone(payloads[3]['timestamp_ns'])
            timelines=set()
            for chunk in RrdReader(prefix.with_suffix('.rrd')).stream():
                timelines.update(chunk.to_record_batch().schema.names)
            self.assertIn('vehicle_boot_3_1',timelines)
            self.assertIn('vehicle_time',timelines)
            self.assertIn('ground_station_time',timelines)
            result=subprocess.run([sys.executable,'-m','rerun','rrd','verify',str(prefix.with_suffix('.rrd'))],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_each_writer_runs_independently_without_controller_or_viewer(self):
        record=input_record({'left_x':.25},timestamp_ns=1_791_900_000_000_000_000)
        with tempfile.TemporaryDirectory() as tmp:
            for writer_type in (McapLogger,JsonlLogger,RrdLogger):
                writer=writer_type(Path(tmp)/writer_type.__name__)
                writer.open()
                writer.write(record)
                writer.flush()
                writer.close()
                writer.close()
                self.assertTrue(writer.output_path.is_file())
                self.assertFalse(writer.active_path.exists())

    def test_mcap_jsonl_only_does_not_import_rerun_or_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = """
import sys
from boats.heron.backend.logger.session_recorder import SessionRecorder
from boats.heron.backend.logger.records import input_record
with SessionRecorder(sys.argv[1], formats=('mcap','jsonl')) as recorder:
    recorder.write(input_record({'left_x':.5}))
assert 'rerun' not in sys.modules
assert 'boats.heron.backend.controller.vehicle_control' not in sys.modules
"""
            result = subprocess.run([sys.executable, '-c', code, str(Path(tmp)/'minimal')], cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p.suffix for p in Path(tmp).iterdir()}, {'.mcap','.jsonl'})

    def test_unix_zero_uses_boot_time_and_tx_uses_ground_station_time(self):
        message=self.packet(self.encoder.system_time_encode(0,0))
        received=1_791_900_000_000_000_000
        record=mavlink_record(message,receive_timestamp_ns=received)
        self.assertEqual((record.clock,record.timestamp_ns),('vehicle_boot',0))
        sent=mavlink_record(message,direction='tx',receive_timestamp_ns=received)
        self.assertEqual((sent.clock,sent.timestamp_ns),('ground_station',received))

class PassiveRecorderTests(unittest.TestCase):
    def test_forwarded_feed_writes_three_formats_and_sends_no_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reserve:
                reserve.bind(('127.0.0.1',0)); port=reserve.getsockname()[1]
            prefix=Path(tmp)/'capture'
            process=subprocess.Popen([sys.executable,'-u','-m','boats.heron.backend.logger','--connection',f'udpin:127.0.0.1:{port}','--output',str(prefix),'--duration','1.0'],
                cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=10),'Recorder startup timed out')
                self.assertIn('Recording ',process.stdout.readline())
                encoder=mavutil.mavlink.MAVLink(None,srcSystem=3,srcComponent=1)
                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
                    sender.bind(('127.0.0.1',0))
                    packet=encoder.attitude_encode(54321,.1,.2,.3,0,0,0).pack(encoder)
                    sender.sendto(packet,('127.0.0.1',port))
                    sender.settimeout(.1)
                    with self.assertRaises(socket.timeout): sender.recvfrom(1024)
                process.wait(timeout=10)
                self.assertEqual(process.returncode,0,process.stderr.read())
                self.assertEqual({p.suffix for p in Path(tmp).iterdir()},{'.mcap','.jsonl','.rrd'})
                payload=json.loads(prefix.with_suffix('.jsonl').read_text())
                self.assertEqual(payload['timestamp_ns'],54_321_000_000)
                self.assertEqual(payload['clock'],'vehicle_boot')
                self.assertEqual(payload['direction'],'rx')
                self.assertEqual(payload['sys_id'],3)
            finally:
                if process.poll() is None: process.kill();process.wait()
                process.stdout.close();process.stderr.close()
