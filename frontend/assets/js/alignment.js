'use strict';
const $=id=>document.getElementById(id), query=new URLSearchParams(location.search);
const base={id:query.get('id'),dataset_id:query.get('dataset_id')};
const embedded=query.get('embedded')==='1'&&window.parent!==window;
if(embedded){document.body.classList.add('embedded');$('openSaved').textContent='查看本条已确认点云'}
let token=APP_TOKEN, context=null,candidate=null,busy=false,tool='ground',polygon=[],tail=null,neck=null,saved=null;
let background=null,foreground=null,loadEpoch=0;
let zoom=1,panX=0,panY=0,baseScale=1,drag=null,spaceDown=false;
let draftKey='';
function notify(action,extra={}){if(embedded)window.parent.postMessage({type:'pointcloud-alignment',...base,action,...extra},location.origin)}
function saveDraft(){if(!draftKey||!context||saved)return;try{sessionStorage.setItem(draftKey,JSON.stringify({source_dir:context.source_dir,binding:context.draft_binding,polygon,tail,neck,tool,threshold:$('threshold').value,imageMode:$('imageMode').value,brightness:$('brightness').value,foreground:$('showForeground').checked}))}catch{}}
function restoreDraft(){try{let legacyKey='',raw=sessionStorage.getItem(draftKey);if(!raw){const identity=JSON.parse(draftKey.slice('pointcloud-alignment:'.length));for(const key of Object.keys(sessionStorage).reverse()){if(!key.startsWith('pointcloud-alignment:'))continue;try{const parts=JSON.parse(key.slice('pointcloud-alignment:'.length));if(parts.length===3&&parts[1]===identity[0]&&parts[2]===identity[1]){legacyKey=key;raw=sessionStorage.getItem(key);break}}catch{}}}const d=JSON.parse(raw||'null');if(!d||d.source_dir!==context.source_dir||d.binding&&d.binding!==context.draft_binding)return false;if(!Array.isArray(d.polygon)||d.polygon.length>100)return false;const point=p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite)&&p[0]>=0&&p[0]<context.width&&p[1]>=0&&p[1]<context.height;if(!d.polygon.every(point)||d.tail&&!point(d.tail)||d.neck&&!point(d.neck))return false;polygon=d.polygon;tail=d.tail;neck=d.neck;tool=hints[d.tool]?d.tool:'ground';$('threshold').value=d.threshold||'10';if(['rgb','ir','ir_enhanced','depth'].includes(d.imageMode)&&!(d.imageMode==='rgb'&&!context.rgb_display?.available))$('imageMode').value=d.imageMode;$('brightness').value=d.brightness||'1';$('brightnessValue').textContent=Number($('brightness').value).toFixed(1)+'×';$('showForeground').checked=d.foreground!==false;document.querySelectorAll('[data-tool]').forEach(b=>b.classList.toggle('active',b.dataset.tool===tool));$('toolHint').textContent=hints[tool];if(legacyKey){saveDraft();sessionStorage.removeItem(legacyKey)}return polygon.length>0||!!tail||!!neck}catch{return false}}
const canvas=$('scene'),ctx=canvas.getContext('2d');
const hints={ground:'点击至少 3 个地面顶点；滚轮缩放，空格＋拖动；“全景”查看完整地面。',tail:'点击尾巴与躯干连接处的中点；可滚轮放大后标注。',neck:'按统一躯干切割口径，点击颈根中点。',pan:'拖动图像移动视野；滚轮缩放，点击“全景”复位。'};
function message(text='',error=false){for(const id of ['message','sourceMessage']){const el=$(id);if(el){el.textContent=text;el.title=text;el.classList.toggle('error',error)}}}
async function api(path,data,retry=true){const response=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:JSON.stringify({...base,...data})}),result=await response.json();if(response.status===403&&result.code==='session_expired'&&retry){const session=await fetch('/api/session');token=(await session.json()).token;return api(path,data,false)}if(!response.ok)throw new Error(result.error||'操作失败');return result}
function controls(){document.querySelectorAll('button,input,select').forEach(e=>e.disabled=busy);document.querySelector('#imageMode option[value="rgb"]').disabled=!context?.rgb_display?.available;$('fit').disabled=busy||!context||polygon.length<3||!tail||!neck;$('reuse').disabled=busy||!context?.reference;$('save').disabled=busy||!candidate;$('reuse').hidden=!context?.reference;canvas.parentElement.classList.toggle('canpan',tool==='pan'||spaceDown)}
async function action(fn){if(busy)return;busy=true;notify('busy',{busy:true});controls();try{await fn()}catch(e){message(e.message,true);if(!context&&!$('sourceDialog').open)$('sourceDialog').showModal()}finally{busy=false;controls();notify('busy',{busy:false})}}
function invalidate(){candidate=null;saved=null;$('saved').hidden=true;$('preview').hidden=true;$('preview').removeAttribute('src');$('previewEmpty').hidden=false;$('checkDetails').hidden=true;$('quality').textContent='';$('referenceStatus').textContent='标注已改变，需重新计算';saveDraft();controls()}
function draw(){if(!background)return;canvas.width=background.naturalWidth;canvas.height=background.naturalHeight;ctx.save();ctx.filter=`brightness(${$('brightness').value})`;ctx.drawImage(background,0,0);ctx.restore();if(foreground&&$('showForeground').checked)ctx.drawImage(foreground,0,0);const scale=Math.max(1,canvas.width/650);ctx.lineWidth=2*scale;if(polygon.length){ctx.beginPath();polygon.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));if(polygon.length>=3){ctx.closePath();ctx.fillStyle='#e6b42d30';ctx.fill()}ctx.strokeStyle='#e6b42d';ctx.stroke();for(const p of polygon){ctx.beginPath();ctx.arc(...p,3*scale,0,2*Math.PI);ctx.fillStyle='#ffe06a';ctx.fill()}}for(const [p,label,color] of [[tail,'尾根','#e76f43'],[neck,'颈根','#5d9df5']]){if(!p)continue;ctx.beginPath();ctx.arc(...p,5*scale,0,2*Math.PI);ctx.fillStyle=color;ctx.fill();ctx.font=`${14*scale}px system-ui`;ctx.fillStyle='white';ctx.fillText(label,p[0]+8*scale,p[1]-8*scale)}sizeCanvas()}
function displayHint(){if(!context){$('displayHint').textContent='';return}const mode=$('imageMode').value;let text=mode==='rgb'?'RGB 已映射到深度坐标；灰色棋盘格为无可用 RGB 映射的区域，可切换深度图检查。':mode==='ir_enhanced'?'已提亮暗部并增强局部对比度；没有信号的区域仍无法恢复纹理。':mode==='depth'?'颜色表示距离，黑色为无有效深度；在有深度的地面区域圈选。':'常规灰度显示；背景太暗时切换 RGB 或增强 IR。';if(!context.rgb_display?.available)text+=` RGB 暂不可用：${context.rgb_display?.reason||'缺少已核对的 RGB 映射'}。`;text+=' 所有视图共用原始深度坐标，切换后标注位置保持一致。';$('displayHint').textContent=text}
async function showImage(){if(!context)return;const epoch=++loadEpoch,img=new Image();const sources={rgb:context.rgb_image,ir_enhanced:context.infrared_enhanced_image,ir:context.infrared_image,depth:context.depth_image};if(!sources[$('imageMode').value])$('imageMode').value=context.default_display_mode||'ir';img.src=sources[$('imageMode').value];await img.decode();if(epoch!==loadEpoch)return;background=img;displayHint();draw()}
function positionCanvas(){const box=canvas.parentElement;const mx=Math.max(0,(canvas.width*baseScale*zoom-box.clientWidth)/2),my=Math.max(0,(canvas.height*baseScale*zoom-box.clientHeight)/2);panX=Math.max(-mx,Math.min(mx,panX));panY=Math.max(-my,Math.min(my,panY));canvas.style.transform=`translate(${panX}px,${panY}px) scale(${zoom})`;$('zoomValue').textContent=Math.round(zoom*100)+'%'}
function sizeCanvas(){const box=canvas.parentElement;if(!canvas.width||!box.clientWidth||!box.clientHeight)return;const scale=Math.min((box.clientWidth-4)/canvas.width,(box.clientHeight-4)/canvas.height);panX*=scale/baseScale;panY*=scale/baseScale;baseScale=scale;canvas.style.width=Math.max(1,canvas.width*scale)+'px';canvas.style.height=Math.max(1,canvas.height*scale)+'px';positionCanvas()}
function zoomAt(value,x,y){if(!background)return;const b=canvas.parentElement.getBoundingClientRect(),ratio=Math.max(1,Math.min(8,value))/zoom;const px=(x??b.left+b.width/2)-b.left-b.width/2,py=(y??b.top+b.height/2)-b.top-b.height/2;panX=px-(px-panX)*ratio;panY=py-(py-panY)*ratio;zoom*=ratio;positionCanvas()}
function fitScene(){zoom=1;panX=panY=0;sizeCanvas()}
function focusPig(){if(!context?.foreground_bounds||!background)return;const [x1,y1,x2,y2]=context.foreground_bounds,box=canvas.parentElement;sizeCanvas();zoom=Math.max(1,Math.min(8,Math.min(box.clientWidth/((x2-x1)*1.35),box.clientHeight/((y2-y1)*1.35))/baseScale));panX=(canvas.width/2-(x1+x2)/2)*baseScale*zoom;panY=(canvas.height/2-(y1+y2)/2)*baseScale*zoom;positionCanvas()}
$('zoomIn').onclick=()=>zoomAt(zoom*1.25);$('zoomOut').onclick=()=>zoomAt(zoom/1.25);$('fitScene').onclick=fitScene;$('focusPig').onclick=focusPig;
canvas.parentElement.addEventListener('wheel',e=>{if(busy||!background)return;e.preventDefault();zoomAt(zoom*Math.exp(-e.deltaY*.0015),e.clientX,e.clientY)},{passive:false});
window.addEventListener('keydown',e=>{if(e.code!=='Space'||e.target.matches('input,select,textarea,[contenteditable]')||document.querySelector('dialog[open]'))return;e.preventDefault();spaceDown=true;controls()});
window.addEventListener('keyup',e=>{if(e.code==='Space'){spaceDown=false;controls()}});
window.addEventListener('blur',()=>{spaceDown=false;drag=null;canvas.parentElement.classList.remove('panning');controls()});
new ResizeObserver(sizeCanvas).observe(canvas.parentElement);
function showQuality(record){const q=record.ground_quality;$('quality').textContent=`地面内点 ${(q.inlier_ratio*100).toFixed(1)}%，拟合 RMS ${q.inlier_rmse_mm.toFixed(2)} mm；拟合残差不代表实际测量精度。`}
function restoreConfirmed(result){
 const record=result.saved_alignment;if(!record)return false;
 polygon=record.annotations.ground_polygon;tail=record.annotations.tail.pixel_uv_native;neck=record.annotations.neck.pixel_uv_native;
 $('threshold').value=record.ground_quality.threshold_mm;draw();
 saved={cached:result.alignment_cached,reference_id:record.reference_id};
 $('preview').src='/api/media?'+new URLSearchParams({...base,file:'body_preview.html',cleaned:'1',revision:record.reference_id});
 $('preview').hidden=false;$('previewEmpty').hidden=true;$('checkImage').src=result.saved_check_image;$('checkDetails').hidden=false;
 $('saved').hidden=false;
 $('referenceStatus').textContent=result.alignment_cached?'本条已确认 · 待统一导出':'已保存对齐';showQuality(record);
 message(result.alignment_cached?'本条已确认并暂存，可继续下一条，最后在工作台顶部统一导出。':'已恢复本条确认坐标，可直接纳入统一导出，或修改后重新确认。');return true;
}
async function load(){
 message('正在读取完整场景深度并核对来源…');++loadEpoch;context=null;background=foreground=null;
 zoom=1;panX=panY=0;ctx.clearRect(0,0,canvas.width,canvas.height);displayHint();invalidate();
 const result=await api('alignment/context',{source_root:$('rawRoot').value.trim()});context=result;polygon=[];tail=neck=null;candidate=null;saved=null;$('saved').hidden=true;
 document.querySelector('#imageMode option[value="ir"]').textContent=result.infrared_verified?'原始 IR':'原始深度灰度';
 document.querySelector('#imageMode option[value="ir_enhanced"]').textContent=result.infrared_verified?'增强 IR（提亮背景）':'增强深度灰度';
 $('imageMode').value=result.default_display_mode||'ir';
 if(result.foreground_overlay){const layer=new Image();layer.src=result.foreground_overlay;await layer.decode();foreground=layer}
 $('identity').textContent=`${result.camera?.model||''} · 耳标 ${result.pig_id} / ${result.capture_id} · ${result.input_mode==='rgb'?'RGB':'IR'} 分割`;
 $('referenceStatus').textContent=result.reference?'同次采集已有确认参考，可直接复用':'尚未计算';
 const restored=restoreDraft();$('toolHint').textContent=hints[tool];await showImage();focusPig();if($('sourceDialog').open)$('sourceDialog').close();
 if(restored){$('referenceStatus').textContent='已恢复未确认标注，需重新计算';message('已恢复本次采集的标注；请重新计算、核对后点击“确认坐标对齐”。');return}
 if(restoreConfirmed(result))return;
 message('已放大猪体附近；滚轮缩放，空格＋拖动，点击“全景”查看完整地面。');
}
canvas.onpointerdown=e=>{
 if(busy||!context||!background)return;
 if(e.button===1||e.button===2||spaceDown||tool==='pan'){
  e.preventDefault();drag={id:e.pointerId,x:e.clientX,y:e.clientY,panX,panY};canvas.setPointerCapture(e.pointerId);canvas.parentElement.classList.add('panning');return;
 }
 if(e.button!==0)return;
 const b=canvas.getBoundingClientRect(),p=[(e.clientX-b.left)*canvas.width/b.width,(e.clientY-b.top)*canvas.height/b.height];
 if(p.some(v=>v<0)||p[0]>=canvas.width||p[1]>=canvas.height)return;
 if(tool==='ground'){if(polygon.length>=100){message('地面顶点最多 100 个。',true);return}polygon.push(p)}else if(tool==='tail')tail=p;else neck=p;
 invalidate();draw();controls();
};
canvas.onpointermove=e=>{if(!drag||e.pointerId!==drag.id)return;panX=drag.panX+e.clientX-drag.x;panY=drag.panY+e.clientY-drag.y;positionCanvas()};
function endDrag(e){if(!drag||e.pointerId!==drag.id)return;drag=null;canvas.parentElement.classList.remove('panning');if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId)}
canvas.onpointerup=endDrag;canvas.onpointercancel=endDrag;canvas.oncontextmenu=e=>e.preventDefault();
document.querySelectorAll('[data-tool]').forEach(b=>b.onclick=()=>{tool=b.dataset.tool;document.querySelectorAll('[data-tool]').forEach(x=>x.classList.toggle('active',x===b));$('toolHint').textContent=hints[tool];saveDraft();controls()});
$('undo').onclick=()=>{if(tool==='ground')polygon.pop();else if(tool==='tail')tail=null;else neck=null;invalidate();draw()};$('clear').onclick=()=>{polygon=[];tail=neck=null;invalidate();draw()};$('threshold').oninput=invalidate;$('imageMode').onchange=()=>action(async()=>{await showImage();saveDraft()});
$('showForeground').onchange=()=>{draw();saveDraft()};$('brightness').oninput=()=>{$('brightnessValue').textContent=Number($('brightness').value).toFixed(1)+'×';draw();saveDraft()};$('resetBrightness').onclick=()=>{$('brightness').value='1';$('brightness').oninput()};
async function fit(reuse=false){invalidate();message(reuse?'正在复用同次采集的坐标参考…':'正在拟合地面并建立猪体坐标…');const result=await api('alignment/fit',{context_id:context.context_id,ground_polygon:polygon,tail_uv:tail,neck_uv:neck,threshold_mm:Number($('threshold').value),reuse_reference:reuse,display_mode:$('imageMode').value,display_brightness:Number($('brightness').value)});candidate=result;const a=result.record.annotations;if(reuse){polygon=a.ground_polygon;tail=a.tail.pixel_uv_native;neck=a.neck.pixel_uv_native;$('threshold').value=result.record.ground_quality.threshold_mm;draw();saveDraft()}$('preview').src='/api/alignment/preview?'+new URLSearchParams({...base,context_id:context.context_id,candidate_id:result.candidate_id});$('preview').hidden=false;$('previewEmpty').hidden=true;$('checkImage').src=result.check_image;$('checkDetails').hidden=false;$('referenceStatus').textContent=reuse?'已复用同次采集坐标，待核对':'对齐预览，待核对';const q=result.record.ground_quality;$('quality').textContent=`地面候选 ${q.candidate_points.toLocaleString()} 点，内点 ${(q.inlier_ratio*100).toFixed(1)}%，拟合 RMS ${q.inlier_rmse_mm.toFixed(2)} mm。拟合残差不能代替实际测量精度验证。`;message(result.record.warnings.join(' ')||'请从俯视、侧视核对方向，并展开地面与坐标轴检查图。')}
$('fit').onclick=()=>action(()=>fit(false));$('reuse').onclick=()=>action(()=>fit(true));$('reload').onclick=()=>action(load);
async function choose(field,title){const result=await api('choose_directory',{path:$(field).value,title});if(!result.cancelled)$(field).value=result.path;return !result.cancelled}
$('browseRaw').onclick=()=>action(async()=>{if(await choose('rawRoot','选择原始采集根目录'))await load()});
$('rawRoot').oninput=()=>{++loadEpoch;context=null;invalidate();displayHint();message('原始目录已改变，请重新载入场景。')};
$('save').onclick=()=>action(async()=>{
 message('正在保存对齐结果与保留状态…');
 saved=await api('alignment/confirm',{context_id:context.context_id,candidate_id:candidate.candidate_id,
  alignment_confirmed:true});
 context.reference=true;
 $('preview').src='/api/media?'+new URLSearchParams({...base,file:'body_preview.html',cleaned:'1',revision:saved.reference_id});
 try{sessionStorage.removeItem(draftKey)}catch{}
 notify('saved',{output:saved.output,cached:true});$('savedPath').textContent=saved.output;$('saved').hidden=false;
 $('referenceStatus').textContent='本条已确认 · 待统一导出';
 message(saved.warning||'已确认坐标对齐，可继续下一条；最后点击顶部“导出点云成果”一次保存整批成果。');candidate=null;
});
$('openSaved').onclick=()=>{if(!saved)return;if(embedded){notify('view-saved');return}location.href='/'};
action(async()=>{
 const response=await fetch('/api/state'),state=await response.json();
 if(state.dataset_id!==base.dataset_id)throw new Error('目录已切换，请返回工作台重新打开坐标对齐');
 draftKey='pointcloud-alignment:'+JSON.stringify([state.results,base.id]);
 $('rawRoot').value=state.alignment_source||state.source||'';await load();
});

for(const [button,dialog] of [['sceneSettings','sourceDialog'],['showHelp','helpDialog'],['checkDetails','checkDialog']])$(button).onclick=()=>$(dialog).showModal();
for(const [button,dialog] of [['closeSource','sourceDialog'],['closeHelp','helpDialog'],['closeCheck','checkDialog']])$(button).onclick=()=>$(dialog).close();
