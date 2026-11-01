// Isolated DOM contract exercise for the injected Crew 0.11 presentation bridge.
// This is not a browser E2E test; it deliberately tests the stable RC1 seams.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync(process.argv[2], 'utf8');
const wire = 'kiro-byo-type:tiga:multi';
function element(initial={}) {
  const a = {...initial};
  return {
    getAttribute:k=>a[k]??null,
    setAttribute:(k,v)=>a[k]=v,
    removeAttribute:k=>delete a[k],
    hasAttribute:k=>Object.hasOwn(a,k),
    attrs:a,
    closest:()=>null,
    querySelector:()=>null,
    querySelectorAll:()=>[],
  };
}
function leaf(text, parent) { return {nodeType:3,nodeValue:text,parentElement:parent}; }
function widget(wireName, role='option') {
  const span = element({'data-model-id':wireName});
  const txt = leaf(wireName, span);
  span.childNodes=[txt]; span.firstChild=txt;
  const row=element({role});
  row.querySelector=s=>s==='[data-model-id]' || s.startsWith('[data-model-id^=') ? span:null;
  span.closest=s=>s.includes('role=') ? row:null;
  row.parentElement=null;
  return {row,span,txt};
}
function run(mode, lateMount=false) {
  let mounted=!lateMount;
  const auto=widget('auto');
  const sky=widget('kiro-byo-type:tiga:sky');
  const multi=widget(wire);
  const model=widget('mimo-v2.6-flash');
  const items=[auto,sky,multi,model];
  const list=element();
  list.querySelectorAll=s=>s===':scope > button[role="option"]'?items.map(e=>e.row):[];
  items.forEach(x=>x.row.parentElement=list);
  const chipText=leaf(mode==='chat'?wire:'mimo-v2.6-flash',element());
  const chip={querySelector:s=>s==='span.truncate'?{childNodes:[chipText],firstChild:chipText}:null,
    getAttribute:()=>'',setAttribute:()=>{}};
  const defaultField=element({'data-setting-label':'默认模型'});
  const defaultText=leaf(wire,element());
  const trigger=element({role:'combobox'});
  trigger.children=[defaultText];
  defaultField.querySelectorAll=s=>s==='[role="combobox"], select, option'?[trigger]:[];
  const selectField=element({'data-setting-label':'可选模型'});
  const checkbox=element({'aria-label':wire});
  const multText=leaf(wire,element());
  const multSpan={childNodes:[multText],firstChild:multText};
  const multiRow={querySelector:s=>s==='input[aria-label],button[aria-label]'?checkbox:null,
    querySelectorAll:s=>s==='span'?[multSpan]:[]};
  const multiGroup=element({'aria-label':'可选模型'});
  multiGroup.querySelectorAll=s=>s==='[data-multi-select-option]'?[multiRow]:[];
  const callbacks={};
  let observed=null;
  let portalRows=[];
  const document={
    body:element(),
    addEventListener:(n,fn)=>callbacks[n]=fn,
    querySelector:s=>s==='[data-testid="composer-model-chip"]' && mode==='chat'?chip:null,
    querySelectorAll:s=>{
      if(s==='[data-model-id^="kiro-byo-type:"]') return mounted?[sky.span,multi.span]:[];
      if(s==='button[role="option"] [data-model-id^="kiro-byo-type:"]') return mounted?[sky.span,multi.span]:[];
      if(s.startsWith('[data-kiro-byo-crew11-auto]')) return !mounted?[]:items.map(x=>x.row).filter(row=>row.hasAttribute('data-kiro-byo-crew11-auto')||row.hasAttribute('data-kiro-byo-crew11-type-first')||row.hasAttribute('data-kiro-byo-crew11-model-first'));
      if(s==='[data-setting-label]') return [defaultField,selectField];
      if(s==='[role="group"][aria-label]') return [multiGroup];
      if(s==='[role="listbox"] [role="option"]') return portalRows;
      return [];
    },
    createTreeWalker:root=>{
      const nodes=root.children||[];
      let i=0;
      return {nextNode:()=>nodes[i++]||null};
    }
  };
  vm.runInNewContext(code,{document, NodeFilter:{SHOW_TEXT:4}, MutationObserver:class {constructor(fn){observed=fn} observe(){}}, window:{requestAnimationFrame:fn=>fn()}},{timeout:3000});
  if(lateMount) {
    // RC3 can render human-friendly Type text from the start. The wire id
    // survives only in data-model-id, not in the newly mounted textContent.
    sky.txt.nodeValue='Sky Type';
    multi.txt.nodeValue='Multi Type';
    mounted=true;
    const newMenu={nodeType:1,textContent:'Sky Type Multi Type',
      matches:()=>false,
      querySelector:s=>s==='[data-model-id^="kiro-byo-type:"]'?sky.span:null};
    observed([{type:'childList',addedNodes:[newMenu]}]);
  }
  assert.equal(sky.txt.nodeValue,'Sky Type');
  assert.equal(multi.txt.nodeValue,'Multi Type');
  assert.equal(multi.span.getAttribute('data-model-id'),wire);
  assert.equal(defaultText.nodeValue,'Tiga · Multi Type');
  assert.equal(multText.nodeValue,'Tiga · Multi Type');
  assert.equal(checkbox.getAttribute('aria-label'),'Tiga · Multi Type');
  if(mode==='chat') {
    assert.equal(auto.row.getAttribute('data-kiro-byo-crew11-auto'),'1');
    assert.ok(auto.row.hasAttribute('inert'));
    assert.equal(sky.row.getAttribute('data-kiro-byo-crew11-type-first'),'1');
    assert.equal(sky.row.getAttribute('data-kiro-byo-crew11-group-label'), '形态 · 默认复合型，可手动切换');
    assert.equal(model.row.getAttribute('data-kiro-byo-crew11-model-first'),'1');
    assert.equal(chipText.nodeValue,'Tiga · Multi Type');
  } else {
    assert.equal(auto.row.getAttribute('data-kiro-byo-crew11-auto'),null);
    assert.equal(sky.row.getAttribute('data-kiro-byo-crew11-type-first'),null);
    assert.equal(model.row.getAttribute('data-kiro-byo-crew11-model-first'),null);
  }
  // Simulate Radix's default-model portal opening after pointerdown on Settings.
  if (mode==='settings') {
    const target={closest:s=>s==='[data-setting-label]'?defaultField:null};
    callbacks.pointerdown({target});
    const portalText=leaf(wire,element());
    portalRows=[{children:[portalText]}];
    observed([{type:'childList',addedNodes:[{nodeType:3,nodeValue:wire}]}]);
    assert.equal(portalText.nodeValue,'Tiga · Multi Type');
  }
  assert.equal(model.span.getAttribute('data-model-id'),'mimo-v2.6-flash');
  assert.equal(auto.span.getAttribute('data-model-id'),'auto');
}
run('chat');run('settings');run('chat',true);
console.log('chat grouping / auto isolation / settings labeling / wire identity OK');
