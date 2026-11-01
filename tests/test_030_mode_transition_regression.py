"""Regression: cold chat Type state must track the active BYO Mode safely."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from kiro_byo import crew10_patch, config, paths


def _reconcile_env(monkeypatch, tmp_path: Path):
    configured = SimpleNamespace(models=[object()], mode_pack="dyna", mode_type="flash")
    monkeypatch.setattr(config, "load_catalog_config", lambda _path: configured)
    monkeypatch.setattr(paths, "use_app_home", lambda _path: contextlib.nullcontext())
    for name in ("kiro_crew", "kiro_crew.apps", "kiro_crew.apps.manager"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["kiro_crew.apps.manager"].app_data_dir = lambda _name: tmp_path

    class Slot:
        def __init__(self, model: str, backend: str = "kiro-byo", live: bool = False):
            self.model, self.backend = model, backend
            self.jev_route = False
            self._dirty = False
            self.live = live

    class State:
        def __init__(self, slots):
            self._slots = {str(n): item for n, item in enumerate(slots)}
            self.sessions = SimpleNamespace(get_provider=lambda slot: (
                SimpleNamespace(capabilities=SimpleNamespace(backend=slot.backend)) if slot.live else None
            ))
            self.broadcasts = 0

        def push_slots_update(self):
            self.broadcasts += 1

    ns = {
        "DashboardState": State, "_ChatSlot": Slot,
        "ACP_BACKEND_KIRO_BYO": "kiro-byo", "asyncio": asyncio,
        "logger": logging.getLogger("mode-transition-test"),
        "_configured_backend_for_slot": lambda slot: asyncio.sleep(0, result=slot.backend),
        "effective_session_key": lambda slot: slot,
    }
    exec(crew10_patch._KIRO_BYO_SLOT_RECONCILE_HELPER, ns)
    return configured, Slot, State, ns["_kiro_byo_reconcile_slot_routing"]


def test_dyna_tiga_and_back_only_migrates_cold_cross_mode_types(monkeypatch, tmp_path):
    cfg, Slot, State, reconcile = _reconcile_env(monkeypatch, tmp_path)
    stale = Slot("kiro-byo-type:tiga:power")
    same_mode_manual = Slot("kiro-byo-type:dyna:strong")
    concrete = Slot("provider/specific-model")
    active = Slot("kiro-byo-type:tiga:multi", live=True)
    official = Slot("kiro-byo-type:tiga:sky", backend="")
    state = State([stale, same_mode_manual, concrete, active, official])
    assert asyncio.run(reconcile(state)) is True
    assert stale.model == "kiro-byo-type:dyna:flash"
    assert same_mode_manual.model == "kiro-byo-type:dyna:strong"
    assert concrete.model == "provider/specific-model"
    assert active.model == "kiro-byo-type:tiga:multi"
    assert official.model == ""
    assert not same_mode_manual._dirty and not concrete._dirty and not active._dirty
    assert state.broadcasts == 1
    assert asyncio.run(reconcile(state)) is False
    assert state.broadcasts == 1

    cfg.mode_pack, cfg.mode_type = "tiga", "multi"
    assert asyncio.run(reconcile(state)) is True
    assert stale.model == "kiro-byo-type:tiga:multi"
    assert same_mode_manual.model == "kiro-byo-type:tiga:multi"
    assert active.model == "kiro-byo-type:tiga:multi"
    assert concrete.model == "provider/specific-model"
    assert state.broadcasts == 2


def test_absent_catalogue_cannot_reset_existing_user_type(monkeypatch, tmp_path):
    cfg, Slot, State, reconcile = _reconcile_env(monkeypatch, tmp_path)
    cfg.models = []
    state = State([Slot("kiro-byo-type:tiga:glitter")])
    assert asyncio.run(reconcile(state)) is False
    assert state._slots["0"].model == "kiro-byo-type:tiga:glitter"
    assert state.broadcasts == 0


def test_crew11_mode_notification_revalidates_without_rewriting_react_state(tmp_path):
    """Exercise actual injected browser script's cross-surface message handler."""
    import shutil
    import subprocess
    from kiro_byo.crew11_model_labels import _script
    if not shutil.which("node"):
        import pytest
        pytest.skip("Node.js not installed")
    script = _script({"kiro-byo-type:dyna:flash": {"short": "Flash Type", "full": "Dyna · Flash Type"}})
    script = script.split("<script", 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    fixture = r'''
const vm = require('node:vm');
const fs = require('node:fs');
const calls = { fetches:0, focus:0, visibility:0 };
const window = {
  addEventListener: (name, fn) => { if (name === 'kiro-byo:catalog-changed') window.onCatalog = fn; },
  dispatchEvent: (ev) => { if (ev.type === 'focus') calls.focus++; },
  requestAnimationFrame: (_cb) => 1,
  setInterval: (_fn, _ms) => 1, clearInterval: ()=>{},
  location: { reload: ()=>{throw Error('unexpected reload')} }
};
const document = {
  body: null, visibilityState: 'visible', addEventListener: ()=>{},
  dispatchEvent: (ev) => { if (ev.type === 'visibilitychange') calls.visibility++; },
  querySelectorAll: ()=>[]
};
class BroadcastChannel {
  constructor(name){ if (name !== 'kiro-byo:mode-catalog') throw Error('channel mismatch'); BroadcastChannel.last = this; }
}
const context = { window, document, BroadcastChannel,
  Event: class { constructor(type){ this.type=type; } },
  fetch: (path) => { calls.fetches++; return Promise.resolve({ok:true,json:()=>Promise.resolve(
    path === '/api/models' ? [{model_name:'kiro-byo-type:dyna:flash'}] : []
  )}); }
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
if (!BroadcastChannel.last || !window.onCatalog) throw Error('host listeners missing');
BroadcastChannel.last.onmessage({data:{kind:'catalog-changed',mode:'dyna'}});
if (calls.focus<1 || calls.visibility<1 || calls.fetches<1) throw Error('refresh not signalled: '+JSON.stringify(calls));
console.log('mode cross-surface refresh signal validated');
'''
    js = tmp_path / "injected.js"
    js.write_text(script, encoding="utf-8")
    harness = tmp_path / "harness.cjs"
    harness.write_text(fixture, encoding="utf-8")
    done = subprocess.run(["node", str(harness), str(js)], capture_output=True, text=True, timeout=8)
    assert done.returncode == 0, done.stdout + done.stderr


def test_mode_app_produces_refresh_signal():
    source = (Path(__file__).resolve().parents[1] / 'ui/index.mjs').read_text(encoding='utf-8')
    assert "notifyCrewModeChanged(result.mode?.pack||value)" in source
    assert "new BroadcastChannel('kiro-byo:mode-catalog')" in source
