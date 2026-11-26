'use strict';
const $ = id => document.getElementById(id), canvas = $('cloud'), ctx = canvas.getContext('2d');
const offline = typeof OFFLINE_DATA !== 'undefined';
const query = new URLSearchParams(location.search), identity = {id: query.get('id'), dataset_id: query.get('dataset_id')};
const pageMode = offline ? (OFFLINE_DATA.alignment ? 'review' : 'preview') : (query.get('mode') === 'review' ? 'review' : 'cleaning');
let token = APP_TOKEN, data, busy = false, boxMode = '', drag, rectangle;
let yaw = pageMode === 'review' ? 0 : -.35, pitch = pageMode === 'review' ? Math.PI/2 : -.4;
let zoom = 1, pan = [0, 0], center = [0, 0, 0], extent = 1;
let sourceKind = 'candidate';
let geometry, profileIndex = 0, profileYZ = [], profileTimer;
let geometryDirty = false;
let profileRange = null, profileRangeAnchor = null, profileRangeDragged = false;
const fmt = v => v == null ? '—' : Number(v).toFixed(1);
function configureMode() {
  const cleaning = pageMode === 'cleaning';
  const review = pageMode === 'review';
  $('cleaningSection').hidden = !cleaning;
  $('geometrySection').hidden = !review;
  $('geometrySection').open = review;
  $('cleaningSection').open = cleaning;
  document.title = review ? '测量复核' : cleaning ? '点云清理' : '清理成果预览';
  $('summaryTitle').textContent = review ? '测量复核' : cleaning ? '点云查看与清理' : '清理成果预览';
  $('fit').querySelector('[value=selected]').hidden = !cleaning;
  document.body.dataset.mode = pageMode;
}
function notify(action, extra = {}) { if (!offline && parent !== window) parent.postMessage({type:'pointcloud-processing', ...identity,
  editor_instance:query.get('editor_instance') || '', action, ...extra}, location.origin); }
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
  if (pageMode === 'review') {
    const span = data.alignment?.frame.neck_tail_horizontal_span_mm;
    $('alignmentHint').textContent = Number.isFinite(span) ? `本次对齐 · 尾根到颈根的地面投影 ${fmt(span)} mm` : '';
  } else {
    $('alignmentHint').textContent = data.alignment ? '坐标参考已确认，清理版本沿用同次采集的参考。' : '先查看并保守清理飞点，再进入坐标对齐。';
  }
  $('originalQuality').textContent = data.report ? `生成时有效深度率 ${(data.report.original_valid_depth_ratio*100).toFixed(2)}%；删点后不会将它改记为 100%。` : `已删除 ${data.removed_count.toLocaleString()} 点；原始点云另存。`;
  $('originalQuality').title=$('originalQuality').textContent;
  if(['review','cleaning'].includes(pageMode) && data.report)$('originalQuality').textContent=`生成时有效深度率 ${(data.report.original_valid_depth_ratio*100).toFixed(2)}% · 删点不改写`;
  $('geometryLock').hidden = !!data.alignment;
  $('geometryControls').hidden = !data.alignment;
  const config=data.measurement?.feature_settings || {};
  $('profileCount').value=config.count || 32;$('profileThickness').value=config.thickness_mm || 20;
  $('profileMinimum').value=config.min_points || 20;$('profileGrid').value=config.grid_mm || 10;
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
  for (const id of ['computeGeometry','profileCount','profileThickness','profileMinimum','profileGrid']) $(id).disabled=reviewLocked;
  for (const id of ['profileRangeStart','profileRangeEnd','selectAllProfiles']) $(id).disabled=busy || !geometry || geometryDirty;
  for (const id of ['batchProfileUsable','batchProfileUnusable','batchProfilePending']) $(id).disabled=reviewLocked || !geometry || geometryDirty || !profileRange;
  for(const [status,id] of Object.entries({usable:'profileUsable',unusable:'profileUnusable',pending:'profilePending'})) {
    $(id).disabled=reviewLocked || !geometry || geometryDirty || (status==='usable' && geometry.profiles[profileIndex].width_q02_q98_mm==null);
    $(id).setAttribute('aria-pressed',String(geometry?.profiles[profileIndex]?.status===status));
  }
  $('profileSlider').disabled=busy || !geometry || geometryDirty;
  $('prevProfile').disabled=busy || !geometry || geometryDirty || profileIndex<=0;
  $('nextProfile').disabled=busy || !geometry || geometryDirty || profileIndex>=(geometry?.profiles.length || 1)-1;
  $('restoreAll').disabled = cleaningLocked || !data?.removed_count;
  $('undo').disabled = cleaningLocked || !data?.can_undo; $('redo').disabled = cleaningLocked || !data?.can_redo;
  $('applySelection').disabled = cleaningLocked || !data?.candidate_count;
  $('clearSelection').disabled = cleaningLocked || !data?.candidate_count;
  $('sourceImage').disabled = locked || !data?.image_url;
  $('alignmentMarks').hidden = pageMode !== 'review' || offline || !data?.alignment_revision || !data.alignment;
  $('alignmentMarks').disabled = locked;
  $('reloadData').disabled = busy || offline;
  $('coordinate').disabled = busy || !data || offline || pageMode === 'review';
  $('margin').disabled = cleaningLocked || $('method').value === 'neighbor';
  for (const id of ['neighbors','neighborDistance']) $(id).disabled = cleaningLocked || $('method').value === 'far';
}
function isBody() { return $('coordinate').value === 'body'; }
function cameraToBody(p) {
  const m = data.alignment.frame.camera_to_body_4x4;
  return m.slice(0,3).map(row => row[0]*p[0]+row[1]*p[1]+row[2]*p[2]+row[3]);
}
function point(p) {
  return isBody() && !(offline && data.offline_body) ? cameraToBody(p) : p;
}
function centerlinePoint(xy) {
  const annotations=data.alignment?.annotations;
  if(geometry?.centerline?.version?.startsWith('endpoint-constrained-') && annotations?.tail && annotations?.neck) {
    const tail=cameraToBody(annotations.tail.xyz_camera_mm),neck=cameraToBody(annotations.neck.xyz_camera_mm);
    const t=xy[0]/data.alignment.frame.neck_tail_horizontal_span_mm;
    // Only XY is measured. Interpolate display height so both ends touch the
    // marked points in every view; this is not a measured dorsal ridge.
    return [...xy,tail[2]+t*(neck[2]-tail[2])];
  }
  return [...xy,center[2]];
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
  const legend = pageMode === 'cleaning' ? '红色为选中点' : pageMode === 'review' ? (geometry ? '蓝线为 XY 中心线（高度示意）· 橙色为局部剖面' : data.alignment ? '已对齐' : '待坐标对齐') : '只读预览';
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
  const profile = isBody() && geometry ? geometry.profiles[profileIndex] : null;
  const rendered=[];
  for(let i=0;i<data.xyz.length;i++) {
    if(data.removed[i] && !$('showRemoved').checked) continue;
    const p=point(data.xyz[i]);rendered.push({p,q:project(p),i});
  }
  rendered.sort((a,b)=>b.q[2]-a.q[2]);
  const highlights=[];
  for(const o of rendered) {
    const selected=data.selected[o.i];
    const inSlice=profile && inProfile(o.p,profile);
    const color=selected ? [219,66,54] : data.removed[o.i] ? [175,182,190] : inSlice ? [226,146,42] : profile ? [191,202,210] : data.rgb[o.i];
    const x=w/2+o.q[0]*view.scale,y=h/2+o.q[1]*view.scale;
    if(selected) highlights.push({x,y});
    ctx.fillStyle=`rgb(${color.join(',')})`;ctx.fillRect(x,y,selected?3:1.8,selected?3:1.8);
  }
  // Ensure rare candidate points can be seen without being covered by the body.
  ctx.fillStyle='#db4236';for(const p of highlights)ctx.fillRect(p.x-1,p.y-1,3,3);
  if(profile) {
    const xy=geometry.centerline.xy_mm,valid=geometry.centerline.supported;
    const z=centerlinePoint([profile.center_x_mm,profile.center_y_mm])[2];
    for(let i=1;i<xy.length;i++)if(valid[i-1]&&valid[i])line(centerlinePoint(xy[i-1]),centerlinePoint(xy[i]),'#3979c8',2);
    const u=profile.width_raw_span_mm || 200;
    line([profile.center_x_mm-profile.tangent_y*u/2,profile.center_y_mm+profile.tangent_x*u/2,z],
      [profile.center_x_mm+profile.tangent_y*u/2,profile.center_y_mm-profile.tangent_x*u/2,z],'#d88920',2);
  }
  if (pageMode === 'review' && isBody()) {
    const annotations = data.alignment.annotations;
    for (const [key, label, color] of [['tail','尾根','#a7442d'], ['neck','颈根','#2859a8']]) {
      const anchor = annotations?.[key]?.xyz_camera_mm;
      if (!anchor) continue;
      const [x,y] = screen(cameraToBody(anchor));
      ctx.beginPath(); ctx.arc(x,y,4,0,2*Math.PI);
      ctx.fillStyle = color; ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke();
      ctx.font = 'bold 12px system-ui'; ctx.lineWidth = 3;
      ctx.strokeText(label,x+7,y-6); ctx.fillText(label,x+7,y-6);
    }
  }
  if(rectangle){ctx.fillStyle='#db423622';ctx.strokeStyle='#b94d42';ctx.lineWidth=1;ctx.fillRect(rectangle[0],rectangle[1],rectangle[2]-rectangle[0],rectangle[3]-rectangle[1]);ctx.strokeRect(rectangle[0],rectangle[1],rectangle[2]-rectangle[0],rectangle[3]-rectangle[1]);}
  $('viewHint').textContent = boxMode ? '连续拖动追加选区 · Alt 拖动旋转 · Shift 拖动平移 · Esc 退出框选' : `拖动旋转 · 滚轮缩放 · Shift 拖动平移 · 预览 ${data.xyz.length.toLocaleString()} 点，选点/测量使用完整点云`;
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
  drag={x,y,lastX:x,lastY:y,mode:e.shiftKey?'pan':e.altKey?'rotate':boxMode || 'rotate',view:currentView()};
  if(['remove','restore'].includes(drag.mode))rectangle=[x,y,x,y];canvas.setPointerCapture(e.pointerId);
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
  draw();
  if(saved && ['remove','restore'].includes(saved.mode) && box && Math.abs(box[2]-box[0])>3 && Math.abs(box[3]-box[1])>3) {
    action(async()=>{apply(await api('select',base({action:saved.mode,append:true,candidate_id:data.candidate_id || '',coordinate:$('coordinate').value,view:saved.view,rectangle:box})));message(`已累计选中 ${data.candidate_count.toLocaleString()} 点；可继续框选，核对后确认${saved.mode==='restore'?'恢复':'删除'}。`);});
  } else draw();
};
canvas.onpointercancel=()=>{drag=null;rectangle=null;draw();};
canvas.onwheel=e=>{e.preventDefault();if(busy || drag)return;zoom=Math.max(.05,Math.min(30,zoom*Math.exp(-e.deltaY*.001)));draw();};
function setDefaultView(){const top=pageMode==='review'&&isBody();yaw=top?0:-.35;pitch=top?Math.PI/2:isBody()?.6:-.4;}
function resetView(){setDefaultView();fitView();}
canvas.ondblclick=()=>{if(!boxMode)resetView();};$('resetView').onclick=resetView;$('fit').onchange=fitView;
$('showRemoved').onchange=draw;$('method').onchange=controls;
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{
  [yaw,pitch]=isBody()?({top:[0,Math.PI/2],side:[0,0],rear:[Math.PI/2,0],oblique:[-.35,.6]})[b.dataset.view]:({top:[0,0],side:[Math.PI/2,0],rear:[0,Math.PI/2],oblique:[-.35,-.4]})[b.dataset.view];draw();
});
$('coordinate').onchange=()=>{resetView();apply(data);};
$('togglePanel').onclick=()=>{const hidden=!$('processingPanel').hidden;$('processingPanel').hidden=hidden;$('togglePanel').textContent=hidden?'显示工具':'隐藏工具';$('togglePanel').setAttribute('aria-expanded',String(!hidden));draw();};
$('boxRemove').onclick=()=>setBox('remove');$('boxRestore').onclick=()=>setBox('restore');
window.addEventListener('keydown',e=>{if(e.key==='Escape'){boxMode='';rectangle=null;drag=null;canvas.style.cursor='grab';$('selectionNotice').hidden=true;for(const id of ['boxRemove','boxRestore'])$(id).setAttribute('aria-pressed','false');draw();}});
$('detect').onclick=()=>action(async()=>{message('正在检测完整点云…');apply(await api('detect',base({method:$('method').value,margin_mm:Number($('margin').value),neighbor_mm:Number($('neighborDistance').value),neighbors:Number($('neighbors').value)})));message(data.candidate_count?'候选已高亮；可切换“全部点”或“选中点”取景，核对后再删除。':'当前参数下未检出候选。');});
$('clearSelection').onclick=()=>action(async()=>{apply(await api('clear',base()));message('选点已取消。');});
async function edit(operation) {
  apply(await api('edit',base({operation,candidate_id:data.candidate_id})));
  if ($('fit').value === 'selected') { $('fit').value='body'; fitView(); }
  geometry=null;profileIndex=0;
  notify('saved');message('清理版本已暂存，可撤销；'+(boxMode?'可继续框选。':'测量剖面需按新版本复核。'));
}
$('applySelection').onclick=()=>action(()=>edit('apply'));
for(const op of ['undo','redo'])$(op).onclick=()=>action(()=>edit(op));
$('restoreAll').onclick=()=>action(()=>edit('reset'));
$('reloadData').onclick=()=>action(async()=>{geometry=null;profileIndex=0;apply(await api('context',identity),true);if(data.alignment && pageMode==='review'){await loadGeometry();}message('已重新载入已暂存的清理版本。');});
async function readImage(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const error = await response.json();
    throw Error(error.error || '定位底图不可用');
  }
  const url = URL.createObjectURL(await response.blob()), image = new Image();
  try {
    image.src = url;
    await image.decode();
    return image;
  } finally { URL.revokeObjectURL(url); }
}
function drawDialogImage(image) {
  const target = $('imageCanvas'), painter = target.getContext('2d');
  target.width = image.naturalWidth;
  target.height = image.naturalHeight;
  painter.drawImage(image,0,0);
  return painter;
}
async function openImage() {
  sourceKind = pageMode === 'review' && geometry ? 'profile' : 'candidate';
  const [image, points] = await Promise.all([
    readImage(data.image_url), api('image_points', base({kind:sourceKind, profile_index:profileIndex}))
  ]);
  const g = drawDialogImage(image);
  const [height,width] = points.shape_hw, sx = image.naturalWidth/width, sy = image.naturalHeight/height;
  g.fillStyle = sourceKind === 'candidate' ? '#db4236' : '#e59623';
  for (const [u,v] of points.uv) g.fillRect(u*sx-1,v*sy-1,2.5,2.5);
  $('imageTitle').textContent = '分割底图上的位置';
  $('imageHint').textContent = `绿色为${data.report.reviewed?'已确认掩码':'生成所用掩码（未审核）'}。${sourceKind==='candidate'?'红色为选中点':'橙色为当前局部法向剖面'}，共 ${points.count.toLocaleString()} 点。`;
  $('imageDialog').showModal();
}
async function openAlignmentMarks() {
  const params = new URLSearchParams({...identity, file:'alignment_check.png', alignment_revision:data.alignment_revision});
  const image = await readImage('/api/media?' + params);
  drawDialogImage(image);
  const {annotations} = data.alignment;
  const tailHeight = cameraToBody(annotations.tail.xyz_camera_mm)[2];
  const neckHeight = cameraToBody(annotations.neck.xyz_camera_mm)[2];
  $('imageTitle').textContent = '本次确认的对齐标注';
  $('imageHint').textContent = `橙色 Tail 是尾根，紫色 Neck 是颈根，黄框是圈定的地面，绿点参与地面拟合。尾根距地 ${fmt(tailHeight)} mm，颈根距地 ${fmt(neckHeight)} mm。`;
  $('imageDialog').showModal();
}
$('sourceImage').onclick = () => action(openImage);
$('alignmentMarks').onclick = () => action(openAlignmentMarks);
$('closeImage').onclick = () => $('imageDialog').close();
window.addEventListener('resize',()=>{draw();drawGeometryPlots();});
function inProfile(p,r) {
  const dx=p[0]-r.center_x_mm,dy=p[1]-r.center_y_mm;
  const length=data.alignment.frame.neck_tail_horizontal_span_mm;
  return p[0]>=0 && p[0]<=length && Math.abs(dx*r.tangent_x+dy*r.tangent_y)<=geometry.settings.thickness_mm/2 && Math.abs(dx)<=length*.2;
}
async function loadGeometry(extra={}) {
  if(offline || !data.alignment)return;
  const reference = data.alignment.reference_id;
  const response = await api('features', base({profile_index:profileIndex, ...extra, alignment_reference_id:reference}));
  if (reference && response.features.provenance?.reference_id !== reference) {
    geometry = null;
    $('geometrySummary').replaceChildren();
    $('profileInfo').textContent = '';
    draw();drawGeometryPlots();
    throw Error('测量结果与画面所用的坐标不同，请点击“重新载入”');
  }
  data.revision=response.revision;
  showGeometry(response);
  return response;
}
function showGeometry(response) {
  const previousCount=geometry?.profiles.length;
  geometry=response.features;profileIndex=response.profile_index || 0;geometryDirty=false;
  const config=geometry.settings,quality=geometry.conditions;
  data.measurement={...data.measurement,feature_settings:config,feature_quality:quality};
  $('profileCount').value=config.count;$('profileThickness').value=config.thickness_mm;
  $('profileMinimum').value=config.min_points;$('profileGrid').value=config.grid_mm;
  const row=geometry.profiles[profileIndex];
  profileYZ=response.profile_yz || data.xyz.map(point).filter(p=>inProfile(p,row)).map(p=>[
    -(p[0]-row.center_x_mm)*row.tangent_y+(p[1]-row.center_y_mm)*row.tangent_x,p[2]]);
  $('geometryWarnings').textContent=geometry.quality.warnings.join(' ') || '计算完成；可拖选剖面范围批量复核，不适用的采集可直接在顶部淘汰。';
  for(const id of ['profileRangeStart','profileRangeEnd']) $(id).max=config.count;
  if(!profileRange || previousCount!==config.count)setProfileRange(0,config.count-1);
  const box=$('geometrySummary');box.replaceChildren();
  const progress=document.createElement('p');progress.className='geometry-progress';
  const usable=geometry.profiles.filter(row=>row.status==='usable').length,unusable=geometry.profiles.filter(row=>row.status==='unusable').length;
  progress.textContent=`可算 ${geometry.quality.computable_profiles}/${config.count} 片 · 已确认可测 ${usable}/${config.count} 片 · 淘汰 ${unusable} 片 · 待定 ${config.count-usable-unusable} 片 · 横剖面弧长可算 ${geometry.quality.computable_transverse_arcs ?? '—'} 片`;
  box.append(progress);
  const labels={endpoint_ground_chord_mm:'端点投影距离',visible_centerline_arc_mm:'可见中心线弧长',centerline_arc_chord_ratio:'弯曲比（弧/弦）',
    profile_width_median_mm:'剖面宽度中位数',profile_width_max_mm:'剖面宽度最大值',profile_width_p90_mm:'剖面宽度 P90',
    width_25pct_mm:'弧长 25% 宽度',width_50pct_mm:'弧长 50% 宽度',width_75pct_mm:'弧长 75% 宽度',
    visible_height_p95_mm:'距地面高度 P95',relative_height_p95_p05_mm:'相对高度 P95−P05',
    observed_grid_area_mm2:'已观测网格面积',observed_positive_height_integral_mm3:'已观测高度积分代理'};
  for(const [key,g] of Object.entries(geometry.geometry)) {
    const value=g.value==null?'缺失':g.unit==='mm2'?`${(g.value/1e6).toFixed(4)} m²`:g.unit==='mm3'?`${(g.value/1e6).toFixed(2)} L`:g.unit==='ratio'?g.value.toFixed(4):`${fmt(g.value)} ${g.unit}`;
    const metric=document.createElement('div');metric.className='geometry-metric';
    const label=document.createElement('span');label.className='metric-label';label.title=labels[key] || key;
    label.textContent=({relative_height_p95_p05_mm:'相对高度差',observed_positive_height_integral_mm3:'高度积分代理'})[key] || labels[key] || key;
    const number=document.createElement('span');number.className='metric-value';number.textContent=value;
    metric.append(label,number);
    if(g.valid && !g.reviewed){const flag=document.createElement('small');flag.className='metric-candidate';flag.textContent='候选';number.append(' ',flag);}
    box.append(metric);
  }
  $('profileSlider').max=config.count-1;$('profileSlider').value=profileIndex;
  const arcReasons={narrow_span:'跨度不足',transverse_gap_or_sparse_bin:'横向缺点',transverse_height_jump:'高度跳变',profile_unavailable:'剖面不可算'};
  const arcText=row.visible_transverse_arc_mm==null?`横剖面弧长缺失${arcReasons[row.visible_transverse_arc_missing_reason]?'（'+arcReasons[row.visible_transverse_arc_missing_reason]+'）':''}`:`横剖面弧长 ${fmt(row.visible_transverse_arc_mm)} mm`;
  $('profileInfo').textContent=`${profileIndex+1}/${config.count} · 位置 ${(row.fraction*100).toFixed(1)}% · ${row.point_count.toLocaleString()} 点 · 宽 ${fmt(row.width_q02_q98_mm)} mm · ${arcText} · ${row.missing_reason==='centerline_gap'?'中心线缺支撑':row.missing_reason?'点数不足':({usable:'已确认可测',unusable:'已标记不可测',pending:'待复核'})[row.status]}`;
  controls();drawGeometryPlots();draw();
}
function drawGeometryPlots() {
  for(const id of ['profileTrend','profilePlot']) {
    const c=$(id),w=c.clientWidth,h=c.clientHeight,d=devicePixelRatio||1;
    if(!w||!h)continue;
    c.width=w*d;c.height=h*d;const g=c.getContext('2d');g.setTransform(d,0,0,d,0,0);
    g.fillStyle='#f4f6f9';g.fillRect(0,0,w,h);g.font='10px system-ui';g.fillStyle='#68798b';
    if(!geometry)continue;
    if(id==='profileTrend') {
      const key=$('trendChannel').value,points=geometry.profiles.map(r=>r[key]),valid=points.filter(v=>v!=null);
      g.fillText('尾根 0% → 颈躯连接 100%（按拟合弧长）',8,h-6);
      if(!valid.length){g.fillText('此通道缺失',10,25);continue;}
      const lo=Math.min(...valid),hi=Math.max(...valid),span=Math.max(hi-lo,Math.abs(hi)*.01,.000001);
      const sx=i=>15+i/(points.length-1)*(w-30),sy=v=>h-27-(v-lo)/span*(h-47);
      if(profileRange){g.fillStyle='#417f731c';g.fillRect(sx(profileRange[0])-4,17,sx(profileRange[1])-sx(profileRange[0])+8,h-42);}
      g.fillText(`${hi.toPrecision(4)} / ${lo.toPrecision(4)}`,8,12);
      g.strokeStyle='#3d7dbe';g.lineWidth=1.5;
      for(let i=0;i<points.length;i++)if(points[i]!=null) {
        if(i && points[i-1]!=null){g.beginPath();g.moveTo(sx(i-1),sy(points[i-1]));g.lineTo(sx(i),sy(points[i]));g.stroke();}
        g.fillStyle=({usable:'#238451',unusable:'#b13b32',pending:'#ae7a2c'})[geometry.profiles[i].status];g.fillRect(sx(i)-2,sy(points[i])-2,4,4);
      }
      g.strokeStyle='#d88920';g.beginPath();g.moveTo(sx(profileIndex),15);g.lineTo(sx(profileIndex),h-22);g.stroke();
    } else {
      g.fillText('局部横向 / 地面法向 Z（mm）',8,14);
      if(!profileYZ.length){g.fillText('当前薄带无点',10,35);continue;}
      const ys=profileYZ.map(p=>p[0]),zs=profileYZ.map(p=>p[1]),loY=Math.min(...ys),hiY=Math.max(...ys),loZ=Math.min(...zs),hiZ=Math.max(...zs);
      const scale=Math.min((w-30)/Math.max(hiY-loY,1),(h-40)/Math.max(hiZ-loZ,30));
      g.fillStyle='#467e73';for(const p of profileYZ)g.fillRect(w/2+(p[0]-(loY+hiY)/2)*scale,h/2+7-(p[1]-(loZ+hiZ)/2)*scale,2,2);
      g.fillStyle='#68798b';g.fillText(offline?'图为预览采样；数值来自完整点云':'图最多 6000 点；数值来自完整点云',8,h-7);
    }
  }
}
function navigateProfile(index) {
  if(busy || !geometry || geometryDirty)return;
  index=Math.max(0,Math.min(geometry.profiles.length-1,index));
  if(offline){showGeometry({features:geometry,profile_index:index});return;}
  clearTimeout(profileTimer);profileTimer=setTimeout(()=>action(()=>loadGeometry({profile_index:index})),70);
}
$('profileSlider').oninput=()=>navigateProfile(Number($('profileSlider').value));
$('prevProfile').onclick=()=>navigateProfile(profileIndex-1);$('nextProfile').onclick=()=>navigateProfile(profileIndex+1);
function trendProfileIndex(x) {
  return Math.round(Math.max(0,Math.min(1,(x-15)/Math.max(1,$('profileTrend').clientWidth-30)))*((geometry?.profiles.length || 1)-1));
}
function setProfileRange(first,last) {
  profileRange=[first,last];$('profileRangeStart').value=first+1;$('profileRangeEnd').value=last+1;
  $('profileRangeHint').textContent=`已选择第 ${first+1}–${last+1} 片，共 ${last-first+1} 片；“确认所选”仅确认此范围。`;
  controls();drawGeometryPlots();
}
function readProfileRange() {
  const first=Number($('profileRangeStart').value),last=Number($('profileRangeEnd').value),count=geometry?.profiles.length || 0;
  if(!Number.isInteger(first) || !Number.isInteger(last) || first<1 || last>count || first>last){
    profileRange=null;$('profileRangeHint').textContent=`请输入 1–${count} 之间的起止编号，起始不能大于结束。`;controls();drawGeometryPlots();return;
  }
  setProfileRange(first-1,last-1);
}
for(const id of ['profileRangeStart','profileRangeEnd']) $(id).oninput=readProfileRange;
$('selectAllProfiles').onclick=()=>{if(geometry)setProfileRange(0,geometry.profiles.length-1);};
$('profileTrend').onpointerdown=e=>{
  if(busy || !geometry || geometryDirty || e.button!==0)return;
  profileRangeAnchor={x:e.offsetX,index:trendProfileIndex(e.offsetX)};profileRangeDragged=false;
  $('profileTrend').setPointerCapture(e.pointerId);
};
$('profileTrend').onpointermove=e=>{
  if(!profileRangeAnchor)return;
  if(Math.abs(e.offsetX-profileRangeAnchor.x)>4)profileRangeDragged=true;
  if(profileRangeDragged){const index=trendProfileIndex(e.offsetX);setProfileRange(Math.min(index,profileRangeAnchor.index),Math.max(index,profileRangeAnchor.index));}
};
$('profileTrend').onpointerup=()=>{profileRangeAnchor=null;};
$('profileTrend').onpointercancel=()=>{profileRangeAnchor=null;profileRangeDragged=false;};
$('profileTrend').onclick=e=>{if(profileRangeDragged){profileRangeDragged=false;return;}navigateProfile(trendProfileIndex(e.offsetX));};
$('trendChannel').onchange=drawGeometryPlots;
$('geometrySection').ontoggle=()=>{drawGeometryPlots();draw();};
for(const id of ['profileCount','profileThickness','profileMinimum','profileGrid']) $(id).onchange=()=>{
  geometryDirty=true;controls();message('参数已修改，点击“计算并保存参数”后生效。');
};
$('computeGeometry').onclick=()=>action(async()=>{
  const config={count:Number($('profileCount').value),thickness_mm:Number($('profileThickness').value),min_points:Number($('profileMinimum').value),grid_mm:Number($('profileGrid').value)};
  await loadGeometry({settings:config,profile_index:0});
  notify('saved');message('特征已按当前完整点云计算，参数已暂存；端点与地面沿用已确认对齐。');
});
for(const [status,id] of Object.entries({usable:'profileUsable',unusable:'profileUnusable',pending:'profilePending'})) $(id).onclick=()=>action(async()=>{
  await loadGeometry({status});notify('saved');message('当前局部剖面的复核状态已保存。');
});
for(const [status,id] of Object.entries({usable:'batchProfileUsable',unusable:'batchProfileUnusable',pending:'batchProfilePending'})) $(id).onclick=()=>action(async()=>{
  if(!profileRange)throw Error('请先选择有效的剖面范围。');
  const [first,last]=profileRange,response=await loadGeometry({profile_range:[first,last],status}),update=response.review_update;
  notify('saved');message(`第 ${first+1}–${last+1} 片：已${({usable:'确认可测',unusable:'淘汰',pending:'恢复待定'})[status]} ${update.updated} 片。`+
    (update.skipped.length?` 跳过 ${update.skipped.length} 片（点数或中心线支撑不足），保留原状态。`:'')+
    (status==='usable'?' 其余待定未改动；顶部测量筛选“保留”时会自动淘汰。':''));
});
async function start() {
  configureMode();
  if(offline){
    data={...OFFLINE_DATA,candidate_count:0};$('coordinate').value=data.offline_body?'body':'camera';
    $('profileFinalizationHint').hidden=true;
    $('sourceImage').hidden=true;$('offlineFiles').hidden=false;
    $('geometryExportHint').textContent='已随点云成果保存，文件链接见下方';
    $('bodyFile').hidden=!data.alignment;$('sequenceFiles').hidden=!data.features;
    setDefaultView();apply(data,true);draw();
    if(data.features)showGeometry({features:data.features,profile_index:0});
    message('只读清理成果；剖面数值来自完整点云。原始点云、清理后点云和操作历史分别保存。');return;
  }
  await action(async()=>{
    const response=await api('context',identity);
    $('coordinate').value=pageMode === 'review' && response.alignment ? 'body' : 'camera';
    setDefaultView();
    apply(response,true);
    if(pageMode === 'review' && response.alignment){await loadGeometry();}
    message(pageMode === 'review' ? (response.alignment ? '已载入对齐点云；可按区域或逐片复核左右边界。' : '尚未完成坐标对齐，请先回到“坐标对齐”确认地面、尾根和颈根。') : '已载入完整点云；先检测或框选，再确认删除。');
  });
}
start().finally(()=>notify('busy',{busy:false}));
