'use strict';

const $ = id => document.getElementById(id);
let state = null, stateReceived = 0, available = false, busy = false, actionError = '';
let configured = false, fitted = false, historyKey = '';
let frozen = null;
let lastPanelUpdate = -Infinity;
const viewVisible = {scene: typeof IntersectionObserver === 'undefined', table: typeof IntersectionObserver === 'undefined'};
const canvas = $('scene'), ctx = canvas.getContext('2d');
const camera = {yaw: -.55, pitch: .32, zoom: 1, center: [0, 0, 0], radius: 1, pan: [0, 0]};

function parseHumanIds(value) {
  const ids = new Set();
  for (const part of value.trim().split(/[,，]/)) {
    const match = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!match) throw new Error('人体 ID 格式错误，请输入 0-15 或 0,2,5。');
    const start = Number(match[1]), end = match[2] === undefined ? start : Number(match[2]);
    if (start > end || end > 255) throw new Error('人体 ID 范围为 0–255，范围起点不得大于终点。');
    for (let id = start; id <= end; id++) ids.add(id);
    if (ids.size > 64) throw new Error('人体 ID 最多配置 64 个。');
  }
  return [...ids].sort((a, b) => a - b);
}

async function request(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const options = {signal: controller.signal, cache: 'no-store'};
    if (data !== undefined) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `服务错误 ${response.status}`);
    return result;
  } finally { clearTimeout(timeout); }
}

function setText(id, value) {
  const element = $(id), text = String(value);
  if (element.textContent !== text) element.textContent = text;
}

function stale(item) {
  return !(frozen ? frozen.available : available) || !displayState()?.running || item.stale || (item.age_seconds || 0) + ((frozen ? frozen.at : Date.now()) - (frozen ? frozen.received : stateReceived)) / 1000 > 2;
}

function displayState() { return frozen ? frozen.state : state; }
function toggleFreeze() {
  if (frozen) { frozen = null; if (typeof window !== 'undefined') window.handCalibration?.onUnfreeze(); }
  else if (state) frozen = {state: JSON.parse(JSON.stringify(state)), at: Date.now(), received: stateReceived, available};
  render();
}

function render(refreshViews = true) {
  if (!state) return;
  const s = state, recording = s.recording || {}, running = available && s.running;
  let label = '未连接', kind = '';
  if (!available) { label = '本地服务不可用'; kind = 'failed'; }
  else if (s.error) { label = '接收异常'; kind = 'failed'; }
  else if (running && s.connected) { label = '数据在线'; kind = 'live'; }
  else if (running) { label = s.last_received ? '数据已过期' : '等待设备数据'; kind = 'warn'; }
  const status = $('status');
  status.className = `status ${kind}`;
  if (status.lastElementChild.textContent !== label) status.lastElementChild.textContent = label;
  setText('notice', available ? (running && s.connected && !s.error ? '正在接收设备数据。坐标和四元数保留 SDK 原值。' : s.message || (running ? '接收进程已启动，等待 SDK 数据。' : '等待连接设备。')) : '无法读取本地服务，屏幕上的数据均为缓存。请检查启动窗口。');
  const error = [actionError, s.error, recording.error].filter(Boolean).join(' · ');
  setText('error', error); $('error').hidden = !error;
  $('connect').disabled = busy || !available || running;
  $('disconnect').disabled = busy || !running;
  $('record-start').disabled = busy || !running || recording.active;
  $('record-stop').disabled = busy || !available || !recording.active;
  for (const id of ['host', 'device', 'port', 'human-ids', 'poll-hz']) $(id).disabled = busy || running;
  const config = s.config;
  if (config && running) setText('address', formatAddress(config.device, config.host, config.port));
  else updateAddress();
  const shown = displayState();
  setText('skeleton-count', (shown.skeletons || []).length);
  setText('skeleton-summary', `${(shown.skeleton_joints || []).length} 个骨骼关节 · 原始 / 重定向独立计流`);
  setText('rigid-count', (shown.rigid_bodies || []).length);
  setText('marker-count', (shown.markers || []).length);
  setText('freeze', frozen ? '恢复实时' : '冻结帧');
  $('freeze').disabled = !frozen && !s.last_received;
  $('freeze').setAttribute('aria-pressed', String(!!frozen));
  setText('freeze-status', frozen ? `已冻结 ${new Date(frozen.at).toLocaleTimeString('zh-CN', {hour12: false})} · 后台接收 / 录制继续` : '实时显示');
  $('freeze-status').className = frozen ? 'freeze-status active' : 'freeze-status';
  setText('rate', available && running ? s.event_rate || 0 : 0);
  setText('event-total', `累计 ${s.event_count || 0} · 丢弃 ${s.queue_dropped || 0}`);
  setText('record-events', recording.events || 0);
  setText('record-badge', recording.active ? (available ? '● 录制中' : '状态未知') : '未录制');
  $('record-badge').className = `pill ${recording.active ? 'active' : ''}`;
  setText('record-name', recording.name || '连接后可开始本地录制');
  setText('last-received', shown.last_received ? `${frozen ? '冻结样本' : '最近接收'} ${new Date(shown.last_received * 1000).toLocaleTimeString('zh-CN', {hour12: false})}${!frozen && !available ? ' · 缓存' : ''}` : '尚未收到数据');
  renderHistory(s.recordings || []);
  if (refreshViews) {
    if (viewVisible.table) renderTable();
    if (viewVisible.scene) {
      if (!fitted && allPoints().length) fit();
      draw();
    }
  }
}

function formatAddress(device, host, port) { return `${device}@${host}${Number(port) === 3883 ? '' : `:${port}`}`; }
function updateAddress() { setText('address', formatAddress($('device').value, $('host').value, $('port').value)); }

async function poll() {
  const started = performance.now();
  try {
    state = await request('/api/state');
    stateReceived = Date.now(); available = true;
    if (!configured) {
      if (state.running && state.config) {
        for (const id of ['host', 'device', 'port']) $(id).value = state.config[id];
        $('poll-hz').value = state.config.poll_hz;
        $('human-ids').value = state.config.human_ids.join(',');
      }
      configured = true;
    }
  } catch (_) {
    available = false;
    if (!state) state = {};
  }
  const now = performance.now();
  if (typeof window !== 'undefined') window.handCalibration?.onState(state, available, Date.now());
  // Keep the large numeric table off the high-frequency preview path.
  if (now - lastPanelUpdate >= 250 || !available) {
    render(false);
    if (!document.hidden && viewVisible.table && !frozen) renderTable();
    lastPanelUpdate = now;
  }
  if (!document.hidden && viewVisible.scene && !frozen) {
    if (!fitted && allPoints().length) fit();
    draw();
  }
  const capturing = typeof window !== 'undefined' && window.handCalibration?.isCapturing();
  const interval = !document.hidden && viewVisible.scene && !frozen && state?.running ? 1000 / 60 : capturing ? 50 : document.hidden ? 1000 : 250;
  // Include request/render time in the frame budget; never overlap requests.
  setTimeout(poll, available ? Math.max(0, interval - (performance.now() - started)) : 500);
}

async function action(path, data = {}) {
  if (busy) return;
  busy = true; actionError = ''; render();
  try { await request(path, data); }
  catch (error) { actionError = error.name === 'AbortError' ? '请求超时，请核对服务状态后重试。' : error.message; }
  finally { busy = false; render(); }
}

$('connect-form').addEventListener('submit', event => {
  event.preventDefault();
  try {
    const config = {host: $('host').value.trim(), device: $('device').value.trim(), port: Number($('port').value), poll_hz: Number($('poll-hz').value), human_ids: parseHumanIds($('human-ids').value)};
    fitted = false;
    action('/api/connect', config);
  } catch (error) { actionError = error.message; render(); }
});
$('disconnect').addEventListener('click', () => action('/api/disconnect'));
$('record-start').addEventListener('click', () => action('/api/record/start'));
$('record-stop').addEventListener('click', () => action('/api/record/stop'));
for (const id of ['host', 'device', 'port']) $(id).addEventListener('input', updateAddress);
$('data-filter').addEventListener('change', renderTable);

function renderHistory(files) {
  const key = JSON.stringify(files);
  if (key === historyKey) return;
  historyKey = key;
  const list = $('recordings'); list.replaceChildren();
  if (!files.length) {
    const row = document.createElement('li'); row.className = 'empty-history'; row.textContent = '暂无记录'; list.append(row);
  }
  for (const file of files) {
    const row = document.createElement('li'), title = document.createElement('span');
    title.className = 'file-name'; title.textContent = file.name; row.append(title);
    for (const format of ['jsonl', 'csv']) {
      const link = document.createElement('a');
      link.href = `/api/recordings/${encodeURIComponent(file.name)}?format=${format}`;
      link.download = ''; link.textContent = format.toUpperCase();
      link.setAttribute('aria-label', `下载 ${file.name}，${format.toUpperCase()} 格式`); row.append(link);
    }
    const size = document.createElement('span'); size.className = 'file-size'; size.textContent = `${((file.bytes || 0) / 1024).toFixed(1)} KB`; row.append(size); list.append(row);
  }
}

let tableRows = null;
function renderTable() {
  const rows = [], filter = $('data-filter').value, shown = displayState();
  for (const body of shown?.rigid_bodies || []) if (filter === 'all' || filter === 'rigid') rows.push({label: `刚体 R${body.id}`, item: body, data: body, joint: '—', space: '全局'});
  for (const joint of shown?.skeleton_joints || []) if (filter === 'all' || filter === 'skeleton_joint') rows.push({label: `骨骼关节 S${joint.id}`, item: joint, data: joint, joint: joint.id, space: '全局'});
  for (const skeleton of shown?.skeletons || []) if (filter === 'all' || filter === skeleton.mode) {
    for (const joint of skeleton.joints || []) rows.push({label: `${skeleton.mode === 'retarget' ? '重定向' : '原始'} ${skeleton.id}`, item: skeleton, data: joint, joint: joint.id, space: skeleton.position_space === 'global' ? '全局' : skeleton.position_space === 'local' ? '局部' : '未声明'});
  }
  for (const marker of shown?.markers || []) if (filter === 'all' || filter === 'markers') rows.push({label: `标记点 ${marker.id}`, item: marker, data: marker, joint: '—', space: '全局'});
  const count = Math.min(rows.length, 500);
  if (!tableRows || tableRows.length !== count) {
    const fragment = document.createDocumentFragment();
    tableRows = [];
    for (let i = 0; i < count; i++) {
      const tr = document.createElement('tr'), cells = [];
      for (let j = 0; j < 12; j++) { const cell = document.createElement('td'); cells.push(cell); tr.append(cell); }
      tableRows.push({tr, cells}); fragment.append(tr);
    }
    if (!count) { const tr = document.createElement('tr'), td = document.createElement('td'); td.colSpan = 12; td.className = 'empty-table'; td.textContent = '暂无接收数据'; tr.append(td); fragment.append(tr); }
    $('data-rows').replaceChildren(fragment);
  }
  for (let i = 0; i < count; i++) {
    const row = rows[i], {tr, cells} = tableRows[i], isStale = stale(row.item), className = isStale ? 'is-stale' : '';
    if (tr.className !== className) tr.className = className;
    const values = [row.label, row.joint, row.item.frame ?? '—', row.space, ...[0, 1, 2].map(i => row.data.pos?.[i] ?? '—'), ...[0, 1, 2, 3].map(i => row.data.quat?.[i] ?? '—'), row.data.detected === false ? '未检测' : isStale ? '过期 / 缓存' : '已更新'];
    for (let j = 0; j < values.length; j++) { const text = String(values[j]); if (cells[j].textContent !== text) cells[j].textContent = text; }
  }
  setText('rows-summary', rows.length > 500 ? `共 ${rows.length} 行 · 显示前 500 行，完整数据可录制导出` : `${rows.length} 行`);
}

function validPoint(point) { return Array.isArray(point) && point.length === 3 && point.every(Number.isFinite); }
function skeletonSource(shown = displayState()) {
  const choice = $('skeleton-view').value;
  if (choice === 'joints' || choice === 'source') return choice;
  const joints = (shown?.skeleton_joints || []).filter(joint => validPoint(joint.pos));
  if (joints.some(joint => !stale(joint))) return 'joints';
  if ((shown?.skeletons || []).some(skeleton => skeleton.mode === 'source' && skeleton.position_space === 'global' && !stale(skeleton) && (skeleton.joints || []).some(joint => joint.detected !== false && validPoint(joint.pos)))) return 'source';
  return joints.length ? 'joints' : 'source';
}

function allPoints() {
  const points = [], shown = displayState(), source = skeletonSource(shown);
  for (const body of ($('show-rigids').checked ? shown?.rigid_bodies : []) || []) if (validPoint(body.pos)) points.push(body.pos);
  for (const marker of shown?.markers || []) if (validPoint(marker.pos)) points.push(marker.pos);
  for (const joint of (source === 'joints' ? shown?.skeleton_joints : []) || []) if (validPoint(joint.pos)) points.push(joint.pos);
  for (const skeleton of (source === 'source' ? shown?.skeletons : []) || []) if (skeleton.mode === 'source' && skeleton.position_space === 'global') {
    for (const joint of skeleton.joints || []) if (joint.detected !== false && validPoint(joint.pos)) points.push(joint.pos);
  }
  return points;
}

function fit() {
  camera.pan = [0, 0];
  const points = allPoints();
  if (!points.length) { draw(); return; }
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const point of points) for (let i = 0; i < 3; i++) { min[i] = Math.min(min[i], point[i]); max[i] = Math.max(max[i], point[i]); }
  camera.center = min.map((v, i) => (v + max[i]) / 2);
  camera.radius = Math.max(Math.hypot(...max.map((v, i) => v - min[i])) / 2, .01);
  camera.zoom = 1; fitted = true; draw();
}

function rotateVector(v, q) {
  if (!Array.isArray(q) || q.length !== 4 || !q.every(Number.isFinite)) return null;
  const length = Math.hypot(...q); if (length < 1e-10) return null;
  const [x, y, z, w] = q.map(n => n / length), [a, b, c] = v;
  const tx = 2 * (y * c - z * b), ty = 2 * (z * a - x * c), tz = 2 * (x * b - y * a);
  return [a + w * tx + y * tz - z * ty, b + w * ty + z * tx - x * tz, c + w * tz + x * ty - y * tx];
}

function skeletonEdges(nodes, mode) {
  const scoped = nodes.filter(node => node.mode === mode), names = new Map();
  for (const node of scoped) {
    if (!names.has(node.name)) names.set(node.name, []);
    names.get(node.name).push(node);
  }
  const edges = [];
  for (const node of scoped) {
    const hand = (node.name || '').match(/^(.*)_(LeftHand|RightHand)(Thumb|Index|Middle|Ring|Pinky)([1-4])$/);
    if (!hand) { edges.push({id: node.id, parent: node.parent}); continue; }
    const [, model, side, finger, segment] = hand;
    // Some hand-only models retain full-body parent indices. Match named joints
    // within this exact model/side instead of using those invalid parent IDs.
    const wrists = [...(names.get(model + side) || []), ...(names.get(model + '_' + side) || [])];
    if (wrists.length !== 1 || names.get(node.name).length !== 1) continue;
    const parents = segment === '1' ? wrists : names.get(`${model}_${side}${finger}${Number(segment) - 1}`) || [];
    if (parents.length === 1) edges.push({id: node.id, parent: parents[0].id});
  }
  return edges;
}

let projectedMarkers = [];
function draw() {
  const shown = displayState(), source = skeletonSource(shown);
  setText('skeleton-source', `当前预览：${source === 'joints' ? '关节流（S ID）' : '源骨骼'} · 数值表保留所有流`);
  const width = canvas.clientWidth, height = canvas.clientHeight, dpr = Math.min(devicePixelRatio || 1, 2);
  if (!width || !height) return;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) { canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height);
  const scale = Math.min(width, height) * .35 / camera.radius * camera.zoom;
  const cy = Math.cos(camera.yaw), sy = Math.sin(camera.yaw), cp = Math.cos(camera.pitch), sp = Math.sin(camera.pitch);
  const project = point => {
    const [x, y, z] = point.map((n, i) => n - camera.center[i]);
    const rx = x * cy - z * sy, rz = x * sy + z * cy;
    return [width / 2 + rx * scale + camera.pan[0], height / 2 - (y * cp - rz * sp) * scale + camera.pan[1]];
  };
  const line = (a, b, color, size = 1) => { const pa = project(a), pb = project(b); ctx.strokeStyle = color; ctx.lineWidth = size; ctx.beginPath(); ctx.moveTo(...pa); ctx.lineTo(...pb); ctx.stroke(); };
  const dot = (point, color, radius = 3, label = '') => { const p = project(point); ctx.fillStyle = color; ctx.beginPath(); ctx.arc(...p, radius, 0, Math.PI * 2); ctx.fill(); if (label) { ctx.font = '10px Segoe UI, sans-serif'; ctx.fillText(label, p[0] + 7, p[1] - 7); } };
  const r = camera.radius, center = camera.center, floor = center[1] - r * .65;
  for (let i = -5; i <= 5; i++) {
    const offset = i * r / 3;
    line([center[0] + offset, floor, center[2] - r * 5 / 3], [center[0] + offset, floor, center[2] + r * 5 / 3], '#20314a');
    line([center[0] - r * 5 / 3, floor, center[2] + offset], [center[0] + r * 5 / 3, floor, center[2] + offset], '#20314a');
  }
  const nodes = shown?.hierarchy || [];
  for (const skeleton of (source === 'source' ? shown?.skeletons : []) || []) {
    if (skeleton.mode !== 'source' || skeleton.position_space !== 'global') continue;
    const color = stale(skeleton) ? '#697b93' : '#6e9fff';
    const joints = new Map((skeleton.joints || []).filter(j => j.detected !== false && validPoint(j.pos)).map(j => [String(j.id), j]));
    for (const node of nodes) {
      if (node.mode !== skeleton.mode || node.mode === 'unknown') continue;
      const child = joints.get(String(node.id)), parent = joints.get(String(node.parent));
      if (child && parent && child !== parent) line(parent.pos, child.pos, color, 2);
    }
    for (const joint of joints.values()) dot(joint.pos, color, 3);
  }
  const joints = new Map(((source === 'joints' ? shown?.skeleton_joints : []) || []).filter(joint => validPoint(joint.pos)).map(joint => [String(joint.id), joint]));
  for (const node of skeletonEdges(nodes, 'source')) {
    const child = joints.get(String(node.id)), parent = joints.get(String(node.parent));
    if (child && parent && child !== parent) line(parent.pos, child.pos, stale(child) || stale(parent) ? '#697b93' : '#6e9fff', 2);
  }
  for (const joint of joints.values()) dot(joint.pos, stale(joint) ? '#697b93' : '#6e9fff', 3, `S${joint.id}`);
  for (const body of ($('show-rigids').checked ? shown?.rigid_bodies : []) || []) {
    if (!validPoint(body.pos)) continue;
    const old = stale(body);
    dot(body.pos, old ? '#697b93' : '#61d2c4', 4, `R${body.id}`);
    [[1, 0, 0], [0, 1, 0], [0, 0, 1]].forEach((axis, i) => {
      const rotated = rotateVector(axis, body.quat);
      if (rotated) line(body.pos, body.pos.map((n, k) => n + rotated[k] * r * .2), old ? '#697b93' : ['#f5828b', '#6fd6b3', '#72a3ff'][i], 2);
    });
  }
  projectedMarkers = [];
  for (const marker of shown?.markers || []) if (validPoint(marker.pos)) {
    const label = typeof window !== 'undefined' ? window.handCalibration?.markerLabel(marker.id) || '' : '';
    dot(marker.pos, stale(marker) ? '#697b93' : '#f0c778', 3, label);
    if (label) projectedMarkers.push({id:marker.id, point:project(marker.pos)});
  }
  $('empty-preview').hidden = allPoints().length > 0;
  if (!$('empty-preview').hidden) {
    const onlyLocal = (shown?.skeletons || []).some(s => s.position_space === 'local');
    $('empty-preview').querySelector('strong').textContent = onlyLocal ? '已收到局部坐标骨骼' : '等待空间数据';
    $('empty-preview').querySelector('p').textContent = onlyLocal ? '请在下方查看原始数值；局部坐标不作为全局轨迹绘制' : '连接后显示全局骨骼、刚体和标记点';
  }
}

let drag = null;
canvas.addEventListener('pointerdown', event => {
  if (event.button !== 0 && event.button !== 2) return;
  event.preventDefault();
  drag = {x: event.clientX, y: event.clientY, startX:event.clientX, startY:event.clientY, moved:0, pan: event.button === 2 || event.shiftKey};
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (!drag) return;
  const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
  drag.moved += Math.hypot(dx, dy);
  if (drag.pan) { camera.pan[0] += dx; camera.pan[1] += dy; }
  else { camera.yaw += dx * .008; camera.pitch = Math.max(-1.45, Math.min(1.45, camera.pitch + dy * .008)); }
  drag.x = event.clientX; drag.y = event.clientY; draw();
});
canvas.addEventListener('contextmenu', event => event.preventDefault());
canvas.addEventListener('pointerup', () => {
  if (drag && !drag.pan && drag.moved < 5 && typeof window !== 'undefined' && window.handCalibration?.isMapping()) {
    const rect = canvas.getBoundingClientRect(), x = drag.startX - rect.left, y = drag.startY - rect.top;
    const nearest = projectedMarkers.map(m => ({id:m.id, distance:Math.hypot(m.point[0]-x, m.point[1]-y)})).sort((a,b) => a.distance-b.distance)[0];
    if (nearest && nearest.distance <= 16) window.handCalibration.pick(nearest.id);
  }
  drag = null;
});
canvas.addEventListener('pointercancel', () => { drag = null; });
canvas.addEventListener('lostpointercapture', () => { drag = null; });
canvas.addEventListener('wheel', event => { event.preventDefault(); camera.zoom = Math.max(.08, Math.min(30, camera.zoom * Math.exp(-event.deltaY * .001))); draw(); }, {passive: false});
canvas.addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', 'f', 'F'].includes(event.key)) return;
  event.preventDefault();
  if (event.shiftKey && event.key.startsWith('Arrow')) {
    if (event.key === 'ArrowLeft') camera.pan[0] -= 15;
    if (event.key === 'ArrowRight') camera.pan[0] += 15;
    if (event.key === 'ArrowUp') camera.pan[1] -= 15;
    if (event.key === 'ArrowDown') camera.pan[1] += 15;
    draw(); return;
  }
  if (event.key === 'ArrowLeft') camera.yaw -= .1;
  if (event.key === 'ArrowRight') camera.yaw += .1;
  if (event.key === 'ArrowUp') camera.pitch = Math.min(1.45, camera.pitch + .1);
  if (event.key === 'ArrowDown') camera.pitch = Math.max(-1.45, camera.pitch - .1);
  if (event.key === '+' || event.key === '=') camera.zoom = Math.min(30, camera.zoom * 1.1);
  if (event.key === '-') camera.zoom = Math.max(.08, camera.zoom / 1.1);
  if (event.key.toLowerCase() === 'f') fit();
  draw();
});
$('fit').addEventListener('click', fit);
$('skeleton-view').addEventListener('change', fit);
$('show-rigids').addEventListener('change', fit);
$('freeze').addEventListener('click', toggleFreeze);
new ResizeObserver(() => { if (viewVisible.scene) draw(); }).observe(canvas);
if (typeof IntersectionObserver !== 'undefined') {
  const visibility = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const key = entry.target === canvas ? 'scene' : 'table';
      viewVisible[key] = entry.isIntersecting;
      if (entry.isIntersecting && state) {
        if (key === 'table') renderTable();
        else { if (!fitted && allPoints().length) fit(); draw(); }
      }
    }
  });
  visibility.observe(canvas);
  visibility.observe($('data-rows'));
}
if (typeof window !== 'undefined') window.handPreview = {
  freezeLatest() { if (!state || !available) return null; frozen = {state:JSON.parse(JSON.stringify(state)), at:Date.now(), received:stateReceived, available}; render(); return frozen.state; },
  resume() { if (frozen) toggleFreeze(); },
  redraw:draw
};
poll();
