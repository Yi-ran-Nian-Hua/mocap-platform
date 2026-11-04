const test = require('node:test');
const assert = require('node:assert/strict');
const {CalibrationDraft, handJoints} = require('../static/calibration.js');

function state(t = 10) {
  return {running:true, connected:true, markers:[{id:8,pos:[1,2,3],timestamp:8,stale:true,age_seconds:2}],
    hierarchy:[{id:300,mode:'source',name:'L_HandLeftHand'},{id:301,mode:'source',name:'L_Hand_LeftHandIndex1'}, {id:302,mode:'retarget',name:'R_HandRightHand'}, {id:303,mode:'source',name:'Head'}],
    skeleton_joints:[300,301,302,303].map(id=>({id,pos:[id,0,0],timestamp:t,frame:t,source_time:t-1}))};
}
function draft() { const d = new CalibrationDraft(); d.addMapping(8,'left','index_1',state().markers); return d; }

test('mapping requires an actual selected marker and unique marker / anatomical slot', () => {
  const d = draft();
  assert.throws(()=>d.addMapping(99,'right','wrist',state().markers),/标记/);
  assert.throws(()=>d.addMapping(8,'right','wrist',state().markers),/已/);
  assert.throws(()=>d.addMapping(9,'left','index_1',[{id:9}]),/已/);
  assert.throws(()=>d.addMapping(9,'left','index_5',[{id:9}]),/位置/);
  d.removeMapping(8); assert.equal(d.mapping.length,0);
});
test('only source hierarchy named hands enter calibration', () => {
  assert.deepEqual(handJoints(state()).map(j=>j.id),[300,301]);
});
test('collects at most 20 live frames over two seconds, preserving marker content time and joints', () => {
  const d=draft(); d.start('open',state(),true,10000);
  for(let i=0;i<=200;i++) d.tick(state(10+i/100),true,10000+i*10);
  assert.equal(d.samples.length,1); assert.equal(d.samples[0].frames.length,20);
  const f=d.samples[0].frames[0];
  assert.equal(f.received_at,10); assert.equal(f.markers[0].timestamp,8);
  assert.equal(f.markers[0].stale,true); assert.equal(f.skeleton_joints.length,2);
  assert.equal(f.skeleton_joints[0].source_time,9);
});
test('cached snapshot and age changes never create duplicate samples', () => {
  const d=draft(); d.start('bent',state(),true,10000);
  for(let i=0;i<=20;i++) {const s=state();s.markers[0].age_seconds=i;d.tick(s,true,10000+i*100);}
  assert.equal(d.samples[0].frames.length,1);
});
test('disconnect, SDK errors and disappeared mapped markers discard the incomplete capture', () => {
  for(const fail of [s=>s.connected=false,s=>s.error='SDK',s=>s.markers=[],s=>s.running=false]) {
    const d=draft();d.start('open',state(),true,10000);d.tick(state(),true,10000);
    const bad=state(11);fail(bad);d.tick(bad,true,10100);
    assert.equal(d.capture,null);assert.equal(d.samples.length,0);assert.match(d.message,/中止/);
  }
});
test('immutable snapshots, explicit live availability and bounded drafts', () => {
  const d=draft(), s=state(); assert.throws(()=>d.start('open',s,false,10000),/连接/);
  d.start('open',s,true,10000);d.tick(s,true,10000);s.markers[0].pos[0]=999;
  assert.equal(d.capture.frames[0].markers[0].pos[0],1);
  assert.throws(()=>d.removeMapping(8),/采集/);
  d.tick(state(12),true,12000);d.samples=Array(9).fill(d.samples[0]);
  assert.throws(()=>d.start('open',state(),true,13000),/9/);
});
test('captured frames lock mappings until every sample is removed', () => {
  const d=draft(); d.start('open',state(),true,10000);d.tick(state(),true,10000);d.tick(state(12),true,12000);
  assert.throws(()=>d.addMapping(9,'right','wrist',[{id:9}]),/删除全部样本/);
  assert.throws(()=>d.removeMapping(8),/删除全部样本/);
  d.samples=[];d.removeMapping(8);d.addMapping(9,'right','wrist',[{id:9}]);
  assert.deepEqual(d.mapping,[{marker_id:9,side:'right',landmark:'wrist'}]);
});
test('requires a current source wrist for each mapped side and rejects stale required-side joints', () => {
  for(const change of [
    s=>s.skeleton_joints=s.skeleton_joints.filter(j=>j.id!==300),
    s=>s.hierarchy.forEach(j=>{j.name=j.name.replaceAll('LeftHand','RightHand');}),
    s=>s.skeleton_joints[0].stale=true,
    s=>s.skeleton_joints[1].age_seconds=99,
    s=>s.skeleton_joints[0].timestamp=7
  ]) {
    const d=draft(), s=state();change(s);
    assert.throws(()=>d.start('open',s,true,10000),/左手/);
  }
  const d=draft();d.start('open',state(),true,10000);d.tick(state(),true,10000);
  const old=state(10.1);old.skeleton_joints[1].stale=true;d.tick(old,true,10100);
  assert.equal(d.capture,null);assert.equal(d.samples.length,0);assert.match(d.message,/中止/);
});
test('stale optional opposite-hand joints are omitted while old marker content is preserved', () => {
  const d=draft(), s=state();
  s.hierarchy.push({id:450,mode:'source',name:'R_HandRightHand'});
  s.skeleton_joints.push({id:450,pos:[5,6,7],timestamp:1,stale:true});
  d.start('open',s,true,10000);d.tick(s,true,10000);
  assert.deepEqual(d.capture.frames[0].skeleton_joints.map(j=>j.id),[300,301]);
  assert.equal(d.capture.frames[0].markers[0].stale,true);
});
