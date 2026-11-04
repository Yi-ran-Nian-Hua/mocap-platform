const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function browser() {
  const marks = {lines: 0, dots: 0, labels: []}, elements = new Map();
  const ctx = {setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() { marks.lines++; }, arc() { marks.dots++; }, fill() {}, fillText(text) { marks.labels.push(text); }};
  function node() { return {value: '', textContent: '', className: '', children: [], handlers: {}, lastElementChild: {textContent: ''}, clientWidth: 800, clientHeight: 400,
    addEventListener(name, fn) { this.handlers[name] = fn; }, append(...items) { this.children.push(...items.flatMap(item => item.fragment ? item.children : [item])); }, replaceChildren(...items) { this.children = []; this.append(...items); },
    setAttribute(name, value) { this[name] = value; }, getContext() { return ctx; }, setPointerCapture() {}, querySelector() { return node(); }}; }
  const get = id => { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); };
  get('data-filter').value = 'all';
  get('skeleton-view').value = 'auto';
  get('show-rigids').checked = true;
  const sandbox = {document: {getElementById: get, createElement: node, createDocumentFragment: () => Object.assign(node(), {fragment: true})}, ResizeObserver: class {observe() {}}, devicePixelRatio: 1, Date, performance, setTimeout, clearTimeout};
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\npoll\(\);\s*$/, '');
  vm.runInContext(source, sandbox);
  const run = source => vm.runInContext(source, sandbox);
  run('available = true; stateReceived = Date.now();');
  return {run, get, marks};
}

test('visible preview targets 60 Hz with separate panel updates and no overlapping requests', async () => {
  const {run} = browser();
  run(`clock=0; cost=6; panels=0; frames=0; pending=[];
    performance={now:()=>clock};
    setTimeout=(fn,delay)=>pending.push({fn,delay});
    request=async()=>{clock+=cost;return {running:true}};
    render=()=>panels++; renderTable=()=>{}; draw=()=>frames++;
  `);
  await run('poll()');
  assert.equal(run('panels'), 1);
  assert.ok(Math.abs(run('pending[0].delay') - (1000/60-6)) < .01);
  await run('clock=17;poll()');
  assert.equal(run('panels'), 1);
  assert.equal(run('frames'), 2);
  await run('clock=260;poll()');
  assert.equal(run('panels'), 2);
  await run('cost=25;poll()');
  assert.equal(run('pending.at(-1).delay'), 0);
  assert.equal(run('pending.length'), 4);
  run('request=()=>new Promise(resolve=>{finish=resolve})');
  const waiting = run('poll()');
  assert.equal(run('pending.length'), 4); // next request scheduled only after this one completes
  run('finish({running:true})');
  await waiting;
  assert.equal(run('pending.length'), 5);
  await run('request=async()=>{throw new Error("offline")};poll()');
  assert.equal(run('available'), false);
  assert.ok(run('pending.at(-1).delay') >= 250);
});

test('offscreen and frozen views stop expensive work while calibration can keep sampling', async () => {
  const {run} = browser();
  run(`clock=0; drawings=0; tables=0; delay=0;
    performance={now:()=>clock}; setTimeout=(fn,ms)=>{delay=ms};
    request=async()=>({running:true}); render=()=>{};
    renderTable=()=>tables++; draw=()=>drawings++;
    viewVisible.scene=false; viewVisible.table=false;
  `);
  await run('poll()');
  assert.equal(run('drawings'),0); assert.equal(run('tables'),0);
  assert.equal(run('delay'),250);
  await run('window={handCalibration:{isCapturing:()=>true,onState(){}}};poll()');
  assert.equal(run('delay'),50);
  await run('window=undefined; viewVisible.scene=true;viewVisible.table=true;frozen={};clock=300;poll()');
  assert.equal(run('drawings'),0); assert.equal(run('tables'),0);
  assert.equal(run('delay'),250);
  await run('frozen=null;document.hidden=true;clock=600;poll()');
  assert.equal(run('drawings'),0); assert.equal(run('tables'),0);
  assert.equal(run('delay'),1000);
});

test('boundary channels render R299 and S300 once, with no skeletal axes or duplicate global skeleton', () => {
  const {run, marks, get} = browser();
  run('fitted = true;');
  run(`state = {running: true, rigid_bodies: [{id: 299, pos: [0,0,0], quat: [0,0,0,1]}], skeleton_joints: [{id:300,pos:[1,1,1],quat:[0,0,0,1]},{id:301,pos:[2,2,2],quat:[0,0,0,1]}], skeletons: [{mode:'source',position_space:'global',joints:[{id:0,pos:[99,99,99]}]}], hierarchy:[{id:300,parent:-1,mode:'source'},{id:301,parent:300,mode:'source'}]}; draw(); render();`);
  assert.deepEqual(marks.labels.slice(0, 3).sort(), ['R299','S300','S301']);
  assert.equal(marks.dots, 6); // draw() plus render() draw(): three points each
  assert.equal(marks.lines, 52); // 22 grid + 3 rigid axes + 1 skeletal edge each
  assert.equal(run('allPoints().length'), 3);
  assert.equal(get('rigid-count').textContent, '1');
  assert.equal(get('rows-summary').textContent, '4 行'); // every stream remains accessible in the table
  get('data-filter').value = 'skeleton_joint'; run('renderTable()');
  assert.equal(get('rows-summary').textContent, '2 行');
});

test('without raw skeleton joints, global skeleton fallback remains available', () => {
  const {run, marks} = browser();
  run(`state={running:true,skeletons:[{mode:'source',position_space:'global',joints:[{id:0,pos:[0,0,0]},{id:1,pos:[1,1,1]}]}],hierarchy:[{id:1,parent:0,mode:'source'}]};draw();`);
  assert.equal(marks.dots, 2); assert.equal(marks.lines, 23);
});

test('rigid visibility hides points and axes, excludes fitting bounds, and preserves table data', () => {
  const {run, get, marks} = browser();
  run(`state={running:true,rigid_bodies:[{id:6,pos:[999,999,999],quat:[0,0,0,1]}],skeleton_joints:[{id:300,pos:[0,0,0]}],markers:[{id:1,pos:[1,1,1]}]};`);
  get('show-rigids').checked = false;
  run('draw();renderTable();');
  assert.equal(marks.dots,2); assert.equal(marks.lines,22);
  assert.ok(!marks.labels.includes('R6'));
  assert.equal(run('allPoints().length'),2);
  assert.equal(get('rows-summary').textContent,'3 行');
  run('toggleFreeze()');
  get('show-rigids').checked = true;
  get('show-rigids').handlers.change();
  assert.ok(marks.labels.includes('R6'));
  assert.equal(run('allPoints().length'),3);
});

test('all-data table includes a lone skeleton joint stream', () => {
  const {run, get} = browser();
  run("state={running:true,skeleton_joints:[{id:300,pos:[1,2,3]}]};renderTable();");
  assert.equal(get('rows-summary').textContent, '1 行');
});

test('auto falls back from stale skeleton joints to fresh source, and honors frozen ages', () => {
  const {run, marks} = browser();
  run("state={running:true,skeleton_joints:[{id:300,pos:[1,2,3],stale:true}],skeletons:[{mode:'source',position_space:'global',age_seconds:0,joints:[{id:0,pos:[7,8,9]}]}]};draw();");
  assert.equal(run('JSON.stringify(allPoints())'), '[[7,8,9]]');
  assert.equal(marks.dots, 1); assert.deepEqual(marks.labels, []);
  run('toggleFreeze();stateReceived-=10000;state.skeleton_joints[0].stale=false;');
  assert.equal(run('JSON.stringify(allPoints())'), '[[7,8,9]]');
});

test('explicit source view shows source despite fresh S joints, joint view shows only S joints', () => {
  const {run, get, marks} = browser();
  run("state={running:true,skeleton_joints:[{id:300,pos:[1,2,3]}],skeletons:[{mode:'source',position_space:'global',joints:[{id:0,pos:[7,8,9]}]}]};");
  get('skeleton-view').value = 'source';
  assert.equal(run('JSON.stringify(allPoints())'), '[[7,8,9]]');
  run('draw()'); assert.equal(marks.dots, 1); assert.deepEqual(marks.labels, []);
  get('skeleton-view').value = 'joints';
  assert.equal(run('JSON.stringify(allPoints())'), '[[1,2,3]]');
  run('draw()'); assert.equal(marks.dots, 2); assert.deepEqual(marks.labels, ['S300']);
});

test('freeze deep-copies display data and ages while live status and recording advance', () => {
  const {run, get} = browser();
  run(`state={running:true,connected:true,last_received:100,rigid_bodies:[{id:1,pos:[1,2,3],age_seconds:0}],recording:{active:true,events:1},event_rate:5}; toggleFreeze(); state.rigid_bodies[0].pos[0]=99;state.rigid_bodies.push({id:2,pos:[4,5,6]});state.recording.events=8;state.event_rate=9;state.last_received=200;stateReceived-=10000;available=false;render();`);
  assert.equal(run('displayState().rigid_bodies[0].pos[0]'), 1);
  assert.equal(run('stale(displayState().rigid_bodies[0])'), false);
  assert.equal(get('rigid-count').textContent, '1');
  assert.equal(get('record-events').textContent, '8');
  assert.equal(get('status').lastElementChild.textContent, '本地服务不可用');
  assert.equal(run('displayState().last_received'), 100);
  run('available=true;render()'); assert.equal(get('rate').textContent, '9');
  run('toggleFreeze()');
  assert.equal(run('displayState().rigid_bodies[0].pos[0]'), 99);
  assert.equal(get('rigid-count').textContent, '2');
});

test('right drag and Shift-left drag pan without changing orbit; fit resets pan', () => {
  const {run, get} = browser(), canvas = get('scene');
  run('state={running:true,rigid_bodies:[{id:1,pos:[1,2,3]}]};');
  const orbit = run('JSON.stringify([camera.yaw,camera.pitch])');
  const down = (button, shiftKey) => canvas.handlers.pointerdown({button,shiftKey,clientX:10,clientY:20,pointerId:1,preventDefault() {}});
  down(2, false); canvas.handlers.pointermove({clientX:35,clientY:30});canvas.handlers.pointerup();
  assert.equal(run('JSON.stringify(camera.pan)'), '[25,10]');
  down(0, true); canvas.handlers.pointermove({clientX:20,clientY:25});canvas.handlers.pointerup();
  assert.equal(run('JSON.stringify(camera.pan)'), '[35,15]');
  assert.equal(run('JSON.stringify([camera.yaw,camera.pitch])'), orbit);
  down(1, false); canvas.handlers.pointermove({clientX:100,clientY:100});
  assert.equal(run('JSON.stringify(camera.pan)'), '[35,15]');
  run('fit()'); assert.equal(run('JSON.stringify(camera.pan)'), '[0,0]');
});

test('hand finger bases attach to their named wrist instead of cyclic SDK parents', () => {
  const {run, marks} = browser();
  run(`handNodes = [
    {id:300,parent:-1,name:'L_HandLeftHand',mode:'source'},
    {id:450,parent:-1,name:'R_HandRightHand',mode:'source'}
  ];
  for (const [root, prefix, side, start, badParent] of [[300,'L_Hand_','LeftHand',301,311],[450,'R_Hand_','RightHand',471,465]]) {
    ['Thumb','Index','Middle','Ring','Pinky'].forEach((finger,f) => {
      for (let n=1;n<=4;n++) handNodes.push({id:start+f*4+n-1,parent:n===1?badParent:start+f*4+n-2,name:prefix+side+finger+n,mode:'source'});
    });
  }`);
  const edges = JSON.parse(run("JSON.stringify(skeletonEdges(handNodes, 'source'))"));
  for (const [root, start] of [[300,301],[450,471]]) {
    for (let finger=0;finger<5;finger++) for(let n=0;n<4;n++) {
      const id=start+finger*4+n;
      assert.ok(edges.some(e => e.id===id && e.parent===(n===0?root:id-1)), `wrong parent for ${id}`);
    }
  }
  assert.equal(run('handNodes.find(n=>n.id===309).parent'),311); // original data unchanged
  assert.ok(!edges.some(e=>e.id===309 && e.parent===311));
  run("state={running:true,hierarchy:handNodes,skeleton_joints:handNodes.map(n=>({id:n.id,pos:[n.id,0,0]}))};draw();");
  assert.equal(marks.lines, 62); // 22 grid lines plus exactly 20 edges per hand
});

test('ambiguous hand names are not guessed', () => {
  const {run} = browser();
  const edges = JSON.parse(run(`JSON.stringify(skeletonEdges([
    {id:300,name:'ActorLeftHand',mode:'source'},
    {id:450,name:'ActorLeftHand',mode:'source'},
    {id:301,parent:450,name:'Actor_LeftHandThumb1',mode:'source'}
  ],'source'))`));
  assert.ok(!edges.some(e=>e.id===301));
});

test('hand edges never bridge models, sides, modes or missing finger segments', () => {
  const {run} = browser();
  const edges = JSON.parse(run(`JSON.stringify(skeletonEdges([
    {id:300,parent:-1,name:'ActorALeftHand',mode:'source'},
    {id:600,parent:-1,name:'ActorBLeftHand',mode:'source'},
    {id:301,parent:999,name:'ActorA_LeftHandThumb1',mode:'source'},
    {id:601,parent:300,name:'ActorB_LeftHandThumb1',mode:'source'},
    {id:303,parent:301,name:'ActorA_LeftHandThumb3',mode:'source'},
    {id:321,parent:300,name:'ActorA_RightHandThumb1',mode:'source'},
    {id:800,parent:-1,name:'ActorARightHand',mode:'retarget'},
    {id:7,parent:6,name:'Elbow',mode:'source'}
  ],'source'))`));
  assert.ok(edges.some(e=>e.id===301 && e.parent===300));
  assert.ok(edges.some(e=>e.id===601 && e.parent===600));
  assert.ok(!edges.some(e=>e.id===303 || e.id===321));
  assert.ok(edges.some(e=>e.id===7 && e.parent===6));
});

test('table reuses rows and cells without rewriting unchanged text or stale classes', () => {
  const {run, get} = browser();
  run('state={running:true,rigid_bodies:[{id:1,frame:10,pos:[1.234567891,2,3],quat:[0,0,0,1]}]};renderTable();');
  const row = get('data-rows').children[0], cell = row.children[4];
  let text = cell.textContent, textWrites = 0, className = row.className, classWrites = 0;
  Object.defineProperty(cell, 'textContent', {get: () => text, set: value => { text = value; textWrites++; }});
  Object.defineProperty(row, 'className', {get: () => className, set: value => { className = value; classWrites++; }});
  run('renderTable();');
  assert.equal(get('data-rows').children[0], row);
  assert.equal(row.children[4], cell);
  assert.equal(cell.textContent, '1.234567891');
  assert.equal(textWrites, 0); assert.equal(classWrites, 0);
  run('state.rigid_bodies[0].pos[0]=9.876543219;state.rigid_bodies[0].stale=true;renderTable();');
  assert.equal(get('data-rows').children[0], row);
  assert.equal(cell.textContent, '9.876543219'); assert.equal(textWrites, 1);
  assert.equal(row.className, 'is-stale'); assert.equal(classWrites, 1);
  assert.equal(row.children[11].textContent, '过期 / 缓存');
  run('state.rigid_bodies[0].stale=false;renderTable();');
  assert.equal(row.className, ''); assert.equal(row.children[11].textContent, '已更新');
});

test('table filters resize rows, replace empty placeholders, and retain every channel', () => {
  const {run, get} = browser();
  run(`state={running:true,rigid_bodies:[{id:1,pos:[1,2,3]}],
    skeleton_joints:[{id:300,pos:[4,5,6]}],
    skeletons:[{id:2,mode:'source',position_space:'local',joints:[{id:7,pos:[7,8,9]}]},
      {id:3,mode:'retarget',joints:[{id:8,detected:false}]}],markers:[{id:4,pos:[10,11,12]}]};renderTable();`);
  assert.deepEqual(get('data-rows').children.map(row => row.children[0].textContent), ['刚体 R1','骨骼关节 S300','原始 2','重定向 3','标记点 4']);
  assert.equal(get('data-rows').children[2].children[3].textContent, '局部');
  assert.equal(get('data-rows').children[3].children[11].textContent, '未检测');
  get('data-filter').value = 'markers'; run('renderTable();');
  assert.equal(get('data-rows').children.length, 1);
  assert.equal(get('data-rows').children[0].children[0].textContent, '标记点 4');
  get('data-filter').value = 'rigid'; run('renderTable();');
  assert.equal(get('data-rows').children[0].children[0].textContent, '刚体 R1');
  run('state.rigid_bodies=[];renderTable();');
  const empty = get('data-rows').children[0];
  assert.equal(empty.children.length, 1); assert.equal(empty.children[0].colSpan, 12);
  assert.equal(empty.children[0].textContent, '暂无接收数据');
  run('renderTable();'); assert.equal(get('data-rows').children[0], empty);
  get('data-filter').value = 'all'; run('renderTable();');
  assert.equal(get('data-rows').children.length, 4);
  assert.equal(get('data-rows').children[0].children.length, 12);
});

test('table caps display at 500 rows and uses frozen values until resumed', () => {
  const {run, get} = browser();
  run('state={running:true,markers:Array.from({length:502},(_,id)=>({id,pos:[id,2,3]}))};renderTable();');
  assert.equal(get('data-rows').children.length, 500);
  assert.equal(get('rows-summary').textContent, '共 502 行 · 显示前 500 行，完整数据可录制导出');
  run('toggleFreeze();state.markers[0].pos[0]=77;renderTable();');
  assert.equal(get('data-rows').children[0].children[4].textContent, '0');
  run('toggleFreeze();');
  assert.equal(get('data-rows').children[0].children[4].textContent, '77');
});
