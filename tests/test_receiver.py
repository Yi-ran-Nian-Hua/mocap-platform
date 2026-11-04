import ctypes
import importlib.util
import pathlib
import threading
import unittest
from unittest.mock import patch


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = pathlib.Path(__file__).resolve().parents[1] / 'receiver.py'
        if not path.exists():
            cls.receiver = None
            return
        spec = importlib.util.spec_from_file_location('receiver', path)
        cls.receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.receiver)

    def require_receiver(self):
        self.assertIsNotNone(self.receiver, 'SDK receiver is not implemented')
        return self.receiver

    def test_windows_abi_layout(self):
        r = self.require_receiver()
        self.assertEqual(ctypes.sizeof(r.Timeval), 8)
        self.assertEqual(ctypes.sizeof(r.Tracker), 72)
        self.assertEqual(r.Tracker.pos.offset, 16)
        self.assertEqual(ctypes.sizeof(r.Hierarchy), 144)

    def test_skeleton_keeps_local_rotations_and_detection_flags(self):
        r = self.require_receiver()
        pos = [0.] * 450
        rot = [0.] * 600
        detected = [0] * 150
        pos[6:9] = [1., 2., 3.]
        rot[8:12] = [.1, .2, .3, .4]
        detected[2] = 1
        event = r.skeleton_event(4, 'source', -1, pos, rot, detected)
        self.assertEqual(event['position_space'], 'global')
        self.assertEqual(event['rotation_space'], 'local')
        self.assertEqual(event['frame'], 0xffffffff)
        self.assertEqual(event['joints'][2], {'id': 2, 'pos': [1., 2., 3.], 'quat': [.1, .2, .3, .4], 'detected': True})
        self.assertFalse(event['joints'][0]['detected'])
        self.assertEqual(r.skeleton_event(4, 'retarget', 1, pos, rot, detected)['position_space'], 'unknown')

    def test_dedup_accepts_changed_values_with_reused_timecode(self):
        r = self.require_receiver()
        dedup = r.ChangedEvents()
        self.assertTrue(dedup.changed(('skeleton', 1), {'frame': 0, 'pos': [1, 2, 3]}))
        self.assertFalse(dedup.changed(('skeleton', 1), {'frame': 0, 'pos': [1, 2, 3]}))
        self.assertTrue(dedup.changed(('skeleton', 1), {'frame': 0, 'pos': [1, 2, 4]}))
        self.assertTrue(dedup.changed(('skeleton', 1), {'frame': 1, 'pos': [1, 2, 4]}))

    def test_default_endpoint_matches_server(self):
        r = self.require_receiver()
        self.assertEqual(r.endpoint({'host': '192.168.1.200'}), b'MCAvatar@192.168.1.200')
        self.assertEqual(r.endpoint({'host': '192.168.1.200', 'port': 3884}), b'MCAvatar@192.168.1.200:3884')

    def test_load_failure_is_reported(self):
        r = self.require_receiver()
        events = []
        with patch.object(r.ctypes, 'CDLL', side_effect=OSError('missing test DLL')):
            r.run_receiver({'host': '192.168.1.200'}, events.append, threading.Event())
        self.assertEqual(events[-1]['type'], 'status')
        self.assertEqual(events[-1]['level'], 'error')
        self.assertIn('missing test DLL', events[-1]['message'])

    def test_native_callbacks_live_until_worker_quits_on_poll_error(self):
        r = self.require_receiver()
        events, calls, registered = [], [], {}

        class Function:
            def __init__(self, name):
                self.name = name

            def __call__(self, *args):
                calls.append(self.name)
                if self.name.startswith('CMPluginRegister'):
                    registered[self.name.removeprefix('CMPluginRegister')] = args[2]
                    if self.name == 'CMPluginRegisterTrackerData':
                        bad = r.Tracker()
                        bad.pos[0] = float('nan')
                        args[2](None, bad)
                        bad.pos[0] = float('inf')
                        args[2](None, bad)
                        good = r.Tracker()
                        good.pos[0] = 1.5
                        args[2](None, good)
                elif self.name.startswith('CMPluginUnRegister'):
                    key = self.name.removeprefix('CMPluginUnRegister')
                    self_outer.assertIs(registered[key], args[2])
                elif self.name == 'CMMarkerExtern':
                    raise RuntimeError('test polling failure')
                return 0

        self_outer = self
        names = ['CMVrpnStartExtern', 'CMVrpnQuitExtern', 'CMVrpnEnableLog',
                 'CMPluginConnectServer', 'CMHumanGlobalTLocalRTC',
                 'CMRetargetHumanExternTC', 'CMMarkerExtern']
        for callback in ['TrackerData', 'UpdateHierarchy', 'ResetHierarchy', 'EndHierarchy']:
            names += ['CMPluginRegister' + callback, 'CMPluginUnRegister' + callback]
        fake = type('FakeDll', (), {})()
        for name in names:
            setattr(fake, name, Function(name))
        with patch.object(r.ctypes, 'CDLL', return_value=fake):
            r.run_receiver({'host': '192.168.1.200', 'human_ids': []}, events.append, threading.Event())
        self.assertEqual(calls[-1], 'CMVrpnQuitExtern')
        self.assertEqual(len([x for x in calls if 'UnRegister' in x]), 4)
        bodies = [event for event in events if event['type'] == 'tracker']
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['pos'][0], 1.5)
        warnings = [event for event in events if event.get('level') == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(events[-1]['level'], 'error')
        self.assertIn('test polling failure', events[-1]['message'])


if __name__ == '__main__':
    unittest.main()
