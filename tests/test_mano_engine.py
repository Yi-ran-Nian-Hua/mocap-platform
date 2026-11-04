"""Numerical invariants: millimetres, kinematic origin, independent skin fitting."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ManoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset = ROOT / 'models/mano/MANO_RIGHT.npz'
        if not cls.asset.exists():
            raise unittest.SkipTest('Licensed MANO model not installed')

    def engine(self):
        self.assertIsNotNone(importlib.util.find_spec('mano_engine'), 'Numerical MANO engine is missing')
        import mano_engine
        return mano_engine

    def test_neutral_mesh_is_native_template_in_millimetres(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        actual = model.forward(np.zeros(10), np.zeros(48))
        with np.load(self.asset, allow_pickle=False) as source:
            np.testing.assert_allclose(actual['vertices'], source['v_template'] * 1000, atol=1e-8)
            joints = source['J_regressor'] @ (source['v_template'] * 1000)
            np.testing.assert_allclose(actual['joints'][[0, 1, 5, 9, 13, 17]], joints[[0, 13, 1, 4, 10, 7]], atol=1e-8)
        self.assertEqual(actual['vertices'].shape, (778, 3))
        self.assertEqual(actual['joints'].shape, (21, 3))
        self.assertGreaterEqual(model.faces.min(), 0)
        self.assertLess(model.faces.max(), 778)
        np.testing.assert_allclose(actual['joints'][[4, 8, 12, 16, 20]], actual['vertices'][[744, 320, 443, 554, 671]])

    def test_root_rotation_scale_and_translation_are_equivariant(self):
        model = self.engine().ManoModel(self.asset)
        beta = np.arange(10) * .04
        pose = np.zeros(48)
        pose[5] = .2
        rest = model.forward(beta, pose)
        root = model.forward(beta, np.zeros(48))['joints'][0]
        pose[:3] = [.3, -.4, .1]
        rotation = Rotation.from_rotvec(pose[:3]).as_matrix()
        moved = model.forward(beta, pose, [310, -95, 12], scale=1.1)
        for key in ('vertices', 'joints'):
            expected = ((rest[key] - root) @ rotation.T + root) * 1.1 + [310, -95, 12]
            np.testing.assert_allclose(moved[key], expected, atol=1e-8)

    def test_left_personal_shape_mirrors_right_personal_shape(self):
        engine = self.engine()
        left = engine.ManoModel(self.asset.with_name('MANO_LEFT.npz'))
        right = engine.ManoModel(self.asset)
        beta = np.arange(10) * .12
        lverts = left.forward(beta, np.zeros(48))['vertices']
        rverts = right.forward(beta, np.zeros(48))['vertices'] * [-1, 1, 1]
        np.testing.assert_allclose(lverts, rverts, atol=.04)

    def test_known_marker_spacing_uses_surface_normal_not_free_fit_offset(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        fitter = engine.HandFitter(model, {'betas': [0] * 10}, 'skeleton')
        mesh = model.forward(np.zeros(10), np.zeros(48))
        # Deliberately displaced observations must not inflate the measured spacing.
        samples = [('open', mesh['joints'], mesh['joints'] + [0, 0, 20])]
        attached = engine._attachments(model, np.zeros(10), 1., [fitter], samples, 2.65, 1.)
        np.testing.assert_allclose(attached['offset_norms_mm'], 3.65, atol=1e-8)
        self.assertGreater(max(attached['calibration_rms_mm']), 1.)

    def test_measured_marker_spacing_does_not_shrink_when_fingers_bend(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        indices = list(range(0, 630, 30))
        fitter = engine.HandFitter(model, dict(betas=[0] * 10, attachments=dict(
            vertex_indices=indices, offsets_mm=[[3.65, 0, 0]] * 21,
            spacing_source='supplied_radius_plus_base_thickness')), 'markers')
        pose = np.zeros(48)
        pose.reshape(16, 3)[1:, 2] = .65
        vertices = model.forward(np.zeros(10), pose)['vertices'][indices]
        np.testing.assert_allclose(np.linalg.norm(fitter._points(pose) - vertices, axis=1), 3.65, atol=1e-8)

    def test_synthetic_pose_recovery_and_nonfinite_rejection(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        pose = np.zeros(48)
        pose[:3] = [.25, -.4, .2]
        pose[5] = .3
        target = model.forward(np.zeros(10), pose, [150, 200, -60])['joints']
        fitter = engine.HandFitter(model, {'betas': [0] * 10, 'scale': 1.0}, 'skeleton')
        for _ in range(4):
            fitted = fitter.fit(target)
        self.assertLess(fitted['residual_rms_mm'], 1.0)
        self.assertEqual(set(fitted['tips']), {'thumb', 'index', 'middle', 'ring', 'pinky'})
        json.dumps(fitted, allow_nan=False)
        target[0, 0] = np.nan
        with self.assertRaises(ValueError):
            fitter.fit(target)

    def test_open_hand_fit_discards_unobservable_large_finger_twist(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        target = model.forward(np.zeros(10), np.zeros(48))['joints']
        fitter = engine.HandFitter(model, {'betas': [0] * 10}, 'skeleton')
        # A terminal joint can twist around the fingertip ray with almost no
        # positional error. Such a warm start must not persist as twisted skin.
        ray = target[8] - target[7]
        fitter.pose[9:12] = ray / np.linalg.norm(ray) * 3.8
        fitter.started = True
        for _ in range(4):
            result = fitter.fit(target)
        self.assertLess(result['residual_rms_mm'], 1.)
        self.assertLess(np.linalg.norm(np.array(result['pose'])[9:12]), .5)

    def test_marker_mode_uses_surface_attachments_and_local_offsets(self):
        engine = self.engine()
        model = engine.ManoModel(self.asset)
        indices = [0, 700, 710, 730, 744, 250, 280, 300, 320, 370, 400, 430, 443,
                   480, 510, 540, 554, 600, 630, 650, 671]
        offsets = np.tile([0., 0., 2.], (21, 1))
        calibration = {'betas': [0] * 10, 'scale': 1., 'attachments': {
            'vertex_indices': indices, 'offsets_mm': offsets.tolist()}}
        # Neutral pose has identity skin transforms, so this target is independently known.
        target = model.forward(np.zeros(10), np.zeros(48))['vertices'][indices] + offsets + [40, 90, 25]
        fitter = engine.HandFitter(model, calibration, 'markers')
        for _ in range(4):
            fitted = fitter.fit(target)
        self.assertLess(fitted['residual_rms_mm'], 1.)
        # Surface attachment centers are not returned as anatomical joint centers.
        self.assertGreater(np.linalg.norm(np.array(fitted['joints']) - target), 20.)

    def test_incomplete_or_ambiguous_mapping_is_rejected(self):
        engine = self.engine()
        for mapping in ([], [{'side': 'left', 'landmark': 'wrist', 'marker_id': 1}]):
            with self.assertRaises(ValueError):
                engine.calibrate_profile({'units': 'mm', 'mapping': mapping, 'samples': []}, self.asset.parent)

    def test_attachment_calibration_selects_low_motion_low_lag_real_frame(self):
        engine = self.engine()
        names = ['LeftHand'] + [f'LeftHand{finger.title()}{i}' for finger in engine.FINGERS for i in range(1, 5)]
        mapping = dict(zip(engine.LANDMARKS, range(21)))
        frames = []
        for frame_id, shift in enumerate([0., 10., 20., 20.01]):
            stamp = frame_id * .1
            frames.append(dict(skeleton_joints=[dict(name=name, pos=[shift, i, 0], frame=frame_id, timestamp=stamp) for i, name in enumerate(names)],
                               markers=[dict(id=i, pos=[shift, i, 2], timestamp=stamp - (.08 if frame_id < 3 else .001)) for i in range(21)]))
        samples, _ = engine._sample_points({'samples': [dict(pose='open', frames=frames), dict(pose='bent', frames=frames)]}, 'left', mapping)
        self.assertEqual(samples[0][1][0, 0], 20.01)


if __name__ == '__main__':
    unittest.main()
