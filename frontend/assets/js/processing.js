'use strict';
const $ = id => document.getElementById(id), canvas = $('cloud'), ctx = canvas.getContext('2d');
const offline = typeof OFFLINE_DATA !== 'undefined';
const query = new URLSearchParams(location.search), identity = {id: query.get('id'), dataset_id: query.get('dataset_id')};
const pageMode = offline ? (OFFLINE_DATA.alignment ? 'review' : 'preview') : (query.get('mode') === 'review' ? 'review' : 'cleaning');
let token = APP_TOKEN, data, slices, busy = false, boxMode = '', drag, rectangle;
let yaw = -.35, pitch = -.4, zoom = 1, pan = [0, 0], center = [0, 0, 0], extent = 1, sliceIndex = 0;
let sliceRequest = 0, sliceTimer, sourceKind = 'candidate';
let notesDirty = false;
const fmt = v => v == null ? '—' : Number(v).toFixed(1);
function configureMode() {
  const cleaning = pageMode === 'cleaning';
  const review = pageMode === 'review';
  $('cleaningSection').hidden = !cleaning;
  $('measurementSection').hidden = !review;
  $('cleaningSection').open = cleaning;
  $('measurementSection').open = review;
  document.title = review ? '测量复核' : cleaning ? '点云清理' : '清理成果预览';
  $('summaryTitle').textContent = review ? '测量复核' : cleaning ? '点云查看与清理' : '清理成果预览';
  $('fit').querySelector('[value=selected]').hidden = !cleaning;
  document.body.dataset.mode = pageMode;
}
function notify(action, extra = {}) { if (!offline && parent !== window) parent.postMessage({type:'pointcloud-processing', ...identity, action, ...extra}, location.origin); }
function message(text = '', error = false) { $('message').textContent = text; $('message').classList.toggle('error', error); }
function base(extra = {}) { return {...identity, context_id:data?.context_id, revision:data?.revision || '', ...extra}; }
async function api(method, payload) {
  const send = () => fetch('/api/processing/'+method, {method:'POST', headers:{'Content-Type':'application/json','X-App-Token':token}, body:JSON.stringify(payload)});
  let response = await send(), body = await response.json();
  if (response.status === 403 && body.code === 'session_expired') {
    token = (await (await fetch('/api/session')).json()).token;
    response = await send(); body = await response.json();
  }
  if (!response.ok) throw Error(body.error || '操作失败');
  return body;
}
async function action(fn) {
  if (busy || offline) return;
  busy = true; notify('busy', {busy:true}); controls();
  try { await fn(); } catch (e) { message(e.message, true); }
  finally { busy = false; controls(); notify('busy', {busy:false}); }
}
function apply(response, refit = false) {
  data = response;
  $('coordinate').querySelector('[value=body]').disabled = !data.alignment;
  if (!data.alignment) $('coordinate').value = 'camera';
  else if (pageMode === 'review') $('coordinate').value = 'body';
  $('counts').textContent = `保留 ${data.kept_count.toLocaleString()} / ${data.total.toLocaleString()} 点`;
  $('alignmentHint').textContent = pageMode === 'review' ? (data.alignment ? '使用已确认的猪体坐标和当前清理版本，逐片检查可见边界。' : '尚未完成坐标对齐，测量功能暂不可用。') : (data.alignment ? '坐标参考已确认；清理版本沿用同次采集的参考。' : '先查看并保守清理飞点，再进入坐标对齐。');
  $('originalQuality').textContent = data.report ? `生成时有效深度率 ${(data.report.original_valid_depth_ratio*100).toFixed(2)}%；删点后不会将它改记为 100%。` : `已删除 ${data.removed_count.toLocaleString()} 点；原始点云另存。`;
  $('measurementLock').hidden = !!data.alignment;
  $('measurementControls').hidden = !data.alignment;
  $('thickness').value = data.measurement?.thickness_mm || 15;
  $('minPoints').value = data.measurement?.min_points || 20;
  if (!notesDirty) $('notes').value = data.measurement?.notes || '';
  const n = data.candidate_count || 0;
  $('candidateInfo').textContent = n ? `选中 ${n.toLocaleString()} 点 · ${data.candidate_action === 'restore' ? '准备恢复' : '准备删除'}（尚未应用）` : '尚未选择点';
  $('applySelection').textContent = data.candidate_action === 'restore' ? '确认恢复选中点' : '确认删除选中点';
  if (refit) fitView(); else draw();
  controls();
}
function controls() {
  const locked = busy || offline || !data;
  const cleaningLocked = locked || pageMode !== 'cleaning', reviewLocked = locked || pageMode !== 'review' || !data?.alignment;
  for (const id of ['detect','boxRemove','boxRestore','method','margin','neighbors','neighborDistance','showRemoved']) $(id).disabled = cleaningLocked;
  for (const id of ['buildSlices','saveNotes','thickness','minPoints','notes']) $(id).disabled = reviewLocked;
  $('restoreAll').disabled = cleaningLocked || !data?.removed_count;
  $('undo').disabled = cleaningLocked || !data?.can_undo; $('redo').disabled = cleaningLocked || !data?.can_redo;
  $('applySelection').disabled = cleaningLocked || !data?.candidate_count;
  $('clearSelection').disabled = cleaningLocked || !data?.candidate_count;
  for (const [status,id] of Object.entries({usable:'sliceUsable',unusable:'sliceUnusable',pending:'slicePending'})) {
    $(id).disabled = reviewLocked || !slices || (status === 'usable' && !slices.current.point_support_sufficient);
    $(id).setAttribute('aria-pressed', String(slices?.current.status === status));
  }
  $('sourceImage').disabled = locked || !data?.image_url;
  $('reloadData').disabled = busy || offline;
  $('coordinate').disabled = busy || !data || offline || pageMode === 'review';
  $('sliceSlider').disabled = busy || !slices;
  $('prevSlice').disabled = busy || !slices || sliceIndex <= 0;
  $('nextSlice').disabled = busy || !slices || sliceIndex >= (slices?.rows.length || 1)-1;
  $('margin').disabled = cleaningLocked || $('method').value === 'neighbor';
  for (const id of ['neighbors','neighborDistance']) $(id).disabled = cleaningLocked || $('method').value === 'far';
}
function isBody() { return $('coordinate').value === 'body'; }
function point(p) {
  if (!isBody() || (offline && data.offline_body)) return p;
  const m = data.alignment.frame.camera_to_body_4x4;
  return m.slice(0,3).map(row => row[0]*p[0]+row[1]*p[1]+row[2]*p[2]+row[3]);
}
function matrix() {
  const c = Math.cos(yaw), s = Math.sin(yaw), a = Math.sin(pitch), b = Math.cos(pitch);
  return isBody() ? [[c,-s,0],[a*s,a*c,-b],[b*s,b*c,a]] : [[c,0,s],[a*s,b,-a*c],[-b*s,a,b*c]];
}
function rotate(p, r = matrix()) { const a = p.map((v,j) => v-center[j]); return r.map(row => row.reduce((sum,v,j) => sum+v*a[j],0)); }
function currentView() {
  const w = canvas.clientWidth, h = canvas.clientHeight, scale = Math.min(w,h)*.7/extent*zoom;
  const r = matrix();
  // Pan is represented by a shifted world center, keeping server projection
  // identical to rendering and independent of devicePixelRatio.
  const adjusted = center.map((v,j) => v-(r[0][j]*pan[0]+r[1][j]*pan[1])/scale);
  return {rotation:r, center:adjusted, scale, size:[w,h]};
}
function fitView() {
  if (!data) return;
  let points = data.xyz.map((p,i) => ({p:point(p),i})).filter(o => !$('showRemoved').checked ? !data.removed[o.i] : true);
  const fit = $('fit').value;
  if (fit === 'selected') points = points.filter(o => data.selected[o.i]);
  if (!points.length) { message('当前取景没有可显示的点。'); return; }
  const bounds = [0,1,2].map(j => {
    const v = points.map(o => o.p[j]).sort((a,b) => a-b);
    const trimmed = fit === 'body' && v.length > 100;
    return [v[trimmed ? Math.floor(v.length*.005) : 0], v[trimmed ? Math.min(v.length-1,Math.floor(v.length*.995)) : v.length-1]];
  });
  center = bounds.map(b => (b[0]+b[1])/2); extent = Math.max(10,...bounds.map(b => b[1]-b[0]));
  zoom = 1; pan = [0,0]; draw();
}
function draw() {
  const w = canvas.clientWidth, h = canvas.clientHeight, d = devicePixelRatio || 1;
  if (!w || !h) return;
  canvas.width = Math.round(w*d); canvas.height = Math.round(h*d); ctx.setTransform(d,0,0,d,0,0);
  ctx.fillStyle = '#eef2f6'; ctx.fillRect(0,0,w,h); if (!data) return;
  const legend = pageMode === 'cleaning' ? '红色为选中点' : pageMode === 'review' ? (slices ? '橙色为当前切片' : data.alignment ? '正在载入切片' : '待坐标对齐') : '只读预览';
  $('viewBadge').textContent = `${isBody() ? '猪体' : '相机'}坐标 · ${legend} · 已删 ${data.removed_count.toLocaleString()} 点`;
  const names=isBody()?{top:'俯视 XY',side:'侧视 XZ',rear:'后视 YZ',oblique:'斜视'}:{top:'相机正视',side:'侧视',rear:'上方视图',oblique:'斜视'};
  document.querySelectorAll('[data-view]').forEach(b=>b.textContent=names[b.dataset.view]);
  const view = currentView(), r = view.rotation;
  const project = p => { const a=p.map((v,j)=>v-view.center[j]); return r.map(row=>row.reduce((sum,v,j)=>sum+v*a[j],0)); };
  const screen = p => { const a=project(p); return [w/2+a[0]*view.scale,h/2+a[1]*view.scale]; };
  function line(a,b,color,width=1) { ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(...screen(a));ctx.lineTo(...screen(b));ctx.stroke(); }
  if (isBody()) {
    const span = Math.min(5000,Math.max(1000,extent*1.5)), step = span > 2500 ? 500 : 250;
    for(let v=-span;v<=span;v+=step) { line([v,-span,0],[v,span,0],'#d4dfe5');line([-span,v,0],[span,v,0],'#d4dfe5'); }
    [[250,0,0],[0,250,0],[0,0,250]].forEach((p,i)=>{ const c=['#c6413d','#238451','#386ccd'][i];line([0,0,0],p,c,2);ctx.fillStyle=c;ctx.font='11px system-ui';ctx.fillText(['X 颈根','Y 左侧','Z 上方'][i],...screen(p)); });
  }
  const slice = isBody() && slices && pageMode === 'review' ? slices.current : null;
  const rendered=[];
  for(let i=0;i<data.xyz.length;i++) {
    if(data.removed[i] && !$('showRemoved').checked) continue;
    const p=point(data.xyz[i]);rendered.push({p,q:project(p),i});
  }
  rendered.sort((a,b)=>b.q[2]-a.q[2]);
  const highlights=[];
  for(const o of rendered) {
    const selected=data.selected[o.i];
    const inSlice=slice && o.p[0]>=slice.x_start_mm && o.p[0]<=slice.x_end_mm;
    const color=selected ? [219,66,54] : data.removed[o.i] ? [175,182,190] : inSlice ? [226,146,42] : slice ? [191,202,210] : data.rgb[o.i];
    const x=w/2+o.q[0]*view.scale,y=h/2+o.q[1]*view.scale;
    if(selected) highlights.push({x,y});
    ctx.fillStyle=`rgb(${color.join(',')})`;ctx.fillRect(x,y,selected?3:1.8,selected?3:1.8);
  }
  // Ensure rare candidate points can be seen without being covered by the body.
  ctx.fillStyle='#db4236';for(const p of highlights)ctx.fillRect(p.x-1,p.y-1,3,3);
  if(rectangle){ctx.fillStyle='#db423622';ctx.strokeStyle='#b94d42';ctx.lineWidth=1;ctx.fillRect(rectangle[0],rectangle[1],rectangle[2]-rectangle[0],rectangle[3]-rectangle[1]);ctx.strokeRect(rectangle[0],rectangle[1],rectangle[2]-rectangle[0],rectangle[3]-rectangle[1]);}
  $('viewHint').textContent = boxMode ? '拖动框选 · 贯穿当前视角前后所有点 · Esc 取消框选' : `拖动旋转 · 滚轮缩放 · Shift 拖动平移 · 预览 ${data.xyz.length.toLocaleString()} 点，选点/测量使用完整点云`;
}
function setBox(mode) {
  if (pageMode !== 'cleaning' || busy || offline) return;
  boxMode = boxMode === mode ? '' : mode;
  if (mode === 'restore') $('showRemoved').checked = true;
  $('boxRemove').setAttribute('aria-pressed',String(boxMode==='remove'));$('boxRestore').setAttribute('aria-pressed',String(boxMode==='restore'));
  $('selectionNotice').hidden = !boxMode; canvas.style.cursor=boxMode?'crosshair':'grab';draw();
}
canvas.onpointerdown = e => {
  if (busy || !data) return;
  const b=canvas.getBoundingClientRect(),x=e.clientX-b.left,y=e.clientY-b.top;
  drag={x,y,lastX:x,lastY:y,mode:boxMode || (e.shiftKey?'pan':'rotate'),view:currentView()};
  if(boxMode)rectangle=[x,y,x,y];canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove = e => {
  if(!drag)return;const b=canvas.getBoundingClientRect(),x=Math.max(0,Math.min(canvas.clientWidth,e.clientX-b.left)),y=Math.max(0,Math.min(canvas.clientHeight,e.clientY-b.top));
  if(['remove','restore'].includes(drag.mode))rectangle=[drag.x,drag.y,x,y];
  else if(drag.mode==='pan'){pan[0]+=x-drag.lastX;pan[1]+=y-drag.lastY;}
  else {yaw+=(x-drag.lastX)*.008;pitch+=(y-drag.lastY)*.008;}
  drag.lastX=x;drag.lastY=y;draw();
};
canvas.onpointerup = () => {
  const saved=drag,box=rectangle;drag=null;rectangle=null;
  if(saved && ['remove','restore'].includes(saved.mode) && box && Math.abs(box[2]-box[0])>3 && Math.abs(box[3]-box[1])>3) {
    setBox('');action(async()=>{apply(await api('select',base({action:saved.mode,coordinate:$('coordinate').value,view:saved.view,rectangle:box})));message('已按完整点云选中；请转动核对或查看原图，再确认。');});
  } else draw();
};
canvas.onpointercancel=()=>{drag=null;rectangle=null;draw();};
canvas.onwheel=e=>{e.preventDefault();if(busy || drag)return;zoom=Math.max(.05,Math.min(30,zoom*Math.exp(-e.deltaY*.001)));draw();};
function resetView(){yaw=-.35;pitch=isBody() ? .6 : -.4;fitView();}
canvas.ondblclick=resetView;$('resetView').onclick=resetView;$('fit').onchange=fitView;
$('showRemoved').onchange=draw;$('method').onchange=controls;
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{
  [yaw,pitch]=isBody()?({top:[0,Math.PI/2],side:[0,0],rear:[Math.PI/2,0],oblique:[-.35,.6]})[b.dataset.view]:({top:[0,0],side:[Math.PI/2,0],rear:[0,Math.PI/2],oblique:[-.35,-.4]})[b.dataset.view];draw();
});
$('coordinate').onchange=()=>{resetView();apply(data);};
$('togglePanel').onclick=()=>{const hidden=!$('processingPanel').hidden;$('processingPanel').hidden=hidden;$('togglePanel').textContent=hidden?'显示工具':'隐藏工具';$('togglePanel').setAttribute('aria-expanded',String(!hidden));draw();};
$('boxRemove').onclick=()=>setBox('remove');$('boxRestore').onclick=()=>setBox('restore');
window.addEventListener('keydown',e=>{if(e.key==='Escape'){boxMode='';rectangle=null;drag=null;$('selectionNotice').hidden=true;for(const id of ['boxRemove','boxRestore'])$(id).setAttribute('aria-pressed','false');draw();}});
$('detect').onclick=()=>action(async()=>{message('正在检测完整点云…');apply(await api('detect',base({method:$('method').value,margin_mm:Number($('margin').value),neighbor_mm:Number($('neighborDistance').value),neighbors:Number($('neighbors').value)})));message(data.candidate_count?'候选已高亮；可切换“全部点”或“选中点”取景，核对后再删除。':'当前参数下未检出候选。');});
$('clearSelection').onclick=()=>action(async()=>{apply(await api('clear',base()));message('选点已取消。');});
async function edit(operation) {
  apply(await api('edit',base({operation,candidate_id:data.candidate_id})));
  if ($('fit').value === 'selected') { $('fit').value='body'; fitView(); }
  if(slices)await loadSlice(sliceIndex);
  notify('saved');message('清理版本已暂存，可撤销；测量切片需按新版本复核。');
}
$('applySelection').onclick=()=>action(()=>edit('apply'));
for(const op of ['undo','redo'])$(op).onclick=()=>action(()=>edit(op));
$('restoreAll').onclick=()=>action(()=>edit('reset'));
function drawSlice() {
  const c=$('slicePlot'),g=c.getContext('2d'),w=c.clientWidth,h=c.clientHeight,d=devicePixelRatio||1;
  if(!w||!h)return;c.width=w*d;c.height=h*d;g.setTransform(d,0,0,d,0,0);g.fillStyle='#f4f6f9';g.fillRect(0,0,w,h);
  const points=slices?.yz || [];g.font='10px system-ui';g.fillStyle='#68798b';g.fillText('Y 左右方向 / Z 距地面法向距离（mm）',9,15);
  if(!points.length){g.fillText('当前切片无有效点',15,h/2);return;}
  const ys=points.map(p=>p[0]),zs=points.map(p=>p[1]);
  const loY=Math.min(...ys),hiY=Math.max(...ys),loZ=Math.min(...zs),hiZ=Math.max(...zs),scale=Math.min((w-35)/Math.max(hiY-loY,1),(h-45)/Math.max(hiZ-loZ,30));
  const midY=(loY+hiY)/2,midZ=(loZ+hiZ)/2;
  g.fillStyle='#467e73';for(const p of points)g.fillRect(w/2+(p[0]-midY)*scale,h/2+10-(p[1]-midZ)*scale,2,2);
  const row=slices.current;g.fillStyle='#b46d27';g.fillText(`可见跨度 ${fmt(row.candidate_width_mm)} mm`,9,h-8);
}
function showSummary(summary) {
  const box=$('featureSummary');box.replaceChildren();
  const lines=[`端点地面投影长度：${fmt(summary.length_ground_projection_mm)} mm`,
    `已确认可测 ${summary.usable_slices}/${summary.slice_count} 片；不可测 ${summary.unusable_slices} 片`];
  for(const part of summary.regions || [])lines.push(`${['后','中','前'][part.region]}段最大可见宽度：候选 ${fmt(part.candidate_max_visible_width_mm)} / 确认 ${fmt(part.confirmed_max_visible_width_mm)} mm`);
  for(const [name,w] of Object.entries(summary.fixed_widths || {}))lines.push(`${name} 所在带宽：候选 ${fmt(w.candidate_width_mm)} / 确认 ${fmt(w.confirmed_width_mm)} mm`);
  for(const line of lines){const p=document.createElement('p');p.textContent=line;box.append(p);}
}
function showSlice(response) {
  slices=response;sliceIndex=response.slice_index;
  $('sliceSlider').max=slices.rows.length-1;$('sliceSlider').value=sliceIndex;
  const r=slices.current;
  $('sliceLabel').textContent=`${sliceIndex+1}/${slices.rows.length} · ${['后','中','前'][r.region]}段 · X ${fmt(r.x_start_mm)}–${fmt(r.x_end_mm)} mm`;
  $('sliceInfo').textContent=`${r.points.toLocaleString()} 点 · 候选宽 ${fmt(r.candidate_width_mm)} mm · ${({usable:'已确认可测',unusable:'已标记不可测',pending:'待人工复核'})[r.status]}`;
  showSummary(slices.summary);controls();drawSlice();draw();
}
async function loadSlice(index=0) {
  if(!data?.alignment || offline)return;
  const request=++sliceRequest;
  const response=await api('slices',base({slice_index:index}));
  if(request===sliceRequest)showSlice(response);
}
function navigateSlice(index) {
  if(busy||!slices)return;
  const bounded=Math.max(0,Math.min(slices.rows.length-1,index));
  if(offline){showSlice(offlineSlice(bounded));return;}
  clearTimeout(sliceTimer);sliceTimer=setTimeout(()=>action(()=>loadSlice(bounded)),70);
}
$('sliceSlider').oninput=()=>navigateSlice(Number($('sliceSlider').value));
$('prevSlice').onclick=()=>navigateSlice(sliceIndex-1);$('nextSlice').onclick=()=>navigateSlice(sliceIndex+1);
$('measurementSection').ontoggle=()=>{draw();drawSlice();};
$('buildSlices').onclick=()=>action(async()=>{apply(await api('review',base({thickness_mm:Number($('thickness').value),min_points:Number($('minPoints').value)})));await loadSlice(0);notify('saved');message('切片参数已保存；请检查左右边界。');});
for(const [status,id] of Object.entries({usable:'sliceUsable',unusable:'sliceUnusable',pending:'slicePending'}))$(id).onclick=()=>action(async()=>{
  apply(await api('review',base({slice_index:sliceIndex,status})));
  const next=$('autoNextSlice').checked?Math.min(sliceIndex+1,slices.rows.length-1):sliceIndex;
  await loadSlice(next);notify('saved');message('切片标记已保存。');
});
$('notes').oninput=()=>notesDirty=true;
$('saveNotes').onclick=()=>action(async()=>{const response=await api('review',base({notes:$('notes').value}));notesDirty=false;apply(response);notify('saved');message('测量备注已保存。');});
$('reloadData').onclick=()=>action(async()=>{slices=null;apply(await api('context',identity),true);if($('measurementSection').open && data.alignment)await loadSlice(0);message('已重新载入已暂存的清理版本。');});
async function openImage() {
  sourceKind=pageMode === 'review' && slices?'slice':'candidate';
  const [response,points]=await Promise.all([fetch(data.image_url),api('image_points',base({kind:sourceKind,slice_index:sliceIndex}))]);
  if(!response.ok){const error=await response.json();throw Error(error.error||'定位底图不可用');}
  const blob=await response.blob(),url=URL.createObjectURL(blob),im=new Image();
  try{await new Promise((resolve,reject)=>{im.onload=resolve;im.onerror=()=>reject(Error('定位底图无法显示'));im.src=url;});
    const c=$('imageCanvas'),g=c.getContext('2d');c.width=im.naturalWidth;c.height=im.naturalHeight;g.drawImage(im,0,0);
    const [height,width]=points.shape_hw,sx=c.width/width,sy=c.height/height;
    g.fillStyle=sourceKind==='slice'?'#e59623':'#db4236';for(const [u,v] of points.uv)g.fillRect(u*sx-1,v*sy-1,2.5,2.5);
    $('imageHint').textContent=`绿色为${data.report.reviewed?'已确认掩码':'生成所用掩码（未审核）'}；${sourceKind==='slice'?'橙色为当前切片':'红色为选中点'}，共 ${points.count.toLocaleString()} 点。底图使用分割对应的图像坐标。`;
    $('imageDialog').showModal();
  }finally{URL.revokeObjectURL(url);}
}
$('sourceImage').onclick=()=>action(openImage);$('closeImage').onclick=()=>$('imageDialog').close();
window.addEventListener('resize',()=>{draw();drawSlice();});
function offlineSlice(index) {
  const row=data.rows[index];
  const yz=data.xyz.filter(p=>p[0]>=row.x_start_mm && (index===data.rows.length-1?p[0]<=row.x_end_mm:p[0]<row.x_end_mm)).map(p=>p.slice(1));
  return {rows:data.rows,summary:data.summary,current:row,slice_index:index,yz};
}
async function start() {
  configureMode();
  if(offline){
    data={...OFFLINE_DATA,candidate_count:0};$('coordinate').value=data.offline_body?'body':'camera';
    $('sourceImage').hidden=true;$('offlineFiles').hidden=false;
    $('bodyFile').hidden=!data.alignment;$('sliceFile').hidden=!data.alignment;
    apply(data,true);pitch=data.offline_body ? .6 : -.4;draw();
    if(data.rows.length && data.alignment){$('measurementSection').open=true;showSlice(offlineSlice(0));}
    message('只读清理成果；切片数值来自完整点云。原始点云、清理后点云和操作历史分别保存。');return;
  }
  await action(async()=>{
    const response=await api('context',identity);
    $('coordinate').value=pageMode === 'review' && response.alignment ? 'body' : 'camera';
    pitch=response.alignment && pageMode === 'review' ? .6 : -.4;
    apply(response,true);
    if(pageMode === 'review' && response.alignment) await loadSlice(0);
    message(pageMode === 'review' ? (response.alignment ? '已载入对齐点云；请逐片复核左右边界。' : '尚未完成坐标对齐，请先回到“坐标对齐”确认地面、尾根和颈根。') : '已载入完整点云；先检测或框选，再确认删除。');
  });
}
start().finally(()=>notify('busy',{busy:false}));
