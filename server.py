"""Local-only CMAvatar monitor. Python standard library, Windows SDK worker."""
import argparse
import copy
import csv
import io
import ipaddress
import json
import multiprocessing as mp
import queue
import re
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from calibration import CalibrationStore, MAX_PROFILE_BYTES
from hand_service import HandService

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = dict(host='192.168.1.200', device='MCAvatar', port=3883,
                      human_ids=list(range(16)), poll_hz=120)


def validate_config(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULT_CONFIG):
        raise ValueError('连接参数无效')
    config = {**copy.deepcopy(DEFAULT_CONFIG), **values}
    try:
        if not isinstance(config['host'], str): raise ValueError()
        ipaddress.IPv4Address(config['host'])
    except (ValueError, TypeError):
        raise ValueError('请输入有效的主机 IPv4 地址') from None
    if not isinstance(config['device'], str) or not re.fullmatch(r'[\w.-]{1,64}', config['device'], re.ASCII):
        raise ValueError('设备名只能包含字母、数字、下划线、点和短横线')
    for key, low, high in [('port', 1, 65535), ('poll_hz', 1, 240)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f'{key} 必须为 {low}–{high} 的整数')
    ids = config['human_ids']
    if not isinstance(ids, list) or len(ids) > 64 or any(type(i) is not int or not 0 <= i <= 255 for i in ids):
        raise ValueError('人体 ID 必须为 0–255 的整数列表，最多 64 个')
    config['human_ids'] = sorted(set(ids))
    return config


class CaptureStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.file = None
        self.recording = dict(active=False, name=None, events=0, error=None)
        self.reset()

    def reset(self):
        with self.lock:
            self.rigids, self.skeletons, self.hierarchy = {}, {}, {}
            self.skeleton_joints = {}
            self.pending_hierarchy = {}
            self.markers = []
            self.marker_event = None
            self.marker_time = 0
            self.last_received = None
            self.event_count = 0
            self.times = deque()
            self.error = None
            self.message = '尚未连接'

    def ingest(self, event, flush=True):
        event = copy.deepcopy(event)
        event.setdefault('timestamp', time.time())
        kind = event.get('type')
        if kind in ('tracker', 'rigid_body'):
            # Sender's ID convention: 300 and above are skeleton joints.
            kind = event['type'] = 'skeleton_joint' if event['id'] >= 300 else 'rigid_body'
        with self.lock:
            if kind == 'rigid_body': self.rigids[event['id']] = event
            elif kind == 'skeleton_joint': self.skeleton_joints[event['id']] = event
            elif kind == 'skeleton': self.skeletons[(event['id'], event.get('mode', 'source'))] = event
            elif kind == 'markers':
                self.markers = event['points']
                self.marker_time = event['timestamp']
                self.marker_event = event
            elif kind == 'hierarchy':
                event['mode'] = 'unknown'
                self.pending_hierarchy[event['id']] = event
                self.hierarchy[('unknown', event['id'])] = event
            elif kind == 'hierarchy_end':
                mode = {0:'source', 1:'retarget'}.get(event.get('retarget_flag'), 'unknown')
                if mode != 'unknown':
                    self.hierarchy = {k:v for k,v in self.hierarchy.items() if k[0] not in (mode, 'unknown')}
                    for key, node in self.pending_hierarchy.items():
                        self.hierarchy[(mode, key)] = dict(node, mode=mode)
                    self.pending_hierarchy.clear()
            elif kind == 'hierarchy_reset':
                self.hierarchy.clear()
                self.pending_hierarchy.clear()
            elif kind == 'status':
                self.message = event.get('message', '')
                if event.get('level') == 'error': self.error = self.message
            if kind in ('rigid_body', 'skeleton_joint', 'skeleton', 'markers'):
                # An empty marker sample is a clear, not evidence of an active sender.
                if ((kind in ('rigid_body', 'skeleton_joint')) or (kind == 'markers' and event['points']) or
                        (kind == 'skeleton' and any(j.get('detected') for j in event['joints']))):
                    self.last_received = event['timestamp']
                    self.event_count += 1
                    self.times.append(event['timestamp'])
                    while self.times and self.times[0] < event['timestamp'] - 1: self.times.popleft()
            if self.file:
                try:
                    self.file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
                    if flush: self.file.flush()
                    self.recording['events'] += 1
                except (OSError, ValueError) as exc:
                    self.recording['error'] = f'录制写入失败：{exc}'
                    self.stop_recording()

    def start_recording(self, config):
        with self.lock:
            if self.file: raise ValueError('正在录制，请先停止当前录制')
            name = datetime.now().strftime('capture_%Y%m%d_%H%M%S_%f.jsonl')
            handle = (self.directory / name).open('x', encoding='utf-8')
            try:
                metadata = dict(type='metadata', timestamp=time.time(), format_version=2,
                                skeleton_tracker_id_min=300,
                                config=config, quaternion_order='xyzw',
                                note='SDK sampled stream; skeleton/marker polling is not guaranteed lossless. Coordinates retain SDK units.')
                snapshots = [dict(e, snapshot=True) for e in [*self.rigids.values(), *self.skeleton_joints.values(), *self.skeletons.values(),
                             *([self.marker_event] if self.marker_event else [])]]
                for item in [metadata, *self.hierarchy.values(), *snapshots]:
                    handle.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
            except Exception:
                handle.close()
                raise
            self.file = handle
            self.recording = dict(active=True, name=name, events=len(snapshots), error=None)
            return name

    def stop_recording(self):
        with self.lock:
            if self.file:
                try: self.file.close()
                except OSError as exc: self.recording['error'] = str(exc)
            self.file = None
            self.recording['active'] = False

    def resolve_recording(self, name):
        if not re.fullmatch(r'capture_\d{8}_\d{6}_\d{6}\.jsonl', name):
            raise ValueError('录制文件名无效')
        with self.lock:
            if self.recording['active'] and name == self.recording['name']:
                raise ValueError('请先停止录制再导出')
        path = (self.directory / name).resolve()
        if path.parent != self.directory.resolve() or not path.is_file():
            raise ValueError('录制文件不存在')
        return path

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        # Published event payloads are immutable: ingestion replaces references.
        # Copy references under lock, then do deep copies and disk reads outside it
        # so browser polling cannot hold up the high-frequency receiver.
        with self.lock:
            rigids = list(self.rigids.values())
            joints = list(self.skeleton_joints.values())
            skeletons = list(self.skeletons.values())
            markers, marker_time = self.markers, self.marker_time
            hierarchy = list(self.hierarchy.values())
            recording = dict(self.recording)
            result = dict(connected=self.last_received is not None and now - self.last_received <= 2,
                          error=self.error, message=self.message, event_count=self.event_count,
                          event_rate=sum(t >= now - 1 for t in self.times),
                          last_received=self.last_received, recording=recording)
        def aged(event):
            item = copy.deepcopy(event)
            age = max(0, now - item['timestamp'])
            item.update(age_seconds=age, stale=age > 2)
            return item
        recordings = [dict(name=p.name, bytes=p.stat().st_size)
                      for p in sorted(self.directory.glob('capture_*.jsonl'), reverse=True)[:100]
                      if p.name != recording['name'] or not recording['active']]
        result.update(rigid_bodies=[aged(e) for e in rigids],
                      skeleton_joints=[aged(e) for e in joints],
                      skeletons=[aged(e) for e in skeletons],
                      markers=[aged(dict(p, timestamp=marker_time)) for p in markers],
                      hierarchy=copy.deepcopy(hierarchy), recordings=recordings)
        return result


CSV_FIELDS = ['timestamp','type','id','mode','frame','source_time','position_space','snapshot','freshness_unknown',
              'joint_id','x','y','z','qx','qy','qz','qw','detected','name','parent']


def csv_lines(path):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction='ignore')
    writer.writeheader()
    yield buffer.getvalue()
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            try: event = json.loads(line)
            except json.JSONDecodeError: continue  # A crash may leave one incomplete final line.
            kind = event.get('type')
            if kind not in ('rigid_body','skeleton_joint','skeleton','markers','hierarchy'): continue
            parts = event.get('joints', []) if kind == 'skeleton' else event.get('points', []) if kind == 'markers' else [event]
            for part in parts:
                row = {k:event.get(k, '') for k in CSV_FIELDS}
                if kind == 'skeleton': row['joint_id'] = part.get('id', '')
                if kind == 'skeleton_joint': row['joint_id'] = event.get('id', '')
                if kind == 'markers': row['id'] = part.get('id', '')
                row['detected'] = part.get('detected', '')
                row.update(zip(('x','y','z'), part.get('pos', [])))
                row.update(zip(('qx','qy','qz','qw'), part.get('quat', [])))
                for key, value in row.items():
                    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
                        row[key] = "'" + value
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow(row)
                yield buffer.getvalue()


class EventBatcher:
    """Amortize IPC overhead without coalescing samples or altering timestamps."""
    def __init__(self, events, dropped, batch_size=128):
        self.events, self.dropped, self.batch_size = events, dropped, batch_size
        self.pending = []
        self.lock = threading.Lock()

    def emit(self, event):
        with self.lock:
            self.pending.append(event)
            if len(self.pending) >= self.batch_size: self._flush()

    def _flush(self):
        if not self.pending: return
        batch, self.pending = self.pending, []
        try: self.events.put_nowait(batch)
        except queue.Full:
            with self.dropped.get_lock(): self.dropped.value += len(batch)

    def flush(self):
        with self.lock: self._flush()


def sdk_worker(config, events, stop, dropped):
    batcher = EventBatcher(events, dropped)
    finished = threading.Event()
    def flush_periodically():
        while not finished.wait(.005): batcher.flush()
    flusher = threading.Thread(target=flush_periodically, daemon=True)
    flusher.start()
    try:
        from receiver import run_receiver
        run_receiver(config, batcher.emit, stop)
    except Exception as exc:
        batcher.emit(dict(type='status', level='error', message=f'SDK 接收失败：{exc}'))
    finally:
        finished.set()
        flusher.join()
        batcher.flush()


class Application:
    def __init__(self, directory=ROOT / 'recordings'):
        self.store = CaptureStore(directory)
        self.calibrations = CalibrationStore(Path(directory) / 'calibrations')
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.lock = threading.RLock()
        self.process = None
        self.pump = None
        self.dropped = None
        self.generation = 0
        self.hands = HandService(self.snapshot, self.calibrations, ROOT / 'models' / 'mano', Path(directory) / 'hand_fits')

    def connect(self, values):
        config = validate_config(values)
        with self.lock:
            if self.process and self.process.is_alive(): raise ValueError('已启动接收，请先断开')
            self.disconnect()
            self.store.reset()
            self.config = config
            ctx = mp.get_context('spawn')
            self.events, self.stop = ctx.Queue(maxsize=64), ctx.Event()
            self.dropped = ctx.Value('L', 0)
            self.process = ctx.Process(target=sdk_worker, args=(config, self.events, self.stop, self.dropped), daemon=True)
            self.process.start()
            self.store.message = '接收已启动，等待实际数据'
            self.pump = threading.Thread(target=self._consume, args=(self.process, self.events, self.generation), daemon=True)
            self.pump.start()

    def _consume(self, process, events, generation):
        while True:
            try:
                batch = events.get(timeout=.1)
                with self.store.lock:
                    if generation != self.generation: return
                    batch = batch if isinstance(batch, list) else [batch]
                    for index, event in enumerate(batch):
                        self.store.ingest(event, flush=index == len(batch) - 1)
            except queue.Empty:
                if not process.is_alive(): break
            except (EOFError, OSError): break
        with self.store.lock:
            if generation != self.generation: return
            if process.exitcode:
                self.store.ingest(dict(type='status', level='error', message=f'SDK 子进程退出（{process.exitcode}），可重新连接'))
            self.store.stop_recording()

    def disconnect(self):
        with self.lock:
            if self.process:
                self.stop.set()
                self.process.join(timeout=3)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
                if self.pump: self.pump.join(timeout=5)
                self.events.close()
                self.process = None
            with self.store.lock: self.generation += 1
            self.store.stop_recording()
            self.store.reset()
            self.store.message = '已断开'

    def snapshot(self):
        with self.lock:
            result = self.store.snapshot()
            running = bool(self.process and self.process.is_alive())
            result.update(running=running, config=copy.deepcopy(self.config),
                          queue_dropped=self.dropped.value if self.dropped else 0)
            result['connected'] = running and result['connected']
            return result


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def _local_request(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed: return False
            origin = self.headers.get('Origin')
            return origin is None or origin in {f'http://{h}' for h in allowed}

        def _headers(self, code, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")

        def _json(self, code, data):
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self._headers(code, 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                if not self._local_request(): return self._json(403, {'error':'仅接受本机页面访问'})
                parsed = urlsplit(self.path)
                path = unquote(parsed.path)
                if path == '/api/state': return self._json(200, app.snapshot())
                if path == '/api/hands': return self._json(200, app.hands.status())
                if path == '/api/hands/topology': return self._json(200, app.hands.topology())
                if path == '/api/hands/export':
                    data = json.dumps(app.hands.export(), ensure_ascii=False, allow_nan=False).encode()
                    self._headers(200, 'application/json; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="hand-skin-estimate.json"')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == '/api/calibrations': return self._json(200, {'profiles': app.calibrations.list()})
                if path.startswith('/api/calibrations/'):
                    return self._json(200, app.calibrations.get(path.removeprefix('/api/calibrations/')))
                if path.startswith('/api/recordings/'):
                    file = app.store.resolve_recording(path.removeprefix('/api/recordings/'))
                    fmt = parse_qs(parsed.query).get('format', ['jsonl'])[0]
                    if fmt not in ('jsonl', 'csv'): raise ValueError('导出格式无效')
                    self._headers(200, 'text/csv; charset=utf-8' if fmt == 'csv' else 'application/x-ndjson; charset=utf-8')
                    self.send_header('Content-Disposition', f'attachment; filename="{file.stem}.{fmt}"')
                    self.end_headers()
                    if fmt == 'csv':
                        self.wfile.write(b'\xef\xbb\xbf')
                        for line in csv_lines(file): self.wfile.write(line.encode('utf-8'))
                    else:
                        with file.open('rb') as handle:
                            while chunk := handle.read(65536): self.wfile.write(chunk)
                    return
                assets = {'/':('index.html','text/html'), '/index.html':('index.html','text/html'),
                          '/calibration.js':('calibration.js','text/javascript'),
                          '/hand_skin.js':('hand_skin.js','text/javascript'), '/hand_skin.css':('hand_skin.css','text/css'),
                          '/app.js':('app.js','text/javascript'), '/style.css':('style.css','text/css')}
                if path not in assets: return self._json(404, {'error':'未找到'})
                filename, mime = assets[path]
                payload = (ROOT / 'static' / filename).read_bytes()
                self._headers(200, mime + '; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, FileNotFoundError) as exc: self._json(400, {'error':str(exc)})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        def do_POST(self):
            try:
                if not self._local_request(): return self._json(403, {'error':'仅接受本机页面访问'})
                path = urlsplit(self.path).path
                size = int(self.headers.get('Content-Length', '0'))
                limit = MAX_PROFILE_BYTES if path == '/api/calibrations' else 8192
                if not 0 <= size <= limit: raise ValueError('请求过大')
                values = json.loads(self.rfile.read(size) or b'{}')
                if path.startswith('/api/hands/'):
                    if not isinstance(values, dict): raise ValueError('皮肤请求参数无效')
                    if path == '/api/hands/calibrate':
                        if set(values) != {'profile_id'}: raise ValueError('请选择标定档案')
                        app.hands.calibrate(values['profile_id'])
                    elif path in ('/api/hands/start','/api/hands/stop'):
                        if values: raise ValueError('此操作不接受参数')
                        if path.endswith('/start'): app.hands.start()
                        else: app.hands.stop()
                    else: return self._json(404, {'error':'未找到'})
                    return self._json(200, {'ok':True})
                if path == '/api/calibrations': return self._json(200, app.calibrations.save(values))
                if path == '/api/connect': app.connect(values)
                elif path == '/api/disconnect': app.disconnect()
                elif path == '/api/record/start':
                    with app.lock:
                        if not app.process or not app.process.is_alive(): raise ValueError('请先启动接收')
                        app.store.start_recording(app.config)
                elif path == '/api/record/stop': app.store.stop_recording()
                else: return self._json(404, {'error':'未找到'})
                self._json(200, {'ok':True})
            except (ValueError, TypeError, UnicodeDecodeError) as exc: self._json(400, {'error':str(exc)})
            except OSError as exc: self._json(500, {'error':str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description='CMAvatar 本机动捕平台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='打开默认浏览器')
    args = parser.parse_args()
    app = Application()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(app))
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'CMAvatar 平台: {url}\n默认接口: MCAvatar@192.168.1.200\nCtrl+C 停止服务', flush=True)
    if args.open: webbrowser.open(url)
    try: server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt: pass
    finally:
        app.hands.close()
        app.disconnect()
        server.server_close()


if __name__ == '__main__':
    mp.freeze_support()
    main()
