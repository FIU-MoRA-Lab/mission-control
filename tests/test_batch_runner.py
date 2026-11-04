"""Cover real runner-owned startup, sequential files, partial logs and UI attachment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from mcap.reader import make_reader

from backend.batch import discover, backend_session

ROOT = Path(__file__).resolve().parents[1]
START = 'Waypoint(25.7581,-80.37365)'


def controls(body='pass'):
    return f'''from backend.sdk import Waypoint

def run(mc):
    with mc.control(start={START}) as control:
        control.hold(.1, throttle=.2, steering=0)
        {body}
'''


class DiscoveryTests(unittest.TestCase):
    def test_sorted_files_and_ordered_multiple_entries_skip_main_and_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'helper.py').write_text('VALUE = 1\n')
            (root/'b.py').write_text('def second(mc): pass\ndef first(mc): pass\nEXPERIMENTS = {"second":second,"first":first}\n')
            (root/'a.py').write_text('def run(mc): pass\nif __name__ == "__main__": raise AssertionError("must not execute")\n')
            entries = discover([root,root/'a.py'])
            self.assertEqual([e.name for e in entries],['a','second','first'])

    def test_invalid_and_async_entries_fail_before_backend_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'bad.py'
            path.write_text('async def run(mc): pass\n')
            with self.assertRaises(ValueError): discover([path])
            path.write_text('EXPERIMENTS = []\n')
            with self.assertRaises(ValueError): discover([path])


class FailureUiTests(unittest.TestCase):
    def test_default_batch_directory_respects_the_shared_log_root(self):
        from backend.batch import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root/'case.py'
            script.write_text('def run(mc): pass\n')
            logs = root/'custom-logs'
            with patch.dict(os.environ,{'MISSION_CONTROL_DATA_DIR':str(root/'data'),'MISSION_CONTROL_LOG_DIR':str(logs)}), \
                 patch('backend.batch.backend_session',side_effect=RuntimeError('startup fixture')):
                result = main([str(script)])
            self.assertEqual(result,1)
            batches = list((logs/'batches').iterdir())
            self.assertEqual(len(batches),1)
            self.assertTrue((batches[0]/'results.json').exists())
            self.assertFalse((root/'data/batches').exists())

    def test_failure_keeps_backend_alive_until_attached_ui_is_closed(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock
        from backend.batch import main
        active = []
        mc = Mock()
        mc.state.return_value = {'viewer_attached':True}
        @contextmanager
        def backend(*args,**kwargs):
            active.append(True)
            try: yield mc,Path('/tmp/unused-descriptor')
            finally: active.pop()
        ui = Mock()
        ui.poll.return_value = None
        def wait(**kwargs):
            if not kwargs:
                self.assertEqual(active,[True])
                ui.poll.return_value = 1
            return 0
        ui.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as tmp:
            entry = SimpleNamespace(path=Path(tmp)/'case.py')
            with patch('backend.batch.discover',return_value=[entry]),patch('backend.batch.backend_session',backend), \
                 patch('backend.launch.launch_viewer',return_value=ui),patch('backend.batch.execute',side_effect=RuntimeError('case failed')):
                result = main([tmp,'--ui','--output-dir',str(Path(tmp)/'out')])
        self.assertEqual(result,1)
        mc._call.assert_called_with('release')
        self.assertEqual(active,[])
        self.assertTrue(any(not call.kwargs for call in ui.wait.call_args_list))


class BatchProcessTests(unittest.TestCase):
    def run_batch(self, temp, files):
        root = Path(temp)
        scripts = root/'scripts'
        scripts.mkdir()
        for name,source in files.items(): (scripts/name).write_text(source)
        data = root/'data'
        env = dict(os.environ,MISSION_CONTROL_DATA_DIR=str(data),MISSION_CONTROL_CACHE_DIR=str(root/'cache'))
        process = subprocess.run([sys.executable,'-m','backend.batch',str(scripts),
                                  '--output-dir',str(root/'batches'),'--startup-timeout','15'],
                                 cwd=ROOT,env=env,capture_output=True,text=True,timeout=45)
        directory = next((root/'batches').iterdir())
        return process,directory,json.loads((directory/'results.json').read_text())

    def test_one_backend_runs_files_sequentially_and_records_each_separately(self):
        path_run = '''from backend.sdk import Waypoint, Plan, Leg, ConstantThrottle

def run(mc):
    plan = Plan([Leg(Waypoint(25.7581,-80.37365),Waypoint(25.758154,-80.37365),ConstantThrottle(1))])
    execution = mc.run("path",plan)
    execution.wait_until_finished()
'''
        with tempfile.TemporaryDirectory() as tmp:
            process,directory,summary = self.run_batch(tmp,{'01_controls.py':controls(),'02_path.py':path_run})
            self.assertEqual(process.returncode,0,process.stdout+process.stderr+(directory/'backend.log').read_text())
            self.assertEqual(summary['status'],'completed')
            results = summary['results']
            self.assertEqual([r['name'] for r in results],['01_controls','02_path'])
            self.assertEqual(len({r['directory'] for r in results}),2)
            owners = set()
            for result in results:
                run = Path(result['directory'])
                manifest = json.loads((run/'manifest.json').read_text())
                owners.add(manifest['owner_pid'])
                self.assertEqual(manifest['status'],'completed')
                self.assertTrue((run/'source.py').exists())
                with (run/'recording.mcap').open('rb') as stream:
                    self.assertGreater(len(list(make_reader(stream,validate_crcs=True).iter_messages())),2)
            self.assertEqual(len(owners),1)
            self.assertFalse((directory/'connection.json').exists())
            self.assertEqual(len(list((directory/'session').rglob('*.mcap'))),1)
            self.assertFalse((Path(tmp)/'data/logs').exists())

    def test_navigation_only_occurs_when_explicitly_requested_by_experiment(self):
        source = """from backend.sdk import Waypoint

def run(mc):
    with mc.control(kind='thrusters',start=Waypoint(25.7583,-80.3738),heading_deg=90) as control:
        control.play([(0,{'left':0,'right':0}),(.1,{'left':.2,'right':.2}),(.2,{'left':0,'right':0})])
"""
        config = {'home':{'lat':25.7583,'lon':-80.3738},'end':{'lat':25.7583,'lon':-80.373775},'heading_deg':90,'speed_mps':1}
        for explicit_navigation in (False, True):
            with self.subTest(explicit_navigation=explicit_navigation), tempfile.TemporaryDirectory() as tmp:
                script = source
                if explicit_navigation:
                    script = script.replace("    with mc.control", "    mc.navigate(Waypoint(25.7583,-80.3738), speed_mps=1, heading_deg=90)\n    with mc.control")
                    script += '    mc.navigate(Waypoint(25.7583,-80.373775), speed_mps=1)\n    mc.navigate(Waypoint(25.7583,-80.3738), speed_mps=1, heading_deg=90)\n'
                process,directory,summary = self.run_batch(tmp,{'01_profile.py':script,'batch.json':json.dumps(config)})
                self.assertEqual(process.returncode,0,process.stderr+(directory/'backend.log').read_text())
                manifest = json.loads((Path(summary['results'][0]['directory'])/'manifest.json').read_text())
                self.assertEqual(manifest['recording_errors'],[])
                with Path(manifest['recording']).open('rb') as stream:
                    payloads = [json.loads(m.data)['data'] for _,_,m in make_reader(stream).iter_messages()]
                self.assertTrue(any(p.get('event') == 'input_profile' for p in payloads))
                transfers = [p for p in payloads if p.get('event') == 'auto_transfer_started']
                self.assertEqual(len(transfers), 3 if explicit_navigation else 0)
                if explicit_navigation:
                    self.assertEqual([p['destination'] for p in transfers],
                                     [config['home'], config['end'], config['home']])

    def test_movement_goals_complete_in_batch_and_record_feedback_without_schema_errors(self):
        source = """from backend.sdk import Waypoint

def run(mc):
    start = Waypoint(25.7583, -80.3738)
    with mc.control(start=start, heading_deg=90) as control:
        control.move(speed_mps=0.5, heading_deg=90, duration_s=0.2, acceleration_mps2=0.5)
        control.turn_to(heading_deg=90)
        control.go_to(start, speed_mps=0.5)
"""
        with tempfile.TemporaryDirectory() as tmp:
            process, directory, summary = self.run_batch(tmp, {"01_movement.py":source})
            self.assertEqual(process.returncode, 0, process.stderr + (directory/'backend.log').read_text())
            manifest = json.loads((Path(summary['results'][0]['directory'])/'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'completed')
            self.assertEqual(manifest['recording_errors'], [])
            with Path(manifest['recording']).open('rb') as stream:
                payloads = [json.loads(m.data)['data'] for _, _, m in make_reader(stream).iter_messages()]
            self.assertEqual(sum(p.get('event') == 'movement_started' for p in payloads), 3)
            self.assertEqual(sum(p.get('event') == 'movement_completed' for p in payloads), 3)
            self.assertTrue(any('target_speed_mps' in p for p in payloads))

    def test_failure_neutralizes_saves_partial_run_and_does_not_run_next_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            process,directory,summary = self.run_batch(tmp,{'01_fail.py':controls('raise RuntimeError("intentional failure")'),
                                                          '02_never.py':controls()})
            self.assertEqual(process.returncode,1,process.stdout+process.stderr)
            self.assertEqual(summary['status'],'failed')
            self.assertEqual(len(summary['results']),1)
            result = summary['results'][0]
            run = Path(result['directory'])
            self.assertEqual(json.loads((run/'manifest.json').read_text())['status'],'failed')
            with (run/'recording.mcap').open('rb') as stream:
                payloads = [json.loads(message.data)['data'] for _,_,message in make_reader(stream).iter_messages()]
            ended = [p for p in payloads if p.get('event') == 'control_ended']
            self.assertTrue(ended)
            self.assertEqual(ended[-1]['throttle'],0)
            self.assertIn('intentional failure',(directory/'failure.txt').read_text())
            self.assertFalse((directory/'connection.json').exists())

    def test_viewer_can_attach_and_detach_without_owning_backend(self):
        import rerun as rr
        import socket
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'MISSION_CONTROL_DATA_DIR':tmp,'MISSION_CONTROL_CACHE_DIR':tmp,
        }):
            directory = Path(tmp)/'batch'
            directory.mkdir()
            sink = rr.RecordingStream('test-batch-viewer')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0))
                port = probe.getsockname()[1]
            url = sink.serve_grpc(grpc_port=port)
            try:
                with backend_session(directory,mode='simulation',connection='',mission_file=None,inputs=False,timeout=15) as (mc,endpoint):
                    self.assertTrue(endpoint.exists())
                    self.assertFalse(mc.state()['viewer_attached'])
                    ready = mc._request('/command',{'action':'attach_viewer','url':url})
                    self.assertIn('basemap',ready)
                    self.assertTrue(mc.state()['viewer_attached'])
                    mc._request('/command',{'action':'detach_viewer'})
                    self.assertFalse(mc.state()['viewer_attached'])
                    with mc.record('after-detach'):
                        from backend.sdk import Waypoint
                        with mc.control(start=Waypoint(25.7581,-80.37365)) as control:
                            control.hold(.1,throttle=.2,steering=0)
                self.assertFalse(endpoint.exists())
            finally:
                sink.disconnect()


if __name__ == '__main__': unittest.main()
