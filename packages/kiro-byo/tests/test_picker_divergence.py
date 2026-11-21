"""Reproduce stale open-model-picker in host-injected script, without a real Windows host."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kiro_byo.crew11_model_labels import block


@pytest.mark.skipif(not shutil.which('node'), reason='Node unavailable')
def test_open_picker_stale_mode_with_matching_chip_and_bounded_retries(tmp_path: Path):
    script = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S).group(1)
    (tmp_path / 'inject.js').write_text(script, encoding='utf8')
    runner = r'''
const vm=require('node:vm'),fs=require('node:fs');
(async()=>{
 const src=fs.readFileSync(process.argv[2],'utf8');
 const store=new Map();let now=1700000000000;let server='dyna';let displayed='tiga';
 let reloads=0,queries=0,slots=0;
 const settle=async()=>{for(let i=0;i<16;i++)await new Promise(r=>setImmediate(r));};
 async function mount(){
  let tick;
  const window={setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{},
    addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
    sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
    location:{reload:()=>{reloads++;}}};
  const modelNode={getAttribute:()=> 'kiro-byo-type:'+displayed+':multi',
    closest:()=>({})};
  const chip={getAttribute:key=>key==='data-model-id'?'kiro-byo-type:'+server+':'+(server==='dyna'?'flash':'multi'):'',
    textContent:'',closest:()=>null};
  const document={body:null,visibilityState:'visible',addEventListener:()=>{},dispatchEvent:()=>{},
    querySelector:key=>key==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:key=>key.startsWith('[data-model-id^=')?[modelNode]:[]};
  const fetch=async path=>{
    if(path==='/api/models'){queries++;return {ok:true,json:async()=>[
      {model_name:'kiro-byo-type:'+server+':'+(server==='dyna'?'flash':'multi')} ]};}
    if(path==='/api/chat/slots'){slots++;return {ok:true,json:async()=>[{running:false,queue_depth:0}]};}
    throw Error('unexpected '+path);
  };
  vm.runInNewContext(src,{document,window,fetch,
    Date:class extends Date{static now(){return now;}},
    Event:class{constructor(t){this.type=t;}},MutationObserver:class{observe(){}},
    BroadcastChannel:class{constructor(){}},console});
  await settle();return tick;
 }
 await mount();if(reloads!==1)throw Error('stale picker not refreshed '+reloads);
 await mount();if(reloads!==1)throw Error('reloaded immediately despite cooloff');
 now+=16000;await mount();if(reloads!==2)throw Error('second bounded attempt missing '+reloads);
 now+=16000;await mount();if(reloads!==2)throw Error('reload loop not bounded '+reloads);
 // Switching back to Tiga with a converged chip resets the prior Dyna guard.
 server='tiga';await mount();if(reloads!==2)throw Error('matched state reloaded');
 server='dyna';await mount();if(reloads!==3)throw Error('return transition remains poisoned '+reloads);
 console.log('ok',JSON.stringify({reloads,queries,slots}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    (tmp_path / 'runner.cjs').write_text(runner, encoding='utf8')
    run = subprocess.run(['node', str(tmp_path / 'runner.cjs'), str(tmp_path / 'inject.js')],
                         capture_output=True, text=True, timeout=12)
    assert run.returncode == 0, run.stdout + run.stderr


def test_read_only_mode_sync_route_registered():
    import ast
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "backend" / "routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "get_mode_sync"
               for n in tree.body)
    assert 'AppRoute("GET", "/mode-sync", get_mode_sync)' in source
