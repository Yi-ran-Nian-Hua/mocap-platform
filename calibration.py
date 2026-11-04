"""Validated millimetre calibration drafts; no model loading or fitting."""
import copy
import json
import math
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_PROFILE_BYTES = 8 * 1024 * 1024
LANDMARKS = {'wrist'} | {f'{finger}_{joint}' for finger in
    ('thumb', 'index', 'middle', 'ring', 'pinky') for joint in range(1, 5)}
PROFILE_FIELDS = {'name', 'units', 'mapping', 'measurements', 'samples'}
TIMESTAMP_NOTE = (
    'received_at is browser wall-clock capture time; timestamp is receiver wall-clock '
    'event time; source_time, when present, is the SDK source clock. These clocks and '
    'marker/skeleton samples are not synchronized. Repeated or stale samples do not '
    'establish temporal alignment. Coordinates and manual measurements use mm.')


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - (set(required) | set(optional)) or set(required) - set(value):
        raise ValueError('标定字段缺失或包含未知字段')


def _number(value, minimum=None, positive=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (minimum is not None and value < minimum) or (positive and value <= 0):
        raise ValueError('标定数值必须有限且在有效范围内')


def _id(value):
    if type(value) is not int or value < 0:
        raise ValueError('标定 ID 和帧号必须为非负整数')


def _list(value, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f'标定列表长度必须在 {minimum}–{maximum} 之间')


def _points(points, skeleton=False):
    _list(points, 0, 256)
    seen = set()
    for point in points:
        _object(point, {'id', 'pos', 'timestamp'} | ({'name'} if skeleton else set()),
                {'stale', 'age_seconds'} | ({'quat', 'frame', 'source_time'} if skeleton else set()))
        _id(point['id'])
        if point['id'] in seen: raise ValueError('同一帧中的点 ID 重复')
        seen.add(point['id'])
        _list(point['pos'], 3, 3)
        for coordinate in point['pos']: _number(coordinate)
        _number(point['timestamp'])
        if 'stale' in point and type(point['stale']) is not bool:
            raise ValueError('stale 必须为布尔值')
        if 'age_seconds' in point: _number(point['age_seconds'], minimum=0)
        if skeleton:
            if not isinstance(point['name'], str) or not 1 <= len(point['name'].strip()) <= 128:
                raise ValueError('骨骼关节必须有有效名称')
            if 'quat' in point:
                _list(point['quat'], 4, 4)
                for coordinate in point['quat']: _number(coordinate)
            if 'frame' in point: _id(point['frame'])
            if 'source_time' in point: _number(point['source_time'])


def validate_profile(value):
    _object(value, PROFILE_FIELDS)
    if not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 80:
        raise ValueError('标定名称须为 1–80 个字符')
    if value['units'] != 'mm': raise ValueError('标定单位必须为 mm')
    _list(value['mapping'], 1, 42)
    markers, slots = set(), set()
    for mapping in value['mapping']:
        _object(mapping, {'marker_id', 'side', 'landmark'})
        _id(mapping['marker_id'])
        if mapping['side'] not in ('left', 'right') or not isinstance(mapping['landmark'], str) or mapping['landmark'] not in LANDMARKS:
            raise ValueError('手侧或解剖位置无效')
        slot = (mapping['side'], mapping['landmark'])
        if mapping['marker_id'] in markers or slot in slots:
            raise ValueError('标记 ID 和解剖位置均不能重复映射')
        markers.add(mapping['marker_id'])
        slots.add(slot)
    _object(value['measurements'], set(), {'left', 'right'})
    for measurements in value['measurements'].values():
        _object(measurements, set(), {'palm_width', 'palm_thickness', 'hand_length', 'marker_radius', 'marker_base_thickness'})
        for key, number in measurements.items():
            _number(number, minimum=0, positive=key not in ('marker_radius', 'marker_base_thickness'))
    _list(value['samples'], 1, 12)
    for sample in value['samples']:
        _object(sample, {'pose', 'frames'})
        if sample['pose'] not in ('open', 'together', 'bent'): raise ValueError('标定姿势无效')
        _list(sample['frames'], 1, 60)
        for frame in sample['frames']:
            _object(frame, {'received_at', 'markers', 'skeleton_joints'})
            _number(frame['received_at'])
            _points(frame['markers'])
            _points(frame['skeleton_joints'], skeleton=True)
    result = copy.deepcopy(value)
    result['name'] = result['name'].strip()
    return result


class CalibrationStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def save(self, value):
        profile = validate_profile(value)
        profile.update(id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc).isoformat(),
                       schema_version=1, intended_model='MANO',
                       fitting_status='pending_model_and_fit', timestamp_note=TIMESTAMP_NOTE)
        payload = json.dumps(profile, ensure_ascii=False, allow_nan=False)
        if len(payload.encode('utf-8')) > MAX_PROFILE_BYTES:
            raise ValueError('标定文件过大')
        path = self.directory / (profile['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        with self.lock:
            try:
                with temporary.open('x', encoding='utf-8') as handle: handle.write(payload)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return profile

    def get(self, profile_id):
        if not isinstance(profile_id, str) or not re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', profile_id):
            raise ValueError('标定 ID 无效')
        path = (self.directory / (profile_id + '.json')).resolve()
        if path.parent != self.directory.resolve() or not path.is_file():
            raise ValueError('标定文件不存在')
        with self.lock:
            if path.stat().st_size > MAX_PROFILE_BYTES: raise ValueError('标定文件过大')
            result = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(result, dict) or result.get('id') != profile_id:
                raise ValueError('标定文件无效')
            validate_profile({key: result[key] for key in PROFILE_FIELDS if key in result})
            return result

    def list(self):
        profiles = []
        with self.lock:
            for path in self.directory.glob('*.json'):
                try:
                    profile = self.get(path.stem)
                    profiles.append(dict(id=profile['id'], name=profile['name'],
                        created_at=profile['created_at'], sample_count=len(profile['samples']),
                        mapping_count=len(profile['mapping'])))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return sorted(profiles, key=lambda profile: profile['created_at'], reverse=True)
