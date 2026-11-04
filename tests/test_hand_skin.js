const test = require('node:test');
const assert = require('node:assert/strict');
const skin = require('../static/hand_skin.js');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('indexed mesh normals preserve coordinates, winding and reject unsafe geometry', () => {
  const vertices = [[0,0,0],[2,0,0],[0,3,0]];
  const mesh = skin.makeGeometry(vertices, [[0,1,2]]);
  assert.deepEqual(Array.from(mesh.positions), [0,0,0,2,0,0,0,3,0]);
  assert.deepEqual(Array.from(mesh.normals), [0,0,1,0,0,1,0,0,1]);
  assert.deepEqual(Array.from(mesh.indices), [0,1,2]);
  assert.throws(() => skin.makeGeometry(vertices, [[0,1,3]]));
  assert.throws(() => skin.makeGeometry([[NaN,0,0]], [[0,0,0]]));
});

test('each hand uses its own triangle winding with legacy topology fallback', () => {
  const vertices = [[0,0,0],[2,0,0],[0,3,0]];
  const topology = {faces:[[0,1,2]],faces_by_side:{left:[[0,2,1]],right:[[0,1,2]]}};
  const left = skin.makeFrameGeometry({side:'left',vertices},topology);
  const right = skin.makeFrameGeometry({side:'right',vertices},topology);
  assert.deepEqual(Array.from(left.indices),[0,2,1]);
  assert.equal(left.normals[2],-1);
  assert.deepEqual(Array.from(right.indices),[0,1,2]);
  assert.equal(right.normals[2],1);
  assert.deepEqual(Array.from(skin.makeFrameGeometry({side:'left',vertices},{faces:[[0,1,2]]}).indices),[0,1,2]);
});

test('freeze holds the selected frame, tracks latest input and retains source age', () => {
  const frames = new skin.FrameStore();
  const first = {frames:[{side:'left',mode:'skeleton',valid:true,age_seconds:0.2,vertices:[[1,2,3]]}]};
  frames.update(first, 1000);
  frames.setFrozen(true);
  frames.update({frames:[{side:'right',mode:'markers',valid:true,age_seconds:0,vertices:[[4,5,6]]}]}, 2000);
  assert.equal(frames.visible(3000)[0].side, 'left');
  assert.equal(frames.visible(3000)[0].display_age, 2.2);
  frames.setFrozen(false);
  assert.equal(frames.visible(3000)[0].mode, 'markers');
  assert.equal(frames.visible(3000)[0].display_age, 1);
});

test('invalid current frames never inherit last valid geometry', () => {
  const frames = new skin.FrameStore();
  frames.update({frames:[{valid:true,vertices:[[1,2,3]]}]}, 0);
  frames.update({frames:[{valid:false,warning:'missing'}]}, 20);
  assert.equal(frames.visible(30)[0].vertices, undefined);
  assert.equal(frames.visible(30)[0].valid, false);
});

test('unchanged geometry sequence still updates server validity and source age', () => {
  const frames = new skin.FrameStore();
  frames.update({sequence:1,frames:[{valid:true,age_seconds:0,vertices:[[1,2,3]]}]}, 0);
  const key = frames.latest.geometryKey;
  frames.update({sequence:1,frames:[{valid:false,age_seconds:0.1,warning:'paused',vertices:[[1,2,3]]}]}, 100);
  assert.equal(frames.latest.geometryKey, key);
  assert.equal(frames.visible(100)[0].valid, false);
  assert.equal(frames.visible(100)[0].display_age, 0.1);
  frames.update({sequence:1,frames:[{valid:false}]}, 200);
  assert.notEqual(frames.latest.geometryKey, key);
});

test('poller serialises slow requests, backs off while hidden and stops cleanly', async () => {
  let release, calls = 0, hidden = false;
  const queued = [];
  const poller = skin.createPoller(() => { calls++; return new Promise(resolve => { release = resolve; }); },
    () => hidden, (callback, delay) => { queued.push({callback, delay}); return queued.length; }, () => {});
  const pending = poller.start();
  poller.start();
  assert.equal(calls, 1);
  assert.equal(queued.length, 0);
  release(); await pending;
  assert.ok(queued[0].delay >= 50);
  hidden = true;
  const next = queued.shift().callback();
  assert.equal(calls, 2);
  release(); await next;
  assert.ok(queued[0].delay >= 1000);
  const callback = queued[0].callback;
  poller.stop();
  await callback();
  assert.equal(calls, 2);
});

function browser() {
  const elements=new Map(), calls=[], timers=new Map();
  let timer=0;
  function node() { return {value:'',checked:true,hidden:false,children:[],handlers:{},classList:{toggle(){}},
    addEventListener(name,fn){this.handlers[name]=fn;},setAttribute(name,value){this[name]=value;},
    append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;},
    get options(){return this.children;},getContext(){return null;}}; }
  const get=id=>{if(!elements.has(id)) elements.set(id,node());return elements.get(id);};
  get('skin-mode').value='both';
  const current={phase:'ready',running:true,profile_id:'participant-1',profile_name:'Example',sequence:1,frames:[]};
  const sandbox={document:{getElementById:get,createElement:node,addEventListener(){},hidden:false},window:{addEventListener(){}},
    performance:{now:()=>100},AbortController,console,
    setTimeout:(fn,delay)=>{timers.set(++timer,{fn,delay});return timer;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>{
      if(url==='/api/calibrations') return {profiles:[{id:'participant-1',name:'Example',sample_count:3}]};
      if(url==='/api/hands/topology') return {faces:[[0,1,2]]};
      return {...current};
    }};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/hand_skin.js'),'utf8'),sandbox);
  return {get,calls,timers,current,settle:()=>new Promise(setImmediate)};
}

test('browser reports unavailable GPU and sends explicit profile and empty pause/resume payloads', async () => {
  const b=browser(); await b.settle();
  assert.match(b.get('skin-empty').textContent,/WebGL/);
  assert.equal(b.get('skin-profiles').options[1].value,'participant-1');
  b.get('skin-profiles').value='participant-1';
  b.get('skin-profiles').handlers.change();
  assert.equal(b.get('skin-calibrate').disabled,true);
  b.current.running=false;
  await [...b.timers.values()].find(t=>t.delay===50).fn();
  assert.equal(b.get('skin-calibrate').disabled,false);
  b.get('skin-calibrate').handlers.click(); await b.settle();
  const fit=b.calls.find(c=>c.url==='/api/hands/calibrate');
  assert.equal(fit.options.method,'POST');
  assert.deepEqual(JSON.parse(fit.options.body),{profile_id:'participant-1'});
  b.current.running=true;
  await [...b.timers.values()].find(t=>t.delay===50).fn();
  await b.get('skin-pause').handlers.click();
  assert.equal(b.calls.at(-1).url,'/api/hands/stop');
  assert.deepEqual(JSON.parse(b.calls.at(-1).options.body),{});
  b.current.running=false;
  await [...b.timers.values()].find(t=>t.delay===50).fn();
  await b.get('skin-pause').handlers.click();
  assert.equal(b.calls.at(-1).url,'/api/hands/start');
  assert.deepEqual(JSON.parse(b.calls.at(-1).options.body),{});
});
