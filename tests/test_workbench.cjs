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
      contentWindow:{}, setAttribute(name, value) {this[name]=value;}, removeAttribute(name) {delete this[name];}, scrollIntoView() {}, focus() {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      showModal() { this.open=true; }, close() { this.open=false; this.listeners.close?.(); }};
  }
  for (const match of fs.readFileSync(path.join(app,'index.html'),'utf8').matchAll(/id="([^"]+)"/g)) {
    elements.set(match[1], element());
  }
  elements.get('filter').value='all';
  for (const id of ['exportKeep','exportPending','exportReject']) elements.get(id).checked=true;
  const requests=[];
  const listeners = {};
  const sandbox = {APP_TOKEN:'test', URLSearchParams, Set, requestAnimationFrame:fn=>fn(), location:{origin:'http://localhost'},
    setTimeout:()=>1, clearTimeout() {}, window:{addEventListener(name, fn) {listeners[name]=fn;}},
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
  const notify = data => listeners.message({origin:'http://localhost',source:elements.get('alignmentEditor').contentWindow,
    data:{type:'pointcloud-alignment',dataset_id:'one',id:'1',...data}});
  return {elements, requests, sandbox, listeners, notify, run:code=>vm.runInContext(code,context)};
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

test('aligned exports open in the body frame and can switch back to the camera frame', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/aligned',frames:[frame('1',{
    has_cloud:true,has_alignment:true,points:100,cloud_input_mode:'infrared'})]});
  assert.equal(ui.elements.get('coordinateFrame').value,'body');
  assert.match(ui.elements.get('viewer').src,/body_preview.html/);
  ui.elements.get('coordinateFrame').value='camera';ui.elements.get('coordinateFrame').onchange();
  assert.match(ui.elements.get('viewer').src,/file=preview.html/);
  ui.run("state.frames[0].has_alignment=false; renderViewer();");
  assert.equal(ui.elements.get('bodyCoordinate').disabled,true);
  assert.equal(ui.elements.get('coordinateFrame').value,'camera');
});

test('coordinate alignment stays in the workbench and switching views keeps the editor', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.sandbox.window.open=()=>assert.fail('must not open a new tab');
  ui.elements.get('alignCurrent').onclick();
  const url=ui.elements.get('alignmentEditor').src;
  assert.match(url,/^\/alignment\?/);assert.match(url,/dataset_id=one/);assert.match(url,/id=1/);
  assert.match(url,/embedded=1/);
  assert.equal(ui.elements.get('alignmentEditor').hidden,false);
  assert.equal(ui.elements.get('viewer').hidden,true);
  await ui.notify({action:'busy',busy:false});
  ui.elements.get('viewCloud').onclick();
  assert.equal(ui.elements.get('viewer').hidden,false);
  assert.equal(ui.elements.get('alignmentEditor').src,url);
  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.elements.get('alignmentEditor').src,url);
});

test('saving an inline alignment refreshes body viewing without replacing the capture list', async () => {
  const initial={...dataset,clouds:2,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})]};
  const ui=await setup(initial);ui.elements.get('alignCurrent').onclick();
  await ui.notify({action:'busy',busy:false});
  const url=ui.elements.get('alignmentEditor').src;
  ui.sandbox.reply=async()=>({...initial,frames:[{...initial.frames[0],has_alignment:true,alignment_revision:'new'},initial.frames[1]]});
  await ui.notify({action:'saved',output:'/aligned'});
  assert.equal(ui.run('state.frames.length'),2);
  assert.equal(ui.run('state.results'),'/clouds');
  assert.equal(ui.elements.get('coordinateFrame').value,'body');
  assert.equal(ui.elements.get('alignmentEditor').src,url);
  assert.match(ui.elements.get('viewer').src,/body_preview.html.*alignment=new/);
  assert.equal(ui.elements.has('nextAlignment'),false);
  ui.run("selectCapture('2')");
  assert.equal(ui.run('current'),'2');
  assert.match(ui.elements.get('alignmentEditor').src,/id=2/);
});

test('an editor load failure releases action locks and allows retry', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.run('alignmentBusy'),true);
  ui.elements.get('alignmentEditor').onerror();
  assert.equal(ui.run('alignmentBusy'),false);
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.equal(ui.elements.get('viewer').hidden,false);
  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.elements.get('alignmentEditor').hidden,false);
});

test('only the current inline editor may refresh or unlock the workbench', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.elements.get('alignCurrent').onclick();
  await ui.notify({action:'busy',busy:false,id:'old'});
  assert.equal(ui.run('alignmentBusy'),true);
  await ui.listeners.message({origin:'https://other.test',source:ui.elements.get('alignmentEditor').contentWindow,
    data:{type:'pointcloud-alignment',action:'busy',busy:false,id:'1',dataset_id:'one'}});
  assert.equal(ui.run('alignmentBusy'),true);
  await ui.notify({action:'busy',busy:false});
  assert.equal(ui.run('alignmentBusy'),false);
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

test('cloud quality selection is authenticated and separate from segmentation review', async () => {
  const row=frame('1',{has_cloud:true,points:100,cloud_decision:'pending',reviewed:false,status:'draft'});
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[row]});
  ui.sandbox.reply=async(url,payload)=>({...dataset,clouds:1,results:'/clouds',frames:[{...row,cloud_decision:payload.decision}]});
  await ui.elements.get('cloudReject').onclick();
  assert.equal(ui.elements.get('cloudReject')['aria-pressed'],'true');
  assert.equal(ui.elements.get('cloudPending')['aria-pressed'],'false');
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/review_cloud').payload,{dataset_id:'one',id:'1',decision:'reject'});
  assert.equal(ui.run('state.frames[0].reviewed'),false);
  assert.equal(ui.run('state.frames[0].cloud_decision'),'reject');
  ui.elements.get('filter').value='reject';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['1']);
});

test('aligned export includes all three selected review states in separate folders', async () => {
  const frames=[frame('1',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'keep'}),
    frame('2',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'pending'}),
    frame('3',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'reject'}),
    frame('4',{has_cloud:true,points:100,has_alignment:false,cloud_decision:'keep'})];
  const ui=await setup({...dataset,frames,clouds:4,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.equal(ui.elements.has('exportAligned'),false);
  assert.equal(ui.elements.get('exportContent').value,'aligned');
  assert.equal(ui.elements.get('exportDialog').open,true);
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','2','3']);
  for (const label of ['保留/','待定/','淘汰/']) assert.ok(ui.elements.get('exportTree').textContent.includes(label));
  ui.elements.get('exportPending').checked=false;ui.elements.get('exportPending').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','3']);
  ui.elements.get('exportParent').value='/chosen';ui.elements.get('exportName').value='aligned-output';
  await ui.elements.get('confirmExport').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/export').payload,
    {dataset_id:'one',ids:['1','3'],scope_ids:['1','2','3','4'],parent:'/chosen',name:'aligned-output',aligned_only:true,decisions:['keep','reject'],group_by_decision:true});
});

test('full export also groups pending and rejected clouds and enforces review checkboxes', async () => {
  const frames=[frame('1',{has_cloud:true,has_alignment:true,cloud_decision:'keep'}),
    frame('2',{has_cloud:true,has_alignment:true,cloud_decision:'pending'}),
    frame('3',{has_cloud:true,cloud_decision:'reject'}),frame('4',{has_cloud:true,cloud_decision:'pending'})];
  const ui=await setup({...dataset,frames,clouds:4,results:'/clouds'});
  ui.elements.get('export').onclick();
  ui.elements.get('exportContent').value='all';ui.elements.get('exportContent').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','2','3','4']);
  await ui.elements.get('confirmExport').onclick();
  const payload=ui.requests.find(r=>r.url==='/api/export').payload;
  assert.equal(payload.aligned_only,false);assert.equal(payload.group_by_decision,true);
  assert.deepEqual(payload.decisions,['keep','pending','reject']);
  ui.elements.get('exportContent').value='aligned';ui.elements.get('exportPending').checked=false;
  ui.elements.get('exportContent').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1']);
  for (const id of ['exportKeep','exportPending','exportReject']) ui.elements.get(id).checked=false;
  ui.elements.get('exportKeep').onchange();
  assert.equal(ui.elements.get('confirmExport').disabled,true);
  assert.match(ui.elements.get('exportEligibility').textContent,/至少勾选/);
});

test('alignment hides input skip action while pending and reject remain directly editable', async () => {
  const row=frame('1',{has_cloud:true,points:100});
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[row]});
  assert.equal(ui.elements.get('toggleExcluded').hidden,false);
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  assert.equal(ui.elements.get('toggleExcluded').hidden,true);
  ui.sandbox.reply=async(url,payload)=>({...dataset,clouds:1,results:'/clouds',frames:[{...row,cloud_decision:payload.decision}]});
  for (const decision of ['reject','pending']) {
    await ui.elements.get({reject:'cloudReject',pending:'cloudPending'}[decision]).onclick();
    assert.equal(ui.run('state.frames[0].cloud_decision'),decision);
    assert.equal(ui.run('state.frames[0].has_alignment'),undefined);
  }
  ui.elements.get('viewCloud').onclick();
  assert.equal(ui.elements.get('toggleExcluded').hidden,false);
});

test('a failed export keeps its destination and error visible in the export dialog', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.elements.get('export').onclick();ui.elements.get('exportName').value='existing';
  ui.sandbox.reply=async()=>{throw new Error('成果文件夹已存在')};
  await ui.elements.get('confirmExport').onclick();
  assert.equal(ui.elements.get('exportDialog').open,true);
  assert.equal(ui.elements.get('exportName').value,'existing');
  assert.match(ui.elements.get('exportMessage').textContent,/已存在/);
  assert.equal(ui.elements.get('confirmExport').disabled,false);
});

test('quality buttons auto advance only after a successful save, skipping aligned and rejected captures', async () => {
  const frames=[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100,has_alignment:true}),
    frame('3',{has_cloud:true,points:100,cloud_decision:'reject'}),frame('4',{has_cloud:true,points:100})];
  const ui=await setup({...dataset,frames,clouds:4,results:'/clouds'});
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  ui.elements.get('autoNextAlignment').checked=true;
  let finish;
  ui.sandbox.reply=()=>new Promise(resolve=>{finish=resolve;});
  const save=ui.elements.get('cloudPending').onclick();
  assert.equal(ui.run('current'),'1');
  finish({...dataset,frames,clouds:4,results:'/clouds'});await save;
  assert.equal(ui.run('current'),'4');
  assert.match(ui.elements.get('alignmentEditor').src,/id=4/);
  await ui.notify({action:'busy',busy:false,id:'4'});
  ui.sandbox.reply=async()=>({...dataset,frames,clouds:4,results:'/clouds'});
  await ui.elements.get('cloudPending').onclick();
  assert.equal(ui.run('current'),'4');
  assert.match(ui.elements.get('message').textContent,/后面已没有/);
});

test('quality save failure never advances or changes the highlighted decision', async () => {
  const frames=[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})];
  const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  ui.elements.get('autoNextAlignment').checked=true;
  ui.sandbox.reply=async()=>{throw new Error('写入失败')};
  await ui.elements.get('cloudReject').onclick();
  assert.equal(ui.run('current'),'1');
  assert.equal(ui.elements.get('cloudPending')['aria-pressed'],'true');
  assert.match(ui.elements.get('message').textContent,/写入失败/);
});

test('alignment confirmation auto advances after the editor unlocks in either message order', async () => {
  for (const unlockFirst of [false,true]) {
    const frames=[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})];
    const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
    ui.elements.get('alignCurrent').onclick();
    ui.elements.get('autoNextAlignment').checked=true;
    let finish;
    ui.sandbox.reply=()=>new Promise(resolve=>{finish=resolve;});
    const save=ui.notify({action:'saved',output:'/aligned'});
    if (unlockFirst) await ui.notify({action:'busy',busy:false});
    finish({...dataset,frames:[{...frames[0],has_alignment:true,cloud_decision:'keep'},frames[1]],clouds:2,results:'/clouds'});
    await save;
    if (!unlockFirst) {
      assert.equal(ui.run('current'),'1');
      await ui.notify({action:'busy',busy:false});
    }
    assert.equal(ui.run('current'),'2');
    assert.equal(ui.run('state.frames[0].cloud_decision'),'keep');
  }
});

test('without auto advance the saved capture stays visible even when its filter no longer matches', async () => {
  const frames=[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})];
  const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
  ui.elements.get('filter').value='pending';ui.elements.get('filter').listeners.input();
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  ui.sandbox.reply=async()=>({...dataset,frames:[{...frames[0],cloud_decision:'reject'},frames[1]],clouds:2,results:'/clouds'});
  await ui.elements.get('cloudReject').onclick();
  assert.equal(ui.run('current'),'1');
  assert.equal(ui.elements.get('cloudReject')['aria-pressed'],'true');
  assert.equal(ui.elements.get('alignmentEditor').hidden,false);
});

test('late confirmation response cannot move a capture the user has since selected', async () => {
  const frames=[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})];
  const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  ui.elements.get('autoNextAlignment').checked=true;
  let finish;ui.sandbox.reply=()=>new Promise(resolve=>{finish=resolve;});
  const save=ui.notify({action:'saved',output:'/aligned'});
  ui.run("selectCapture('2')");
  finish({...dataset,frames:[{...frames[0],has_alignment:true},frames[1]],clouds:2,results:'/clouds'});await save;
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.run('pendingAdvance'),null);

});

const mixedFrames = () => [
  frame('itof-ir',{has_cloud:true,points:100,cloud_camera:{type:'itof'},cloud_input_mode:'infrared'}),
  frame('stereo-ir',{has_cloud:true,points:100,cloud_camera:{type:'stereo'},cloud_input_mode:'infrared'}),
  frame('stereo-rgb',{has_cloud:true,points:100,cloud_camera:{type:'stereo'},cloud_input_mode:'rgb'}),
  frame('itof-rgb',{has_cloud:true,points:100,cloud_camera:{type:'itof'},cloud_input_mode:'rgb'})
];

test('camera and segmentation filters combine and move preview to a matching capture', async () => {
  const ui=await setup({...dataset,frames:mixedFrames(),clouds:4,results:'/clouds'});
  assert.equal(ui.elements.get('cameraFilter').children.find(r=>r.value==='itof').textContent,'iToF（2）');
  assert.equal(ui.elements.get('cameraFilter').children.find(r=>r.value==='stereo').textContent,'双目（2）');
  assert.equal(ui.elements.get('modeFilter').children.find(r=>r.value==='rgb').textContent,'RGB 分割（2）');
  ui.elements.get('cameraFilter').value='stereo';
  ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['stereo-ir','stereo-rgb']);
  assert.equal(ui.run('current'),'stereo-ir');
  assert.match(ui.elements.get('currentTitle').textContent,/双目 · IR 分割/);
  assert.equal(ui.run('selected.size'),4);
  assert.match(ui.elements.get('selectedCount').textContent,/隐藏 2/);
  ui.elements.get('modeFilter').value='rgb';
  ui.elements.get('modeFilter').onchange();
  assert.equal(ui.run('current'),'stereo-rgb');
  assert.match(ui.elements.get('currentTitle').textContent,/双目 · RGB 分割/);
  const chips=ui.elements.get('frames').children[0].children[1].children[2].textContent;
  assert.match(chips,/双目 · RGB 分割/);
  ui.run('selected.clear(); controls();');
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['stereo-rgb']);
  ui.elements.get('selectAll').checked=false;
  ui.elements.get('selectAll').onchange();
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['stereo-rgb']);
});

test('filtering no matches clears the old preview and reports an empty list', async () => {
  const ui=await setup({...dataset,frames:mixedFrames(),clouds:4,results:'/clouds'});
  ui.run('selected.clear();');
  ui.elements.get('search').value='no-such-pig';
  ui.elements.get('search').listeners.input();
  assert.equal(ui.elements.get('viewer').hidden,true);
  assert.match(ui.elements.get('frames').children[0].textContent,/没有符合筛选/);
  assert.match(ui.elements.get('currentTitle').textContent,/没有符合筛选/);
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.equal(ui.elements.get('exportCurrent').disabled,true);
});

test('camera labels come from capture metadata, and missing metadata stays unknown', async () => {
  const rows=[frame('1',{relative_dir:'itof-results/stereo-capture',camera:{type:'stereo'},input_mode:'rgb'}),
    frame('2',{relative_dir:'itof-results/unknown-capture'})];
  const ui=await setup({...dataset,frames:rows});
  assert.match(ui.elements.get('currentTitle').textContent,/双目 · RGB 分割/);
  ui.elements.get('cameraFilter').value='unknown';
  ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['2']);
  assert.match(ui.elements.get('currentTitle').textContent,/相机待识别/);
});

test('read-only results hide input setup hints and retain the generation reason in the button title', async () => {
  const ui=await setup({...dataset,source:'',segmentation:'',frames:mixedFrames(),clouds:4,
    results:'/clouds',results_match_inputs:false});
  assert.equal(ui.elements.get('generationHint').hidden,true);
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generate').title,/目录设置/);
});


test('opening valid results replaces the stale restoration warning in the page', async () => {
  const warning='未恢复点云目录：文件夹不存在：/old-results';
  const ui=await setup({...dataset,warning});
  assert.equal(ui.elements.get('message').textContent,warning);
  const opened={...dataset,warning:'',results:'/new-results',clouds:1,
    frames:[frame('1',{has_cloud:true,points:100,cloud_camera:{type:'stereo'},cloud_input_mode:'infrared'})]};
  ui.sandbox.reply=async url=>url==='/api/choose_directory' ? {path:'/new-results'} : opened;
  await ui.elements.get('openResults').onclick();
  assert.equal(ui.elements.get('message').textContent,'');
  assert.equal(ui.elements.get('viewer').hidden,false);
  assert.equal(ui.run('state.results'),'/new-results');
});


test('viewing results keeps stale input restoration warnings inside directory settings', async () => {
  for (const initialClouds of [0,1]) {
    const warning='未恢复输入目录：文件夹不存在：/old-segmentation';
    const initial={...dataset,source:'',segmentation:'',warning,clouds:initialClouds,
      results:initialClouds ? '/clouds' : '',
      frames:initialClouds ? [frame('1',{has_cloud:true,points:100})] : dataset.frames};
    const ui=await setup(initial);
    assert.equal(ui.elements.get('message').textContent,initialClouds ? '' : warning);
    // Emulate the existing server, which still returns the stale startup warning.
    ui.sandbox.reply=async url=>url==='/api/choose_directory' ? {path:'/clouds'} : {
      ...initial,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]};
    await ui.elements.get('openResults').onclick();
    assert.equal(ui.elements.get('message').textContent,'');
    assert.equal(ui.elements.get('viewer').hidden,false);
    assert.equal(ui.elements.get('generate').disabled,true);
    ui.elements.get('configure').onclick();
    assert.equal(ui.elements.get('directoryMessage').textContent,warning);
  }
});

test('opening a missing results folder still displays the current error', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.sandbox.reply=async url=>{
    if (url==='/api/choose_directory') return {path:'/missing-results'};
    throw new Error('文件夹不存在：/missing-results');
  };
  await ui.elements.get('openResults').onclick();
  assert.equal(ui.elements.get('message').textContent,'文件夹不存在：/missing-results');
  assert.equal(ui.run('state.results'),'/clouds');
});


test('mode mismatches remain visible and are submitted for recorded skips while good captures generate', async () => {
  const bad=frame('bad', {input_mode:'infrared', validation_errors:['分割清单与逐帧输入模式不一致（清单：RGB，采集：IR）']});
  const ui=await setup({...dataset, frames:[bad, frame('good')], reviewed:2});
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['good']);
  ui.run("selected = new Set(['bad','good']); controls();");
  assert.match(ui.elements.get('generationHint').textContent,/核对失败 1 次.*自动跳过/);
  const first=ui.elements.get('frames').children[0];
  assert.match(first.title,/清单：RGB，采集：IR/);
  assert.match(first.children[1].children[2].textContent,/核对失败.*自动跳过/);
  assert.match(first.children[1].children[3].textContent,/输入模式不一致/);
  await ui.elements.get('generate').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/generate').payload.ids,['bad','good']);
  ui.run("selected = new Set(['bad']); controls();");
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generationHint').textContent,/核对结果/);
  ui.run("selected = new Set(['bad','good']); controls();");
  assert.equal(ui.elements.get('generate').disabled,false);
});

test('invalid capture filter exposes mode mismatches without enabling reconstruction', async () => {
  const bad=frame('bad', {validation_errors:['分割清单与逐帧输入模式不一致']});
  const ui=await setup({...dataset, frames:[bad, frame('good')]});
  ui.elements.get('filter').value='failed';
  ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['bad']);
  assert.equal(ui.elements.get('generate').disabled,true);
});

test('loading mixed inputs reports marked captures and keeps generation available', async () => {
  const ui=await setup();
  const mixed={...dataset, frames:[frame('good'),frame('bad', {validation_errors:['输入模式不一致']})]};
  ui.sandbox.reply=async()=>clone(mixed);
  await ui.elements.get('finishDirectories').onclick();
  assert.match(ui.elements.get('message').textContent,/通过 1 次.*失败 1 次.*自动跳过/);
  assert.equal(ui.elements.get('generate').disabled,false);
});
