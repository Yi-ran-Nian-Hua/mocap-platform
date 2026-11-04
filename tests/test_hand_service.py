import copy
import http.client
import json
import multiprocessing as mp
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from hand_service import HandService, extract_points, skeleton_bindings, LANDMARKS, _PoseWorker
from calibration import CalibrationStore
from server import Application, handler_for


def fixture():
    now = time.time()
    mapping = [dict(side='left', landmark=name, marker_id=i) for i, name in enumerate(LANDMARKS)]
    hierarchy, joints, markers = [], [], []
    for i, name in enumerate(LANDMARKS):
        suffix = '' if name == 'wrist' else name.split('_')[0].title() + name[-1]
        hierarchy.append(dict(id=300+i, mode='source', name='Subject_LeftHand'+suffix))
        joints.append(dict(id=300+i, pos=[i, 2, 3], timestamp=now, stale=False))
        markers.append(dict(id=i, pos=[i, 4, 5], timestamp=now, stale=False))
    return mapping, dict(running=True, connected=True, hierarchy=hierarchy,
                         skeleton_joints=joints, markers=markers)


class HandInputTests(unittest.TestCase):
    def test_sources_are_independent_and_canonical(self):
        mapping, state = fixture()
        points, timing = extract_points(state, mapping, 'left', 'skeleton')
        self.assertEqual(points[0], [0, 2, 3])
        self.assertEqual(len(points), 21)
        state['skeleton_joints'] = []
        points, timing = extract_points(state, mapping, 'left', 'markers')
        self.assertEqual(points[-1], [20, 4, 5])
        with self.assertRaises(ValueError): extract_points(state, mapping, 'left', 'skeleton')

    def test_missing_stale_ambiguous_and_nonfinite_rejected(self):
        mapping, original = fixture()
        for change in ('missing', 'stale', 'nonfinite', 'ambiguous'):
            state = copy.deepcopy(original)
            if change == 'missing': state['skeleton_joints'].pop()
            elif change == 'stale': state['skeleton_joints'][0]['timestamp'] -= 3
            elif change == 'nonfinite': state['skeleton_joints'][0]['pos'][0] = float('nan')
            else: state['hierarchy'].append(dict(id=800, mode='source', name='Other_LeftHand'))
            with self.subTest(change=change), self.assertRaises(ValueError):
                extract_points(state, mapping, 'left', 'skeleton')
        state = copy.deepcopy(original)
        state['markers'][0]['timestamp'] -= 3
        with self.assertRaises(ValueError): extract_points(state, mapping, 'left', 'markers')

    def test_disconnect_rejects_even_recent_cached_coordinates(self):
        mapping, state = fixture()
        state['connected'] = False
        with self.assertRaises(ValueError): extract_points(state, mapping, 'left', 'markers')

    def test_saved_skeleton_identity_excludes_other_avatars(self):
        mapping, state=fixture()
        source=copy.deepcopy(state['skeleton_joints'])
        for point,node in zip(source,state['hierarchy']): point['name']=node['name']
        binding=skeleton_bindings({'samples':[{'frames':[{'skeleton_joints':source}]}]})
        state['hierarchy'].append(dict(id=900,mode='source',name='Other_LeftHand'))
        state['skeleton_joints'].append(dict(id=900,pos=[900,0,0],timestamp=time.time()))
        points,_=extract_points(state,mapping,'left','skeleton',bindings=binding)
        self.assertEqual(points[0],[0,2,3])
        state['hierarchy'][0]['name']='Reassigned_LeftHand'
        with self.assertRaises(ValueError): extract_points(state,mapping,'left','skeleton',bindings=binding)

    def test_service_initial_state_and_aging_dont_claim_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = HandService(lambda: {}, None, Path(tmp), Path(tmp))
            self.assertEqual(service.status()['frames'], [])
            with self.assertRaises(ValueError): service.start()
            service.state['frames'] = [dict(side='left', mode='markers', valid=True,
                source_timestamp=time.time()-3, warning='', vertices=[[0,0,0]])]
            frame = service.status()['frames'][0]
            self.assertFalse(frame['valid'])
            self.assertIn('过期', frame['warning'])
            service.close()

    def test_fatal_state_and_refitting_do_not_export_valid_or_mixed_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=HandService(lambda: {},None,Path(tmp),Path(tmp))
            service.state.update(phase='error',running=False,profile_id='A',frames=[dict(valid=True,source_timestamp=time.time())])
            self.assertFalse(service.status()['frames'][0]['valid'])
            service.prepared={'profile':{'id':'A'},'fit':{'sides':{'left':{}}}}
            service.state.update(phase='calibrating',profile_id='B')
            with self.assertRaises(ValueError): service.export()
            service.close()

    def test_cancel_and_close_terminate_a_stuck_stream_worker(self):
        context=mp.get_context('spawn')
        worker=_PoseWorker.__new__(_PoseWorker)
        worker.connection, child=context.Pipe()
        worker.process=context.Process(target=time.sleep,args=(20,),daemon=True)
        worker.process.start()
        stop=threading.Event(); stop.set()
        started=time.monotonic()
        try:
            with self.assertRaises(InterruptedError): worker.result(stop)
            worker.close()
            self.assertFalse(worker.process.is_alive())
            self.assertLess(time.monotonic()-started,1.5)
        finally:
            child.close()
            if worker.process.is_alive(): worker.process.terminate(); worker.process.join()

    def test_async_fit_export_stop_and_reload_preserve_provenance(self):
        mapping, state = fixture()
        class Faces(list):
            def tolist(self): return list(self)
        class Model:
            def __init__(self, path): self.faces = Faces([[0,1,2]])
        class Fitter:
            def __init__(self, model, calibration, mode): pass
            def fit(self, points):
                return dict(vertices=points, joints=points, tips={'thumb':points[4]}, residual_rms_mm=0, fit_ms=.1)
        def calibrate(profile, path, progress):
            progress('calibrating')
            return dict(sides={'left':{'betas':[0]*10}}, quality={'reference_validated':False})
        def wait_until(condition):
            end=time.monotonic()+3
            while not condition() and time.monotonic()<end: time.sleep(.01)
            self.assertTrue(condition())
        with tempfile.TemporaryDirectory() as tmp:
            store=CalibrationStore(Path(tmp)/'calibrations')
            source=copy.deepcopy(state['skeleton_joints'])
            for p,n in zip(source,state['hierarchy']): p['name']=n['name']
            profile=store.save(dict(name='test',units='mm',measurements={},mapping=mapping,
                samples=[dict(pose='open',frames=[dict(received_at=time.time(),markers=state['markers'],skeleton_joints=source)])]))
            service=HandService(lambda: state,store,Path(tmp)/'models',Path(tmp)/'fits',parallel=False)
            service.models_dir.mkdir()
            (service.models_dir/'MANO_LEFT.npz').write_bytes(b'fake model for service orchestration test')
            with patch.object(service,'_requirements'), patch.object(service,'_engine',return_value=(Model,Fitter,calibrate)):
                try:
                    service.calibrate(profile['id'])
                    wait_until(lambda: len(service.status()['frames'])==2)
                    payload=service.export()
                    self.assertEqual(payload['calibration']['profile']['mapping'],mapping)
                    self.assertTrue(payload['estimate_only'])
                    self.assertEqual(payload['live']['frames'][0]['input_timestamps'],[state['skeleton_joints'][0]['timestamp']]*21)
                    state['connected']=False
                    wait_until(lambda: all(not f['valid'] for f in service.status()['frames']))
                finally: service.close()
            self.assertFalse(service.worker.is_alive())
            reloaded=HandService(lambda: state,store,Path(tmp),Path(tmp)/'fits')
            self.assertEqual(reloaded.status()['profile_id'],profile['id'])
            self.assertFalse(reloaded.status()['running'])


class HandHTTPTests(unittest.TestCase):
    def test_local_status_start_validation_and_host_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Application(Path(tmp))
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(app))
            worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            def request(method,path,body=None,headers=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    connection.request(method,path,json.dumps(body) if body is not None else None,headers or {})
                    response=connection.getresponse()
                    return response.status,json.loads(response.read())
                finally: connection.close()
            try:
                code,data=request('GET','/api/hands')
                self.assertEqual(code,200); self.assertEqual(data['frames'],[])
                self.assertEqual(request('POST','/api/hands/start',{})[0],400)
                self.assertEqual(request('POST','/api/hands/calibrate',{'profile_id':'../invalid'})[0],400)
                self.assertEqual(request('POST','/api/hands/stop',{'unexpected':1})[0],400)
                self.assertEqual(request('POST','/api/hands/stop',{})[0],200)
                self.assertEqual(request('GET','/api/hands',headers={'Origin':'https://example.org'})[0],403)
                self.assertEqual(request('GET','/api/hands/export')[0],400)
            finally:
                app.hands.close(); server.shutdown(); server.server_close(); worker.join()
