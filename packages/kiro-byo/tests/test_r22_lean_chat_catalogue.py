"""R22: polling is chat-scoped, model fingerprint is order independent.

Tests the exact injected script in a Node VM; this is not a desktop E2E test.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kiro_byo.crew11_model_labels import block


@pytest.mark.skipif(shutil.which('node') is None, reason='Node unavailable')
def test_chat_only_poll_and_stable_model_catalogue(tmp_path: Path) -> None:
    match = re.search(r'<script[^>]*>(.*?)</script>', block(), re.S)
    assert match
    script = tmp_path / 'script.js'
    script.write_text(match.group(1), encoding='utf-8')
    harness = tmp_path / 'harness.cjs'
    harness.write_text(r'''
const fs=require('node:fs'),vm=require('node:vm');
(async()=>{
  let tick, interval, composer=false, reloads=0, calls=0;
  let catalogue=['model-a','model-b'];
  const option={getAttribute:k=>k==='data-model-id'?'kiro-byo-type:tiga:multi':'',closest:()=>({})};
  const chip={getAttribute:k=>k==='data-model-id'?'kiro-byo-type:tiga:multi':'',textContent:'Multi Type'};
  const store=new Map();
  const window={setInterval:(fn,ms)=>{tick=fn;interval=ms;return 1;},clearInterval:()=>{},
    addEventListener:()=>{},dispatchEvent:()=>{},requestAnimationFrame:()=>1,
    sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
    location:{reload:()=>reloads++}};
  const document={body:null,visibilityState:'visible',addEventListener:()=>{},dispatchEvent:()=>{},
    querySelector:k=>composer&&k==='[data-testid="composer-model-chip"]'?chip:null,
    querySelectorAll:k=> k.startsWith('[data-model-id^=')?[option]:
      k==='[role="option"] [data-model-id]'?[option,...['model-a','model-b'].map(id=>({getAttribute:()=>id}))]:[]};
  const fetch=async path=>{
    if(path==='/api/models'){calls++;return {ok:true,json:async()=>[
      {model_name:'kiro-byo-type:tiga:multi'},...catalogue.map(model_name=>({model_name}))]};}
    if(path==='/api/chat/slots')return {ok:true,json:async()=>[{running:false,queue_depth:0}]};
    throw Error('unexpected '+path);
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),{window,document,fetch,
    Event:class{constructor(type){this.type=type;}},BroadcastChannel:class{constructor(){}},
    MutationObserver:class{observe(){}}});
  const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
  await settle();
  if(interval!==10000)throw Error('poll not reduced to 10 seconds');
  for(let i=0;i<6;i++){tick();await settle();}
  if(calls!==0)throw Error('polled outside chat '+calls);
  composer=true;
  tick();await settle();
  if(calls!==1||reloads!==0)throw Error('chat baseline changed');
  catalogue=['model-b','model-a','model-a'];
  tick();await settle();
  if(reloads!==0)throw Error('model reorder/duplicate caused reload');
  catalogue.push('relay-new-model');
  tick();await settle();
  if(reloads!==1)throw Error('actual new relay model did not trigger guarded refresh');
  composer=false;
  for(let i=0;i<6;i++){tick();await settle();}
  if(calls!==3)throw Error('kept polling outside chat '+calls);
  console.log('ok',calls,reloads);
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
    result = subprocess.run(['node', str(harness), str(script)], capture_output=True, text=True, timeout=18)
    assert result.returncode == 0, result.stdout + result.stderr
