from __future__ import annotations

import json
from pathlib import Path

from kiro_byo import crew10_patch
from kiro_byo.integration_manager import _transformer_for

ROOT = Path(__file__).resolve().parents[1]
SLOT_EFFORT = "src/kiro_crew/dashboard/chat_api/slot_effort.py"
CHAT_HANDLERS = "src/kiro_crew/dashboard/chat_handlers.py"
CHAT_TITLE = "src/kiro_crew/dashboard/chat_title.py"
HARNESS_REL = "src/kiro_crew/acp/harness/kiro_byo.py"


def test_crew10_adapter_metadata_matches_transformer() -> None:
    adapter = ROOT / "integration/kiro_crew/v0.10-adaptive"
    compat = json.loads((adapter / "compatibility.json").read_text(encoding="utf-8"))
    manifest = json.loads((adapter / "manifest.json").read_text(encoding="utf-8"))
    assert compat["transformer"] == "adaptive10"
    assert compat["verified_through"] == "0.10.0-insider.4"
    assert compat["verified_commit"] == "e9bfada"
    assert tuple(compat["generated_files"]) == crew10_patch.TARGET_FILES
    assert SLOT_EFFORT in crew10_patch.TARGET_FILES
    assert CHAT_HANDLERS in crew10_patch.TARGET_FILES
    assert CHAT_TITLE in crew10_patch.TARGET_FILES
    assert HARNESS_REL not in crew10_patch.TARGET_FILES
    assert HARNESS_REL in compat["created_files"]
    assert HARNESS_REL in manifest["files"]
    transform_files, error, inspect, patch_text = _transformer_for(compat)
    assert transform_files == frozenset(crew10_patch.TARGET_FILES)
    assert error is crew10_patch.PatchError
    assert inspect is crew10_patch.inspect
    assert patch_text is crew10_patch.patch_text


def _minimal_handlers_namespace() -> str:
    return '''from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS  # noqa: F401
from kiro_crew.dashboard import chat_api as _chat_api
from kiro_crew.dashboard.chat_api import slot_effort as _owner_slot_effort
from kiro_crew.dashboard.chat_api.slot_effort import (
    _configured_backend_for_slot,
    api_chat_slot_reasoning_effort,
    api_chat_slot_selection_capabilities,
)
_chat_api.compose(globals(), (_owner_slot_effort,))
'''


def test_crew10_handlers_namespace_imports_composed_owner_helpers() -> None:
    source = _minimal_handlers_namespace()
    assert crew10_patch.inspect(CHAT_HANDLERS, source).state == "clean"
    patched = crew10_patch.patch_text(CHAT_HANDLERS, source)
    assert "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO" in patched
    assert "_kiro_byo_cold_selection_capabilities," in patched
    assert "_kiro_byo_type_selection_active," in patched
    assert "_kiro_byo_reconcile_slot_routing," in patched
    compile(patched, CHAT_HANDLERS, "exec")
    assert crew10_patch.inspect(CHAT_HANDLERS, patched).state == "patched"
    assert crew10_patch.patch_text(CHAT_HANDLERS, patched) == patched


def _minimal_slot_effort_source() -> str:
    return '''from __future__ import annotations
import asyncio
import contextlib
import logging
from typing import Any
from aiohttp import web
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
from kiro_crew.dashboard.chat_persistence import (
    cap_effort_capability_levels,
    get_reasoning_effort_ordered,
    get_reasoning_effort_values,
    register_reasoning_effort_values,
)

logger = logging.getLogger(__name__)

class _ChatSlot:
    model = ""

class AcpProvider:
    pass

class DashboardState:
    pass

def _normalize_model(value):
    return str(value or "").lower()

def effective_session_key(slot):
    return "s"

def deny_app_slot_access(*args):
    return None

def slot_not_found():
    return None

async def _configured_backend_for_slot(slot: _ChatSlot) -> str:
    return "kiro-byo"

async def api_chat_slot_selection_capabilities(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_selection_capabilities")
    if denied is not None:
        return denied
    provider = state.sessions.get_provider(effective_session_key(slot))
    if not isinstance(provider, AcpProvider):
        backend = await _configured_backend_for_slot(slot)
        return web.json_response({
            "known": False,
            "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
        })
    backend = provider.capabilities.backend
    supported = provider.supports_effort()
    levels = []
    if supported and not levels:
        levels = ["low", "high"]
    return web.json_response({
        "known": True,
        "backend": backend,
        "effort_supported": supported and bool(levels),
        "effort_levels": levels,
        "model_effort_pair_ids": backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    })

async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if slot is None:
        return slot_not_found()
    session_key = effective_session_key(slot)
    async with contextlib.AsyncExitStack():
        _live = state.sessions.get_provider(session_key)
        effort = "high"
        if effort and isinstance(_live, AcpProvider) and not _live.accepts_effort_level(effort):
            return web.json_response({"error": "not offered"})
        slot.reasoning_effort = effort
        return web.json_response({"ok": True})
'''


def test_crew10_slot_effort_bridge_moves_with_owner_split() -> None:
    source = _minimal_slot_effort_source()
    assert crew10_patch.inspect(SLOT_EFFORT, source).state == "clean"
    patched = crew10_patch.patch_text(SLOT_EFFORT, source)
    assert "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO" in patched
    assert "async def _kiro_byo_cold_selection_capabilities(" in patched
    assert "async def _kiro_byo_type_selection_active(" in patched
    assert "async def _kiro_byo_reconcile_slot_routing(" in patched
    assert "await _kiro_byo_reconcile_slot_routing(state, slot)" in patched
    assert 'if _backend == ACP_BACKEND_KIRO_BYO:' in patched
    assert '_slot.jev_route = False' in patched
    assert "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)" in patched
    assert "suppress Crew backend fallback" in patched
    assert '"code": "effort_managed_by_type"' in patched
    compile(patched, SLOT_EFFORT, "exec")
    assert crew10_patch.inspect(SLOT_EFFORT, patched).state == "patched"
    assert crew10_patch.patch_text(SLOT_EFFORT, patched) == patched


def test_crew10_slot_effort_fails_closed_if_backend_anchor_moves() -> None:
    source = _minimal_slot_effort_source().replace(
        "async def _configured_backend_for_slot(slot: _ChatSlot) -> str:\n    return \"kiro-byo\"\n\n",
        "",
    )
    report = crew10_patch.inspect(SLOT_EFFORT, source)
    assert report.state == "incompatible"
    assert "_configured_backend_for_slot" in report.detail


def _minimal_chat_title_source() -> str:
    return '''from __future__ import annotations
import asyncio
import logging
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.llm_helpers import run_bg_oneliner
logger = logging.getLogger(__name__)
_TITLE_MODEL = "auto"

async def _generate_title_via_kiro(state, prompt, session_key=""):
    return await run_bg_oneliner(
        state.sessions,
        prompt,
        model=_TITLE_MODEL,
        timeout=30.0,
    )

async def _generate_refreshed_title(state, prompt, session_key=""):
    return await run_bg_oneliner(
        state.sessions,
        prompt,
        model=_TITLE_MODEL,
        timeout=30.0,
    )
'''


def test_crew10_chat_title_uses_background_role_only_for_byo() -> None:
    source = _minimal_chat_title_source()
    assert crew10_patch.inspect(CHAT_TITLE, source).state == "clean"
    patched = crew10_patch.patch_text(CHAT_TITLE, source)
    assert "async def _kiro_byo_title_model() -> str:" in patched
    assert 'acp_backend", "") or "") == "kiro-byo"' in patched
    assert patched.count("model=await _kiro_byo_title_model(),") == 2
    compile(patched, CHAT_TITLE, "exec")
    assert crew10_patch.inspect(CHAT_TITLE, patched).state == "patched"
    assert crew10_patch.patch_text(CHAT_TITLE, patched) == patched


def test_crew10_chat_title_fails_closed_if_title_calls_move() -> None:
    source = _minimal_chat_title_source().replace("model=_TITLE_MODEL,", "model='different',", 1)
    report = crew10_patch.inspect(CHAT_TITLE, source)
    assert report.state == "incompatible"
    assert "title one-liner call shape moved" in report.detail


def test_crew10_slot_routing_reconciliation_migrates_only_routing_sentinels(monkeypatch, tmp_path: Path) -> None:
    import asyncio
    import contextlib
    import logging
    import sys
    import types
    from types import SimpleNamespace

    import kiro_byo.config as byo_config
    import kiro_byo.paths as byo_paths

    class Slot:
        def __init__(self, model: str, jev_route: bool, backend: str) -> None:
            self.model = model
            self.jev_route = jev_route
            self.backend = backend
            self._dirty = False

    class State:
        def __init__(self, slots: list[Slot]) -> None:
            self._slots = {str(i): slot for i, slot in enumerate(slots)}
            self.pushes = 0
            self.sessions = SimpleNamespace(get_provider=lambda _key: None)

        def push_slots_update(self) -> None:
            self.pushes += 1

    async def backend_for(slot: Slot) -> str:
        return slot.backend

    monkeypatch.setattr(
        byo_config,
        "load_catalog_config",
        lambda _path: SimpleNamespace(models=[object()], mode_pack="tiga", mode_type="multi"),
    )
    monkeypatch.setattr(byo_paths, "use_app_home", lambda _path: contextlib.nullcontext())

    # The helper imports this module lazily so the test needs only the contract
    # it consumes, not a KiroCrew installation.
    crew = types.ModuleType("kiro_crew")
    apps = types.ModuleType("kiro_crew.apps")
    manager = types.ModuleType("kiro_crew.apps.manager")
    manager.app_data_dir = lambda _name: tmp_path
    monkeypatch.setitem(sys.modules, "kiro_crew", crew)
    monkeypatch.setitem(sys.modules, "kiro_crew.apps", apps)
    monkeypatch.setitem(sys.modules, "kiro_crew.apps.manager", manager)

    ns = {
        "DashboardState": State,
        "_ChatSlot": Slot,
        "ACP_BACKEND_KIRO_BYO": "kiro-byo",
        "asyncio": asyncio,
        "logger": logging.getLogger("test-slot-reconcile"),
        "_configured_backend_for_slot": backend_for,
        "effective_session_key": lambda slot: str(id(slot)),
    }
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER, ns)
    reconcile = ns["_kiro_byo_reconcile_slot_routing"]

    stale_jev = Slot("auto", True, "kiro-byo")
    concrete = Slot("mimo-v2.5-pro", False, "kiro-byo")
    old_byo_on_kiro = Slot("kiro-byo-type:tiga:sky", False, "")
    old_byo_other = Slot("kiro-byo-type:tiga:multi", False, "claude-agent-acp")
    state = State([stale_jev, concrete, old_byo_on_kiro, old_byo_other])

    changed = asyncio.run(reconcile(state))
    assert changed is True
    assert stale_jev.model == "kiro-byo-type:tiga:multi"
    assert stale_jev.jev_route is False
    assert stale_jev._dirty is True
    assert concrete.model == "mimo-v2.5-pro"
    assert concrete.jev_route is False
    assert concrete._dirty is False
    assert old_byo_on_kiro.model == ""
    assert old_byo_other.model == ""
    assert state.pushes == 1

    # Idempotence matters because /api/models and selection-capabilities can
    # both probe this helper during one UI transition.
    assert asyncio.run(reconcile(state)) is False
    assert state.pushes == 1


def test_crew10_warm_byo_mode_switch_retires_idle_session_in_both_directions(monkeypatch, tmp_path):
    """Regression: an idle warm ACP handle must not pin the composer to the old Mode."""
    import asyncio
    import contextlib
    import logging
    import sys
    import types
    from types import SimpleNamespace

    import kiro_byo.config as byo_config
    import kiro_byo.paths as byo_paths

    current = {'pack': 'tiga', 'type': 'multi'}
    monkeypatch.setattr(byo_config, 'load_catalog_config', lambda _: SimpleNamespace(
        models=[object()], mode_pack=current['pack'], mode_type=current['type']))
    monkeypatch.setattr(byo_paths, 'use_app_home', lambda _: contextlib.nullcontext())
    root = types.ModuleType('kiro_crew')
    apps = types.ModuleType('kiro_crew.apps')
    manager = types.ModuleType('kiro_crew.apps.manager')
    manager.app_data_dir = lambda _: tmp_path
    dashboard = types.ModuleType('kiro_crew.dashboard')
    handlers = types.ModuleType('kiro_crew.dashboard.chat_handlers')
    for name, module in {'kiro_crew': root, 'kiro_crew.apps': apps,
                         'kiro_crew.apps.manager': manager, 'kiro_crew.dashboard': dashboard,
                         'kiro_crew.dashboard.chat_handlers': handlers}.items():
        monkeypatch.setitem(sys.modules, name, module)
    dashboard.chat_handlers = handlers

    class Slot:
        def __init__(self, name, model, *, busy=False, children=False, app='', linked=''):
            self.key = name
            self.model = model
            self.jev_route = False
            self.running = busy
            self.children = children
            self._app = app
            self.linked_session_key = linked
            self._dirty = False
            self._lock = asyncio.Lock()
            self._model_pick_lock = asyncio.Lock()
            self._model_pick_gen = 0

    class State:
        def __init__(self, slots):
            self._slots = {s.key: s for s in slots}
            self.providers = {s.key: SimpleNamespace(capabilities=SimpleNamespace(backend='kiro-byo'))
                              for s in slots}
            self.sessions = SimpleNamespace(get_provider=lambda key: self.providers.get(key))
            self.pushes = 0
            self.resets = []
        def push_slots_update(self):
            self.pushes += 1

    lock = asyncio.Lock()
    @contextlib.asynccontextmanager
    async def session_lock(_key):
        async with lock:
            yield

    async def attached(_state, slot, _key, _reason):
        return object() if slot.children else None

    async def reset(state, slot, key, *, switch_kind):
        assert switch_kind == 'model'
        state.resets.append((key, slot.model))
        if getattr(slot, 'decline', False):
            return False
        state.providers.pop(key)
        return True

    handlers._slot_switch_session_lock = session_lock
    handlers._switch_target_busy = lambda _state, slot, _key, _provider: slot.running
    handlers._subagents_attached_response = attached
    handlers._reset_slot_session_or_warn = reset
    handlers._broadcast_context_reset = lambda *_args: None

    async def backend_for(_slot):
        return 'kiro-byo'

    ns = {'DashboardState': State, '_ChatSlot': Slot, 'ACP_BACKEND_KIRO_BYO': 'kiro-byo',
          'asyncio': asyncio, 'logger': logging.getLogger('test-warm-mode'),
          '_configured_backend_for_slot': backend_for, 'effective_session_key': lambda s: s.key}
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER, ns)
    reconcile = ns['_kiro_byo_reconcile_slot_routing']

    warm = Slot('warm', 'kiro-byo-type:dyna:flash')
    busy = Slot('busy', 'kiro-byo-type:dyna:strong', busy=True)
    children = Slot('children', 'kiro-byo-type:dyna:miracle', children=True)
    concrete = Slot('concrete', 'demo/model')
    current_tiga = Slot('tiga', 'kiro-byo-type:tiga:power')
    app = Slot('app', 'kiro-byo-type:dyna:flash', app='other-app')
    declined = Slot('declined', 'kiro-byo-type:dyna:flash')
    declined.decline = True
    state = State([warm, busy, children, concrete, current_tiga, app, declined])

    assert asyncio.run(reconcile(state)) is True
    assert warm.model == 'kiro-byo-type:tiga:multi'
    assert state.resets == [('warm', 'kiro-byo-type:tiga:multi'), ('declined', 'kiro-byo-type:tiga:multi')]
    assert warm._model_pick_gen == 1 and warm._dirty
    assert state.providers.get('warm') is None  # actual session retired, not just UI change
    assert busy.model == 'kiro-byo-type:dyna:strong' and children.model == 'kiro-byo-type:dyna:miracle'
    assert declined.model == 'kiro-byo-type:dyna:flash' and declined._model_pick_gen == 0
    assert app.model == 'kiro-byo-type:dyna:flash'
    assert concrete.model == 'demo/model' and current_tiga.model == 'kiro-byo-type:tiga:power'
    assert state.pushes == 1
    assert asyncio.run(reconcile(state)) is False  # repeated model-list reads are idempotent
    assert state.pushes == 1

    current.update(pack='dyna', type='flash')
    # New warm provider after first Tiga turn.
    state.providers['warm'] = SimpleNamespace(capabilities=SimpleNamespace(backend='kiro-byo'))
    assert asyncio.run(reconcile(state)) is True
    assert warm.model == 'kiro-byo-type:dyna:flash'
    assert warm._model_pick_gen == 2 and state.providers.get('warm') is None
    assert busy.model == 'kiro-byo-type:dyna:strong'  # same-Mode manual Type retained
    assert concrete.model == 'demo/model'

    # A manual concrete-model pin arriving while the reconciler waits on
    # Crew's slot lock must not be overwritten by the older stale snapshot.
    race = Slot('race', 'kiro-byo-type:tiga:multi')
    state._slots[race.key] = race
    state.providers[race.key] = SimpleNamespace(capabilities=SimpleNamespace(backend='kiro-byo'))
    async def pin_while_queued():
        await race._lock.acquire()
        try:
            task = asyncio.create_task(reconcile(state, race))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            race.model = 'manual/model'
        finally:
            race._lock.release()
        return await task
    assert asyncio.run(pin_while_queued()) is False
    assert race.model == 'manual/model'
    assert 'race' in state.providers
    assert all(key != 'race' for key, _target in state.resets)


def test_crew10_warm_mode_switch_fail_closed_when_host_contract_unavailable(monkeypatch, tmp_path):
    """Do not claim Tiga while an unresettable Dyna ACP session is still live."""
    import asyncio
    import contextlib
    import logging
    import sys
    import types
    from types import SimpleNamespace
    import kiro_byo.config as byo_config
    import kiro_byo.paths as byo_paths

    monkeypatch.setattr(byo_config, 'load_catalog_config', lambda _: SimpleNamespace(
        models=[object()], mode_pack='tiga', mode_type='multi'))
    monkeypatch.setattr(byo_paths, 'use_app_home', lambda _: contextlib.nullcontext())
    for name in ('kiro_crew', 'kiro_crew.apps', 'kiro_crew.dashboard'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    m = types.ModuleType('kiro_crew.apps.manager')
    m.app_data_dir = lambda _: tmp_path
    monkeypatch.setitem(sys.modules, 'kiro_crew.apps.manager', m)
    h = types.ModuleType('kiro_crew.dashboard.chat_handlers')
    monkeypatch.setitem(sys.modules, 'kiro_crew.dashboard.chat_handlers', h)
    class Slot:
        key = 'a'
        model = 'kiro-byo-type:dyna:flash'
        jev_route = False
        _dirty = False
        _lock = asyncio.Lock()
        _model_pick_lock = asyncio.Lock()
        _model_pick_gen = 0
    slot = Slot()
    live = SimpleNamespace(capabilities=SimpleNamespace(backend='kiro-byo'))
    class State:
        _slots = {'a': slot}
        sessions = SimpleNamespace(get_provider=lambda _: live)
        pushes = 0
        def push_slots_update(self):
            self.pushes += 1
    async def backend_for(_slot):
        return 'kiro-byo'
    ns = {'DashboardState': State, '_ChatSlot': Slot, 'ACP_BACKEND_KIRO_BYO': 'kiro-byo',
          'asyncio': asyncio, 'logger': logging.getLogger('test-fail-closed'),
          '_configured_backend_for_slot': backend_for, 'effective_session_key': lambda s: s.key}
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER, ns)
    state = State()
    assert asyncio.run(ns['_kiro_byo_reconcile_slot_routing'](state)) is False
    assert (slot.model, slot._dirty, state.pushes) == ('kiro-byo-type:dyna:flash', False, 0)
