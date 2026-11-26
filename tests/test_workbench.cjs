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

async function setup(initial = dataset, {storage = new Map()} = {}) {
  const elements = new Map();
  const scrollCalls = [];
  function element() {
    return {value:'', checked:false, disabled:false, hidden:false, textContent:'', children:[],
      classList:{toggle() {}, add() {}}, parentElement:{}, listeners:{},
      append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children=[...items]; },
      contentWindow:{}, setAttribute(name, value) {this[name]=value;}, removeAttribute(name) {delete this[name];}, scrollIntoView() {}, focus() {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      showModal() { this.open=true; }, close() { this.open=false; this.listeners.close?.(); }};
  }
  for (const match of fs.readFileSync(path.join(app,'frontend','pages','index.html'),'utf8').matchAll(/id="([^"]+)"/g)) {
    elements.set(match[1], element());
  }
  elements.get('filter').value='all';
  for (const id of ['exportKeep','exportPending','exportReject','exportCleaning','exportAlignment','exportMeasurement']) elements.get(id).checked=true;
  const requests=[];
  const listeners = {}, handlers = {};
  const sandbox = {APP_TOKEN:'test', URLSearchParams, Set, requestAnimationFrame:fn=>fn(), location:{origin:'http://localhost'},
    localStorage:{getItem(key) {if (!storage) throw new Error('storage unavailable'); return storage.get(key) ?? null;},
      setItem(key, value) {if (!storage) throw new Error('storage unavailable'); storage.set(key,String(value));}},
    setTimeout:()=>1, clearTimeout() {}, window:{addEventListener(name, fn) {
      (handlers[name] ||= []).push(fn);
      listeners[name] = async event => { for (const handler of handlers[name]) await handler(event); };
    }},
    document:{getElementById:id=>elements.get(id), createElement:element,
      querySelectorAll:()=>[], querySelector:selector=>selector === '.frame.active'
        ? {scrollIntoView:options=>scrollCalls.push(options)}
        : selector.includes('scope') ? {value:'selected'} : null},
    fetch: async (url, options) => {
      const payload = options?.body ? JSON.parse(options.body) : undefined;
      requests.push({url,payload});
      const data = sandbox.reply ? await sandbox.reply(url,payload) : clone(initial);
      return {ok:true, status:200, json:async()=>data};
    }};
  const context=vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(app,'frontend','assets','js','workbench.js'),'utf8'),context);
  await settle();
  const notify = data => listeners.message({origin:'http://localhost',source:elements.get('alignmentEditor').contentWindow,
    data:{type:'pointcloud-alignment',dataset_id:'one',id:'1',...data}});
  const notifyProcessing = data => listeners.message({origin:'http://localhost',source:elements.get('processingEditor').contentWindow,
    data:{type:'pointcloud-processing',dataset_id:'one',id:vm.runInContext('current',context),
      editor_instance:String(vm.runInContext('processingInstance',context)),...data}});
  // A real processing iframe sends this after its initial context has loaded.
  await notifyProcessing({action:'busy',busy:false});
  return {elements, requests, sandbox, listeners, storage, scrollCalls, notify, notifyProcessing,
    run:code=>vm.runInContext(code,context)};
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

test('primary files follow the segmentation modality for IR and RGB captures', async () => {
  for (const [mode, ply, other] of [['infrared','cloud_ir_mm.ply','cloud_rgb_mm.ply'],['rgb','cloud_rgb_mm.ply','cloud_ir_mm.ply']]) {
    const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{
      has_cloud:true,points:100,cloud_input_mode:mode,cloud_ply:ply,has_ir_file:true})]});
    ui.run('exportPlan()');
    assert.equal(ui.run('currentFiles(state.frames[0]).ply'),ply);
    assert.notEqual(ui.run('currentFiles(state.frames[0]).ply'),other);
  }
});

test('opening results selects a generated capture when current input has no cloud', async () => {
  const ui=await setup();
  assert.equal(ui.run('current'),'1');
  const data={...dataset,clouds:1,results:'/other-results',frames:[frame('1'),frame('2',{
    has_cloud:true,points:100,cloud_input_mode:'infrared',cloud_ply:'cloud_ir_mm.ply'})]};
  ui.run(`accept(${JSON.stringify(data)})`);
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.elements.get('processingEditor').hidden,false);
  assert.match(ui.elements.get('processingEditor').src,/mode=cleaning/);
});

test('cleaning and review share the point viewer with distinct workflow modes', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/aligned',frames:[frame('1',{
    has_cloud:true,has_alignment:true,points:100,cloud_input_mode:'infrared'})]});
  assert.match(ui.elements.get('processingEditor').src,/mode=cleaning/);
  assert.equal(ui.elements.has('viewer'),false);
  ui.elements.get('reviewCurrent').onclick();
  assert.match(ui.elements.get('processingEditor').src,/mode=review/);
  assert.equal(ui.elements.get('reviewCurrent')['aria-selected'],'true');
  await ui.notifyProcessing({action:'busy',busy:false});
  ui.elements.get('cleanCurrent').onclick();
  assert.match(ui.elements.get('processingEditor').src,/mode=cleaning/);
  assert.equal(ui.elements.get('cleanCurrent')['aria-selected'],'true');
});

test('coordinate alignment stays in the workbench and switching views keeps the editor', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.sandbox.window.open=()=>assert.fail('must not open a new tab');
  ui.elements.get('alignCurrent').onclick();
  const url=ui.elements.get('alignmentEditor').src;
  assert.match(url,/^\/alignment\?/);assert.match(url,/dataset_id=one/);assert.match(url,/id=1/);
  assert.match(url,/embedded=1/);
  assert.equal(ui.elements.get('alignmentEditor').hidden,false);
  assert.equal(ui.elements.get('processingEditor').hidden,true);
  await ui.notify({action:'busy',busy:false});
  ui.elements.get('cleanCurrent').onclick();
  assert.equal(ui.elements.get('processingEditor').hidden,false);
  assert.equal(ui.elements.get('alignmentEditor').src,url);
  await ui.notifyProcessing({action:'busy',busy:false});
  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.elements.get('alignmentEditor').src,url);
});

test('saving an inline alignment preserves its editor and capture list for batch work', async () => {
  const initial={...dataset,clouds:2,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})]};
  const ui=await setup(initial);ui.elements.get('alignCurrent').onclick();
  await ui.notify({action:'busy',busy:false});
  ui.elements.get('frames').scrollTop=222;
  const scrollCount=ui.scrollCalls.length;
  const url=ui.elements.get('alignmentEditor').src;
  ui.sandbox.reply=async()=>({...initial,frames:[{...initial.frames[0],has_alignment:true,alignment_revision:'new'},initial.frames[1]]});
  await ui.notify({action:'saved',output:'/aligned'});
  assert.equal(ui.run('state.frames.length'),2);
  assert.equal(ui.run('state.results'),'/clouds');
  assert.equal(ui.elements.get('alignmentEditor').src,url);
  assert.equal(ui.run('state.frames[0].alignment_revision'),'new');
  assert.equal(ui.run('workspaceView'),'alignment');
  assert.equal(ui.elements.has('nextAlignment'),false);
  assert.equal(ui.elements.get('frames').scrollTop,222);
  assert.equal(ui.scrollCalls.length,scrollCount);
  ui.run("selectCapture('2')");
  assert.equal(ui.run('current'),'2');
  assert.match(ui.elements.get('alignmentEditor').src,/id=2/);
});

test('review uses the confirmed reference and saving keeps the current editor context', async () => {
  const initial={...dataset,clouds:1,results:'/clouds',frames:[frame('1',{
    has_cloud:true,points:100,has_alignment:true,alignment_revision:'first'})]};
  const ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();
  await ui.notifyProcessing({action:'busy',busy:false});
  const key=ui.run('processingKey');
  ui.sandbox.reply=async()=>({...initial,frames:[{...initial.frames[0],has_cleaning:true,removed_points:3,cleaning_digest:'edited'}]});
  await ui.notifyProcessing({action:'saved'});
  assert.equal(ui.run('processingKey'),key);
  assert.equal(ui.run('processingBusy'),false);
  assert.equal(ui.run('state.frames[0].removed_points'),3);
  ui.run("state.frames[0].alignment_revision='second'; renderViewer();");
  assert.notEqual(ui.run('processingKey'),key);
  assert.equal(ui.run('processingBusy'),true);
});

test('reconfirmed coordinates reach review before navigation and survive a delayed old refresh', async () => {
  const initial={...dataset,clouds:1,results:'/clouds',frames:[frame('1',{
    has_cloud:true,points:100,has_alignment:true,alignment_revision:'first'})]};
  const ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();
  await ui.notifyProcessing({action:'busy',busy:false});
  let finishOldRefresh;
  ui.sandbox.reply=()=>new Promise(resolve=>{finishOldRefresh=resolve;});
  const oldRefresh=ui.notifyProcessing({action:'saved'});
  await settle();
  ui.elements.get('alignCurrent').onclick();
  await ui.notify({action:'busy',busy:true});
  const reconfirmed={...initial,frames:[{...initial.frames[0],alignment_revision:'second'}]};
  await ui.notify({action:'saved',workspace_state:reconfirmed,realigned:true});
  assert.equal(ui.elements.get('reviewCurrent').disabled,true);
  finishOldRefresh(initial);
  await oldRefresh;
  await ui.notify({action:'busy',busy:false});
  ui.elements.get('reviewCurrent').onclick();
  assert.equal(ui.run('workspaceView'),'review');
  assert.match(ui.run('processingKey'),/second/);
});

test('review remains reachable before alignment and changing captures clears its old context', async () => {
  const initial={...dataset,clouds:2,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100}),frame('2',{has_cloud:true,points:100})]};
  const ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();
  assert.match(ui.elements.get('alignmentStatus').textContent,/尚未完成坐标对齐/);
  assert.match(ui.elements.get('processingEditor').src,/id=1.*mode=review/);
  await ui.notifyProcessing({action:'busy',busy:false});
  ui.run("selectCapture('2')");
  assert.match(ui.elements.get('processingEditor').src,/id=2.*mode=review/);
  await ui.notifyProcessing({action:'busy',busy:false,id:'1'});
  assert.equal(ui.run('processingBusy'),true);
  await ui.notifyProcessing({action:'busy',busy:false,id:'2'});
  assert.equal(ui.run('processingBusy'),false);
});

test('an editor load failure releases action locks and allows retry', async () => {
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[frame('1',{has_cloud:true,points:100})]});
  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.run('alignmentBusy'),true);
  ui.elements.get('alignmentEditor').onerror();
  assert.equal(ui.run('alignmentBusy'),false);
  assert.equal(ui.elements.get('processingEditor').hidden,false);
  await ui.notifyProcessing({action:'busy',busy:false});
  assert.equal(ui.elements.get('generate').disabled,false);
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
  ui.elements.get('showValidation').onclick();
  assert.equal(ui.elements.get('validationDialog').open,true);
  const issues=ui.elements.get('validationIssues').children;
  assert.deepEqual(issues.map(item=>item.children[0].children[0].textContent),['pig_2/capture','pig_3/capture']);
  assert.match(issues[0].children[0].children[1].textContent,/分割清单与逐帧输入模式不一致/);
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
  assert.deepEqual(clone(ui.run('[...selected]')),['itof-rgb','itof-ir']);
  ui.elements.get('modeFilter').value='infrared';ui.elements.get('modeFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['itof-ir']);
  assert.deepEqual(clone(ui.run('[...selected]')),['itof-ir']);
  assert.equal(ui.run('current'),'itof-ir');
  ui.elements.get('cameraFilter').value='stereo';ui.elements.get('modeFilter').value='rgb';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['stereo-rgb']);
  assert.deepEqual(clone(ui.run('[...selected]')),['stereo-rgb']);
  ui.elements.get('cameraFilter').value='all';ui.elements.get('modeFilter').value='all';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(clone(ui.run('[...selected]')),['itof-rgb','itof-ir','stereo-rgb','unknown']);
  ui.elements.get('search').value='itof-rgb';ui.elements.get('search').listeners.input();
  assert.deepEqual(clone(ui.run('[...selected]')),['itof-rgb']);
  ui.elements.get('search').value='';ui.elements.get('search').listeners.input();
  ui.elements.get('filter').value='completed';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('[...selected]')),[]);
  assert.equal(ui.elements.get('selectedCount').textContent,'已勾选 0');
  ui.elements.get('filter').value='all';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('[...selected]')),['itof-rgb','itof-ir','stereo-rgb','unknown']);
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
  assert.equal(ui.elements.get('processingEditor').hidden,true);
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

test('terminal polling keeps task status, destinations and failures in footer details', async () => {
  for (const [status,label,success,error,warning] of [
    ['completed','完成',1,'',''],
    ['completed_with_errors','部分完成',1,'','部分采集失败，请查看任务详情'],
    ['stopped','已停止',0,'',''],
    ['failed','失败',0,'目标目录不可写',''],
  ]) {
    const runningJob={id:'job',mode:'generate',status:'running',done:0,total:2,success:0,errors:[],skipped:[]};
    const ui=await setup({...dataset,job:runningJob});
    assert.equal(ui.elements.get('generationHint').hidden,true);
    const job={...runningJob,status,done:2,success,output:'/out/result',error,warning,
      errors:status==='completed_with_errors' ? [{relative_dir:'pig_2/capture',error:'配准失败'}] : []};
    ui.sandbox.reply=async url=>url==='/api/job' ? {job} : {...dataset,job};
    await ui.run('poll()');
    assert.equal(ui.elements.get('showJob').hidden,false);
    assert.match(ui.elements.get('showJob').textContent,new RegExp(label+' 2/2'));
    assert.equal(ui.elements.get('message').textContent,error || warning);
    assert.equal(ui.elements.get('generationHint').hidden,false);
    assert.equal(ui.elements.get('stop').hidden,true);
    assert.equal(ui.elements.get('progress').value,2);
    assert.equal(ui.elements.get('jobCounts').textContent,'2 / 2，成功 '+success);
    if(success) assert.ok(ui.elements.get('jobCurrent').textContent.includes('/out/result'));
    if(error || warning) assert.ok(ui.elements.get('jobCurrent').textContent.includes(error || warning));
    ui.elements.get('showJob').onclick();
    assert.equal(ui.elements.get('jobDialog').open,true);
    assert.equal(ui.elements.get('jobPanel').hidden,false);
    if(job.errors.length) {
      assert.equal(ui.elements.get('jobDetails').hidden,false);
      assert.equal(ui.elements.get('jobErrors').children[0].textContent,'pig_2/capture：配准失败');
    }
  }
});

test('cloud quality selection is authenticated and separate from segmentation review', async () => {
  const row=frame('1',{has_cloud:true,points:100,cloud_decision:'pending',reviewed:false,status:'draft'});
  const ui=await setup({...dataset,clouds:1,results:'/clouds',frames:[row]});
  ui.sandbox.reply=async(url,payload)=>({...dataset,clouds:1,results:'/clouds',frames:[{...row,cloud_decision:payload.decision}]});
  await ui.elements.get('cloudReject').onclick();
  assert.equal(ui.elements.get('cloudReject')['aria-pressed'],'true');
  assert.equal(ui.elements.get('cloudPending')['aria-pressed'],'false');
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/review_cloud').payload,{dataset_id:'one',id:'1',decision:'reject',stage:'cleaning'});
  assert.equal(ui.run('state.frames[0].reviewed'),false);
  assert.equal(ui.run('state.frames[0].cloud_decision'),'reject');
  ui.elements.get('filter').value='all';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['1']);
});

test('export includes aligned and unaligned captures without the removed condition selector', async () => {
  const frames=[frame('1',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'keep'}),
    frame('2',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'pending'}),
    frame('3',{has_cloud:true,points:100,has_alignment:true,cloud_decision:'reject'}),
    frame('4',{has_cloud:true,points:100,has_alignment:false,cloud_decision:'keep'})];
  const ui=await setup({...dataset,frames,clouds:4,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.equal(ui.elements.has('exportAligned'),false);
  assert.equal(ui.elements.has('exportContent'),false);
  assert.equal(ui.elements.get('exportDialog').open,true);
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','2','3','4']);
  ui.elements.get('exportPending').checked=false;ui.elements.get('exportPending').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','3','4']);
  ui.elements.get('exportParent').value='/chosen';ui.elements.get('exportName').value='aligned-output';
  await ui.elements.get('confirmExport').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/export').payload,
    {dataset_id:'one',ids:['1','3','4'],scope_ids:['1','2','3','4'],parent:'/chosen',name:'aligned-output',export_layout:'split',stages:['cleaning','alignment','review'],decisions:['keep','reject'],group_by_decision:true});
});

test('full export also groups pending and rejected clouds and enforces review checkboxes', async () => {
  const frames=[frame('1',{has_cloud:true,has_alignment:true,cloud_decision:'keep'}),
    frame('2',{has_cloud:true,has_alignment:true,cloud_decision:'pending'}),
    frame('3',{has_cloud:true,cloud_decision:'reject'}),frame('4',{has_cloud:true,cloud_decision:'pending'})];
  const ui=await setup({...dataset,frames,clouds:4,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','2','3','4']);
  await ui.elements.get('confirmExport').onclick();
  const payload=ui.requests.find(r=>r.url==='/api/export').payload;
  assert.equal('aligned_only' in payload,false);assert.equal(payload.group_by_decision,true);
  assert.deepEqual(payload.decisions,['keep','pending','reject']);
  ui.elements.get('exportPending').checked=false;ui.elements.get('exportPending').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1','3']);
  for (const id of ['exportKeep','exportPending','exportReject']) ui.elements.get(id).checked=false;
  ui.elements.get('exportKeep').onchange();
  assert.equal(ui.elements.get('confirmExport').disabled,true);
  assert.match(ui.elements.get('exportEligibility').textContent,/至少勾选/);
});

test('capture screening context and export eligibility agree across all three steps', async () => {
  const row=frame('1',{has_cloud:true,points:100,has_alignment:true,has_cleaning:true,removed_points:10,
    cloud_decision:'pending',cloud_review_stale:true,measurement_slice_count:32,measurement_usable_slices:3});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  ui.elements.get('exportLayout').value='merged';
  ui.run("selected.add('1')");
  for (const view of ['cleaning','alignment','review']) {
    ui.run(`setWorkspaceView('${view}'); alignmentBusy=false; processingBusy=false; controls();`);
    assert.match(ui.elements.get('cloudReviewHint').textContent,/本次采集.*阶段/);
    assert.match(ui.elements.get('cloudReviewHint').textContent,/3\/32 片/);
    assert.match(ui.elements.get('cloudReviewHint').textContent,/重新确认/);
    assert.equal(ui.elements.get('cloudPending')['aria-pressed'],'true');
    ui.elements.get('export').onclick();
    assert.equal(ui.elements.has('exportContent'),false);
    assert.match(ui.elements.get('exportEligibility').textContent,/测量已复核 0 次/);
    ui.elements.get('exportDialog').close();
  }
  ui.run("state.frames[0].has_alignment=false; renderViewer(); alignmentBusy=false; processingBusy=false;");
  ui.elements.get('export').onclick();
});

test('export jobs lock the current editor without reloading its context against a busy workspace', async () => {
  const row=frame('1',{has_cloud:true,points:100,has_cleaning:true,removed_points:10});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  const key=ui.run('processingKey'), src=ui.elements.get('processingEditor').src;
  ui.run("accept({...state,job:{id:'export-1',mode:'export',status:'running',done:0,total:1,success:0,skipped:[],errors:[]}})");
  assert.equal(ui.run('processingKey'),key);
  assert.equal(ui.elements.get('processingEditor').src,src);
  assert.equal(ui.elements.get('processingEditor').inert,true);
  ui.run("accept({...state,job:{...state.job,status:'completed',done:1,success:1}})");
  assert.equal(ui.run('processingKey'),key);
  assert.equal(ui.run('processingBusy'),false);
  assert.equal(ui.elements.get('processingEditor').inert,false);
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
  ui.elements.get('cleanCurrent').onclick();
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
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,blocked_by:''};
  const frames=['1','2'].map(id=>frame(id,{has_cloud:true,has_alignment:true,points:100,
    workflow_stages:stageStatuses({alignment:{...keep}})}));
  const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
  ui.elements.get('filter').value='alignment';ui.elements.get('filter').listeners.input();
  ui.elements.get('alignCurrent').onclick();await ui.notify({action:'busy',busy:false});
  ui.sandbox.reply=async()=>({...dataset,frames:[{...frames[0],workflow_stages:stageStatuses({alignment:{...keep,decision:'reject'}})},frames[1]],clouds:2,results:'/clouds'});
  await ui.elements.get('cloudReject').onclick();
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['2']);
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
  assert.deepEqual(clone(ui.run('[...selected]')),['stereo-ir','stereo-rgb']);
  assert.equal(ui.elements.get('selectedCount').textContent,'已勾选 2');
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['stereo-ir','stereo-rgb']);
  ui.elements.get('modeFilter').value='rgb';
  ui.elements.get('modeFilter').onchange();
  assert.equal(ui.run('current'),'stereo-rgb');
  assert.deepEqual(clone(ui.run('[...selected]')),['stereo-rgb']);
  assert.equal(ui.elements.get('selectedCount').textContent,'已勾选 1');
  assert.match(ui.elements.get('currentTitle').textContent,/双目 · RGB 分割/);
  const card=ui.elements.get('frames').children[0].children[1];
  assert.match(card.children[1].textContent,/双目 · RGB 分割/);
  assert.equal(card.children[2].children.length,3);
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
  assert.equal(ui.run('current'),null);
  assert.equal(ui.elements.get('processingEditor').hidden,true);
  assert.equal(ui.elements.get('alignmentEditor').hidden,true);
  assert.equal(ui.elements.get('processingEditor').src,undefined);
  assert.equal(ui.elements.get('alignmentEditor').src,undefined);
  assert.equal(ui.elements.get('empty').hidden,false);
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
  assert.equal(ui.elements.get('processingEditor').hidden,false);
  assert.equal(new URL(ui.elements.get('processingEditor').src,'http://localhost').pathname,'/processing');
  assert.equal(new URL(ui.elements.get('processingEditor').src,'http://localhost').searchParams.get('mode'),'cleaning');
  assert.equal(ui.elements.get('empty').hidden,true);
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
    assert.equal(ui.elements.get('processingEditor').hidden,false);
    assert.equal(new URL(ui.elements.get('processingEditor').src,'http://localhost').pathname,'/processing');
    assert.equal(new URL(ui.elements.get('processingEditor').src,'http://localhost').searchParams.get('mode'),'cleaning');
    assert.equal(ui.elements.get('empty').hidden,true);
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
  assert.equal(first.children[1].children[2].children.length,3);
  assert.match(first.children[1].children[3].textContent,/核对失败.*自动跳过.*输入模式不一致/);
  assert.equal(ui.elements.get('captureIssue').hidden,false);
  assert.match(ui.elements.get('captureIssue').textContent,/核对失败.*生成时自动跳过.*输入模式不一致/);
  await ui.elements.get('generate').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/generate').payload.ids,['bad','good']);
  ui.run("selected = new Set(['bad']); controls();");
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generationHint').textContent,/核对结果/);
  ui.run("selected = new Set(['bad','good']); controls();");
  assert.equal(ui.elements.get('generate').disabled,false);
});

test('validation dialog exposes mode mismatches and selecting only invalid captures blocks reconstruction', async () => {
  const bad=frame('bad', {validation_errors:['分割清单与逐帧输入模式不一致']});
  const ui=await setup({...dataset, frames:[bad, frame('good')]});
  assert.equal(ui.elements.get('generate').disabled,false);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['good']);
  assert.match(ui.elements.get('showValidation').textContent,/核对失败 1/);
  ui.elements.get('showValidation').onclick();
  assert.equal(ui.elements.get('validationDialog').open,true);
  assert.equal(ui.elements.get('validationIssues').children.length,1);
  const issue=ui.elements.get('validationIssues').children[0].children[0];
  assert.equal(issue.children[0].textContent,bad.relative_dir);
  assert.match(issue.children[1].textContent,/输入模式不一致.*自动跳过/);
  assert.deepEqual(clone(ui.run('visibleRows().map(r=>r.id)')),['bad','good']);
  ui.elements.get('closeValidation').onclick();
  assert.equal(ui.elements.get('validationDialog').open,false);
  const check=ui.elements.get('frames').children[0].children[0];
  check.checked=true;check.onchange();
  assert.deepEqual(clone(ui.run('[...selected]')),['bad']);
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.match(ui.elements.get('generate').title,/核对结果/);
});

test('missing input files stay visible beside three stage statuses and block only invalid selections', async () => {
  const bad=frame('bad',{missing:['depth.png','ir.png']});
  const ui=await setup({...dataset,frames:[bad,frame('good')]});
  const card=ui.elements.get('frames').children[0];
  assert.match(card.title,/缺少：depth.png、ir.png/);
  assert.equal(card.children[1].children[2].children.length,3);
  assert.equal(card.children[1].children[3].className,'capture issue');
  assert.match(card.children[1].children[3].textContent,/核对失败.*自动跳过.*缺少：depth.png、ir.png/);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['good']);
  const check=card.children[0];check.checked=true;check.onchange();
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


test('capture cards show only cleaning, alignment and current profile review progress', async () => {
  const row=frame('1',{has_cloud:true,points:100,has_alignment:true,alignment_cached:true,
    has_cleaning:true,removed_points:10,cloud_decision:'keep',reviewed:true,
    measurement_slice_count:32,measurement_usable_slices:3});
  const ui=await setup({...dataset,frames:[row],clouds:1});
  const card=ui.elements.get('frames').children[0];
  const chips=card.children[1].children[2];
  assert.deepEqual(chips.children.map(line=>line.textContent),[
    '点云清理：已记录（已删 10 点）','坐标对齐：已确认','测量复核：已确认 3/32 片']);
  row.measurement_ready=true;row.measurement_usable_slices=32;
  ui.run(`state.frames=${JSON.stringify([row])};renderList()`);
  assert.match(ui.elements.get('frames').children[0].children[1].children[2].children[2].textContent,/已复核 32\/32 片/);
});

test('capture stage colors follow each decision, including blocked downstream stages', async () => {
  const row=frame('1',{has_cloud:true,has_alignment:true,workflow_stages:{
    cleaning:{decision:'keep'},alignment:{decision:'pending'},review:{decision:'reject'}}});
  const ui=await setup({...dataset,frames:[row],clouds:1});
  const colors=()=>ui.elements.get('frames').children[0].children[1].children[2].children.map(line=>line.className);
  assert.deepEqual(colors(),['stage-status stage-keep','stage-status stage-pending','stage-status stage-reject']);
  ui.run("state.frames[0].workflow_stages.alignment={decision:'keep',blocked_by:'cleaning'};renderList()");
  assert.equal(colors()[1],'stage-status stage-reject');
});

const stageStatuses = (extra = {}) => ({
  cleaning:{decision:'keep',available:true,has_output:true,reviewed:true,version:{cleaning_digest:''},blocked_by:''},
  alignment:{decision:'pending',available:true,has_output:false,reviewed:false,version:{},blocked_by:''},
  review:{decision:'pending',available:false,has_output:false,reviewed:false,version:{},blocked_by:''}, ...extra});

test('camera progress shows completed pigs and captures separately for each camera',async()=>{
  const complete={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const done=(id,pig,camera)=>frame(id,{pig_id:pig,has_cloud:true,has_alignment:true,camera:{type:camera},
    workflow_stages:stageStatuses({alignment:{...complete},review:{...complete}})});
  const pending=(id,pig,camera)=>frame(id,{pig_id:pig,has_cloud:true,camera:{type:camera},
    workflow_stages:stageStatuses()});
  const frames=[done('a1','A','itof'),done('a2','A','itof'),done('b1','B','itof'),pending('b2','B','itof'),
    done('c1','C','stereo'),done('d1','D','stereo'),pending('d2','D','stereo')];
  const ui=await setup({...dataset,frames,clouds:7,pigs:4});
  const progressLines=()=>ui.elements.get('cameraProgressText').children.map(line=>line.textContent);
  assert.deepEqual(progressLines(),['iToF：1 / 2 头，3/4 条','双目：1 / 2 头，2/3 条']);
  assert.match(ui.elements.get('cameraProgressText').title,/同一耳标下全部对应采集.*采集完成数/);
  ui.elements.get('cameraFilter').value='itof';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(progressLines(),['iToF：1 / 2 头，3/4 条']);
  ui.elements.get('cameraFilter').value='stereo';ui.elements.get('cameraFilter').onchange();
  assert.deepEqual(progressLines(),['双目：1 / 2 头，2/3 条']);
  ui.elements.get('filter').value='completed';ui.elements.get('filter').listeners.input();
  assert.deepEqual(progressLines(),['双目：1 / 2 头，2/3 条'],
    'camera progress stays scoped to all captures rather than only the filtered rows');
});

test('export includes only completed stages and cleaning can be selected independently', async () => {
  const row=frame('1',{has_cloud:true,cloud_ply:'cloud_ir_mm.ply',workflow_stages:stageStatuses()});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.deepEqual(clone(ui.run('Object.keys(exportStageEntries(state.frames[0]))')),['cleaning']);
  assert.match(ui.elements.get('exportEligibility').textContent,/点云清理 1 次、坐标对齐 0 次、测量复核 0 次/);
  ui.elements.get('exportCleaning').checked=false;ui.elements.get('exportCleaning').onchange();
  assert.equal(ui.elements.get('confirmExport').disabled,true);
  ui.elements.get('exportAlignment').checked=false;
  ui.elements.get('exportMeasurement').checked=false;
  ui.run('exportPlan()');
  assert.match(ui.elements.get('exportEligibility').textContent,/至少勾选一个导出阶段/);
});

test('measurement export explains geometry and sequences and follows the selected stage', async () => {
  const stages=stageStatuses({alignment:{decision:'keep',available:true,has_output:true,reviewed:true,blocked_by:''},
    review:{decision:'pending',available:true,has_output:true,reviewed:false,blocked_by:''}});
  const row=frame('1',{has_cloud:true,has_alignment:true,workflow_stages:stages});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.match(ui.elements.get('exportHint').textContent,/整体几何特征.*批量 CSV.*剖面序列/);
  assert.match(ui.elements.get('exportEligibility').textContent,/测量复核 1 次/);
  ui.elements.get('exportMeasurement').checked=false;ui.elements.get('exportMeasurement').onchange();
  assert.equal(ui.run('exportStageEntries(state.frames[0]).review'),undefined);
});

test('each tab displays and submits its own stage decision and version', async () => {
  const stages=stageStatuses({alignment:{decision:'reject',available:true,has_output:true,reviewed:true,blocked_by:'',version:{alignment_reference_id:'a'}},
    review:{decision:'pending',available:false,has_output:false,blocked_by:'alignment'}});
  const row=frame('1',{has_cloud:true,has_alignment:true,workflow_stages:stages});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  assert.equal(ui.elements.get('cloudKeep')['aria-pressed'],'true');
  ui.run("setWorkspaceView('alignment'); alignmentBusy=false; controls()");
  assert.equal(ui.elements.get('cloudReject')['aria-pressed'],'true');
  assert.equal(ui.elements.get('reviewCurrent').disabled,true);
  assert.equal(ui.elements.get('stageReviewLabel').textContent,'对齐筛选');
  ui.sandbox.reply=async()=>({...dataset,frames:[row],clouds:1,results:'/clouds'});
  await ui.elements.get('cloudPending').onclick();
  assert.deepEqual(ui.requests.find(r=>r.url==='/api/review_cloud').payload,
    {dataset_id:'one',id:'1',decision:'pending',stage:'alignment',stage_version:{alignment_reference_id:'a'}});
});

test('later rejection does not remove cleaning keep from a keep-only export', async () => {
  const stages=stageStatuses({alignment:{decision:'reject',available:true,has_output:true,reviewed:true,blocked_by:''},
    review:{decision:'pending',available:false,has_output:true,blocked_by:'alignment'}});
  const row=frame('1',{has_cloud:true,has_alignment:true,cloud_ply:'cloud_ir_mm.ply',workflow_stages:stages});
  const ui=await setup({...dataset,frames:[row],clouds:1,results:'/clouds'});
  ui.elements.get('export').onclick();
  ui.elements.get('exportPending').checked=false;ui.elements.get('exportReject').checked=false;
  ui.run('exportPlan()');
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),['1']);
  assert.deepEqual(clone(ui.run('Object.keys(exportStageEntries(state.frames[0]))')),['cleaning']);
  ui.run("state.frames[0].workflow_stages.cleaning.decision='reject'; state.frames[0].workflow_stages.alignment.blocked_by='cleaning'; renderViewer(); controls()");
  assert.equal(ui.elements.get('alignCurrent').disabled,true);
  assert.equal(ui.elements.get('reviewCurrent').disabled,true);
});

test('export defaults to stage folders and can switch to merged folders with the same selection',async()=>{
  const stages=stageStatuses({cleaning:{decision:'keep',has_output:true,reviewed:true,blocked_by:''},
    alignment:{decision:'keep',has_output:true,reviewed:true,blocked_by:''},
    review:{decision:'pending',has_output:true,reviewed:true,blocked_by:''}});
  const ui=await setup({...dataset,frames:[frame('1',{has_cloud:true,has_alignment:true,workflow_stages:stages})],clouds:1,results:'/clouds'});
  ui.elements.get('export').onclick();
  assert.equal(ui.elements.get('exportLayout').value,'split');
  assert.match(ui.elements.get('exportLayoutHint').textContent,/该阶段.*不重复复制/);
  const ids=clone(ui.run('exportRows().map(r=>r.id)'));
  ui.elements.get('exportLayout').value='merged';ui.elements.get('exportLayout').onchange();
  assert.deepEqual(clone(ui.run('exportRows().map(r=>r.id)')),ids);
  assert.match(ui.elements.get('exportLayoutHint').textContent,/最后导出阶段/);
  await ui.elements.get('confirmExport').onclick();
  const payload=ui.requests.find(r=>r.url==='/api/export').payload;
  assert.equal(payload.export_layout,'merged');
  assert.deepEqual(payload.stages,['cleaning','alignment','review']);
});

test('five cumulative progress filters include later completions and stay independent of the editor', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const frames=[
    frame('clean',{has_cloud:true,workflow_stages:stageStatuses()}),
    frame('align',{has_cloud:true,has_alignment:true,workflow_stages:stageStatuses({alignment:{...keep}})}),
    frame('done',{has_cloud:true,has_alignment:true,workflow_stages:stageStatuses({alignment:{...keep},review:{...keep}})}),
    frame('align-only',{has_cloud:true,has_alignment:true,workflow_stages:stageStatuses({cleaning:{...keep,decision:'pending'},alignment:{...keep}})}),
    frame('skipped',{excluded:true}),
  ];
  const ui=await setup({...dataset,frames,clouds:4});
  const shown=()=>clone(ui.run('visibleRows().map(row=>row.id)'));
  const apply=value=>{ui.elements.get('filter').value=value;ui.elements.get('filter').listeners.input();};
  apply('cleaning');assert.deepEqual(shown(),['clean','align','done']);
  apply('alignment');assert.deepEqual(shown(),['align','done']);
  apply('completed');assert.deepEqual(shown(),['done']);
  assert.equal(ui.run('current'),'done');
  for (const view of ['cleaning','alignment','review']) {
    ui.run(`setWorkspaceView('${view}')`);
    assert.deepEqual(shown(),['done']);
  }
  apply('excluded');assert.deepEqual(shown(),['skipped']);
  apply('all');assert.deepEqual(shown(),frames.map(row=>row.id));
  const html=fs.readFileSync(path.join(app,'frontend','pages','index.html'),'utf8');
  const filter=html.match(/<select id="filter"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...filter.matchAll(/<option value="([^"]+)"/g)].map(match=>match[1]),
    ['all','cleaning','alignment','completed','excluded']);
  for (const id of ['cleaningFilter','alignmentFilter','reviewFilter']) assert.equal(ui.elements.has(id),false);
});

test('camera progress counts completed pigs and captures without changing under list filters', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const done=id=>frame(id,{has_cloud:true,has_alignment:true,has_cleaning:false,measurement_ready:false,
    workflow_stages:{cleaning:{...keep},alignment:{...keep},review:{...keep}}});
  const frames=['done','stale','blocked','pending','rejected','unreviewed','no-output','no-cloud'].map(done);
  frames[1].workflow_stages.cleaning.stale=true;
  frames[2].workflow_stages.review.blocked_by='alignment';
  frames[3].workflow_stages.alignment.decision='pending';
  frames[4].workflow_stages.review.decision='reject';
  frames[5].workflow_stages.review.reviewed=false;
  frames[6].workflow_stages.alignment.has_output=false;
  frames[7].has_cloud=false;
  frames.push(frame('legacy',{has_cloud:true,has_alignment:true,cloud_decision:'keep'}));
  const initial={...dataset,frames,clouds:8};
  const ui=await setup(initial);
  const progress=()=>ui.elements.get('cameraProgressText').children[0].textContent;
  assert.equal(progress(),'未识别相机：1 / 9 头，1/9 条');
  assert.match(ui.elements.get('datasetSummary').textContent,/已采集完毕 1 次/);
  const badge=ui.elements.get('frames').children[0].children[1].children[0].children[0];
  assert.equal(badge.textContent,'已采集完毕');
  ui.elements.get('filter').value='completed';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['done']);
  assert.equal(progress(),'未识别相机：1 / 9 头，1/9 条');
  ui.elements.get('filter').value='cleaning';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['done','blocked','pending','rejected','unreviewed','no-output']);
  ui.elements.get('filter').value='alignment';ui.elements.get('filter').listeners.input();
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['done','blocked','rejected','unreviewed']);
  ui.elements.get('filter').value='all';
  ui.elements.get('search').value='stale';ui.elements.get('search').listeners.input();
  assert.equal(ui.run('visibleRows().length'),1);
  assert.equal(progress(),'未识别相机：1 / 9 头，1/9 条');
  // An upstream edit invalidates completion immediately on the next state refresh.
  frames[0].workflow_stages.cleaning.stale=true;
  ui.run(`accept(${JSON.stringify(initial)})`);
  assert.equal(progress(),'未识别相机：0 / 9 头，0/9 条');
  assert.match(ui.elements.get('datasetSummary').textContent,/已采集完毕 0 次/);
});

test('empty datasets show a compact zero progress', async () => {
  const ui=await setup({...dataset,frames:[],pigs:0,reviewed:0,clouds:0});
  assert.deepEqual(ui.elements.get('cameraProgressText').children.map(line=>line.textContent),
    ['未识别相机：0 / 0 头，0/0 条']);
});

test('collapsing filters preserves scope and selections, exposes hidden conditions and remembers its state', async () => {
  const storage=new Map([['pointcloud-filters-collapsed','1']]);
  const frames=[frame('1',{camera:{type:'itof'},input_mode:'rgb'}),frame('2',{camera:{type:'stereo'},input_mode:'infrared'})];
  const initial={...dataset,frames};
  const ui=await setup(initial,{storage});
  assert.equal(ui.elements.get('listFilters').hidden,true);
  assert.equal(ui.elements.get('toggleFilters')['aria-expanded'],'false');
  assert.equal(ui.elements.get('toggleFilters').textContent,'展开筛选');
  ui.elements.get('cameraFilter').value='itof';ui.elements.get('cameraFilter').onchange();
  ui.elements.get('modeFilter').value='rgb';ui.elements.get('modeFilter').onchange();
  ui.elements.get('search').value='1';ui.elements.get('search').listeners.input();
  ui.run("selected=new Set(['1','2']);renderList();controls()");
  assert.equal(ui.elements.get('filterSummary').textContent,'全部采集 · iToF · RGB 分割 · 搜索“1”');
  assert.match(ui.elements.get('filterSummary').title,/全部采集；iToF；RGB 分割；搜索“1”/);
  assert.match(ui.elements.get('selectedCount').textContent,/已勾选 2 · 隐藏 1/);
  const current=ui.run('current'), requestCount=ui.requests.length;
  ui.elements.get('toggleFilters').onclick();
  assert.equal(ui.elements.get('listFilters').hidden,false);
  assert.equal(ui.elements.get('filterSummary').hidden,true);
  assert.equal(ui.elements.get('toggleFilters')['aria-expanded'],'true');
  assert.equal(storage.get('pointcloud-filters-collapsed'),'0');
  ui.elements.get('toggleFilters').onclick();
  assert.equal(storage.get('pointcloud-filters-collapsed'),'1');
  assert.equal(ui.elements.get('search').value,'1');
  assert.equal(ui.elements.get('cameraFilter').value,'itof');
  assert.equal(ui.elements.get('modeFilter').value,'rgb');
  assert.equal(ui.elements.get('filter').value,'all');
  assert.equal(ui.run('current'),current);
  assert.deepEqual(clone(ui.run('[...selected]')),['1','2']);
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['1']);
  assert.equal(ui.requests.length,requestCount);
  const reloaded=await setup(initial,{storage});
  assert.equal(reloaded.elements.get('listFilters').hidden,true);
  assert.equal(reloaded.elements.get('toggleFilters').textContent,'展开筛选');
  assert.equal(reloaded.elements.get('filterSummary').textContent,'全部采集');
  const html=fs.readFileSync(path.join(app,'frontend','pages','index.html'),'utf8');
  const panel=html.slice(html.indexOf('<aside class="listpanel">'),html.indexOf('</aside>'));
  assert.ok(panel.indexOf('cameraProgressText')<panel.indexOf('id="listFilters"'));
  assert.ok(panel.indexOf('id="selectAll"')<panel.indexOf('id="listFilters"'));
  assert.ok(panel.indexOf('id="listFilters"')<panel.indexOf('id="frames"'));
  assert.doesNotMatch(panel,/listfooter|stagefilters/);
});

test('filter disclosure still works when browser preference storage is unavailable', async () => {
  const ui=await setup(dataset,{storage:null});
  assert.equal(ui.elements.get('listFilters').hidden,false);
  ui.elements.get('toggleFilters').onclick();
  assert.equal(ui.elements.get('listFilters').hidden,true);
  ui.elements.get('toggleFilters').onclick();
  assert.equal(ui.elements.get('listFilters').hidden,false);
});

test('generation from collapsed cumulative filters selects the visible scope and reports eligibility', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const clean=(id,extra={})=>frame(id,{has_cloud:true,camera:{type:'itof'},input_mode:'rgb',cloud_input_mode:'rgb',
    workflow_stages:stageStatuses({cleaning:{...keep}}),...extra});
  const frames=[clean('ready'),clean('pending',{workflow_stages:stageStatuses({cleaning:{...keep,decision:'pending'}})}),
    clean('draft',{reviewed:false,status:'draft'}),clean('skipped',{excluded:true}),
    clean('failed',{validation_errors:['缺少输入图']}),clean('other',{camera:{type:'stereo'},cloud_input_mode:'infrared'})];
  const ui=await setup({...dataset,frames,clouds:6});
  ui.run('selected.clear()');
  ui.elements.get('cameraFilter').value='itof';ui.elements.get('cameraFilter').onchange();
  ui.elements.get('modeFilter').value='rgb';ui.elements.get('modeFilter').onchange();
  ui.elements.get('filter').value='cleaning';ui.elements.get('filter').listeners.input();
  ui.elements.get('toggleFilters').onclick();
  assert.deepEqual(clone(ui.run('visibleRows().map(row=>row.id)')),['ready','draft','skipped','failed']);
  assert.deepEqual(clone(ui.run('[...selected]')),['ready','draft','skipped','failed']);
  assert.deepEqual(clone(ui.run('generationPlan().ids')),['ready','draft','skipped','failed']);
  assert.match(ui.elements.get('filterSummary').textContent,/清理已确认 · iToF · RGB 分割/);
  assert.match(ui.elements.get('generationHint').textContent,/其中 1 次可生成.*核对失败 1 次.*手动标记 1 次/);
  assert.equal(ui.elements.get('generate').disabled,false);
  await ui.elements.get('generate').onclick();
  assert.deepEqual(ui.requests.find(request=>request.url==='/api/generate').payload.ids,
    ['ready','draft','skipped','failed']);
});

test('next capture follows the filtered order and scrolls the active card into view', async () => {
  const frames=[frame('1',{has_cloud:true,has_alignment:true}),frame('2',{has_cloud:true,has_alignment:true}),
    frame('3',{has_cloud:true,has_alignment:true})];
  const initial={...dataset,frames,clouds:3,results:'/clouds'},ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();
  await ui.notifyProcessing({action:'busy',busy:false});
  const selectedBefore=clone(ui.run('[...selected]'));
  assert.equal(ui.run('current'),'1');
  assert.equal(ui.elements.get('nextCapture').disabled,false);

  ui.elements.get('nextCapture').onclick();
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.run('workspaceView'),'cleaning');
  assert.match(ui.elements.get('processingEditor').src,/id=2.*mode=cleaning/);
  assert.match(ui.elements.get('frames').children[1].className,/active/);
  assert.doesNotMatch(ui.elements.get('frames').children[0].className,/active/);
  assert.deepEqual(clone(ui.scrollCalls.at(-1)),{block:'center',behavior:'smooth'});
  assert.match(ui.elements.get('message').textContent,/第 2\/3 条采集.*进入点云清理/);
  assert.deepEqual(clone(ui.run('[...selected]')),selectedBefore);

  assert.equal(ui.elements.get('nextCapture').disabled,true);
  await ui.notifyProcessing({action:'busy',busy:false});
  assert.equal(ui.elements.get('nextCapture').disabled,false);
  ui.elements.get('nextCapture').onclick();
  assert.equal(ui.run('current'),'3');
  assert.equal(ui.run('workspaceView'),'cleaning');
  assert.match(ui.elements.get('frames').children[2].className,/active/);
  assert.equal(ui.elements.get('nextCapture').disabled,true);

  ui.elements.get('search').value='pig_2';
  ui.elements.get('search').listeners.input();
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.elements.get('frames').children.length,1);
  assert.match(ui.elements.get('frames').children[0].className,/active/);
  assert.equal(ui.elements.get('nextCapture').disabled,true);
});

test('list rebuilds preserve scroll for selection, capture clicks and state refreshes', async () => {
  const frames=Array.from({length:30},(_,i)=>frame(String(i+1),{has_cloud:true,has_alignment:true}));
  const initial={...dataset,frames,clouds:30,results:'/clouds'},ui=await setup(initial);
  const list=ui.elements.get('frames'),replace=list.replaceChildren;
  // Simulate the browser clamping scroll when list content is replaced.
  // The normal DOM mock retains scrollTop and used to hide this regression.
  list.replaceChildren=function(...items) { replace.call(this); this.append(...items); this.scrollTop=0; };
  list.scrollTop=1450;

  for (const checked of [false,true]) {
    ui.elements.get('selectAll').checked=checked;
    ui.elements.get('selectAll').onchange();
    assert.equal(list.scrollTop,1450);
    assert.equal(ui.run('selected.size'),checked ? 30 : 0);
  }
  const check=list.children[12].children[0];
  check.checked=false;check.onchange();
  assert.equal(list.scrollTop,1450);
  assert.equal(ui.run("selected.has('13')"),false);
  list.children[12].children[1].onclick();
  assert.equal(ui.run('current'),'13');
  assert.equal(list.scrollTop,1450);
  await ui.notifyProcessing({action:'busy',busy:false});
  ui.run(`accept(${JSON.stringify(initial)},false,true)`);
  assert.equal(list.scrollTop,1450);
  assert.equal(ui.scrollCalls.length,0);

  ui.elements.get('nextCapture').onclick();
  assert.equal(ui.run('current'),'14');
  assert.deepEqual(clone(ui.scrollCalls),[{block:'center',behavior:'smooth'}]);
});

test('switching workflow steps keeps the same capture at the same list position', async () => {
  const frames=[frame('1',{has_cloud:true,has_alignment:true}),frame('2',{has_cloud:true,has_alignment:true})];
  const ui=await setup({...dataset,frames,clouds:2,results:'/clouds'});
  ui.elements.get('frames').scrollTop=145;

  ui.elements.get('alignCurrent').onclick();
  assert.equal(ui.run('workspaceView'),'alignment');
  assert.equal(ui.elements.get('frames').scrollTop,145);
  await ui.notify({action:'busy',busy:false});

  ui.elements.get('reviewCurrent').onclick();
  assert.equal(ui.run('workspaceView'),'review');
  assert.equal(ui.elements.get('frames').scrollTop,145);
  await ui.notifyProcessing({action:'busy',busy:false});

  ui.elements.get('cleanCurrent').onclick();
  assert.equal(ui.run('workspaceView'),'cleaning');
  assert.equal(ui.elements.get('frames').scrollTop,145);
  assert.match(ui.elements.get('frames').children[0].className,/active/);
});

test('measurement keep reloads only the current review iframe and reports automatic rejections', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const row=frame('1',{has_cloud:true,has_alignment:true,measurement_slice_count:32,measurement_usable_slices:31,
    measurement_unusable_slices:0,measurement_pending_slices:1,
    workflow_stages:stageStatuses({alignment:{...keep},review:{...keep,decision:'pending',version:{measurement_revision:'before'}}})});
  const initial={...dataset,frames:[row],clouds:1,results:'/clouds'};
  const ui=await setup(initial);ui.elements.get('reviewCurrent').onclick();
  await ui.notifyProcessing({action:'busy',busy:false});
  ui.elements.get('frames').scrollTop=231;
  const scrollCount=ui.scrollCalls.length;
  ui.elements.get('autoNextAlignment').checked=true;
  const source=ui.elements.get('processingEditor').src,oldInstance=String(ui.run('processingInstance'));
  assert.match(ui.elements.get('cloudKeep').title,/自动淘汰其余待定剖面/);
  assert.match(ui.elements.get('cloudReviewHint').textContent,/确认所选.*仅确认选区.*其余待定剖面自动淘汰/);
  const fresh={...initial,measurement_finalization:{auto_rejected_profiles:1},frames:[{...row,
    measurement_unusable_slices:1,measurement_pending_slices:0,
    workflow_stages:{...row.workflow_stages,review:{...keep,version:{measurement_revision:'after'}}}}]};
  ui.sandbox.reply=async()=>fresh;
  await ui.elements.get('cloudKeep').onclick();
  const submitted=ui.requests.find(request=>request.url==='/api/review_cloud').payload;
  assert.deepEqual(submitted,{dataset_id:'one',id:'1',decision:'keep',stage:'review',stage_version:{measurement_revision:'before'}});
  assert.equal(ui.run('current'),'1');assert.equal(ui.run('workspaceView'),'review');
  assert.notEqual(ui.elements.get('processingEditor').src,source);
  assert.equal(ui.run('processingBusy'),true);
  assert.equal(ui.run("state.frames[0].workflow_stages.review.version.measurement_revision"),'after');
  assert.match(ui.elements.get('message').textContent,/测量复核已保留.*自动淘汰 1 片待定剖面.*31\/32 片可测.*1 片淘汰.*0 片待定/);
  assert.match(ui.elements.get('frames').children[0].children[1].children[2].children[2].textContent,/淘汰 1/);
  assert.equal(ui.elements.get('frames').scrollTop,231);
  assert.equal(ui.scrollCalls.length,scrollCount);
  const before=ui.requests.length;
  await ui.notifyProcessing({action:'busy',busy:false,editor_instance:oldInstance});
  await ui.notifyProcessing({action:'saved',editor_instance:oldInstance});
  assert.equal(ui.run('processingBusy'),true);assert.equal(ui.requests.length,before);
  await ui.notifyProcessing({action:'busy',busy:false});
  assert.equal(ui.run('processingBusy'),false);assert.equal(ui.elements.get('cloudKeep').disabled,false);
  // Keeping an already finalized review is idempotent but still loads the current server revision.
  ui.sandbox.reply=async()=>({...fresh,measurement_finalization:{auto_rejected_profiles:0}});
  const currentSource=ui.elements.get('processingEditor').src;
  await ui.elements.get('cloudKeep').onclick();
  assert.notEqual(ui.elements.get('processingEditor').src,currentSource);
  assert.match(ui.elements.get('message').textContent,/当前没有待定剖面，无需再次淘汰.*31\/32 片可测.*1 片淘汰.*0 片待定/);
});

test('other stage decisions and failed measurement keep preserve the loaded editors', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const row=frame('1',{has_cloud:true,has_alignment:true,
    workflow_stages:stageStatuses({alignment:{...keep},review:{...keep}})});
  for (const [stage,decision,failed] of [['cleaning','keep',false],['alignment','keep',false],
      ['review','pending',false],['review','reject',false],['review','keep',true]]) {
    const initial={...dataset,frames:[row],clouds:1,results:'/clouds'},ui=await setup(initial);
    ui.run(`setWorkspaceView('${stage}')`);
    await (stage==='alignment'?ui.notify:ui.notifyProcessing)({action:'busy',busy:false});
    const processingSource=ui.elements.get('processingEditor').src,alignmentSource=ui.elements.get('alignmentEditor').src;
    const instance=ui.run('processingInstance');
    ui.sandbox.reply=async()=>{if(failed)throw new Error('测量版本已变化');return initial;};
    await ui.elements.get({keep:'cloudKeep',pending:'cloudPending',reject:'cloudReject'}[decision]).onclick();
    assert.equal(ui.elements.get('processingEditor').src,processingSource,`${stage}/${decision}`);
    assert.equal(ui.elements.get('alignmentEditor').src,alignmentSource,`${stage}/${decision}`);
    assert.equal(ui.run('processingInstance'),instance);
    assert.equal(ui.run('current'),'1');
    if(failed){assert.match(ui.elements.get('message').textContent,/测量版本已变化/);assert.equal(ui.run('processingBusy'),false);}
  }
});

test('late processing state cannot overwrite the finalized review after its iframe refresh', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const row=frame('1',{has_cloud:true,has_alignment:true,workflow_stages:stageStatuses({alignment:{...keep},review:{...keep,decision:'pending'}})});
  const initial={...dataset,frames:[row],clouds:1,results:'/clouds'},ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();await ui.notifyProcessing({action:'busy',busy:false});
  let finishOldState;
  const finalized={...initial,measurement_finalization:{auto_rejected_profiles:2},frames:[{...row,
    workflow_stages:{...row.workflow_stages,review:{...keep}}}]};
  ui.sandbox.reply=(url)=>url==='/api/state'?new Promise(resolve=>{finishOldState=resolve;}):Promise.resolve(finalized);
  const late=ui.notifyProcessing({action:'saved'});
  await ui.elements.get('cloudKeep').onclick();
  const source=ui.elements.get('processingEditor').src;
  finishOldState(initial);await late;
  assert.equal(ui.run("state.frames[0].workflow_stages.review.decision"),'keep');
  assert.equal(ui.elements.get('processingEditor').src,source);
  assert.equal(ui.run('processingBusy'),true);
});

test('measurement keep response does not reload a capture selected since submission', async () => {
  const keep={decision:'keep',available:true,has_output:true,reviewed:true,stale:false,blocked_by:''};
  const frames=['1','2'].map(id=>frame(id,{has_cloud:true,has_alignment:true,
    workflow_stages:stageStatuses({alignment:{...keep},review:{...keep}})}));
  const initial={...dataset,frames,clouds:2,results:'/clouds'},ui=await setup(initial);
  ui.elements.get('reviewCurrent').onclick();await ui.notifyProcessing({action:'busy',busy:false});
  let finish;ui.sandbox.reply=()=>new Promise(resolve=>{finish=resolve;});
  const save=ui.elements.get('cloudKeep').onclick();
  ui.run("current='2';renderList();renderViewer();controls()");
  const source=ui.elements.get('processingEditor').src;
  finish({...initial,measurement_finalization:{auto_rejected_profiles:0}});await save;
  assert.equal(ui.run('current'),'2');
  assert.equal(ui.elements.get('processingEditor').src,source);
});
