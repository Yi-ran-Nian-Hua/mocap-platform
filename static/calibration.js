'use strict';

(() => {
  const clone = value => JSON.parse(JSON.stringify(value));
  const point = value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
  const poses = {open: '张手', together: '并指', bent: '弯指'};
  const landmarks = ['wrist', ...['thumb', 'index', 'middle', 'ring', 'pinky'].flatMap(f => [1,2,3,4].map(n => `${f}_${n}`))];
  const handName = name => typeof name === 'string' && /(?:LeftHand|RightHand)(?:(?:Thumb|Index|Middle|Ring|Pinky)[1-4])?$/.test(name);
  const handSide = name => /LeftHand(?:(?:Thumb|Index|Middle|Ring|Pinky)[1-4])?$/.test(name) ? 'left' : 'right';
  const freshJoint = (joint, now) => !joint.stale && (joint.age_seconds || 0) <= 2 && Math.max(0, now / 1000 - joint.timestamp) <= 2;

  function handJoints(state) {
    const names = new Map();
    for (const node of state.hierarchy || []) if (node.mode === 'source' && handName(node.name)) {
      if (names.has(node.id)) names.set(node.id, null);
      else names.set(node.id, node.name);
    }
    return (state.skeleton_joints || []).filter(j => names.get(j.id) && point(j.pos) && Number.isFinite(j.timestamp)).map(j => {
      const item = {id:j.id, name:names.get(j.id), pos:[...j.pos], timestamp:j.timestamp};
      for (const key of ['quat','frame','source_time','stale','age_seconds']) if (j[key] !== undefined) item[key] = clone(j[key]);
      return item;
    });
  }

  class CalibrationDraft {
    constructor() { this.mapping = []; this.samples = []; this.capture = null; this.message = ''; this.dirty = false; }
    addMapping(markerId, side, landmark, markers) {
      if (this.capture) throw Error('采集中不能修改对应关系。');
      if (this.samples.length) throw Error('已有姿态样本，请先删除全部样本再修改对应关系。');
      if (!Number.isInteger(markerId) || !(markers || []).some(m => m.id === markerId)) throw Error('请选择快照中实际存在的标记点。');
      if (!['left','right'].includes(side) || !landmarks.includes(landmark)) throw Error('请选择有效的手侧和解剖位置。');
      if (this.mapping.some(m => m.marker_id === markerId)) throw Error('该标记点已分配，请先移除旧关系。');
      if (this.mapping.some(m => m.side === side && m.landmark === landmark)) throw Error('该解剖位置已有标记点。');
      this.mapping.push({marker_id:markerId, side, landmark}); this.dirty = true;
    }
    removeMapping(id) {
      if (this.capture) throw Error('采集中不能修改对应关系。');
      if (this.samples.length) throw Error('已有姿态样本，请先删除全部样本再修改对应关系。');
      this.mapping = this.mapping.filter(m => m.marker_id !== id); this.dirty = true;
    }
    checkLive(state, available, now) {
      if (!available || !state?.running || !state.connected || state.error) throw Error('连接不可用或 SDK 异常。');
      if (!this.mapping.length) throw Error('请先确认至少一个标记点对应关系。');
      for (const m of this.mapping) if (!(state.markers || []).some(p => p.id === m.marker_id && point(p.pos) && Number.isFinite(p.timestamp))) throw Error(`标记点 M${m.marker_id} 丢失。`);
      const joints = handJoints(state);
      for (const side of new Set(this.mapping.map(m => m.side))) {
        const required = joints.filter(j => handSide(j.name) === side);
        const wrists = required.filter(j => /(?:LeftHand|RightHand)$/.test(j.name));
        if (wrists.length !== 1 || required.some(j => !freshJoint(j, now))) {
          throw Error(`${side === 'left' ? '左手' : '右手'}源骨骼缺失、身份不明确或已过期。`);
        }
      }
      return joints.filter(j => freshJoint(j, now));
    }
    start(pose, state, available, now) {
      if (this.capture) throw Error('正在采集，请等待结束。');
      if (this.samples.length >= 9) throw Error('最多保留 9 组姿态，请先删除多余样本。');
      if (!poses[pose]) throw Error('无效姿态。');
      this.checkLive(state, available, now);
      this.capture = {pose, started:now, last:-Infinity, signature:'', frames:[]}; this.message = '正在采集实时数据…';
    }
    tick(state, available, now) {
      const c = this.capture; if (!c) return;
      let joints;
      try { joints = this.checkLive(state, available, now); } catch (error) { this.capture = null; this.message = `采集中止，本组未保存：${error.message}`; return; }
      if (now - c.started >= 2000) {
        if (c.frames.length) { this.samples.push({pose:c.pose, frames:c.frames}); this.dirty = true; }
        this.message = c.frames.length ? `已保存${poses[c.pose]} ${c.frames.length} 帧（去除重复缓存）；尚未拟合。` : '本次无新增帧，未保存。';
        this.capture = null; return;
      }
      if (now - c.last < 100 || c.frames.length >= 20) return;
      const ids = new Set(this.mapping.map(m => m.marker_id));
      const markers = (state.markers || []).filter(m => ids.has(m.id)).map(m => {
        const item = {id:m.id, pos:[...m.pos], timestamp:m.timestamp};
        for (const key of ['stale','age_seconds']) if (m[key] !== undefined) item[key] = m[key];
        return item;
      });
      // Ages change during caching, so only source content/timing identifies a new frame.
      const signature = JSON.stringify([markers.map(m => [m.id,m.pos,m.timestamp]), joints.map(j => [j.id,j.pos,j.quat,j.timestamp,j.frame,j.source_time])]);
      if (signature === c.signature) return;
      c.frames.push({received_at:now / 1000, markers, skeleton_joints:joints});
      c.signature = signature; c.last = now;
    }
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {CalibrationDraft, handJoints};
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const $ = id => document.getElementById(id), draft = new CalibrationDraft();
  let live = null, online = false, snapshot = [], mappingActive = false, lastRender = 0, httpBusy = false;
  const fields = ['palm_width','palm_thickness','hand_length','marker_radius','marker_base_thickness'];
  const labels = {wrist:'手腕',thumb:'拇指',index:'食指',middle:'中指',ring:'无名指',pinky:'小指'};
  const landmarkLabel = value => value === 'wrist' ? labels.wrist : `${labels[value.split('_')[0]]} ${value.split('_')[1]}`;
  function message(text) { $('cal-message').textContent = text; }
  function attempt(fn) { try { fn(); } catch (error) { message(error.message); } }
  function renderLists() {
    $('cal-map-add').disabled = !mappingActive || !!draft.capture || draft.samples.length > 0;
    $('cal-mappings').replaceChildren();
    for (const m of draft.mapping) {
      const li = document.createElement('li'), button = document.createElement('button');
      li.textContent = `M${m.marker_id} → ${m.side === 'left' ? '左' : '右'}手 · ${landmarkLabel(m.landmark)} `;
      button.type = 'button'; button.textContent = '移除'; button.disabled = !!draft.capture || draft.samples.length > 0;
      button.addEventListener('click', () => attempt(() => { draft.removeMapping(m.marker_id); renderLists(); }));
      li.append(button); $('cal-mappings').append(li);
    }
    $('cal-samples').replaceChildren();
    draft.samples.forEach((sample, index) => {
      const li = document.createElement('li'), button = document.createElement('button');
      li.textContent = `${poses[sample.pose]} · ${sample.frames.length} 帧 `; button.type = 'button'; button.textContent = '删除'; button.disabled = !!draft.capture;
      button.addEventListener('click', () => { if (draft.capture) return; draft.samples.splice(index,1); draft.dirty = true; renderLists(); });
      li.append(button); $('cal-samples').append(li);
    });
    $('cal-summary').textContent = `${draft.mapping.length} 个对应关系 · ${draft.samples.length} 组姿态${draft.dirty ? ' · 有未保存更改' : ''}${draft.samples.length ? ' · 修改对应关系前须先删除全部样本' : ''}`;
  }
  function endMapping() { mappingActive = false; $('cal-map-add').disabled = true; $('cal-map-end').disabled = true; window.handPreview?.redraw(); }
  function renderStatus(now) {
    $('cal-capture').disabled = !!draft.capture || httpBusy || !online;
    $('cal-snapshot').disabled = !!draft.capture || !online;
    $('cal-save').disabled = !!draft.capture || httpBusy;
    $('cal-load').disabled = !!draft.capture || httpBusy;
    if (draft.capture) message(`正在采集${poses[draft.capture.pose]}：${draft.capture.frames.length} 帧 · ${Math.max(0, 2-(now-draft.capture.started)/1000).toFixed(1)} 秒`);
  }
  window.handCalibration = {
    isCapturing() { return !!draft.capture; },
    onState(state, available, now) {
      live = state; online = available && !!state?.running && !!state.connected && !state.error;
      const before = draft.capture; draft.tick(state, available, now);
      if (before && !draft.capture) { renderLists(); message(draft.message); }
      if (now - lastRender >= 250 || before !== draft.capture) { renderStatus(now); lastRender = now; }
    },
    markerLabel(id) { return mappingActive ? `M${id}` : ''; },
    isMapping() { return mappingActive; },
    pick(id) { if (mappingActive && snapshot.some(m => m.id === id)) { $('cal-marker').value = String(id); message(`已选 M${id}，请选择手侧与解剖位置，再确认对应。`); } },
    onUnfreeze() { if (mappingActive) { endMapping(); message('已恢复实时预览；重新冻结后可继续标注。'); } }
  };
  for (const value of landmarks) { const option = document.createElement('option'); option.value = value; option.textContent = landmarkLabel(value); $('cal-landmark').append(option); }
  $('cal-snapshot').addEventListener('click', () => attempt(() => {
    if (!online) throw Error('请先连接设备。');
    const shown = window.handPreview?.freezeLatest();
    snapshot = clone((shown?.markers || []).filter(m => point(m.pos)));
    if (!snapshot.length) throw Error('当前实时帧没有可标注的标记点。');
    mappingActive = true; $('cal-marker').replaceChildren();
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '点击黄点或选择 ID'; $('cal-marker').append(placeholder);
    for (const m of snapshot) { const option = document.createElement('option'); option.value = String(m.id); option.textContent = `M${m.id}`; $('cal-marker').append(option); }
    $('cal-map-add').disabled = draft.samples.length > 0; $('cal-map-end').disabled = false;
    message('标注快照已冻结。点击黄点或选择 ID，再手动确认解剖位置。'); window.handPreview.redraw();
  }));
  $('cal-map-end').addEventListener('click', () => { endMapping(); message('标注已结束，快照仍冻结；采集将明确恢复实时预览。'); });
  $('cal-map-add').addEventListener('click', () => attempt(() => {
    if (!mappingActive || $('cal-marker').value === '') throw Error('请先选择实际标记点。');
    draft.addMapping(Number($('cal-marker').value), $('cal-side').value, $('cal-landmark').value, snapshot);
    renderLists(); message('对应关系已加入；手侧与解剖位置由人工确认。');
  }));
  $('cal-capture').addEventListener('click', () => attempt(() => {
    if (!$('cal-observed').checked) throw Error('请先确认已观察到本人当前姿态与标记点。');
    draft.start($('cal-pose').value, live, online, Date.now()); endMapping(); window.handPreview?.resume();
    $('cal-observed').checked = false; renderLists(); renderStatus(Date.now());
  }));
  $('cal-name').addEventListener('input', () => { draft.dirty = true; renderLists(); });
  for (const side of ['left','right']) for (const key of fields) $(`cal-${side}-${key}`).addEventListener('input', () => { draft.dirty = true; renderLists(); });
  function profile() {
    const name = $('cal-name').value.trim(), measurements = {};
    if (!name || name.length > 80) throw Error('请输入 1–80 字的受试者名称。');
    if (!draft.mapping.length || !draft.samples.some(s => s.frames.length)) throw Error('至少确认一个对应关系并采集一组实时姿态后才能保存。');
    for (const side of ['left','right']) {
      const values = {};
      for (const key of fields) { const input = $(`cal-${side}-${key}`); if (input.value.trim() === '') continue; const n = Number(input.value); if (!Number.isFinite(n) || (['marker_radius','marker_base_thickness'].includes(key) ? n < 0 : n <= 0)) throw Error('实测尺寸须为正数，标记球半径与底座厚度可为 0。'); values[key] = n; }
      if (Object.keys(values).length) measurements[side] = values;
    }
    return {name, units:'mm', mapping:clone(draft.mapping), measurements, samples:clone(draft.samples)};
  }
  async function api(path, data) {
    const response = await fetch(path, data ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)} : {cache:'no-store'});
    const result = await response.json(); if (!response.ok) throw Error(result.error || '标定请求失败。'); return result;
  }
  async function operation(fn) {
    if (httpBusy) return; httpBusy = true; renderStatus(Date.now());
    try { await fn(); } catch (error) { message(error.message); }
    finally { httpBusy = false; renderStatus(Date.now()); }
  }
  function exportLink(id) { $('cal-export').href = `/api/calibrations/${encodeURIComponent(id)}`; $('cal-export').hidden = false; }
  async function refresh() {
    const result = await api('/api/calibrations'), select = $('cal-profiles'), previous = select.value; select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择已保存档案'; select.append(empty);
    for (const item of result.profiles) { const option = document.createElement('option'); option.value = item.id; option.textContent = `${item.name} · ${item.sample_count} 组`; select.append(option); }
    if ([...select.options].some(o => o.value === previous)) select.value = previous;
  }
  $('cal-refresh').addEventListener('click', () => operation(async () => { await refresh(); message('档案列表已刷新，当前草稿未更改。'); }));
  $('cal-save').addEventListener('click', () => operation(async () => {
    const data = profile(), result = await api('/api/calibrations', data); exportLink(result.id);
    // Inputs can still change while a save is in flight; only clear the saved version.
    if (JSON.stringify(profile()) === JSON.stringify(data)) draft.dirty = false;
    renderLists(); message('已保存个人标定草稿 · 尚未执行手型与姿态拟合。'); await refresh(); $('cal-profiles').value = result.id;
  }));
  $('cal-load').addEventListener('click', () => operation(async () => {
    const id = $('cal-profiles').value; if (!id) throw Error('请选择已保存档案。');
    if (draft.dirty && !$('cal-replace').checked) throw Error('当前有未保存更改，请勾选允许替换当前草稿后再加载。');
    const before = JSON.stringify({data:[$('cal-name').value,...['left','right'].flatMap(side => fields.map(key => $(`cal-${side}-${key}`).value))],mapping:draft.mapping,samples:draft.samples});
    const data = await api(`/api/calibrations/${encodeURIComponent(id)}`);
    const after = JSON.stringify({data:[$('cal-name').value,...['left','right'].flatMap(side => fields.map(key => $(`cal-${side}-${key}`).value))],mapping:draft.mapping,samples:draft.samples});
    if (before !== after) throw Error('加载期间草稿已修改，已保留本地输入，请重新加载。');
    draft.mapping = clone(data.mapping); draft.samples = clone(data.samples); draft.dirty = false; $('cal-name').value = data.name;
    for (const side of ['left','right']) for (const key of fields) $(`cal-${side}-${key}`).value = data.measurements[side]?.[key] ?? '';
    endMapping(); $('cal-replace').checked = false; exportLink(data.id); renderLists(); message('档案已加载 · 尚未执行手型与姿态拟合。');
  }));
  window.addEventListener('beforeunload', event => { if (draft.dirty || draft.capture) { event.preventDefault(); event.returnValue = ''; } });
  renderLists(); renderStatus(Date.now());
})();
