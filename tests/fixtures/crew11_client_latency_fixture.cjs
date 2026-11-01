const fs = require('fs'), vm = require('vm');
const script = fs.readFileSync(process.argv[2], 'utf8');
(async function main() {
 async function runCase(spec) {
  const listeners = {}, timers = [];
  let tick = 0, stamp = 100, paint = false, fetches = 0;
  const root = {isConnected:true,getClientRects:()=>[{}],checkVisibility:()=>true,
    closest:()=>null};
  const input = {...root, contains:n=>n===button};
  const messages = [{...root,matches:()=>false}];
  const chat = {...root,querySelectorAll:s=>s==='.message-bubble'?messages:[]};
  const chip = {...root};
  let badge = null;
  const doc = {
    visibilityState:'visible', documentElement:{lang:spec.lang||'zh-CN'},
    body:{appendChild:el=>{badge=el;el.parentNode={removeChild:()=>badge=null};}},
    addEventListener:(kind,fn)=>{(listeners[kind]||(listeners[kind]=[])).push(fn);},
    querySelector:s=>s==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:s=>s==='.input-area'?[input]:s==='.chat-container'?[chat]:[],
    createElement:()=>({style:{},setAttribute(){},textContent:'',parentNode:null})
  };
  const button = {type:'submit',disabled:false, getAttribute:()=>'',closest:s=>s==='.input-area'?input:null};
  const wnd = {requestAnimationFrame:fn=>{stamp+=6;fn();},
    setTimeout:(fn,delay)=>{const id={fn,delay}; timers.push(id);return id;},
    clearTimeout:id=>{const i=timers.indexOf(id);if(i>=0) timers.splice(i,1);},
    addEventListener:()=>{}};
  const ctx={document:doc,window:wnd,performance:{now:()=>stamp},
    fetch:async()=>({ok:true,json:async()=>{fetches++;return spec.slots(fetches);}}),
    Set,Array,Number,String};
  vm.runInNewContext(script,ctx,{timeout:2000});
  listeners.click[0]({target:{closest:s=>s==='button'?button:null}});
  async function next(when,after) {
    if(!timers.length) throw Error('timer missing');
    stamp=when;
    const task=timers.shift(); task.fn();
    for(let j=0;j<10;j++) await Promise.resolve();
    if(after)after();
  }
  await next(1100,()=>{
    if(spec.addAnswer){
      messages.push({...root,matches:()=>false}); // user bubble
      messages.push({...root,matches:s=>s.includes('[data-role="assistant"]')});
    } else if(spec.addUser){
      messages.push({...root,matches:()=>false}); // only user bubble
    }
  });
  if (timers.length) await next(9100);
  if(spec.expectBadge) {
    if(!badge || badge.textContent!==(spec.label||'界面实测')+' ≈ 9.0 s')
      throw Error('missing measured label: '+(badge&&badge.textContent));
  }else if(badge)throw Error('unverifiable timing must not be displayed');
  if(spec.expectFetches && fetches!==spec.expectFetches)throw Error('unexpected requests '+fetches);
 }
 await runCase({slots:n=>[{id:'main',running:n===1}],addAnswer:true,expectBadge:true});
 for(const [lang,label] of [['zh-TW','介面實測'],['en','UI observed'],['ja','画面で計測'],['ko','화면 측정']])
   await runCase({lang,label,slots:n=>[{id:'main',running:n===1}],addAnswer:true,expectBadge:true});
 await runCase({slots:n=>[{id:'main',running:n===1},{id:'secondary',running:false}],addAnswer:true,expectBadge:true});
 await runCase({slots:n=>[{id:'main',running:n===1}],addUser:true,expectBadge:false});
 await runCase({slots:n=>[{id:'a',running:true},{id:'b',running:true}],addAnswer:true,expectBadge:false,expectFetches:2});
 await runCase({slots:n=>[{id:'main',running:false}],addAnswer:true,expectBadge:false});
 console.log('client latency: observed, multi-slot bind, user-only, ambiguous and no-busy cases verified');
})().catch(e=>{console.error(e);process.exitCode=1;});
