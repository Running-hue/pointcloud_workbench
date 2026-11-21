'use strict';
// Exercise the shipped event handlers with an in-memory DOM and API transport.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = path.resolve(__dirname, '..');
const clone = value => JSON.parse(JSON.stringify(value));
const frame = (id, extra = {}) => ({id, relative_dir:`pig_${id}/capture`, pig_id:id,
  capture_id:'capture', reviewed:true, status:'reviewed', missing:[], has_cloud:false, ...extra});
const dataset = {dataset_id:'one', source:'/raw', segmentation:'/masks', export_parent:'/out',
  suggested_name:'new-result', frames:[frame('1'),frame('2', {reviewed:false,status:'draft'})],
  results:'', pigs:2, reviewed:1, clouds:0, job:null};
const settle = () => new Promise(resolve => setImmediate(resolve));

async function setup(initial = dataset) {
  const elements = new Map();
  function element() {
    return {value:'', checked:false, disabled:false, hidden:false, textContent:'', children:[],
      classList:{toggle() {}, add() {}}, parentElement:{}, listeners:{},
      append(...items) { this.children.push(...items); }, replaceChildren() { this.children=[]; },
      setAttribute() {}, removeAttribute() {}, scrollIntoView() {}, focus() {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      showModal() { this.open=true; }, close() { this.open=false; this.listeners.close?.(); }};
  }
  for (const match of fs.readFileSync(path.join(app,'index.html'),'utf8').matchAll(/id="([^"]+)"/g)) {
    elements.set(match[1], element());
  }
  elements.get('filter').value='all';
  const requests=[];
  const sandbox = {APP_TOKEN:'test', URLSearchParams, Set, requestAnimationFrame:fn=>fn(),
    setTimeout:()=>1, clearTimeout() {}, window:{addEventListener() {}},
    document:{getElementById:id=>elements.get(id), createElement:element,
      querySelectorAll:()=>[], querySelector:selector=>selector.includes('scope') ? {value:'selected'} : null},
    fetch: async (url, options) => {
      const payload = options?.body ? JSON.parse(options.body) : undefined;
      requests.push({url,payload});
      const data = sandbox.reply ? await sandbox.reply(url,payload) : clone(initial);
      return {ok:true, status:200, json:async()=>data};
    }};
  const context=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(app,'workbench.js'),'utf8'),context);
  await settle();
  return {elements, requests, sandbox, run:code=>vm.runInContext(code,context)};
}

test('reload with no selection offers reviewed captures and never includes drafts by default', async () => {
  const ui=await setup({...dataset, job:{id:'failed', mode:'generate', status:'failed', done:2,
    total:2,success:0,errors:[],skipped:[],error:'输入未对齐'}});
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.match(ui.elements.get('generationHint').textContent,/已确认.*1 次/);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['1']);
  assert.equal(ui.elements.get('jobCurrent').textContent,'输入未对齐');
});

test('draft permission is explicit and the submitted scope follows the visible hint', async () => {
  const ui=await setup();
  ui.elements.get('draft').checked=true;
  ui.elements.get('draft').onchange();
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['1','2']);
  await ui.elements.get('generate').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/generate').payload.ids,['1','2']);
});

test('selected ineligible frames stay blocked with a reason, eligible selection enables retry', async () => {
  const ui=await setup();
  ui.run("selected = new Set(['2']); controls();");
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generationHint').textContent,/分割确认状态/);
  ui.run("selected = new Set(['1']); controls();");
  assert.equal(ui.elements.get('generate').disabled,false);
});

test('finish validates paths, clears old failure and preserves selection for unchanged inputs', async () => {
  const ui=await setup();
  ui.run("selected.add('1'); state.job={status:'failed'};");
  ui.sandbox.reply=async()=>clone({...dataset,dataset_id:'reloaded'});
  ui.elements.get('name').value='用户选择的成果名';
  ui.elements.get('directoryDialog').open=true;
  await ui.elements.get('finishDirectories').onclick();
  assert.equal(ui.elements.get('directoryDialog').open,false);
  assert.equal(ui.elements.get('jobPanel').hidden,true);
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.deepEqual(clone(ui.run('[...selected]')),['1']);
  assert.equal(ui.elements.get('name').value,'用户选择的成果名');
});

test('changed paths require loading and clear prior selections only after validation succeeds', async () => {
  const ui=await setup();
  ui.run("selected.add('1');");
  ui.elements.get('source').value='/new-raw';
  ui.elements.get('source').listeners.input();
  assert.equal(ui.elements.get('generate').disabled,true);
  ui.sandbox.reply=async()=>{ throw new Error('目录不匹配'); };
  ui.elements.get('directoryDialog').open=true;
  await ui.elements.get('finishDirectories').onclick();
  assert.equal(ui.elements.get('directoryDialog').open,true);
  assert.match(ui.elements.get('directoryMessage').textContent,/目录不匹配/);
  ui.sandbox.reply=async()=>clone({...dataset,source:'/new-raw',dataset_id:'two'});
  await ui.elements.get('finishDirectories').onclick();
  assert.deepEqual(clone(ui.run('[...selected]')),[]);
  assert.equal(ui.elements.get('generate').disabled,false);
});

test('late job polling cannot restore an old failure after directory reload', async () => {
  const ui=await setup();
  ui.run("state.job={id:'old',status:'running'};");
  let finish;
  ui.sandbox.reply=()=>new Promise(resolve=>{finish=resolve;});
  const poll=ui.run('poll()');
  ui.run(`accept(${JSON.stringify({...dataset,dataset_id:'new'})},true)`);
  finish({job:{id:'old',status:'failed'}});
  await poll;
  assert.equal(ui.run('state.job'),null);
  assert.equal(ui.elements.get('jobPanel').hidden,true);
  assert.equal(ui.elements.get('generate').disabled,false);
});

test('export plan lists only the segmentation modality for IR and RGB captures', async () => {
  for (const [mode, ply, other] of [['infrared','cloud_ir_mm.ply','cloud_rgb_mm.ply'],['rgb','cloud_rgb_mm.ply','cloud_ir_mm.ply']]) {
    const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{
      has_cloud:true,points:100,cloud_input_mode:mode,cloud_ply:ply,has_ir_file:true})]});
    ui.run('exportPlan()');
    assert.ok(ui.elements.get('exportTree').textContent.includes(ply));
    assert.ok(!ui.elements.get('exportTree').textContent.includes(other));
  }
});

test('opening results selects a generated capture when current input has no cloud', async () => {
  const ui=await setup();
  assert.equal(ui.run('current'),'1');
  const data={...dataset,clouds:1,results:'/other-results',frames:[frame('1'),frame('2',{
    has_cloud:true,points:100,cloud_input_mode:'infrared',cloud_ply:'cloud_ir_mm.ply'})]};
  ui.run(`accept(${JSON.stringify(data)})`);
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.elements.get('viewer').hidden,false);
});

test('results from another segmentation can be viewed but require reloading inputs before generation', async () => {
  const ui=await setup({...dataset,results_match_inputs:false});
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generationHint').textContent,/不是同一批输入/);
});

test('failed and manually skipped captures stay visible and never enter automatic generation', async () => {
  const frames=[frame('1'),frame('2',{reviewed:true,status:'reviewed',
    validation_errors:['分割清单与逐帧输入模式不一致']}),frame('3',{excluded:true})];
  const ui=await setup({...dataset,frames,pigs:3});
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['1']);
  assert.match(ui.elements.get('generationHint').textContent,/核对失败 1 次.*手动标记 1 次/);
  assert.match(ui.elements.get('showValidation').textContent,/核对失败 1/);
  ui.elements.get('filter').value='failed';
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['2']);
  ui.elements.get('filter').value='excluded';
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['3']);
  ui.run("selected = new Set(['1','2','3']); controls();");
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['1','2','3']);
});

test('marking current capture updates workspace through authenticated API without changing segmentation', async () => {
  const ui=await setup();
  ui.sandbox.reply=async(url,payload)=>{
    assert.equal(url,'/api/mark_skipped');
    assert.equal(payload.excluded,true);
    return clone({...dataset,frames:[frame('1',{excluded:true}),dataset.frames[1]]});
  };
  await ui.elements.get('toggleExcluded').onclick();
  const request=ui.requests.find(r=>r.url==='/api/mark_skipped');
  assert.deepEqual(request.payload,{dataset_id:'one',ids:['1'],excluded:true});
  assert.equal(ui.elements.get('toggleExcluded').textContent,'取消跳过标记');
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('message').textContent,/标记 1 次采集为跳过/);
});

test('camera and segmentation filters are independent and use recorded camera metadata', async () => {
  const rows=[frame('itof-rgb',{camera:{type:'itof'},input_mode:'rgb'}),
    frame('itof-ir',{camera:{type:'itof'},input_mode:'infrared'}),
    frame('stereo-rgb',{camera:{type:'stereo'},input_mode:'rgb',relative_dir:'itof_misleading/capture'}),
    frame('unknown',{input_mode:'rgb'})];
  const ui=await setup({...dataset,frames:rows});
  assert.match(ui.elements.get('cameraFilter').children.find(c=>c.value==='itof').textContent,/2/);
  ui.elements.get('cameraFilter').value='itof';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['itof-rgb','itof-ir']);
  ui.elements.get('modeFilter').value='infrared';ui.elements.get('modeFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['itof-ir']);
  assert.equal(ui.run('current'),'itof-ir');
  ui.elements.get('cameraFilter').value='stereo';ui.elements.get('modeFilter').value='rgb';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['stereo-rgb']);
});

test('without explicit selection generation follows the visible camera and segmentation scope', async () => {
  const ui=await setup({...dataset,frames:[frame('one',{camera:{type:'itof'},input_mode:'rgb'}),
    frame('two',{camera:{type:'stereo'},input_mode:'rgb'}),frame('three',{camera:{type:'itof'},input_mode:'infrared'})]});
  ui.elements.get('cameraFilter').value='itof';ui.elements.get('modeFilter').value='rgb';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['one']);
  await ui.elements.get('generate').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/generate').payload.ids,['one']);
});

test('empty filtered lists clear the viewer and refresh generation availability', async () => {
  const ui=await setup({...dataset,frames:[frame('one',{camera:{type:'itof'},input_mode:'rgb'})]});
  ui.elements.get('cameraFilter').value='stereo';ui.elements.get('cameraFilter').onchange();
  assert.equal(ui.run('current'),null);
  assert.equal(ui.elements.get('viewer').hidden,true);
  assert.equal(ui.elements.get('generate').disabled,true);
});

test('finished task details remain available from the compact footer', async () => {
  const ui=await setup({...dataset,job:{id:'done',mode:'generate',status:'completed_with_errors',
    total:2,done:2,success:1,current:'已保存',output:'/out/result',skipped:[],errors:[{relative_dir:'pig_2/capture',error:'配准失败'}]}});
  assert.equal(ui.elements.get('showJob').hidden,false);
  assert.match(ui.elements.get('showJob').textContent,/部分完成 2\/2.*1 项/);
  ui.elements.get('showJob').onclick();assert.equal(ui.elements.get('jobDialog').open,true);
  assert.match(ui.elements.get('jobErrors').children[0].textContent,/pig_2\/capture.*配准失败/);
  ui.elements.get('closeJob').onclick();assert.equal(ui.elements.get('jobDialog').open,false);
});
