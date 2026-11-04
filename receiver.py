"""CMVrpn adapter. Keep this module in the isolated receiver process.

Signatures and layouts follow ChingMuPythonSDKs/Demo. Polling APIs expose
cached samples; they cannot guarantee lossless recording or marker freshness.
"""
import ctypes
import json
import pathlib
import time


MAX_JOINTS = 150
MAX_MARKERS = 2000
Int = ctypes.c_int32
Double = ctypes.c_double


class Timeval(ctypes.Structure):
    _fields_ = [('tv_sec', Int), ('tv_usec', Int)]


class Tracker(ctypes.Structure):
    _fields_ = [('msg_time', Timeval), ('sensor', Int), ('frameCounter', Int),
                ('pos', Double * 3), ('quat', Double * 4)]


class Hierarchy(ctypes.Structure):
    _fields_ = [('msg_time', Timeval), ('sensor', Int), ('parent', Int),
                ('name', ctypes.c_char * 127)]


class HierarchyEnd(ctypes.Structure):
    _fields_ = [('msg_time', Timeval), ('retarget_flag', Int)]


def endpoint(config):
    value = '{}@{}'.format(config.get('device', 'MCAvatar'), config['host'])
    port = config.get('port', 3883)
    if port != 3883:
        value += ':' + str(port)
    return value.encode('gbk')


def skeleton_event(human_id, mode, frame, positions, rotations, detected):
    return {
        'type': 'skeleton', 'id': human_id, 'mode': mode,
        'frame': frame & 0xffffffff,
        # The example confirms global source T but does not establish retarget T space.
        'position_space': 'global' if mode == 'source' else 'unknown',
        'rotation_space': 'local',
        'joints': [{'id': i, 'pos': list(positions[i * 3:i * 3 + 3]),
                    'quat': list(rotations[i * 4:i * 4 + 4]),
                    'detected': bool(detected[i])} for i in range(MAX_JOINTS)],
    }


class ChangedEvents:
    """Suppress cache repeats, retaining changed data with reused timecodes."""
    def __init__(self):
        self.previous = {}

    def changed(self, key, event):
        signature = json.dumps(event, sort_keys=True, separators=(',', ':'))
        if self.previous.get(key) == signature:
            return False
        self.previous[key] = signature
        return True


def _bind(dll, name, args, result=Int):
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = result
    return fn


def run_receiver(config, emit, stop_event):
    """Emit copied Python data, never pass native pointers out of this process."""
    dll = None
    started = False
    callbacks = []
    host = endpoint(config)
    last_invalid_warning = float('-inf')

    def send(event):
        nonlocal last_invalid_warning
        try:
            json.dumps(event, allow_nan=False)
        except (ValueError, TypeError):
            now = time.monotonic()
            if now - last_invalid_warning >= 5:
                last_invalid_warning = now
                emit({'type': 'status', 'level': 'warning', 'timestamp': time.time(),
                      'message': '已忽略包含无效数值（NaN/Infinity）的 SDK 数据'})
            return
        event.setdefault('timestamp', time.time())
        emit(event)

    def status(message, level='info'):
        send({'type': 'status', 'level': level, 'message': message})

    def tracker_callback(_user, data):
        send({'type': 'tracker', 'id': data.sensor,
              'frame': data.frameCounter, 'pos': list(data.pos),
              'quat': list(data.quat),
              'source_time': data.msg_time.tv_sec + data.msg_time.tv_usec / 1e6})

    def hierarchy_callback(_user, data):
        raw_name = bytes(data.name)
        try:
            name = raw_name.decode('utf-8')
        except UnicodeDecodeError:
            name = raw_name.decode('gbk', errors='replace')
        send({'type': 'hierarchy', 'id': data.sensor,
              'parent': data.parent, 'name': name})

    try:
        dll = ctypes.CDLL(str(pathlib.Path(__file__).resolve().parent / 'sdk' / 'CMVrpn.dll'))
        _bind(dll, 'CMVrpnStartExtern', [], None)
        _bind(dll, 'CMVrpnQuitExtern', [], None)
        _bind(dll, 'CMVrpnEnableLog', [ctypes.c_bool], None)
        _bind(dll, 'CMPluginConnectServer', [ctypes.c_char_p])
        p_int, p_double = ctypes.POINTER(Int), ctypes.POINTER(Double)
        _bind(dll, 'CMHumanGlobalTLocalRTC',
              [ctypes.c_char_p, Int, p_int, p_double, p_double, p_int], ctypes.c_bool)
        _bind(dll, 'CMRetargetHumanExternTC',
              [ctypes.c_char_p, Int, Int, p_int, p_double, p_double, p_int], ctypes.c_bool)
        _bind(dll, 'CMMarkerExtern', [ctypes.c_char_p, Int, p_double, p_int])
        dll.CMVrpnStartExtern()
        started = True
        dll.CMVrpnEnableLog(False)

        registrations = [
            ('TrackerData', Tracker, tracker_callback),
            ('UpdateHierarchy', Hierarchy, hierarchy_callback),
            ('ResetHierarchy', Timeval,
             lambda _user, _data: send({'type': 'hierarchy_reset'})),
            ('EndHierarchy', HierarchyEnd,
             lambda _user, data: send({'type': 'hierarchy_end',
                                       'retarget_flag': data.retarget_flag})),
        ]
        for name, struct_type, handler in registrations:
            callback_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, struct_type)
            callback = callback_type(handler)
            register = _bind(dll, 'CMPluginRegister' + name,
                             [ctypes.c_char_p, ctypes.c_void_p, callback_type])
            register(host, None, callback)
            callbacks.append((name, callback_type, callback))
        dll.CMPluginConnectServer(host)
        status('SDK 已启动，等待 {} 的实际数据'.format(host.decode('gbk')))
        changed = ChangedEvents()
        had_skeleton = set()
        # The SDK cannot distinguish a static cached marker from a fresh sample.
        # Only changed marker contents are emitted, explicitly marked uncertain.
        changed.changed('markers', {'type': 'markers', 'points': [], 'freshness_unknown': True})
        poll_frame = 0
        interval = 1.0 / config.get('poll_hz', 120)
        while not stop_event.is_set():
            begin = time.monotonic()
            for human_id in config.get('human_ids', list(range(16))):
                for mode in ('source', 'retarget'):
                    positions = (Double * (MAX_JOINTS * 3))()
                    rotations = (Double * (MAX_JOINTS * 4))()
                    detected = (Int * MAX_JOINTS)()
                    frame = Int()
                    if mode == 'source':
                        valid = dll.CMHumanGlobalTLocalRTC(
                            host, human_id, ctypes.byref(frame), positions, rotations, detected)
                    else:
                        valid = dll.CMRetargetHumanExternTC(
                            host, human_id, poll_frame, ctypes.byref(frame), positions, rotations, detected)
                    key = (human_id, mode)
                    if valid:
                        had_skeleton.add(key)
                        event = skeleton_event(human_id, mode, frame.value, positions, rotations, detected)
                        if changed.changed(key, event):
                            send(event)
                    elif key in had_skeleton:
                        had_skeleton.remove(key)
                        changed.previous.pop(key, None)
                        event = skeleton_event(human_id, mode, 0, positions, rotations, detected)
                        event['joints'] = []
                        send(event)
            positions = (Double * (MAX_MARKERS * 3))()
            marker_ids = (Int * MAX_MARKERS)()
            count = dll.CMMarkerExtern(host, poll_frame, positions, marker_ids)
            if not 0 <= count <= MAX_MARKERS:
                raise ValueError('SDK 返回无效标记点数量: {}'.format(count))
            event = {'type': 'markers', 'freshness_unknown': True,
                     'points': [{'id': marker_ids[i], 'pos': list(positions[i * 3:i * 3 + 3])}
                                for i in range(count)]}
            if changed.changed('markers', event):
                send(event)
            poll_frame = (poll_frame + 1) & 0x7fffffff
            stop_event.wait(max(0, interval - (time.monotonic() - begin)))
    except Exception as exc:
        status('接收失败: {}'.format(exc), 'error')
    finally:
        if started:
            for name, callback_type, callback in reversed(callbacks):
                # Upstream hierarchy example has an inconsistent capital L.
                candidates = ['CMPluginUnRegister' + name, 'CMPLuginUnRegister' + name]
                for symbol in candidates:
                    if hasattr(dll, symbol):
                        try:
                            fn = _bind(dll, symbol, [ctypes.c_char_p, ctypes.c_void_p, callback_type])
                            fn(host, None, callback)
                        except Exception:
                            pass
                        break
            dll.CMVrpnQuitExtern()
            # Callback objects remain alive until the native worker has quit.
