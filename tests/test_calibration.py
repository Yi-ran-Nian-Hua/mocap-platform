import copy
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import Application, handler_for


def profile():
    return dict(name='个人手部', units='mm',
                mapping=[dict(marker_id=7, side='left', landmark='index_1')],
                measurements={'left': {'palm_width': 80, 'marker_radius': 0, 'marker_base_thickness':1}},
                samples=[dict(pose='open', frames=[dict(received_at=100.25,
                    markers=[dict(id=7, pos=[1, 2, 3], timestamp=100, stale=False, age_seconds=.25)],
                    skeleton_joints=[dict(id=301, name='LeftHandIndex1', pos=[4, 5, 6],
                        quat=[0, 0, 0, 1], timestamp=100.1, frame=42, source_time=12.5)])])])


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.disconnect()
        self.tmp.cleanup()

    def request(self, method, path='/api/calibrations', data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            body = json.dumps(data).encode() if data is not None else None
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_save_and_reload_preserves_measurements_and_independent_timestamps(self):
        original = profile()
        code, saved = self.request('POST', data=original)
        self.assertEqual(code, 200)
        self.assertEqual(saved['samples'], original['samples'])
        self.assertEqual(saved['measurements'], original['measurements'])
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(saved['intended_model'], 'MANO')
        self.assertEqual(saved['fitting_status'], 'pending_model_and_fit')
        self.assertIn('not synchronized', saved['timestamp_note'])
        self.assertRegex(saved['id'], r'^[0-9a-f-]{36}$')
        self.assertTrue(saved['created_at'].endswith('+00:00'))
        self.assertEqual(self.request('GET', '/api/calibrations/' + saved['id']), (200, saved))
        code, listing = self.request('GET')
        self.assertEqual(code, 200)
        self.assertEqual(listing['profiles'], [dict(id=saved['id'], name='个人手部',
            created_at=saved['created_at'], sample_count=1, mapping_count=1)])
        files = list(Path(self.tmp.name).rglob('*.json'))
        self.assertEqual(files, [Path(self.tmp.name) / 'calibrations' / (saved['id'] + '.json')])
        fresh_app = Application(Path(self.tmp.name))
        self.assertEqual(fresh_app.calibrations.get(saved['id']), saved)

    def test_rejects_duplicate_markers_and_anatomical_slots(self):
        for extra in [dict(marker_id=7, side='right', landmark='wrist'),
                      dict(marker_id=8, side='left', landmark='index_1')]:
            value = profile()
            value['mapping'].append(extra)
            self.assertEqual(self.request('POST', data=value)[0], 400)

    def test_rejects_bad_schema_nonfinite_numbers_and_spoofed_metadata(self):
        mutations = [
            lambda p: p.update(name=' '), lambda p: p.update(units='m'),
            lambda p: p.update(id='../../outside'), lambda p: p.update(path='outside.json'),
            lambda p: p.update(fitting_status='fitted'), lambda p: p.update(mapping=[]),
            lambda p: p.update(samples=[]), lambda p: p.update(measurements={'left': {'palm_width': 0}}),
            lambda p: p.update(measurements={'right': {'marker_radius': -1}}),
            lambda p: p['mapping'][0].update(marker_id=True),
            lambda p: p['mapping'][0].update(landmark='index_5'),
            lambda p: p['samples'][0].update(pose='unknown'),
            lambda p: p['samples'][0].update(frames=[]),
            lambda p: p['samples'][0]['frames'][0].update(received_at=float('inf')),
            lambda p: p['samples'][0]['frames'][0]['markers'][0].update(pos=[1, float('nan'), 3]),
            lambda p: p['samples'][0]['frames'][0]['markers'][0].update(stale=1),
            lambda p: p['samples'][0]['frames'][0]['skeleton_joints'][0].update(quat=[0, 1]),
            lambda p: p['samples'][0]['frames'][0]['skeleton_joints'][0].update(name=''),
            lambda p: p['samples'][0]['frames'][0]['skeleton_joints'][0].update(frame=-1),
            lambda p: p['samples'][0]['frames'][0]['skeleton_joints'][0].update(source_time=float('nan')),
        ]
        for mutate in mutations:
            value = profile()
            mutate(value)
            with self.subTest(value=value):
                self.assertEqual(self.request('POST', data=value)[0], 400)
        self.assertEqual(list(Path(self.tmp.name).rglob('*.json')), [])

    def test_rejects_unbounded_collections_and_duplicate_frame_points(self):
        value = profile()
        value['samples'] *= 13
        self.assertEqual(self.request('POST', data=value)[0], 400)
        value = profile()
        value['samples'][0]['frames'] *= 61
        self.assertEqual(self.request('POST', data=value)[0], 400)
        for channel in ('markers', 'skeleton_joints'):
            value = profile()
            value['samples'][0]['frames'][0][channel] *= 257
            self.assertEqual(self.request('POST', data=value)[0], 400)
            value['samples'][0]['frames'][0][channel] = value['samples'][0]['frames'][0][channel][:2]
            self.assertEqual(self.request('POST', data=value)[0], 400)

    def test_only_calibration_post_accepts_more_than_8192_bytes(self):
        value = profile()
        value['samples'][0]['frames'] = [copy.deepcopy(value['samples'][0]['frames'][0]) for _ in range(40)]
        self.assertGreater(len(json.dumps(value)), 8192)
        self.assertEqual(self.request('POST', data=value)[0], 200)
        self.assertEqual(self.request('POST', '/api/record/stop', data=value)[0], 400)
        self.assertEqual(self.request('POST', data={}, headers={'Content-Length': str(8 * 1024 * 1024 + 1)})[0], 400)

    def test_rejects_path_escape_and_cross_origin(self):
        for name in ('../outside', '%2e%2e%2foutside', '1234', '00000000-0000-0000-0000-000000000000'):
            self.assertEqual(self.request('GET', '/api/calibrations/' + name)[0], 400)
        self.assertEqual(self.request('POST', data=profile(), headers={'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.request('GET', headers={'Host': 'example.com'})[0], 403)


if __name__ == '__main__':
    unittest.main()
