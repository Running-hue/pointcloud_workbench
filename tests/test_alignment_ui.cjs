'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'frontend/pages/alignment.html'),'utf8');
const script=fs.readFileSync(path.join(root,'frontend/assets/js/alignment.js'),'utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function setup(){
  const elements=new Map(),requests=[];
  const drawing=new Proxy({}, {get:(_,name)=>name==='measureText'?()=>({width:20}):()=>{}});
  function element(){return {value:'',hidden:false,disabled:false,open:false,style:{},clientWidth:1000,clientHeight:700,
    classList:{toggle(){},add(){},remove(){}},getContext:()=>drawing,addEventListener(){},removeAttribute(name){delete this[name];},
    getBoundingClientRect:()=>({left:0,top:0,width:1000,height:700}),
    showModal(){this.open=true;},close(){this.open=false;}};}
  const container=element();
  for(const match of html.matchAll(/id="([^"]+)"/g))elements.set(match[1],{...element(),parentElement:container});
  elements.get('brightness').value='1';
  const sandbox={APP_TOKEN:'token',URLSearchParams,location:{search:'?id=one&dataset_id=test',origin:'http://localhost'},
    sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
    window:{addEventListener(){}},ResizeObserver:class{observe(){}},
    Image:class{constructor(){this.width=100;this.height=80;}async decode(){}},
    document:{body:{classList:{add(){}}},querySelectorAll:()=>[],querySelector:()=>element(),getElementById:id=>{
      assert.ok(elements.has(id),'missing alignment DOM: '+id);return elements.get(id);
    }},fetch:async(url,options)=>{
      const payload=options?JSON.parse(options.body):null;requests.push({url,payload});
      const body=url==='/api/state'?{dataset_id:'test',results:'moved-results',alignment_source:'old-root'}:
        {context_id:'ctx',source_root:payload?.source_root||'verified-moved-root',source_dir:'capture',width:100,height:80,
          infrared_verified:true,infrared_image:'image',infrared_enhanced_image:'image',depth_image:'image',
          default_display_mode:'ir',rgb_display:{available:false,reason:'not present'},pig_id:'0001',capture_id:'capture'};
      return {ok:true,status:200,json:async()=>body};
    }};
  sandbox.window.parent=sandbox.window;
  const context=vm.createContext(sandbox);vm.runInContext(script,context);
  for(let i=0;i<5;i++)await settle();
  return {elements,requests};
}
test('alignment startup resolves moved roots instead of forcing a stale settings path',async()=>{
  const ui=await setup();
  const request=ui.requests.find(r=>r.url==='/api/alignment/context');
  assert.ok(request,'startup must request the original scene');
  assert.equal(Object.hasOwn(request.payload,'source_root'),false);
  assert.equal(ui.elements.get('rawRoot').value,'verified-moved-root');
  assert.equal(ui.elements.get('sourceDialog').open,false);
  assert.match(ui.elements.get('message').textContent,/已放大/);
});
test('manual scene reload preserves the explicitly selected root',async()=>{
  const ui=await setup();
  ui.elements.get('rawRoot').value='manual-root';
  ui.elements.get('rawRoot').oninput();
  await ui.elements.get('reload').onclick();
  const request=ui.requests.filter(r=>r.url==='/api/alignment/context').at(-1);
  assert.equal(request.payload.source_root,'manual-root');
  assert.equal(ui.elements.get('rawRoot').value,'manual-root');
});
