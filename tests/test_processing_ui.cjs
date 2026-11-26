'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'frontend/pages/processing.html'),'utf8');
const script=fs.readFileSync(path.join(root,'frontend/assets/js/processing.js'),'utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function setup(offline=false,aligned=true,mode='review',fixture={}) {
  const elements=new Map();
  function element(){return {value:'',hidden:false,disabled:false,children:[],style:{},clientWidth:0,clientHeight:0,
    classList:{toggle(){}},getContext:()=>({}),querySelector:()=>({}),setAttribute(name,value){this[name]=value;},setPointerCapture(){},getBoundingClientRect:()=>({left:0,top:0}),
    replaceChildren(){this.children=[];},append(...children){this.children.push(...children);}};}
  for(const match of html.matchAll(/id="([^"]+)"/g))elements.set(match[1],element());
  elements.get('coordinate').value='camera';elements.get('fit').value='body';
  const features={settings:{count:32,thickness_mm:20,min_points:20,grid_mm:10},conditions:{endpoints:'complete',ground:'reliable'},
    provenance:{reference_id:fixture.resultReference || 'current-reference'},geometry:{},profiles:Array.from({length:32},(_,index)=>({index,fraction:index/31,status:fixture.statuses?.[index] || (index<3?'usable':'pending'),
      point_count:50,width_q02_q98_mm:100,center_x_mm:index*10,center_y_mm:0,tangent_x:1,tangent_y:0})),
    quality:{warnings:[],computable_profiles:32,reviewed_profiles:3,computable_transverse_arcs:20}};
  const data={context_id:'context',revision:fixture.revision || 'v1',alignment:aligned?{reference_id:'current-reference',frame:{neck_tail_horizontal_span_mm:310,camera_to_body_4x4:[[1,0,0,0],[0,1,0,0],[0,0,1,0]]}}:null,
    kept_count:2,total:2,removed_count:0,xyz:[[0,0,0],[310,100,10]],rgb:[[0,0,0],[0,0,0]],removed:[0,0],selected:[0,0],
    measurement:{},candidate_count:0,report:{original_valid_depth_ratio:.99},features,offline_body:aligned};
  const requests=[],messages=[],events={};
  const sandbox={APP_TOKEN:'test',URLSearchParams,location:{search:`?mode=${mode}&id=one&dataset_id=test&editor_instance=frame-1`,origin:'http://localhost'},devicePixelRatio:1,
    setTimeout:fn=>{fn();return 1;},clearTimeout(){},parent:{postMessage:value=>messages.push(value)},
    window:{addEventListener:(name,fn)=>events[name]=fn},
    document:{title:'',body:{dataset:{}},querySelectorAll:()=>[],createElement:element,getElementById:id=>{
      assert.ok(elements.has(id),`missing DOM element: ${id}`);return elements.get(id);
    }},fetch:async(url,options)=>{
      const payload=JSON.parse(options.body);requests.push({url,payload});
      let body=data;
      if(url.endsWith('/select')) {
        data.candidate_count=(data.candidate_action===payload.action?data.candidate_count:0)+1;
        data.candidate_id='candidate-'+requests.length;data.candidate_action=payload.action;
      }
      if(url.endsWith('/edit') || url.endsWith('/clear')) {
        data.candidate_count=0;data.candidate_id='';
      }
      if(url.endsWith('/features')) {
        let review_update=null;
        if(payload.status){
          const range=payload.profile_range || [payload.profile_index,payload.profile_index],skipped=[];
          for(let i=range[0];i<=range[1];i++){
            if(payload.status==='usable' && features.profiles[i].width_q02_q98_mm==null)skipped.push(i);
            else features.profiles[i].status=payload.status;
          }
          review_update={profile_range:range,status:payload.status,updated:range[1]-range[0]+1-skipped.length,skipped};
          features.quality.reviewed_profiles=features.profiles.filter(r=>r.status==='usable').length;
        }
        body={features,revision:fixture.revision || 'v2',profile_index:payload.profile_index||0,profile_yz:[],review_update};
      }
      return {ok:true,status:200,json:async()=>body};
    }};
  if(offline)sandbox.OFFLINE_DATA=data;
  const context=vm.createContext(sandbox);vm.runInContext(script,context);await settle();await settle();
  return {elements,requests,messages,events,run:code=>vm.runInContext(code,context)};
}

test('review startup and marking use profiles with no legacy slice request or missing DOM',async()=>{
  const ui=await setup();
  assert.deepEqual(ui.requests.map(r=>r.url),['/api/processing/context','/api/processing/features']);
  assert.equal(ui.run('yaw===0&&pitch===Math.PI/2'),true,'measurement review must start in top-down XY');
  ui.run('yaw=.7;pitch=.2;resetView()');
  assert.equal(ui.run('yaw===0&&pitch===Math.PI/2'),true,'reset must return measurement review to top-down XY');
  assert.match(ui.elements.get('geometrySummary').children[0].textContent,/已确认可测 3\/32 片/);
  await ui.elements.get('profilePending').onclick();
  assert.match(ui.elements.get('geometrySummary').children[0].textContent,/已确认可测 2\/32 片/);
  assert.ok(ui.messages.some(m=>m.action==='saved'));
  assert.equal(ui.elements.get('message').textContent,'当前局部剖面的复核状态已保存。');
  ui.events.resize();
  await ui.elements.get('reloadData').onclick();
  assert.ok(ui.requests.every(r=>!/\/slices$|\/review$/.test(r.url)));
  assert.equal(ui.elements.has('measurementSection'),false);
  assert.equal(ui.elements.has('endpointQuality'),false);
  assert.equal(ui.elements.has('groundQuality'),false);
});

test('review sends the displayed reference and refuses geometry from another alignment',async()=>{
  const ui=await setup(false,true,'review',{resultReference:'old-reference'});
  assert.equal(ui.requests.at(-1).payload.alignment_reference_id,'current-reference');
  assert.match(ui.elements.get('alignmentHint').textContent,/310\.0 mm/);
  assert.match(ui.elements.get('message').textContent,/坐标不同/);
  assert.equal(ui.elements.get('batchProfileUsable').disabled,true);
});

test('constrained centerline display joins both marked heights without changing its XY',async()=>{
  const ui=await setup();
  ui.run(`geometry.centerline={version:'endpoint-constrained-smoothing-spline-v1'};
    data.alignment.annotations={tail:{xyz_camera_mm:[0,0,420]},neck:{xyz_camera_mm:[310,0,680]}}`);
  assert.deepEqual(Array.from(ui.run('centerlinePoint([0,0])')),[0,0,420]);
  assert.deepEqual(Array.from(ui.run('centerlinePoint([310,0])')),[310,0,680]);
  assert.deepEqual(Array.from(ui.run('centerlinePoint([155,30])')),[155,30,550]);
  ui.run(`geometry.centerline.version='local-smoothed-mid-envelope-v1'`);
  assert.equal(ui.run('centerlinePoint([155,30])[2]===center[2]'),true);
});

test('offline profile preview starts, navigates and stays read only',async()=>{
  const ui=await setup(true);
  assert.equal(ui.requests.length,0);
  assert.match(ui.elements.get('message').textContent,/只读清理成果/);
  assert.equal(ui.elements.get('profileUsable').disabled,true);
  assert.equal(ui.elements.get('batchProfileUsable').disabled,true);
  assert.equal(ui.elements.get('batchProfileUnusable').disabled,true);
  assert.equal(ui.elements.get('sequenceFiles').hidden,false);
  assert.match(ui.elements.get('geometryExportHint').textContent,/已随点云成果保存/);
  ui.elements.get('nextProfile').onclick();
  assert.equal(ui.run('profileIndex'),1);
  assert.equal(ui.elements.get('nextProfile').disabled,false);
});

test('unaligned review opens without requesting measurements',async()=>{
  const ui=await setup(false,false);
  assert.equal(ui.requests.length,1);
  assert.equal(ui.elements.get('geometryLock').hidden,false);
  assert.equal(ui.elements.get('computeGeometry').disabled,true);
  assert.match(ui.elements.get('message').textContent,/尚未完成坐标对齐/);
});

test('range review uses one save and preserves the selected range across navigation',async()=>{
  const ui=await setup();
  assert.deepEqual(Array.from(ui.run('profileRange')),[0,31]);
  ui.elements.get('profileRangeStart').value='5';ui.elements.get('profileRangeEnd').value='12';
  ui.elements.get('profileRangeStart').oninput();
  ui.elements.get('nextProfile').onclick();await settle();
  assert.deepEqual(Array.from(ui.run('profileRange')),[4,11]);
  const before=ui.requests.length;
  await ui.elements.get('batchProfileUsable').onclick();
  assert.equal(ui.requests.length,before+1);
  assert.deepEqual(ui.requests.at(-1).payload.profile_range,[4,11]);
  assert.equal(ui.requests.at(-1).payload.status,'usable');
  assert.equal(ui.requests.at(-1).payload.profile_index,1);
  assert.match(ui.elements.get('message').textContent,/已确认可测 8 片/);
  await ui.elements.get('batchProfileUnusable').onclick();
  assert.equal(ui.requests.at(-1).payload.status,'unusable');
  ui.elements.get('selectAllProfiles').onclick();
  await ui.elements.get('batchProfilePending').onclick();
  assert.deepEqual(ui.requests.at(-1).payload.profile_range,[0,31]);
  assert.equal(ui.requests.at(-1).payload.status,'pending');
});

test('invalid ranges and changed parameters disable batch saves, new count selects all',async()=>{
  const ui=await setup();
  for(const [first,last] of [['12','5'],['0','4'],['1','33'],['2.5','8'],['','8']]){
    ui.elements.get('profileRangeStart').value=first;ui.elements.get('profileRangeEnd').value=last;
    ui.elements.get('profileRangeStart').oninput();
    assert.equal(ui.elements.get('batchProfileUsable').disabled,true);
  }
  ui.elements.get('selectAllProfiles').onclick();
  ui.elements.get('profileCount').onchange();
  assert.equal(ui.elements.get('batchProfileUsable').disabled,true);
  ui.run('showGeometry({features:{...geometry,settings:{...geometry.settings,count:16},profiles:geometry.profiles.slice(0,16)},profile_index:0,profile_yz:[]})');
  assert.deepEqual(Array.from(ui.run('profileRange')),[0,15]);
  assert.equal(ui.elements.get('profileRangeEnd').max,16);
  assert.equal(ui.elements.get('batchProfileUsable').disabled,false);
});

test('dragging the curve selects a range without navigating or saving',async()=>{
  const ui=await setup(),trend=ui.elements.get('profileTrend');
  ui.run('drawGeometryPlots=()=>{}');trend.clientWidth=340;
  const before=ui.requests.length;
  trend.onpointerdown({button:0,offsetX:65,pointerId:1});
  trend.onpointermove({offsetX:135});trend.onpointerup();trend.onclick({offsetX:135});
  assert.deepEqual(Array.from(ui.run('profileRange')),[5,12]);
  assert.equal(ui.requests.length,before);
  assert.match(ui.elements.get('profileRangeHint').textContent,/第 6–13 片/);
});

test('batch confirmation reports skipped missing profiles',async()=>{
  const ui=await setup();
  ui.run('geometry.profiles[5].width_q02_q98_mm=null');
  await ui.elements.get('batchProfileUsable').onclick();
  assert.match(ui.elements.get('message').textContent,/确认可测 31 片.*跳过 1 片/);
});

test('cleaning keeps box mode across consecutive selections and confirmation',async()=>{
  const ui=await setup(false,false,'cleaning'),canvas=ui.elements.get('cloud');
  ui.run('draw=()=>{}');canvas.clientWidth=200;canvas.clientHeight=100;
  const rectangle=async()=>{
    canvas.onpointerdown({clientX:20,clientY:20,pointerId:1});
    canvas.onpointermove({clientX:40,clientY:40});canvas.onpointerup();await settle();
  };
  ui.elements.get('boxRemove').onclick();
  await rectangle();
  assert.equal(ui.requests.at(-1).payload.append,true);
  assert.equal(ui.requests.at(-1).payload.candidate_id,'');
  const candidate=ui.run('data.candidate_id');
  await rectangle();
  assert.equal(ui.requests.at(-1).payload.candidate_id,candidate);
  assert.equal(ui.run('data.candidate_count'),2);
  assert.equal(ui.run('boxMode'),'remove');
  assert.equal(ui.elements.get('boxRemove')['aria-pressed'],'true');
  await ui.elements.get('applySelection').onclick();
  assert.equal(ui.run('boxMode'),'remove');
  assert.equal(ui.run('data.candidate_count'),0);
  await rectangle();
  assert.equal(ui.requests.at(-1).payload.candidate_id,'');
  assert.equal(ui.run('data.candidate_count'),1);
  const before=ui.requests.length;
  canvas.onpointerdown({clientX:20,clientY:20,pointerId:1,altKey:true});
  canvas.onpointermove({clientX:40,clientY:40});canvas.onpointerup();
  canvas.onpointerdown({clientX:20,clientY:20,pointerId:1,shiftKey:true});
  canvas.onpointermove({clientX:40,clientY:40});canvas.onpointerup();
  assert.equal(ui.requests.length,before);
  assert.equal(ui.run('boxMode'),'remove');
  ui.events.keydown({key:'Escape'});
  assert.equal(ui.run('boxMode'),'');
  assert.equal(canvas.style.cursor,'grab');
  assert.equal(ui.run('data.candidate_count'),1);
});

test('cancelled and tiny rectangles keep continuous mode without selecting',async()=>{
  const ui=await setup(false,false,'cleaning'),canvas=ui.elements.get('cloud');
  ui.run('draw=()=>{}');canvas.clientWidth=200;canvas.clientHeight=100;
  ui.elements.get('boxRestore').onclick();const before=ui.requests.length;
  canvas.onpointerdown({clientX:20,clientY:20,pointerId:1});canvas.onpointerup();
  canvas.onpointerdown({clientX:20,clientY:20,pointerId:1});canvas.onpointercancel();
  assert.equal(ui.requests.length,before);
  assert.equal(ui.run('boxMode'),'restore');
  assert.equal(ui.elements.get('showRemoved').checked,true);
  canvas.ondblclick();assert.equal(ui.run('boxMode'),'restore');
});

test('reloaded finalized measurements show usable and rejected counts and submit the latest revision',async()=>{
  const statuses=Array.from({length:32},(_,index)=>index===31?'unusable':'usable');
  const ui=await setup(false,true,'review',{statuses,revision:'finalized-v3'});
  assert.match(ui.elements.get('geometrySummary').children[0].textContent,/已确认可测 31\/32 片 · 淘汰 1 片 · 待定 0 片/);
  assert.equal(ui.run('data.revision'),'finalized-v3');
  assert.ok(ui.messages.every(message=>message.editor_instance==='frame-1'));
  const colors=[],trend=ui.elements.get('profileTrend'),plot={setTransform(){},fillText(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},
    fillRect(){colors.push(this.fillStyle);}};
  ui.elements.get('trendChannel').value='width_q02_q98_mm';
  trend.clientWidth=340;trend.clientHeight=120;trend.getContext=()=>plot;
  ui.run('drawGeometryPlots()');
  assert.equal(colors.filter(color=>color==='#238451').length,31);
  assert.equal(colors.filter(color=>color==='#b13b32').length,1);
  assert.equal(colors.filter(color=>color==='#ae7a2c').length,0);
  ui.run('profileIndex=31');
  await ui.elements.get('profilePending').onclick();
  assert.equal(ui.requests.at(-1).payload.revision,'finalized-v3');
  assert.equal('editor_instance' in ui.requests.at(-1).payload,false);
  assert.match(ui.elements.get('geometrySummary').children[0].textContent,/已确认可测 31\/32 片 · 淘汰 0 片 · 待定 1 片/);
});

test('range confirmation explains its limited scope and leaves pending profiles outside it unchanged',async()=>{
  const ui=await setup();
  ui.elements.get('profileRangeStart').value='5';ui.elements.get('profileRangeEnd').value='12';
  ui.elements.get('profileRangeStart').oninput();
  assert.match(ui.elements.get('profileRangeHint').textContent,/确认所选.*仅确认此范围/);
  await ui.elements.get('batchProfileUsable').onclick();
  assert.equal(ui.run('geometry.profiles[12].status'),'pending');
  assert.equal(ui.run('geometry.profiles[4].status'),'usable');
  assert.match(ui.elements.get('message').textContent,/其余待定未改动.*顶部测量筛选“保留”时会自动淘汰/);
  assert.match(html,/“确认所选”只确认选区.*其余待定剖面自动淘汰/);
  const offline=await setup(true);
  assert.equal(offline.elements.get('profileFinalizationHint').hidden,true);
});
