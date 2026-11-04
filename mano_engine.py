"""MANO linear blend skinning and regularised marker/joint fitting, in mm.

Pose contains 16 absolute axis-angle triplets (zero is flat, no implicit mean).
The LBS equations and tip indices follow the official smplx implementation:
https://github.com/vchoutas/smplx/blob/main/smplx/lbs.py
https://github.com/vchoutas/smplx/blob/main/smplx/vertex_ids.py
No licensed model data is embedded here. Input SDK joints, fitted attachments,
and model surface tips are different estimates; residual is not true accuracy.
"""
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

FINGERS = ('thumb', 'index', 'middle', 'ring', 'pinky')
LANDMARKS = ('wrist',) + tuple(f'{finger}_{i}' for finger in FINGERS for i in range(1, 5))
TIPS = np.array([744, 320, 443, 554, 671])
JOINT_ORDER = np.array([0, 13, 14, 15, 16, 1, 2, 3, 17, 4, 5, 6, 18, 10, 11, 12, 19, 7, 8, 9, 20])
SOLVER = dict(max_nfev=5, initial_max_nfev=16, ftol=1e-3, xtol=1e-3, gtol=1e-3,
              pca_components=15, pca_prior_mm=.8, temporal_prior_mm=.15)


def _array(value, shape, name):
    result = np.asarray(value, dtype=float)
    if result.shape != shape or not np.isfinite(result).all():
        raise ValueError(f'{name} must be finite with shape {shape}')
    return result


class ManoModel:
    def __init__(self, npz_path):
        with np.load(npz_path, allow_pickle=False) as data:
            self.template = _array(data['v_template'], (778, 3), 'v_template')
            self.shapedirs = _array(data['shapedirs'], (778, 3, 10), 'shapedirs')
            self.posedirs = _array(data['posedirs'], (778, 3, 135), 'posedirs')
            self.regressor = _array(data['J_regressor'], (16, 778), 'J_regressor')
            self.weights = _array(data['weights'], (778, 16), 'weights')
            self.faces = np.asarray(data['f'], dtype=np.int32)
            self.parents = np.asarray(data['kintree_table'][0], dtype=int)
            self.hand_components = _array(data['hands_components'], (45, 45), 'hands_components')
            self.hand_mean = _array(data['hands_mean'], (45,), 'hands_mean')
        self.left_shapedirs_corrected = False
        counterpart = Path(npz_path).with_name('MANO_RIGHT.npz')
        if Path(npz_path).name.upper() == 'MANO_LEFT.NPZ' and counterpart.exists():
            with np.load(counterpart, allow_pickle=False) as right:
                # Some official distributions copied the right shape basis to the
                # mirrored left template. Correct only when that defect is observed.
                left_x, right_x = self.shapedirs[:, 0], right['shapedirs'][:, 0]
                if np.linalg.norm(left_x - right_x) < .1 * np.linalg.norm(left_x + right_x):
                    self.shapedirs[:, 0] *= -1
                    self.left_shapedirs_corrected = True
        if self.faces.ndim != 2 or self.faces.shape[1] != 3 or self.faces.min() < 0 or self.faces.max() >= 778:
            raise ValueError('Invalid MANO mesh topology')
        if not np.allclose(self.weights.sum(axis=1), 1., atol=1e-5):
            raise ValueError('Invalid MANO skinning weights')
        if self.parents.shape != (16,) or any(not 0 <= self.parents[i] < i for i in range(1, 16)):
            raise ValueError('Invalid MANO joint hierarchy')
        extent = float(np.ptp(self.template, axis=0).max())
        if not .1 < extent < .4:
            raise ValueError('Expected official MANO template in metres')
        self.native_to_mm = 1000.
        self.template *= 1000
        self.shapedirs *= 1000
        self.posedirs *= 1000

    def prepare(self, betas, scale=1., indices=None):
        betas = _array(betas, (10,), 'betas')
        if not np.isfinite(scale) or not .2 < scale < 3.:
            raise ValueError('Invalid model scale')
        return _Prepared(self, betas, scale, indices)

    def forward(self, betas, pose, translation=None, scale=1.):
        pose = _array(pose, (48,), 'pose')
        translation = np.zeros(3) if translation is None else _array(translation, (3,), 'translation')
        vertices, joints, _ = self.prepare(betas, scale).evaluate(pose)
        return dict(vertices=vertices + translation, joints=joints + translation)


class _Prepared:
    """Shape cached once; optimiser only skins requested attachment/tip vertices."""
    def __init__(self, model, betas, scale, indices):
        self.model = model
        self.indices = np.arange(778) if indices is None else np.unique(np.r_[indices, TIPS])
        shaped = (model.template + np.einsum('vck,k->vc', model.shapedirs, betas)) * scale
        self.joints = model.regressor @ shaped
        self.shaped = shaped[self.indices]
        self.posedirs = model.posedirs[self.indices].reshape(-1, 135) * scale
        self.weights = model.weights[self.indices]
        self.tip_slots = np.searchsorted(self.indices, TIPS)

    def evaluate(self, pose):
        rotations = Rotation.from_rotvec(np.asarray(pose).reshape(16, 3)).as_matrix()
        global_r = np.empty((16, 3, 3))
        global_j = np.empty((16, 3))
        global_r[0], global_j[0] = rotations[0], self.joints[0]
        for i in range(1, 16):
            p = self.model.parents[i]
            global_r[i] = global_r[p] @ rotations[i]
            global_j[i] = global_j[p] + global_r[p] @ (self.joints[i] - self.joints[p])
        shifts = global_j - np.einsum('vij,vj->vi', global_r, self.joints)
        skin_r = (self.weights @ global_r.reshape(16, 9)).reshape(-1, 3, 3)
        posed = self.shaped + (self.posedirs @ (rotations[1:] - np.eye(3)).ravel()).reshape(-1, 3)
        vertices = np.einsum('vij,vj->vi', skin_r, posed) + self.weights @ shifts
        canonical = np.vstack([global_j, vertices[self.tip_slots]])[JOINT_ORDER]
        return vertices, canonical, skin_r


def _align(source, target):
    """Proper rotation only: reflecting left/right coordinates is never permitted."""
    u, _, vt = np.linalg.svd((source - source.mean(0)).T @ (target - target.mean(0)))
    correction = np.eye(3)
    correction[2, 2] = np.linalg.det(vt.T @ u.T)
    return vt.T @ correction @ u.T


def _skin_offsets(rotations, offsets, preserve_length=False):
    result = np.einsum('...ij,...j->...i', rotations, offsets)
    if preserve_length:
        result *= np.linalg.norm(offsets, axis=-1, keepdims=True) / np.maximum(np.linalg.norm(result, axis=-1, keepdims=True), 1e-12)
    return result


class HandFitter:
    def __init__(self, model, side_calibration, mode):
        if mode not in ('skeleton', 'markers'):
            raise ValueError('mode must be skeleton or markers')
        self.model, self.mode = model, mode
        self.betas = _array(side_calibration['betas'], (10,), 'betas')
        self.scale = float(side_calibration.get('scale', 1.))
        self.full = model.prepare(self.betas, self.scale)
        self.settings = dict(SOLVER)
        self.settings.update(side_calibration.get('solver', {}))
        count = int(self.settings['pca_components'])
        if not 1 <= count <= 20:
            raise ValueError('Live pose fitting requires 1 to 20 MANO PCA components')
        # The shipped components already contain training standard deviations.
        # A bounded mean blend retains the exact flat pose without adding arbitrary
        # individual-joint twists. All runtime inputs use this same pose family.
        self.pose_basis = np.column_stack([model.hand_components[:count].T, model.hand_mean])
        self.pose_inverse = np.linalg.pinv(self.pose_basis)
        self.pca_count = count
        if mode == 'markers':
            attachment = side_calibration['attachments']
            raw = np.asarray(attachment['vertex_indices'])
            if raw.shape != (21,) or not np.issubdtype(raw.dtype, np.integer) or np.any(raw < 0) or np.any(raw >= 778):
                raise ValueError('Invalid skin attachment indices')
            self.offsets = _array(attachment['offsets_mm'], (21, 3), 'offsets_mm')
            self.fixed_spacing = attachment.get('spacing_source') == 'supplied_radius_plus_base_thickness'
            self.prepared = model.prepare(self.betas, self.scale, raw)
            self.slots = np.searchsorted(self.prepared.indices, raw)
        else:
            self.prepared = model.prepare(self.betas, self.scale, TIPS)
        self.pose = np.zeros(48)
        self.translation = np.zeros(3)
        self.started = False

    def _points(self, pose):
        vertices, joints, rotations = self.prepared.evaluate(pose)
        if self.mode == 'skeleton':
            return joints
        return vertices[self.slots] + _skin_offsets(rotations[self.slots], self.offsets, self.fixed_spacing)

    def _solve(self, points, budget=None):
        points = _array(points, (21, 3), 'points')
        centered = points - points.mean(0)
        local = self.pose_inverse @ self.pose[3:]
        local[:-1] = np.clip(local[:-1], -3., 3.)
        local[-1] = np.clip(local[-1], 0., 1.)
        initial = np.r_[self.pose[:3], local]

        def expand(parameters):
            return np.r_[parameters[:3], self.pose_basis @ parameters[3:]]

        # Re-estimate rigid movement each frame, including large changes after a pause.
        current = self._points(expand(initial))
        anchors = [0, 1, 5, 9, 13, 17] if self.mode == 'skeleton' else list(range(21))
        rotation = _align(current[anchors], points[anchors])
        initial[:3] = (Rotation.from_matrix(rotation) * Rotation.from_rotvec(initial[:3])).as_rotvec()
        prior = initial[3:].copy()

        def residual(parameters):
            predicted = self._points(expand(parameters))
            parts = [(predicted - predicted.mean(0) - centered).ravel(), self.settings['pca_prior_mm'] * parameters[3:-1]]
            if self.started:
                parts.append(self.settings['temporal_prior_mm'] * (parameters[3:] - prior))
            return np.concatenate(parts)

        bounds = (np.r_[np.full(3, -np.pi * 2), np.full(self.pca_count, -3.), 0.],
                  np.r_[np.full(3, np.pi * 2), np.full(self.pca_count, 3.), 1.])
        result = least_squares(residual, initial, bounds=bounds,
                               max_nfev=budget or self.settings['max_nfev' if self.started else 'initial_max_nfev'],
                               ftol=self.settings['ftol'], xtol=self.settings['xtol'], gtol=self.settings['gtol'])
        self.pose = expand(result.x)
        predicted = self._points(self.pose)
        self.translation = (points - predicted).mean(0)
        self.started = True
        error = predicted + self.translation - points
        return float(np.sqrt(np.mean(np.sum(error * error, axis=1)))), int(result.nfev)

    def fit(self, points):
        started = time.perf_counter()
        rms, evaluations = self._solve(points)
        vertices, joints, _ = self.full.evaluate(self.pose)
        vertices += self.translation
        joints += self.translation
        return dict(vertices=vertices.tolist(), joints=joints.tolist(),
                    tips={finger: vertices[index].tolist() for finger, index in zip(FINGERS, TIPS)},
                    residual_rms_mm=rms, fit_ms=(time.perf_counter() - started) * 1000,
                    pose=self.pose.tolist(), translation=self.translation.tolist(), solver_evaluations=evaluations,
                    pose_model=f'{self.pca_count} MANO PCA coefficients plus bounded flat/mean blend')


def _sample_points(profile, side, mapping):
    representatives, counts = [], {}
    prefix = 'Left' if side == 'left' else 'Right'
    names = [f'{prefix}Hand'] + [f'{prefix}Hand{finger.title()}{i}' for finger in FINGERS for i in range(1, 5)]
    for sample in profile.get('samples', []):
        complete = []
        timing = []
        for frame_index, frame in enumerate(sample.get('frames', [])):
            skeleton, markers = {}, {}
            duplicate = False
            for point in frame.get('skeleton_joints', []):
                match = re.search(r'(Left|Right)Hand(?:Thumb|Index|Middle|Ring|Pinky)?[1-4]?$', point.get('name', ''))
                if match and not point.get('stale'):
                    name = match.group(0)
                    duplicate |= name in skeleton
                    skeleton[name] = point['pos']
            for point in frame.get('markers', []):
                if not point.get('stale'):
                    duplicate |= point['id'] in markers
                    markers[point['id']] = point['pos']
            if duplicate:
                raise ValueError('Ambiguous duplicate calibration points')
            if all(name in skeleton for name in names) and all(mapping[name] in markers for name in LANDMARKS):
                hand_points = [point for point in frame['skeleton_joints'] if any(point.get('name', '').endswith(name) for name in names)]
                frame_ids = {point['frame'] for point in hand_points if 'frame' in point}
                if len(frame_ids) > 1:
                    continue
                joints = _array([skeleton[name] for name in names], (21, 3), 'calibration joints')
                surface = _array([markers[mapping[name]] for name in LANDMARKS], (21, 3), 'calibration markers')
                complete.append((joints, surface))
                joint_times = [point['timestamp'] for point in hand_points if 'timestamp' in point]
                marker_times = [point['timestamp'] for point in frame['markers'] if point['id'] in mapping.values() and 'timestamp' in point]
                stamp = float(np.mean(joint_times)) if joint_times else float(frame.get('received_at', frame_index))
                lag = abs(stamp - float(np.mean(marker_times))) if marker_times else 0.
                timing.append((stamp, lag, frame_index))
        if complete:
            # Prefer low motion and low local timestamp lag, without pretending the
            # two capture clocks are hardware synchronized or correcting world points.
            stack = np.array([pair[0] for pair in complete])
            speed = np.zeros(len(complete))
            for i in range(len(complete) - 1):
                elapsed = abs(timing[i + 1][0] - timing[i][0])
                if elapsed > 1e-6:
                    value = float(np.percentile(np.linalg.norm(stack[i + 1] - stack[i], axis=1), 95) / elapsed)
                    speed[i:i + 2] = np.maximum(speed[i:i + 2], value)
            risk = speed * np.array([item[1] for item in timing])
            median_distance = np.sqrt(np.mean((stack - np.median(stack, axis=0)) ** 2, axis=(1, 2)))
            selected = int(np.argmin(risk + median_distance * .0001))
            metadata = dict(frame_index=timing[selected][2], timestamp_lag_seconds=timing[selected][1],
                            estimated_motion_during_lag_mm=float(risk[selected]))
            representatives.append((sample.get('pose', 'unknown'), *complete[selected], metadata))
            counts[sample.get('pose', 'unknown')] = len(complete)
    if len(representatives) < 2:
        raise ValueError(f'{side}: at least two complete calibration poses with skeleton and markers required')
    return representatives, counts


def _dimensions(vertices, joints, model):
    width_axis = joints[5] - joints[17]
    width_axis /= np.linalg.norm(width_axis)
    length_axis = joints[9] - joints[0]
    normal = np.cross(width_axis, length_axis)
    normal /= np.linalg.norm(normal)
    palm = vertices[model.weights[:, 0] > .5]
    return dict(hand_length=float(np.linalg.norm(joints[12] - joints[0])),
                palm_width=float(np.ptp(palm @ width_axis)),
                palm_thickness=float(np.ptp(palm @ normal)))


def _attachments(model, betas, scale, fits, samples, marker_radius, base_thickness=None):
    prepared = model.prepare(betas, scale)
    meshes, transforms = [], []
    for fit in fits:
        vertices, _, skin = prepared.evaluate(fit.pose)
        meshes.append(vertices + fit.translation)
        transforms.append(skin)
    meshes, transforms = np.array(meshes), np.array(transforms)
    targets = np.array([sample[2] for sample in samples])
    normals = np.zeros((778, 3))
    faces = model.faces
    triangle = prepared.shaped[faces]
    face_normals = np.cross(triangle[:, 1] - triangle[:, 0], triangle[:, 2] - triangle[:, 0])
    for corner in range(3):
        np.add.at(normals, faces[:, corner], face_normals)
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    indices, offsets, errors = [], [], []
    for landmark in range(21):
        delta = targets[:, landmark, None, :] - meshes
        # Choose a physical skin vertex close in every pose, then fit one local offset.
        spacing = marker_radius + base_thickness if base_thickness is not None else None
        known_offset = normals * spacing if spacing is not None else None
        shifted = delta if known_offset is None else delta - _skin_offsets(transforms, known_offset, True)
        score = np.mean(np.sum(shifted * shifted, axis=2), axis=0)
        candidates = np.argsort(score)[:32]
        best = None
        for vertex in candidates:
            a = transforms[:, vertex].reshape(-1, 3)
            b = delta[:, vertex].reshape(-1)
            offset = known_offset[vertex].copy() if known_offset is not None else np.linalg.lstsq(
                np.vstack([a, np.eye(3) * .7]), np.r_[b, np.zeros(3)], rcond=None)[0]
            # Offset includes marker size, placement, model mismatch and clock mismatch.
            # It is neither a measurement of skin thickness nor a free world-space shift.
            maximum = 12. + marker_radius
            norm = np.linalg.norm(offset)
            if norm > maximum:
                offset *= maximum / norm
            error = (a @ offset - b) if known_offset is None else (_skin_offsets(transforms[:, vertex], offset, True).ravel() - b)
            objective = np.sum(error * error) + 2. * np.dot(offset, offset)
            if best is None or objective < best[0]:
                best = objective, vertex, offset, np.sqrt(np.mean(error.reshape(-1, 3) ** 2) * 3)
        _, vertex, offset, error = best
        indices.append(int(vertex))
        offsets.append(offset.tolist())
        errors.append(float(error))
    return dict(kind='estimated_skin_vertex_plus_local_offset', vertex_indices=indices, offsets_mm=offsets,
                offset_norms_mm=np.linalg.norm(offsets, axis=1).tolist(), calibration_rms_mm=errors,
                marker_radius_mm=marker_radius,
                marker_base_thickness_mm=base_thickness,
                spacing_source='supplied_radius_plus_base_thickness' if base_thickness is not None else 'estimated_offset',
                offset_convention=('Supplied spacing along normalized LBS-transformed rest vertex normal (approximate deformed normal); attachment location estimated.'
                    if base_thickness is not None else 'Participant-scaled rest-space mm; transformed by the vertex LBS rotation blend. Includes placement/model/clock mismatch; not measured thickness.'))


def calibrate_profile(profile, models_dir, progress=None):
    if profile.get('units') != 'mm':
        raise ValueError('Calibration requires millimetres')
    mappings, seen_ids = {}, set()
    for item in profile.get('mapping', []):
        side, name, marker = item.get('side'), item.get('landmark'), item.get('marker_id')
        if side not in ('left', 'right') or name not in LANDMARKS or marker in seen_ids or name in mappings.get(side, {}):
            raise ValueError('Incomplete or ambiguous hand mapping')
        mappings.setdefault(side, {})[name] = marker
        seen_ids.add(marker)
    if not mappings or any(set(mapping) != set(LANDMARKS) for mapping in mappings.values()):
        raise ValueError('Each mapped hand needs all 21 unique landmarks')
    started = time.perf_counter()
    sides = {}
    for side, mapping in mappings.items():
        if progress:
            progress(f'{side}: fitting participant shape from calibration poses')
        model = ManoModel(Path(models_dir) / f'MANO_{side.upper()}.npz')
        samples, counts = _sample_points(profile, side, mapping)
        measurements = profile.get('measurements', {}).get(side, {})
        for name, value in measurements.items():
            if name not in ('palm_width', 'palm_thickness', 'hand_length', 'marker_radius', 'marker_base_thickness') or not np.isfinite(value) or value < 0 or (name not in ('marker_radius', 'marker_base_thickness') and value == 0):
                raise ValueError('Invalid optional hand dimensions')
        shape = np.r_[np.zeros(10), 1.]
        fits = [HandFitter(model, dict(betas=shape[:10], scale=shape[10]), 'skeleton') for _ in samples]
        # First constrain shape with pose-invariant internal bone lengths and palm spans.
        edges = [(0, first) for first in (1, 5, 9, 13, 17)]
        edges += [(first, first + 1) for first in (1, 2, 5, 6, 9, 10, 13, 14, 17, 18)]
        edges += [(5, 9), (9, 13), (13, 17), (5, 17)]
        a, b = np.array(edges).T
        lengths = np.median([np.linalg.norm(s[1][a] - s[1][b], axis=1) for s in samples], axis=0)

        def shape_prior(x):
            return np.r_[x[:10] * 2., (x[10] - 1.) * 25.]

        def dimensions_residual(x):
            if not any(key in ('palm_width', 'palm_thickness', 'hand_length') for key in measurements):
                return []
            output = model.forward(x[:10], np.zeros(48), scale=x[10])
            dims = _dimensions(output['vertices'], output['joints'], model)
            return [(dims[key] - value) * 2 for key, value in measurements.items() if key in dims]

        def lengths_residual(x):
            _, joints, _ = model.prepare(x[:10], x[10], TIPS).evaluate(np.zeros(48))
            return np.r_[np.linalg.norm(joints[a] - joints[b], axis=1) - lengths, shape_prior(x), dimensions_residual(x)]

        bounds = (np.r_[np.full(10, -3.), .65], np.r_[np.full(10, 3.), 1.45])
        shape = least_squares(lengths_residual, shape, bounds=bounds, max_nfev=30).x
        for iteration in range(3):
            for i, sample in enumerate(samples):
                previous = fits[i]
                fits[i] = HandFitter(model, dict(betas=shape[:10], scale=shape[10]), 'skeleton')
                fits[i].pose, fits[i].started = previous.pose, previous.started
                fits[i]._solve(sample[1], budget=30 if iteration == 0 else 15)

            def shape_residual(x):
                prepared = model.prepare(x[:10], x[10], TIPS)
                errors = []
                for fit, sample in zip(fits, samples):
                    _, joints, _ = prepared.evaluate(fit.pose)
                    delta = joints - sample[1]
                    errors.extend(((delta - delta.mean(0)) / np.sqrt(len(samples))).ravel())
                return np.r_[errors, shape_prior(x), dimensions_residual(x)]

            shape = least_squares(shape_residual, shape, bounds=bounds, max_nfev=20).x
        diagnostics = []
        for i, sample in enumerate(samples):
            previous = fits[i]
            fits[i] = HandFitter(model, dict(betas=shape[:10], scale=shape[10]), 'skeleton')
            fits[i].pose, fits[i].started = previous.pose, True
            rms, _ = fits[i]._solve(sample[1], budget=20)
            diagnostics.append(dict(pose_name=sample[0], skeleton_rms_mm=rms, pose=fits[i].pose.tolist(), translation=fits[i].translation.tolist(), sample_selection=sample[3]))
        if progress:
            progress(f'{side}: estimating skin attachments independently of skeleton joint centres')
        attachments = _attachments(model, shape[:10], shape[10], fits, samples, float(measurements.get('marker_radius', 0.)), measurements.get('marker_base_thickness'))
        sides[side] = dict(betas=shape[:10].tolist(), scale=float(shape[10]), attachments=attachments,
                           solver=dict(SOLVER), diagnostics=diagnostics, usable_frame_counts=counts,
                           representative_frames=len(samples), measurements=measurements,
                           left_shapedirs_corrected=model.left_shapedirs_corrected,
                           pose_model='15 standardized MANO PCA coefficients bounded +/-3; bounded flat-to-mean blend; no independent axial twists',
                           shape_prior='10 betas bounded +/-3, beta penalty 2mm, scale penalty 25mm; SDK joints are estimates',
                           dimension_definitions='Neutral wrist-to-middle-surface-tip distance; root-weight>0.5 palm mesh width and thickness extents',
                           shape_provenance='Regularised multi-pose SDK skeleton estimate; no scan; marker attachments fitted separately; runtime methods share this shape')
    return dict(schema_version=1, model='MANO', units='mm', profile_id=profile.get('id'), sides=sides,
                created_at=datetime.now(timezone.utc).isoformat(), calibration_seconds=time.perf_counter() - started,
                quality=dict(status='estimated', ground_truth_available=False,
                             warning='Shape, skin thickness, attachments and fingertip positions are model estimates. Fit residual is not true distance error. Marker/skeleton clocks are not synchronized.'),
                joint_order=list(LANDMARKS), fingertip_definition='Official MANO surface vertices, not SDK anatomical joint centres')
