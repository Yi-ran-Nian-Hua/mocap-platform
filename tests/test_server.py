import csv
import io
import json
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
import queue
from http.server import ThreadingHTTPServer
from pathlib import Path
from server import Application, CaptureStore, validate_config, csv_lines, handler_for


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CaptureStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.stop_recording()
        self.tmp.cleanup()

    def test_recent_data_then_stale_and_reset(self):
        self.store.ingest({'type': 'rigid_body', 'id': 8, 'pos': [1,2,3], 'quat': [0,0,0,1], 'timestamp': 100})
        fresh = self.store.snapshot(now=101)
        self.assertTrue(fresh['connected'])
        self.assertEqual(fresh['rigid_bodies'][0]['id'], 8)
        self.assertFalse(fresh['rigid_bodies'][0]['stale'])
        stale = self.store.snapshot(now=104)
        self.assertFalse(stale['connected'])
        self.assertTrue(stale['rigid_bodies'][0]['stale'])
        self.store.reset()
        self.assertEqual(self.store.snapshot()['rigid_bodies'], [])

    def test_source_and_retarget_do_not_overwrite(self):
        for mode in ('source', 'retarget'):
            self.store.ingest({'type':'skeleton','id':0,'mode':mode,'joints':[]})
        self.assertEqual(len(self.store.snapshot()['skeletons']), 2)

    def test_tracker_boundary_classifies_skeleton_joints_before_recording(self):
        name = self.store.start_recording({})
        for sensor in (299, 300, 450):
            self.store.ingest({'type':'tracker','id':sensor,'frame':10,'timestamp':100,
                               'pos':[1,2,3],'quat':[0,0,0,1]})
        snapshot = self.store.snapshot(now=101)
        self.assertEqual([e['id'] for e in snapshot['rigid_bodies']], [299])
        self.assertEqual([e['id'] for e in snapshot['skeleton_joints']], [300,450])
        self.assertTrue(snapshot['connected'])
        self.assertEqual(snapshot['event_count'], 3)
        self.store.stop_recording()
        path = self.store.resolve_recording(name)
        events = [json.loads(line) for line in path.read_text().splitlines()][1:]
        self.assertEqual([e['type'] for e in events], ['rigid_body','skeleton_joint','skeleton_joint'])
        rows = list(csv.DictReader(io.StringIO(''.join(csv_lines(path)))))
        self.assertEqual([e['type'] for e in rows], ['rigid_body','skeleton_joint','skeleton_joint'])
        self.assertEqual(rows[1]['joint_id'], '300')

    def test_legacy_tracker_is_reclassified_and_preserved_in_initial_snapshot(self):
        self.store.ingest({'type':'rigid_body','id':300,'timestamp':100,'pos':[1,2,3],'quat':[0,0,0,1]})
        self.assertEqual(self.store.snapshot()['rigid_bodies'], [])
        name = self.store.start_recording({})
        self.store.stop_recording()
        events = [json.loads(line) for line in self.store.resolve_recording(name).read_text().splitlines()]
        self.assertEqual(events[1]['type'], 'skeleton_joint')
        self.assertTrue(events[1]['snapshot'])
        self.store.reset()
        self.assertEqual(self.store.snapshot()['skeleton_joints'], [])

    def test_hierarchy_retains_separate_source_and_retarget_modes(self):
        for flag, parent in [(0, 1), (1, 8)]:
            self.store.ingest({'type':'hierarchy','id':2,'parent':parent,'name':'joint'})
            self.store.ingest({'type':'hierarchy_end','retarget_flag':flag})
        rows = self.store.snapshot()['hierarchy']
        self.assertEqual({r['mode']:r['parent'] for r in rows}, {'source':1,'retarget':8})

    def test_old_session_consumer_cannot_write_or_stop_new_recording(self):
        app = Application(Path(self.tmp.name))
        app.generation = 2
        name = app.store.start_recording({})
        events = queue.Queue()
        events.put({'type':'rigid_body','id':99,'pos':[1,2,3],'quat':[0,0,0,1]})
        class Finished:
            exitcode = 0
            def is_alive(self): return False
        app._consume(Finished(), events, 1)
        self.assertTrue(app.store.snapshot()['recording']['active'])
        self.assertEqual(app.store.snapshot()['rigid_bodies'], [])
        app.store.stop_recording()

    def test_empty_markers_clear_and_hierarchy_not_counted_as_live(self):
        self.store.ingest({'type':'hierarchy','id':2,'parent':1,'name':'head'})
        self.assertFalse(self.store.snapshot()['connected'])
        self.store.ingest({'type':'markers','points':[{'id':1,'pos':[1,2,3]}]})
        self.assertEqual(len(self.store.snapshot()['markers']), 1)
        self.store.ingest({'type':'markers','points':[]})
        self.assertEqual(self.store.snapshot()['markers'], [])
        self.store.ingest({'type':'hierarchy_reset'})
        self.assertEqual(self.store.snapshot()['hierarchy'], [])

    def test_recording_preserves_data_and_exports_all_types(self):
        self.store.ingest({'type':'hierarchy','id':0,'parent':-1,'name':'root'})
        name = self.store.start_recording({'host':'192.168.1.200'})
        events = [
            {'type':'rigid_body','id':7,'frame':9,'pos':[1,2,3],'quat':[0,0,0,1]},
            {'type':'skeleton','id':0,'mode':'source','frame':9,'position_space':'global','joints':[{'id':2,'pos':[4,5,6],'quat':[0,0,0,1],'detected':True}]},
            {'type':'markers','points':[{'id':10,'pos':[7,8,9]}]},
        ]
        for event in events:
            self.store.ingest(event)
        with self.assertRaises(ValueError):
            self.store.resolve_recording(name)
        self.store.stop_recording()
        path = self.store.resolve_recording(name)
        records = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[0]['type'], 'metadata')
        self.assertEqual(records[1]['type'], 'hierarchy')
        self.assertEqual(records[-1]['points'][0]['pos'], [7,8,9])
        rows = list(csv.DictReader(io.StringIO(''.join(csv_lines(path)))))
        self.assertEqual([r['type'] for r in rows], ['hierarchy','rigid_body','skeleton','markers'])
        self.assertEqual(rows[2]['joint_id'], '2')
        self.assertEqual(rows[2]['position_space'], 'global')
        self.assertEqual(self.store.snapshot()['recording']['events'], 3)

    def test_no_recording_path_escape_or_duplicate_start(self):
        for name in ('../secret.jsonl', '/tmp/x.jsonl', 'x.csv', '..\\secret.jsonl'):
            with self.assertRaises(ValueError): self.store.resolve_recording(name)
        self.store.start_recording({})
        with self.assertRaises(ValueError): self.store.start_recording({})

    def test_recorded_csv_names_cannot_be_formulas(self):
        name = self.store.start_recording({})
        self.store.ingest({'type':'hierarchy','id':1,'parent':0,'name':'=1+1'})
        self.store.stop_recording()
        rows = list(csv.DictReader(io.StringIO(''.join(csv_lines(self.store.resolve_recording(name))))))
        self.assertEqual(rows[0]['name'], "'=1+1")

    def test_static_marker_is_recorded_as_initial_snapshot_with_old_time(self):
        self.store.ingest({'type':'markers','timestamp':100,'freshness_unknown':True,'points':[{'id':4,'pos':[1,2,3]}]})
        name = self.store.start_recording({})
        self.store.stop_recording()
        records = [json.loads(s) for s in self.store.resolve_recording(name).read_text(encoding='utf-8').splitlines()]
        markers = [r for r in records if r['type'] == 'markers']
        self.assertEqual(len(markers), 1)
        self.assertTrue(markers[0]['snapshot'])
        self.assertEqual(markers[0]['timestamp'], 100)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_input_validation(self):
        self.assertEqual(validate_config({})['host'], '192.168.1.200')
        for bad in ({'host':'x@evil'}, {'port':0}, {'port':True}, {'human_ids':[-1]}, {'human_ids':'0-15'}, {'poll_hz':10000}, {'device':'A@B'}, {'unknown':True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate_config(bad)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.disconnect()
        self.tmp.cleanup()

    def test_state_and_invalid_requests(self):
        with urllib.request.urlopen(self.url + '/api/state') as r:
            self.assertFalse(json.load(r)['running'])
        for route, data, headers, code in [
            ('/api/connect', b'{"host":"bad"}', {}, 400),
            ('/api/record/start', b'{}', {}, 400),
            ('/api/connect', b'{}', {'Origin':'https://example.com'}, 403),
            ('/api/state', None, {'Host':'example.com'}, 403),
            ('/api/recordings/../secret.jsonl', None, {}, 400),
        ]:
            request = urllib.request.Request(self.url + route, data=data, headers=headers)
            with self.subTest(route=route, code=code), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, code)

    def test_export_http_and_stopped_recording(self):
        name = self.app.store.start_recording({})
        self.app.store.ingest({'type':'markers','points':[{'id':3,'pos':[1,2,3]}]})
        self.app.store.stop_recording()
        with urllib.request.urlopen(self.url + '/api/recordings/' + name + '?format=csv') as r:
            rows = list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
            self.assertEqual(rows[0]['id'], '3')
        with urllib.request.urlopen(self.url + '/api/recordings/' + name) as r:
            self.assertIn(b'"type": "metadata"', r.read())


if __name__ == '__main__':
    unittest.main()
