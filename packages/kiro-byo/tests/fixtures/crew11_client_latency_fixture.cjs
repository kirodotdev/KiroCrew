const fs = require('fs'), vm = require('vm');
const script = fs.readFileSync(process.argv[2], 'utf8');
(async function main() {
 async function runCase(spec) {
  const listeners = {}, timers = [], logs = [];
  let stamp = 100, fetches = 0, legacyRemoved = false, domWrites = 0;
  const root = {isConnected:true,getClientRects:()=>[{}],checkVisibility:()=>true,
    closest:()=>null};
  const input = {...root, contains:n=>n===button};
  const messages = [{...root,matches:()=>false}];
  const chat = {...root,querySelectorAll:s=>s==='.message-bubble'?messages:[]};
  const chip = {...root};
  const doc = {
    visibilityState:'visible', documentElement:{lang:spec.lang||'zh-CN',
      getAttribute:key=>key==='data-kiro-byo-ui-timing' && spec.off ? 'off' : null},
    body:{appendChild:()=>{domWrites++;throw Error('headless probe must not append overlay');}},
    addEventListener:(kind,fn)=>{(listeners[kind]||(listeners[kind]=[])).push(fn);},
    querySelector:s=>s==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:s=>s==='.input-area'?[input]:s==='.chat-container'?[chat]:
      s==='[data-kiro-byo-client-latency]' && spec.legacy ? [{remove:()=>{legacyRemoved=true;}}] : [],
    createElement:()=>{domWrites++;throw Error('headless probe must not create elements');}
  };
  const button = {type:'submit',disabled:false, getAttribute:()=>'',closest:s=>s==='.input-area'?input:null};
  const wnd = {requestAnimationFrame:fn=>{stamp+=6;fn();},
    setTimeout:(fn,delay)=>{const id={fn,delay}; timers.push(id);return id;},
    clearTimeout:id=>{const i=timers.indexOf(id);if(i>=0) timers.splice(i,1);},
    addEventListener:()=>{}};
  const ctx={document:doc,window:wnd,performance:{now:()=>stamp},
    fetch:async()=>({ok:true,json:async()=>{fetches++;return spec.slots(fetches);}}),
    console:{info:s=>logs.push(s)}, Date, Object, Set, Array,Number,String};
  vm.runInNewContext(script,ctx,{timeout:2000});
  if (!wnd.__kiroByoUiTiming || wnd.__kiroByoUiTiming.version!==2)
    throw Error('headless timing inspection API missing');
  if (spec.legacy && !legacyRemoved) throw Error('legacy overlay not cleared');
  if (spec.off) {
    if (listeners.click || timers.length || fetches || domWrites || logs.length ||
        wnd.__kiroByoUiTiming.last())
      throw Error('off must not activate timing or show anything');
    return;
  }
  if (!listeners.click || listeners.click.length!==1)
    throw Error('headless probe should measure by default');
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
      messages.push({...root,matches:()=>false}); // user-only bubble
    }
  });
  if (timers.length) await next(9100);
  const last=wnd.__kiroByoUiTiming.last();
  if(spec.expectMeasured) {
    if (!last || last.seconds!==9.0 || last.kind!=='send-to-final-reply-paint' ||
        !last.observedAt || logs.length!==1 || !logs[0].includes('9.0 s'))
      throw Error('verified reply should record ≈9.0s, got '+JSON.stringify(last));
    // Consumers cannot mutate the stored ring buffer via inspection methods.
    last.seconds=-999;
    if (wnd.__kiroByoUiTiming.last().seconds!==9.0) throw Error('readout must be defensive');
  } else if(last || logs.length) throw Error('unverifiable timing must not be recorded');
  if(domWrites) throw Error('headless timing must never create a DOM overlay');
  if(spec.expectFetches && fetches!==spec.expectFetches)throw Error('unexpected requests '+fetches);
 }
 await runCase({legacy:true,slots:n=>[{id:'main',running:n===1}],addAnswer:true,expectMeasured:true});
 await runCase({off:true,legacy:true,slots:()=>[],addAnswer:true});
 await runCase({slots:n=>[{id:'main',running:n===1}],addAnswer:true,expectMeasured:true});
 await runCase({slots:n=>[{id:'main',running:n===1},{id:'secondary',running:false}],addAnswer:true,expectMeasured:true});
 await runCase({slots:n=>[{id:'main',running:n===1}],addUser:true,expectMeasured:false});
 await runCase({slots:n=>[{id:'a',running:true},{id:'b',running:true}],addAnswer:true,expectMeasured:false,expectFetches:2});
 await runCase({slots:n=>[{id:'main',running:false}],addAnswer:true,expectMeasured:false});
 console.log('client latency: headless default-on measurement, opt-out, legacy cleanup, observed, multi-slot bind, user-only, ambiguous and no-busy cases verified');
})().catch(e=>{console.error(e);process.exitCode=1;});
