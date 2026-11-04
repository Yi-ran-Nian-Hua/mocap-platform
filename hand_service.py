"""Bounded latest-sample MANO fitting, separate from raw event ingestion."""
import copy
import hashlib
import importlib.util
import json
import math
import multiprocessing as mp
import os
import re
import threading
import time
from pathlib import Path

LANDMARKS = ['wrist'] + [f'{finger}_{n}' for finger in ('thumb', 'index', 'middle', 'ring', 'pinky') for n in range(1, 5)]
NAME = re.compile(r'(Left|Right)Hand(?:(Thumb|Index|Middle|Ring|Pinky)([1-4]))?$')
MAX_AGE = 2.0
def _pose_process(connection, models_dir, side, calibration, mode):
    for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[name] = '1'
    try:
        from mano_engine import ManoModel, HandFitter
        fitter = HandFitter(ManoModel(Path(models_dir)/f'MANO_{side.upper()}.npz'), calibration, mode)
        while True:
            points = connection.recv()
            if points is None: break
            try: connection.send((True, fitter.fit(points)))
            except Exception as exc: connection.send((False, str(exc)))
    except (EOFError, OSError):
        pass
    finally: connection.close()


class _PoseWorker:
    """One stream, one warm pose state, at most one outstanding request."""
    def __init__(self, models_dir, side, calibration, mode):
        context = mp.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_pose_process, args=(child, str(models_dir), side, calibration, mode), daemon=True)
        try: self.process.start()
        except Exception:
            self.connection.close()
            raise
        finally: child.close()

    def submit(self, points): self.connection.send(points)

    def result(self, stop_event):
        deadline = time.monotonic()+5
        while not self.connection.poll(.02):
            if stop_event.is_set(): raise InterruptedError('皮肤求解已暂停')
            if not self.process.is_alive(): raise RuntimeError('皮肤计算进程退出')
            if time.monotonic() >= deadline: raise TimeoutError('皮肤计算超时，已停止以避免任务堆积')
        ok, value = self.connection.recv()
        if not ok: raise ValueError(value)
        return value

    def close(self):
        try:
            if self.process.is_alive(): self.connection.send(None)
        except (OSError, EOFError, BrokenPipeError): pass
        self.process.join(timeout=.1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=.3)
        self.connection.close()


def skeleton_bindings(profile):
    result = {'left':{}, 'right':{}}
    for sample in profile.get('samples', []):
        for frame in sample['frames']:
            for point in frame['skeleton_joints']:
                match = NAME.search(point.get('name', ''))
                if not match: continue
                side = match[1].lower()
                key = f'{match[2].lower()}_{match[3]}' if match[2] else 'wrist'
                identity = {'id':point['id'], 'name':point['name']}
                if key in result[side] and result[side][key] != identity:
                    raise ValueError('标定档案包含不同身份的同侧手部骨骼')
                result[side][key] = identity
    return result


def extract_points(state, mapping, side, mode, now=None, bindings=None):
    now = time.time() if now is None else now
    if not state.get('running') or not state.get('connected') or state.get('error'):
        raise ValueError('设备未连接或数据已中断')
    selected = {}
    if mode == 'markers':
        slots = {item['landmark']: item['marker_id'] for item in mapping if item['side'] == side}
        if len(slots) != 21: raise ValueError('该手需要完整的 21 个标记点对应')
        points = {p['id']: p for p in state.get('markers', [])}
        selected = {key: points.get(slots[key]) for key in LANDMARKS}
    elif mode == 'skeleton':
        slots = {}
        if bindings is not None:
            current = {node['id']: node.get('name') for node in state.get('hierarchy', []) if node.get('mode') == 'source'}
            for key in LANDMARKS:
                identity = bindings.get(side, {}).get(key)
                if identity is None or current.get(identity['id']) != identity['name']:
                    raise ValueError(f'标定所用的 {side} {key} 骨骼身份缺失或改变')
                slots[key] = identity['id']
        else:
            for node in state.get('hierarchy', []):
                match = NAME.search(node.get('name', ''))
                if node.get('mode') != 'source' or not match or match[1].lower() != side: continue
                key = f'{match[2].lower()}_{match[3]}' if match[2] else 'wrist'
                if key in slots: raise ValueError('发现多个同侧手部骨骼，身份不明确')
                slots[key] = node['id']
        points = {p['id']: p for p in state.get('skeleton_joints', [])}
        selected = {key: points.get(slots.get(key)) for key in LANDMARKS}
    else:
        raise ValueError('未知驱动方式')
    timestamps, source_times, frame_numbers, xyz = [], [], [], []
    for key in LANDMARKS:
        point = selected.get(key)
        if point is None: raise ValueError(f'{side} {key} 点位缺失')
        pos, timestamp = point.get('pos'), point.get('timestamp')
        if not isinstance(pos, (list, tuple)) or len(pos) != 3 or not all(isinstance(n, (int,float)) and math.isfinite(n) for n in pos):
            raise ValueError(f'{side} {key} 坐标无效')
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError('输入没有有效接收时间')
        if point.get('stale') or now - timestamp > MAX_AGE:
            raise ValueError('标记内容已过期（静止与断流无法区分）' if mode == 'markers' else '骨骼点已过期')
        xyz.append(list(pos))
        timestamps.append(timestamp)
        source_times.append(point.get('source_time'))
        frame_numbers.append(point.get('frame'))
    return xyz, dict(source_timestamp=min(timestamps), input_timestamps=timestamps,
                     source_times=source_times, source_frames=frame_numbers,
                     timing_note='marker content-update time; channels are not hardware synchronized' if mode == 'markers' else 'receiver timestamps; source clock retained separately')


class HandService:
    def __init__(self, snapshot, calibrations, models_dir, directory, parallel=True):
        self.snapshot = snapshot
        self.calibrations = calibrations
        self.models_dir = Path(models_dir)
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.prepared = None
        self.faces = None
        self.faces_by_side = {}
        self.parallel = parallel
        self.state = dict(phase='idle', running=False, message='选择已保存档案，拟合个人手型后启动皮肤预览。',
                          profile_id=None, profile_name=None, quality={}, frames=[], updated_at=None, sequence=0)
        self._load_saved()

    def _load_saved(self):
        for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('format_version') != 1 or not data.get('fit', {}).get('sides'): continue
                self.prepared = data
                self.state.update(phase='ready', profile_id=data['profile']['id'], profile_name=data['profile']['name'],
                                  quality=data['fit'].get('quality', {}), message='已加载个人手型，点击启动预览。')
                break
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _requirements(self):
        if any(not (self.models_dir / f'MANO_{side}.npz').is_file() for side in ('LEFT','RIGHT')):
            raise ValueError('未找到左右手 MANO NPZ 模型，请先准备模型文件')
        if any(importlib.util.find_spec(name) is None for name in ('numpy','scipy')):
            raise ValueError('当前 Python 缺少 NumPy/SciPy，请使用 MANO 专用环境启动')

    def _launch(self, target, *args):
        with self.lock:
            if self.worker and self.worker.is_alive(): raise ValueError('皮肤任务仍在运行，请先暂停并等待当前计算结束')
            self.stop_event = threading.Event()
            self.worker = threading.Thread(target=target, args=args, daemon=True, name='MANO fitting')
            self.worker.start()

    def calibrate(self, profile_id):
        if not isinstance(profile_id, str): raise ValueError('请选择已保存的标定档案')
        profile = self.calibrations.get(profile_id)
        self._requirements()
        with self.lock:
            if self.worker and self.worker.is_alive(): raise ValueError('请先暂停皮肤任务再重新拟合')
            self.state.update(phase='calibrating', running=True, message='正在拟合个人手型…', frames=[],
                              profile_id=profile['id'], profile_name=profile['name'], quality={})
            self._launch(self._calibrate, profile)

    def _engine(self):
        # Avoid a BLAS thread pool contending with the live SDK receiver.
        for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
            os.environ[name] = '1'
        from mano_engine import ManoModel, HandFitter, calibrate_profile
        return ManoModel, HandFitter, calibrate_profile

    def _progress(self, message):
        if self.stop_event.is_set(): raise InterruptedError('拟合已暂停')
        with self.lock: self.state['message'] = str(message)

    def _calibrate(self, profile):
        try:
            _, _, calibrate_profile = self._engine()
            fit = calibrate_profile(profile, self.models_dir, progress=self._progress)
            if self.stop_event.is_set(): return
            prepared = dict(format_version=1, created_at=time.time(), profile=profile, fit=fit,
                            model_hashes={side: hashlib.sha256((self.models_dir/f'MANO_{side.upper()}.npz').read_bytes()).hexdigest() for side in fit['sides']})
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f'{profile["id"]}.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(prepared, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            temporary.replace(path)
            with self.lock:
                self.prepared = prepared
                self.state.update(phase='ready', quality=fit.get('quality',{}), message='个人手型已拟合，正在更新实时皮肤。')
            self._live()
        except InterruptedError:
            pass
        except Exception as exc:
            with self.lock: self.state.update(phase='error', message=f'手型拟合失败：{exc}', running=False)
        finally:
            with self.lock: self.state['running'] = False

    def start(self):
        if not self.prepared: raise ValueError('请先选择标定档案并拟合个人手型')
        self._requirements()
        for side, expected in self.prepared.get('model_hashes', {}).items():
            actual = hashlib.sha256((self.models_dir/f'MANO_{side.upper()}.npz').read_bytes()).hexdigest()
            if actual != expected: raise ValueError('基础模型已改变，请重新拟合个人手型')
        self._launch(self._live)

    def stop(self):
        self.stop_event.set()
        with self.lock:
            self.state.update(running=False, message='皮肤预览已暂停；原始动捕接收仍继续。')
            if self.state['phase'] == 'calibrating': self.state['phase'] = 'ready' if self.prepared else 'idle'
            for frame in self.state['frames']:
                frame.update(valid=False, warning='预览已暂停')

    def close(self):
        self.stop()
        if self.worker: self.worker.join(timeout=2)

    def _live(self):
        workers = {}
        try:
            ManoModel, HandFitter, _ = self._engine()
            fit = self.prepared['fit']
            bindings = skeleton_bindings(self.prepared['profile'])
            models = {side: ManoModel(self.models_dir/f'MANO_{side.upper()}.npz') for side in fit['sides']}
            fitters = {(side, mode): HandFitter(model, fit['sides'][side], mode)
                       for side, model in models.items() for mode in ('skeleton','markers')}
            if self.parallel:
                for key in fitters:
                    workers[key] = _PoseWorker(self.models_dir, key[0], fit['sides'][key[0]], key[1])
            with self.lock:
                self.faces = next(iter(models.values())).faces.tolist()
                self.faces_by_side = {side: model.faces.tolist() for side, model in models.items()}
                self.state.update(phase='ready', running=True, message='实时皮肤预览；残差不是实际距离误差。',
                                  profile_id=self.prepared['profile']['id'], profile_name=self.prepared['profile']['name'],
                                  quality=fit.get('quality', {}))
            previous, results = {}, {}
            while not self.stop_event.is_set():
                started = time.perf_counter()
                state = self.snapshot()
                changed = False
                pending = []
                for (side, mode), fitter in fitters.items():
                    if self.stop_event.is_set(): break
                    key = (side, mode)
                    try:
                        points, timing = extract_points(state, self.prepared['profile']['mapping'], side, mode, bindings=bindings)
                        signature = (timing['source_timestamp'], tuple(tuple(p) for p in points))
                        if previous.get(key) == signature: continue
                        future = workers.get(key)
                        if future: future.submit(points)
                        pending.append((key, fitter, points, timing, signature, future))
                    except Exception as exc:
                        changed = changed or results.get(key, {}).get('warning') != str(exc) or results.get(key, {}).get('valid', True)
                        results[key] = dict(side=side, mode=mode, valid=False, warning=str(exc),
                                            source_timestamp=None, estimate_only=True)
                        previous.pop(key, None)
                for key, fitter, points, timing, signature, future in pending:
                    side, mode = key
                    try:
                        result = future.result(self.stop_event) if future else fitter.fit(points)
                        result.update(side=side, mode=mode, valid=True, warning='', **timing,
                                      fitted_at=time.time(), estimate_only=True)
                        json.dumps(result, allow_nan=False)
                        results[key] = result
                        previous[key] = signature
                        changed = True
                    except (TimeoutError, InterruptedError, RuntimeError, EOFError, BrokenPipeError):
                        raise
                    except Exception as exc:
                        changed = changed or results.get(key, {}).get('warning') != str(exc) or results.get(key, {}).get('valid', True)
                        results[key] = dict(side=side, mode=mode, valid=False, warning=str(exc),
                                            source_timestamp=None, estimate_only=True)
                        previous.pop(key, None)
                if self.stop_event.is_set(): break
                fresh_state = self.snapshot()
                if not fresh_state.get('running') or not fresh_state.get('connected') or fresh_state.get('error'):
                    for result in results.values(): result.update(valid=False, warning='设备连接在计算期间中断')
                    changed = True
                with self.lock:
                    self.state['cycle_ms'] = (time.perf_counter()-started)*1000
                    if changed:
                        self.state.update(frames=list(results.values()), updated_at=time.time(),
                                          sequence=self.state['sequence']+1)
                self.stop_event.wait(max(.001, .05-(time.perf_counter()-started)))
        except InterruptedError:
            pass
        except Exception as exc:
            with self.lock: self.state.update(phase='error', message=f'实时皮肤计算失败：{exc}')
        finally:
            for worker in workers.values(): worker.close()
            with self.lock:
                self.state['running'] = False
                for frame in self.state['frames']: frame.update(valid=False, warning='皮肤计算已停止')

    def status(self):
        with self.lock: state = copy.deepcopy(self.state)
        now = time.time()
        for frame in state['frames']:
            timestamp = frame.get('source_timestamp')
            frame['age_seconds'] = max(0, now-timestamp) if timestamp is not None else None
            if timestamp is not None and frame['age_seconds'] > MAX_AGE:
                frame.update(valid=False, warning='输入数据已过期，皮肤为缓存估计')
            elif not state['running'] or state['phase'] != 'ready':
                frame.update(valid=False, warning='皮肤计算未运行，结果为缓存估计')
        return state

    def topology(self):
        if self.faces is None:
            self._requirements()
            ManoModel, _, _ = self._engine()
            self.faces_by_side = {side: ManoModel(self.models_dir/f'MANO_{side.upper()}.npz').faces.tolist() for side in ('left','right')}
            self.faces = self.faces_by_side['right']
        return {'faces': self.faces, 'faces_by_side': self.faces_by_side}

    def export(self):
        with self.lock:
            if self.prepared is None: raise ValueError('尚无个人手型拟合结果')
            if self.state['phase'] == 'calibrating' or self.state['profile_id'] != self.prepared['profile']['id']:
                raise ValueError('新的手型尚未拟合完成，不能混合导出不同档案')
            prepared = copy.deepcopy(self.prepared)
            live = self.status()
        return dict(format_version=1, units='mm', exported_at=time.time(),
                    coordinate_space='SDK global coordinates', estimate_only=True,
                    accuracy_note='Fitting residuals are not ground-truth surface distance errors. No scan/contact reference validation has been performed. Marker and skeleton streams are not synchronized.',
                    calibration=prepared, topology=self.topology(), live=live)
