(function () {
  'use strict';

  function makeGeometry(vertices, faces) {
    if (!Array.isArray(vertices) || !vertices.length || vertices.length > 65535 || !Array.isArray(faces) || !faces.length) throw Error('蒙皮网格为空或过大');
    if (vertices.some(v => !Array.isArray(v) || v.length !== 3 || !v.every(Number.isFinite))) throw Error('蒙皮坐标无效');
    if (faces.some(f => !Array.isArray(f) || f.length !== 3 || f.some(i => !Number.isInteger(i) || i < 0 || i >= vertices.length))) throw Error('蒙皮三角面索引无效');
    const positions = new Float32Array(vertices.flat()), normals = new Float32Array(positions.length);
    for (const [a,b,c] of faces) {
      const u = vertices[b].map((x,i) => x - vertices[a][i]), v = vertices[c].map((x,i) => x - vertices[a][i]);
      const n = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
      for (const j of [a,b,c]) for (let k=0;k<3;k++) normals[j*3+k] += n[k];
    }
    for (let i=0;i<normals.length;i+=3) {
      const length = Math.hypot(normals[i],normals[i+1],normals[i+2]) || 1;
      for (let k=0;k<3;k++) normals[i+k] /= length;
    }
    return {positions,normals,indices:new Uint16Array(faces.flat())};
  }

  function makeFrameGeometry(frame, topology) {
    return makeGeometry(frame.vertices,topology.faces_by_side?.[frame.side] || topology.faces || topology);
  }

  class FrameStore {
    constructor() { this.latest = {frames:[],at:0}; this.frozen = null; }
    update(status, now) {
      const frames=status.frames || [];
      const geometryKey=status.sequence === undefined ? frames : JSON.stringify([status.profile_id,status.sequence,frames.map(f=>[f.side,f.mode,f.vertices?.length || 0])]);
      this.latest = {frames,at:now,geometryKey};
    }
    setFrozen(value) { this.frozen = value ? this.latest : null; }
    visible(now) {
      const snapshot = this.frozen || this.latest;
      return snapshot.frames.map(frame => ({...frame,display_age:Number.isFinite(frame.age_seconds) ? Math.max(0,frame.age_seconds+(now-snapshot.at)/1000) : null}));
    }
  }

  function createPoller(task, hidden, schedule = setTimeout, cancel = clearTimeout) {
    let running = false, timer = null;
    async function tick() {
      if (!running) return;
      try { await task(); }
      finally { if (running) timer = schedule(tick, hidden() ? 1500 : 50); }
    }
    return {start() { if (running) return; running = true; return tick(); },stop() { running = false; cancel(timer); }};
  }

  if (typeof module !== 'undefined' && module.exports) { module.exports = {makeGeometry,makeFrameGeometry,FrameStore,createPoller}; return; }
  const $ = id => document.getElementById(id);
  const panel = $('hand-skin-panel');
  if (!panel) return;
  const canvas = $('skin-canvas'), store = new FrameStore();
  let renderer = null, status = {}, faces = null, topologyAttempt = -Infinity, busy = false, visible = true;
  let uploaded = null, uploadedFaces = null, autoFit = true, drawPending = false, transportError = '', graphicsError = '', actionError = '';
  let drawnState = '';

  async function api(path, body) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path,{cache:'no-store',signal:controller.signal,...(body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
      const result = await response.json();
      if (!response.ok) throw Error(result.error || result.message || `HTTP ${response.status}`);
      return result;
    } finally { clearTimeout(timeout); }
  }

  function selected(frame) {
    return (frame.side === 'left' ? $('skin-left').checked : $('skin-right').checked) && ($('skin-mode').value === 'both' || $('skin-mode').value === frame.mode);
  }
  function stale(frame) { return !frame.valid || frame.display_age === null || frame.display_age > 2 || !!transportError; }
  function viewState() {
    const snapshot=store.frozen || store.latest;
    return JSON.stringify([snapshot.geometryKey,!!store.frozen,!!faces,graphicsError,store.visible(performance.now()).map(f=>[selected(f),stale(f)])]);
  }
  function renderSoon(force = true) {
    if (!renderer || drawPending || document.hidden || !visible) return;
    if (!force && drawnState === viewState()) return;
    drawPending = true;
    requestAnimationFrame(() => { drawPending = false; draw(); });
  }
  function draw() {
    const frames = store.visible(performance.now());
    const snapshot = store.frozen || store.latest;
    if (faces && (uploaded !== snapshot.geometryKey || uploadedFaces !== faces)) {
      try { renderer.upload(frames, faces); uploaded = snapshot.geometryKey; uploadedFaces = faces; graphicsError=''; }
      catch (error) { graphicsError = error.message; renderer.upload([], faces); }
    }
    if (autoFit && frames.some(f => selected(f) && f.vertices?.length)) { renderer.fit(frames.filter(selected)); autoFit = false; }
    renderer.draw(frames, selected, stale);
    const hasMesh = faces && frames.some(f => selected(f) && f.vertices?.length);
    $('skin-empty').hidden = !!hasMesh && !graphicsError;
    $('skin-empty').textContent = graphicsError || (frames.some(f => f.vertices?.length) && !faces ? '正在加载模型三角面…' : '选择已保存档案，拟合后显示个人手部估计模型');
    drawnState=viewState();
  }
  function updateText() {
    const frames = store.visible(performance.now()), frozen = !!store.frozen;
    const phase = {idle:'尚未拟合',calibrating:'正在拟合个人手型',ready:status.running ? '实时姿态求解' : '姿态求解已暂停',error:'拟合失败',unavailable:'模型不可用'}[status.phase] || '正在读取状态';
    $('skin-state').textContent = frozen ? '画面已冻结 · 后台状态仍更新' : phase;
    $('skin-state').classList.toggle('skin-warning', frozen || !!transportError || status.phase === 'error' || status.phase === 'unavailable');
    $('skin-message').textContent = actionError || transportError || status.message || '请选择已保存的个人标定档案。';
    $('skin-message').classList.toggle('skin-warning', !!actionError || !!transportError || status.phase === 'error' || status.phase === 'unavailable');
    $('skin-profile-name').textContent = status.profile_name ? `当前拟合档案：${status.profile_name}` : '尚未载入个人手型';
    $('skin-freeze').textContent = frozen ? '恢复蒙皮实时显示' : '冻结蒙皮画面';
    $('skin-freeze').setAttribute('aria-pressed', String(frozen));
    $('skin-freeze').disabled = !frozen && !frames.some(f => f.vertices?.length);
    $('skin-pause').textContent = status.running ? '暂停姿态求解' : '恢复姿态求解';
    $('skin-pause').disabled = busy || status.phase !== 'ready';
    $('skin-calibrate').disabled = busy || status.running || status.phase === 'calibrating' || !$('skin-profiles').value;
    $('skin-calibrate').title = status.running ? '请先暂停姿态求解，再重新拟合手型' : '';
    $('skin-export').hidden = !status.profile_id || status.phase !== 'ready';
    const rows = frames.filter(selected).map(frame => {
      const row = document.createElement('tr');
      const state = frozen ? '冻结快照' : stale(frame) ? '无效 / 过期' : '有效';
      const values = [(frame.side === 'left' ? '左手' : '右手')+' · '+(frame.mode === 'skeleton' ? '骨骼驱动' : '标记点驱动'),state,frame.display_age === null ? '—' : frame.display_age.toFixed(2)+' s',Number.isFinite(frame.fit_ms) ? frame.fit_ms.toFixed(1)+' ms' : '—',Number.isFinite(frame.residual_rms_mm) ? frame.residual_rms_mm.toFixed(2)+' mm' : '—',frame.warning || '—'];
      for (const value of values) { const td = document.createElement('td'); td.textContent = value; row.append(td); }
      row.classList.toggle('skin-invalid',stale(frame));
      return row;
    });
    if (!rows.length) { const row=document.createElement('tr'), td=document.createElement('td'); td.colSpan=6; td.textContent='暂无所选方法的拟合结果'; row.append(td); rows.push(row); }
    $('skin-frame-rows').replaceChildren(...rows);
    $('skin-quality').hidden = !status.quality;
    $('skin-quality-text').textContent = status.quality ? JSON.stringify(status.quality,null,2) : '';
  }

  async function poll() {
    try {
      const next = await api('/api/hands');
      status = next; transportError = '';
      // Refresh validity/age on every response; geometryKey keeps GPU buffers.
      store.update(status, performance.now());
      if (!faces && performance.now()-topologyAttempt > 3000 && (status.phase === 'ready' || status.frames?.some(f => f.vertices?.length))) {
        topologyAttempt = performance.now();
        try { const topology = await api('/api/hands/topology'); if (topology.faces?.length) faces = topology; }
        catch (error) { transportError = '模型三角面加载失败：'+error.message; }
      }
    } catch (error) { transportError = '蒙皮服务读取失败：'+error.message; }
    updateText(); renderSoon(false);
  }
  async function refreshProfiles() {
    const button = $('skin-refresh'); button.disabled = true; actionError='';
    try {
      const result = await api('/api/calibrations'), select = $('skin-profiles'), previous = select.value;
      const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择已保存标定档案'; select.replaceChildren(empty);
      for (const item of result.profiles) { const option=document.createElement('option'); option.value=item.id; option.textContent=`${item.name} · ${item.sample_count} 组`; select.append(option); }
      if ([...select.options].some(o => o.value === previous)) select.value=previous;
    } catch (error) { actionError = '档案列表读取失败：'+error.message; }
    finally { button.disabled=false; updateText(); }
  }
  async function command(path, body) {
    busy=true; actionError=''; updateText();
    try { await api(path,body); }
    catch (error) { actionError=error.message; }
    finally { busy=false; updateText(); }
  }
  $('skin-refresh').addEventListener('click',refreshProfiles);
  $('skin-profiles').addEventListener('change',updateText);
  $('skin-calibrate').addEventListener('click',() => { autoFit=true; store.setFrozen(false); command('/api/hands/calibrate',{profile_id:$('skin-profiles').value}); });
  $('skin-pause').addEventListener('click',() => command(status.running ? '/api/hands/stop' : '/api/hands/start',{}));
  $('skin-freeze').addEventListener('click',() => { store.setFrozen(!store.frozen); updateText(); renderSoon(); });
  $('skin-fit').addEventListener('click',() => { renderer?.fit(store.visible(performance.now()).filter(selected)); renderSoon(); });
  for (const id of ['skin-mode','skin-left','skin-right']) $(id).addEventListener('change',() => { updateText(); renderSoon(); });

  function createRenderer() {
    const gl = canvas.getContext('webgl',{alpha:false,antialias:true});
    if (!gl) throw Error('无法启用 WebGL，请启用浏览器硬件加速后刷新。原始数据接收仍可使用。');
    const vertex = 'attribute vec3 aPosition; attribute vec3 aNormal; uniform mat4 uMatrix; uniform mat3 uRotation; varying vec3 vNormal; void main(){gl_Position=uMatrix*vec4(aPosition,1.0);vNormal=uRotation*aNormal;}';
    const fragment = 'precision mediump float; uniform vec3 uColor; varying vec3 vNormal; void main(){vec3 n=normalize(vNormal);float light=0.35+0.65*abs(dot(n,normalize(vec3(-0.4,0.6,1.0))));gl_FragColor=vec4(uColor*light,1.0);}';
    function shader(type, source) { const s=gl.createShader(type); gl.shaderSource(s,source); gl.compileShader(s); if (!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw Error('WebGL 着色器编译失败'); return s; }
    const program=gl.createProgram(), vs=shader(gl.VERTEX_SHADER,vertex), fs=shader(gl.FRAGMENT_SHADER,fragment);
    gl.attachShader(program,vs); gl.attachShader(program,fs); gl.linkProgram(program); gl.deleteShader(vs); gl.deleteShader(fs);
    if (!gl.getProgramParameter(program,gl.LINK_STATUS)) throw Error('WebGL 程序初始化失败');
    gl.useProgram(program); gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.clearColor(0.955,0.967,0.982,1);
    const position=gl.getAttribLocation(program,'aPosition'), normal=gl.getAttribLocation(program,'aNormal');
    const matrix=gl.getUniformLocation(program,'uMatrix'), rotation=gl.getUniformLocation(program,'uRotation'), color=gl.getUniformLocation(program,'uColor');
    let meshes=[], center=[0,0,0], radius=150, zoom=1, yaw=0.25, pitch=-0.2, pan=[0,0], drag=null;
    function fit(frames) {
      const points=frames.flatMap(f => f.vertices || []).filter(p => p.every(Number.isFinite)); if (!points.length) return;
      const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
      for (const p of points) for(let i=0;i<3;i++) { low[i]=Math.min(low[i],p[i]); high[i]=Math.max(high[i],p[i]); }
      center=low.map((v,i) => (v+high[i])/2); radius=Math.max(10,Math.hypot(...high.map((v,i) => v-low[i]))/2); zoom=1; pan=[0,0];
    }
    function buffer(target, data) { const b=gl.createBuffer(); gl.bindBuffer(target,b); gl.bufferData(target,data,gl.DYNAMIC_DRAW); return b; }
    function upload(frames, topology) {
      // Validate the entire update before replacing any existing GPU buffers.
      const data=frames.map(f => f.vertices?.length ? makeFrameGeometry(f,topology) : null);
      for (const m of meshes) if(m) for (const key of ['p','n','i']) gl.deleteBuffer(m[key]);
      meshes=data.map(m => m ? {p:buffer(gl.ARRAY_BUFFER,m.positions),n:buffer(gl.ARRAY_BUFFER,m.normals),i:buffer(gl.ELEMENT_ARRAY_BUFFER,m.indices),count:m.indices.length} : null);
    }
    function draw(frames, isSelected, isStale) {
      const scale=Math.min(window.devicePixelRatio || 1,2), width=Math.max(1,Math.round(canvas.clientWidth*scale)),height=Math.max(1,Math.round(canvas.clientHeight*scale));
      if(canvas.width!==width || canvas.height!==height) { canvas.width=width; canvas.height=height; }
      gl.viewport(0,0,width,height); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
      const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
      const right=[cy,0,-sy],up=[sp*sy,cp,sp*cy],depth=[cp*sy,-sp,cp*cy];
      const unit=zoom/(radius*1.25), sx=unit/Math.max(1,width/height),syScale=unit*Math.min(1,width/height),sz=-unit/20;
      const dot=(a,b) => a.reduce((s,v,i)=>s+v*b[i],0);
      gl.uniformMatrix4fv(matrix,false,new Float32Array([right[0]*sx,up[0]*syScale,depth[0]*sz,0,right[1]*sx,up[1]*syScale,depth[1]*sz,0,right[2]*sx,up[2]*syScale,depth[2]*sz,0,-dot(right,center)*sx+pan[0],-dot(up,center)*syScale+pan[1],-dot(depth,center)*sz,1]));
      gl.uniformMatrix3fv(rotation,false,new Float32Array([right[0],up[0],depth[0],right[1],up[1],depth[1],right[2],up[2],depth[2]]));
      frames.forEach((frame,index) => {
        const mesh=meshes[index]; if(!mesh || !isSelected(frame)) return;
        gl.uniform3fv(color,isStale(frame) ? [0.56,0.58,0.61] : frame.mode==='skeleton' ? [0.18,0.48,0.96] : [0.95,0.7,0.16]);
        for(const [attribute,key] of [[position,'p'],[normal,'n']]) { gl.bindBuffer(gl.ARRAY_BUFFER,mesh[key]); gl.enableVertexAttribArray(attribute); gl.vertexAttribPointer(attribute,3,gl.FLOAT,false,0,0); }
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,mesh.i); gl.drawElements(gl.TRIANGLES,mesh.count,gl.UNSIGNED_SHORT,0);
      });
    }
    canvas.addEventListener('contextmenu',event=>event.preventDefault());
    canvas.addEventListener('pointerdown',event => { if(event.button!==0 && event.button!==2) return; canvas.focus(); drag={x:event.clientX,y:event.clientY,pan:event.button===2 || event.shiftKey,id:event.pointerId}; canvas.setPointerCapture(event.pointerId); });
    canvas.addEventListener('pointermove',event => { if(!drag || drag.id!==event.pointerId) return; const dx=event.clientX-drag.x,dy=event.clientY-drag.y; if(drag.pan) { pan[0]+=dx/canvas.clientWidth*2;pan[1]-=dy/canvas.clientHeight*2; } else {yaw+=dx*0.008;pitch=Math.max(-1.55,Math.min(1.55,pitch+dy*0.008));} drag.x=event.clientX;drag.y=event.clientY;renderSoon(); });
    for (const event of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(event,()=>{drag=null;});
    canvas.addEventListener('wheel',event=>{event.preventDefault();zoom=Math.max(0.05,Math.min(30,zoom*Math.exp(-Math.max(-300,Math.min(300,event.deltaY))*0.001)));renderSoon();},{passive:false});
    canvas.addEventListener('keydown',event=>{
      if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','=','-','f','F'].includes(event.key)) return;
      event.preventDefault();
      if(event.key==='f'||event.key==='F') fit(store.visible(performance.now()).filter(selected));
      else if(event.key==='+'||event.key==='=') zoom=Math.min(30,zoom*1.1);
      else if(event.key==='-') zoom=Math.max(0.05,zoom/1.1);
      else {const horizontal=event.key==='ArrowLeft'||event.key==='ArrowRight',step=event.key==='ArrowLeft'||event.key==='ArrowDown' ? -0.1 : 0.1; if(event.shiftKey) pan[horizontal?0:1]+=step; else if(horizontal) yaw+=step; else pitch=Math.max(-1.55,Math.min(1.55,pitch+step));}
      renderSoon();
    });
    return {fit,upload,draw};
  }

  try { renderer=createRenderer(); } catch(error) { graphicsError=error.message; $('skin-empty').textContent=graphicsError; }
  canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();graphicsError='GPU 上下文已丢失，请刷新页面恢复蒙皮。';$('skin-empty').hidden=false;$('skin-empty').textContent=graphicsError;renderer=null;});
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(renderSoon).observe(canvas);
  else window.addEventListener('resize',renderSoon);
  if (typeof IntersectionObserver !== 'undefined') new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(visible) renderSoon();}).observe(panel);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden) renderSoon();});
  const poller=createPoller(poll,()=>document.hidden || !visible);
  window.addEventListener('pagehide',()=>poller.stop());
  window.addEventListener('pageshow',()=>poller.start());
  refreshProfiles(); poller.start(); renderSoon();
})();
