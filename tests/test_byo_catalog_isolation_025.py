"""0.2.5 hotfix: no official model leaks into BYO menus or source upgrades."""
from __future__ import annotations

import asyncio
import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_byo import crew11_patch, crew_patch_common
from kiro_byo.integration_manager import _converge_semantic_transform
from test_023_parity_guards import _model_helper
from test_crew_shared_transform import _minimal_agents_source_for_member_backend

ROOT = Path(__file__).resolve().parents[1]
AGENTS = 'src/kiro_crew/dashboard/handlers/agents.py'


def test_unconfigured_catalog_never_reintroduces_official_default(monkeypatch, tmp_path):
    helper = _model_helper(monkeypatch, tmp_path, lambda _p: SimpleNamespace(
        providers=(), public_models=(), mode_pack='tiga',
    ))
    assert asyncio.run(helper(SimpleNamespace(app={}), configured_default='official/claude')) == []


def test_removed_providers_do_not_resurrect_stale_advertisements(monkeypatch, tmp_path):
    helper = _model_helper(monkeypatch, tmp_path, lambda _p: SimpleNamespace(
        providers=(), public_models=(), mode_pack='tiga',
    ), advertised=('removed/byo-model', 'official/model'))
    assert asyncio.run(helper(SimpleNamespace(app={}), configured_default='official/model')) == []


def test_configured_byo_but_empty_catalog_fails_closed_not_official(monkeypatch, tmp_path):
    helper = _model_helper(monkeypatch, tmp_path, lambda _p: SimpleNamespace(
        providers=(object(),), public_models=(), mode_pack='tiga',
    ), advertised=('stale/byo-model',))
    with pytest.raises(web.HTTPServiceUnavailable):
        asyncio.run(helper(SimpleNamespace(app={}), configured_default='official/model'))


def test_legacy_025_catalog_helper_upgrades_without_pristine_backup():
    fresh = crew11_patch.patch_text(AGENTS, _minimal_agents_source_for_member_backend())
    old = fresh.replace(crew_patch_common._KIRO_BYO_MODEL_HELPER,
                        crew11_patch._PREVIOUS_AGENTS_MODEL_HELPER, 1)
    assert old != fresh
    assert crew11_patch.inspect(AGENTS, old).state == 'upgradeable'
    upgraded = _converge_semantic_transform(
        AGENTS, old, inspect_fn=crew11_patch.inspect,
        transform_fn=crew11_patch.patch_text, transform_error=crew11_patch.PatchError,
    )
    assert upgraded == fresh
    assert crew11_patch.inspect(AGENTS, upgraded).state == 'patched'
    compile(upgraded, AGENTS, 'exec')


def test_unknown_catalog_helper_mutation_still_rejected():
    fresh = crew11_patch.patch_text(AGENTS, _minimal_agents_source_for_member_backend())
    old = fresh.replace(crew_patch_common._KIRO_BYO_MODEL_HELPER,
                        crew11_patch._PREVIOUS_AGENTS_MODEL_HELPER, 1)
    tampered = old.replace('return type_rows + model_rows', 'return []', 1)
    assert crew11_patch.inspect(AGENTS, tampered).state == 'incompatible'


def test_task_picker_only_contains_byo_models_in_active_mode(tmp_path):
    # Execute the actual TasksPage React tree with tiny fake hooks and DOM nodes.
    # The tests inspect options, not source-string comments.
    ui = (ROOT / 'ui/index.mjs').read_text(encoding='utf-8')
    chunk = 'function TasksPage(' + ui.split('function TasksPage(', 1)[1].split('function RecordsPage(', 1)[0]
    script = r'''
const vm=require('vm'),fs=require('fs');
const src=fs.readFileSync(process.argv[2], 'utf8');
const KSelect={component:'KSelect'}, Panel={component:'Panel'};
const make=(backend)=>{
 const data={main_backend:backend,tasks:[{id:'task1',name:'Job',execution_kind:'agent',enabled:true,model:'official/model'}],
  models:[{id:'byo/model',name:'BYO model'},{id:'kiro-byo-type:tiga:multi',name:'Multi Type'}],
  background:{model:'official/model',resolved_model:'official/model'},execution:{recent:[]}};
 let i=0;
 const useState=(initial)=>{const v=i++===0?data:initial;return [v,()=>{}]};
 const scope={KSelect,Panel,useState,useCallback:fn=>fn,useEffect:()=>{},
  tr:key=>key,h:(type,props,...children)=>({type,props:props||{},children}),api:async()=>{},
  nfmt:x=>x,ms:x=>x,when:x=>x};
 vm.runInNewContext(src+'\nthis.rendered=TasksPage({});',scope);
 const all=[];
 const walk=n=>{if(!n)return;if(Array.isArray(n)){n.forEach(walk);return}if(typeof n==='object'){
   all.push(n);(n.children||[]).forEach(walk);}};
 walk(scope.rendered);
 return all;
};
const byo=make('kiro-byo');
const selects=byo.filter(n=>n.type===KSelect);
if(selects.length!==3)throw Error('Expected task, context, background selectors, got '+selects.length);
const models=selects.filter(n=>n.props.ariaLabel);
if(models.length!==2)throw Error('Missing BYO model selectors');
for(const n of models){
 if(n.props.value!=='official/model')throw Error('Native stale pin was overwritten');
 if(n.props.placeholder!=='linkage.foreign_model')throw Error('Foreign pin not visibly flagged');
 if(n.props.options.some(o=>o.value==='official/model'))throw Error('Official model selectable in BYO');
 if(!n.props.options.some(o=>o.value==='byo/model'))throw Error('BYO model missing');
 if(!n.props.options.some(o=>o.value==='kiro-byo-type:tiga:multi'))throw Error('Type missing');
}
const official=make('kas');
if(official.filter(n=>n.type===KSelect&&n.props.ariaLabel).length)
 throw Error('BYO selection control visible in official mode');
if(!official.some(n=>n.type==='span'&&n.children.includes('official/model')))
 throw Error('Official native pin not displayed read-only');
'''
    js = tmp_path / 'test.cjs'
    src = tmp_path / 'component.js'
    js.write_text(script, encoding='utf-8')
    src.write_text(chunk, encoding='utf-8')
    result = subprocess.run(['node', str(js), str(src)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
