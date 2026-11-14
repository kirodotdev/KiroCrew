// Simulated Crew 0.11.0-rc.1 shared ModelField -> SimpleSelect.
// Both NewCrewmateDialog and CrewEditorModal use this same field.
// Presentation-only: the option wire values MUST remain untouched.
const fs=require('fs');
const vm=require('vm');
const assert=require('assert');
const source=fs.readFileSync(process.argv[2],'utf8');
const wire='kiro-byo-type:tiga:sky';
const second='kiro-byo-type:tiga:multi';
function element(attrs={}) {
  const a={...attrs};
  return {nodeType:1, childNodes:[], attributes:a,children:[],
    getAttribute:k=>a[k]??null,setAttribute:(k,v)=>{a[k]=v},
    hasAttribute:k=>Object.hasOwn(a,k),removeAttribute:k=>{delete a[k]},
    closest:()=>null,querySelector:()=>null,querySelectorAll:()=>[]};
}
function text(value,parent) {const n={nodeType:3,nodeValue:value,parentElement:parent};parent.childNodes.push(n);return n}
function makeScenario({opened, role='dialog'}={}) {
  const callbacks={};let observer;
  const dialog=element({role});
  const trigger=element({'data-ui-location':'agents.model','role':'combobox','aria-controls':'mate-list', 'aria-expanded':opened?'true':'false'});
  const selected=text(wire,trigger);
  const native=element();
  const nativeOption=element({value:second});nativeOption.nodeName='OPTION';
  const nativeText=text(second,nativeOption);
  native.options=[nativeOption];nativeOption.value=second;
  const option1=element({'role':'option','data-state':'unchecked','data-wire':wire});
  const option2=element({'role':'option','data-state':'unchecked','data-wire':second});
  const optionOther=element({'role':'option','data-wire':'DeepSeek-V4.1-Flash'});
  const optionAuto=element({'role':'option','data-wire':'auto'});
  const t1=text(wire,option1),t2=text(second,option2),t3=text('DeepSeek-V4.1-Flash',optionOther),t4=text('继承',optionAuto);
  const list=element({'role':'listbox',id:'mate-list'});
  list.querySelectorAll=q=>q==='[role="option"]'?[optionAuto,option1,option2,optionOther]:[];
  trigger.closest=q=>q==='[role="dialog"]'?dialog:null;
  const note=element();const noteText=text('使用 '+second+' — 来自它所基于的自定义代理。',note);
  dialog.querySelectorAll=q=>q==='[data-kiro-byo-owned-mate-note]'?[note]:[];
  const document={body:element(),
    addEventListener:(name,fn)=>{callbacks[name]=fn},
    querySelector:q=>q==='[data-testid="composer-model-chip"]'?null:null,
    querySelectorAll:q=> {
      if(q==='[data-ui-location="agents.model"]')return [trigger];
      if(q==='[data-setting-label]'||q==='[role="group"][aria-label]'||q.startsWith('[data-kiro-byo-crew11-auto]'))return [];
      return [];
    },
    getElementById:id=>id==='mate-list'?list:null,
    createTreeWalker:root=> {const leaves=[];function collect(n){if(n.nodeType===3){leaves.push(n);return;}for(const c of n.childNodes||[])collect(c)}collect(root);let i=0;return {nextNode:()=>leaves[i++]||null}}
  };
  const ctx={document,NodeFilter:{SHOW_TEXT:4},MutationObserver:class{constructor(fn){observer=fn}observe(){}},window:{requestAnimationFrame:fn=>fn()}};
  vm.runInNewContext(source,ctx,{timeout:3000});
  assert.equal(selected.nodeValue,'Sky Type','selected Mate model trigger');
  if(opened){
    assert.equal(t1.nodeValue,'Sky Type','Mate Sky option label');
    assert.equal(t2.nodeValue,'Multi Type','Mate Multi option label');
    assert.equal(t3.nodeValue,'DeepSeek-V4.1-Flash','concrete model unchanged');
    assert.equal(t4.nodeValue,'继承','inherit preserved');
    assert.equal(option1.getAttribute('data-wire'),wire,'wire untouched');
    assert.equal(option2.getAttribute('data-wire'),second,'second wire untouched');
  } else {
    assert.equal(t1.nodeValue,wire,'closed Mate dropdown should not reach unrelated portal');
  }
  assert.equal(nativeOption.value,second,'native option value unchanged');
  return {trigger, list, selected, observer, option1, noteText};
}
makeScenario({opened:true});
makeScenario({opened:false});
console.log('Mate model trigger / portal choices / selected wire preservation OK');
