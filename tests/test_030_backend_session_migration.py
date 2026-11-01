"""Host-coordinator tests: the *actual* warm provider must retire before relabel."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo import config, crew10_patch, paths, crew_patch_common


@pytest.mark.parametrize('origin,target_backend,pack,prior,expected', [
    ('kiro','kiro-byo','dyna','auto','kiro-byo-type:dyna:flash'),
    ('kiro-byo','kiro','dyna','kiro-byo-type:dyna:flash',''),
    ('kiro-byo','kiro-byo','tiga','kiro-byo-type:dyna:flash','kiro-byo-type:tiga:multi'),
    ('kiro-byo','kiro-byo','dyna','kiro-byo-type:tiga:multi','kiro-byo-type:dyna:flash'),
])
@pytest.mark.parametrize('busy', [False, True])
def test_warm_session_backend_transition_resets_or_preserves_running(
    monkeypatch, tmp_path: Path, origin: str, target_backend: str, pack: str,
    prior: str, expected: str, busy: bool,
) -> None:
    cfg = SimpleNamespace(models=[object()],mode_pack=pack,mode_type='multi' if pack=='tiga' else 'flash')
    monkeypatch.setattr(config,'load_catalog_config',lambda _p:cfg)
    monkeypatch.setattr(paths,'use_app_home',lambda _p:contextlib.nullcontext())
    for name in ['kiro_crew','kiro_crew.apps','kiro_crew.apps.manager','kiro_crew.dashboard','kiro_crew.dashboard.chat_handlers']:
        monkeypatch.setitem(sys.modules,name,types.ModuleType(name))
    sys.modules['kiro_crew.apps.manager'].app_data_dir=lambda _: tmp_path
    ch=sys.modules['kiro_crew.dashboard.chat_handlers']
    ch._slot_switch_session_lock=lambda _key: asyncio.Lock()
    ch._switch_target_busy=lambda *args: busy
    async def _no_children(*_args):return None
    ch._subagents_attached_response=_no_children
    events=[]
    async def reset(*_args,**_kwargs):
        events.append('reset')
        state.provider=None
        return True
    ch._reset_slot_session_or_warn=reset
    ch._broadcast_context_reset=lambda *args:events.append('broadcast')

    slot=SimpleNamespace(
        key='s1', model=prior, jev_route=False, _dirty=False,
        _lock=asyncio.Lock(), _model_pick_lock=asyncio.Lock(), _model_pick_gen=0,
        _app='',linked_session_key=None,_deferred_model_switch_key=None,
        _active_fallback_model=None,_refusal_fallback_primary=None
    )
    class State:
        def __init__(self):
            self._slots={'s1':slot}
            self.provider=SimpleNamespace(capabilities=SimpleNamespace(backend=origin))
            self.sessions=SimpleNamespace(get_provider=lambda _key:self.provider)
            self.pushes=0
        def push_slots_update(self):self.pushes+=1
    state=State()
    async def current(_slot):return target_backend if target_backend!='kiro' else ''
    ns={
      'DashboardState': State,'_ChatSlot': object,'ACP_BACKEND_KIRO_BYO':'kiro-byo',
      'asyncio':asyncio,'logger':logging.getLogger('test-backend-migrate'),
      '_configured_backend_for_slot':current,'effective_session_key':lambda slot:slot.key
    }
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER,ns)
    changed=asyncio.run(ns['_kiro_byo_reconcile_slot_routing'](state))
    assert changed is (not busy)
    assert slot.model == (prior if busy else expected)
    assert events == ([] if busy else ['reset','broadcast'])
    assert state.provider is (None if not busy else state.provider)
    assert state.pushes == int(not busy)


def test_native_models_endpoint_runs_same_reconciler_as_byo() -> None:
    """The official arm must not return before the reconciliation hook."""
    native='''import asyncio
import logging
logger = logging.getLogger('test')
async def api_models(request):
    """Serve native or custom models."""
    cfg = await asyncio.to_thread(lambda: object())
    backend = 'kiro'
    if backend == 'kiro-byo':
        return 'byo'
    return 'official'
'''
    patched=crew_patch_common._patch_models_backend_reconcile(native)
    assert patched==crew_patch_common._patch_models_backend_reconcile(patched)
    assert patched.index('Kiro BYO: reconcile idle sessions') < patched.index("    backend = 'kiro'")
    compile(patched,'agents.py','exec')
