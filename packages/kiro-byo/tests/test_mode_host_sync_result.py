"""The mode-save response must distinguish committed configuration from host state."""
from __future__ import annotations

import ast
import asyncio
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def test_pending_old_mode_slots_are_reported_not_hidden(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "backend/routes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_reconcile_mode_change_with_crew")
    namespace = {"logging": logging}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), namespace)
    function = namespace[fn.name]
    handlers = types.ModuleType("kiro_crew.dashboard.chat_handlers")
    dashboard = types.ModuleType("kiro_crew.dashboard")
    slot_effort = types.ModuleType("kiro_crew.dashboard.chat_api.slot_effort")
    chat_api = types.ModuleType("kiro_crew.dashboard.chat_api")
    chat_api.slot_effort = slot_effort
    dashboard.chat_api = chat_api
    monkeypatch.setitem(sys.modules, "kiro_crew", types.ModuleType("kiro_crew"))
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard", dashboard)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.chat_api", chat_api)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.chat_api.slot_effort", slot_effort)
    old = SimpleNamespace(model="kiro-byo-type:dyna:flash")
    direct = SimpleNamespace(model="DeepSeek-V4.1-Flash")
    current = SimpleNamespace(model="kiro-byo-type:tiga:multi")
    state = SimpleNamespace(_slots={"old": old, "direct": direct, "new": current})
    request = SimpleNamespace(app={"state": state})

    async def reconcile(_state):
        return False
    slot_effort._kiro_byo_reconcile_slot_routing = reconcile
    assert asyncio.run(function(request, "tiga")) == ("pending", 1)
    old.model = "kiro-byo-type:tiga:multi"
    assert asyncio.run(function(request, "tiga")) == ("unchanged", 0)

    async def changed(_state):
        return True
    slot_effort._kiro_byo_reconcile_slot_routing = changed
    assert asyncio.run(function(request, "tiga")) == ("updated", 0)
    assert asyncio.run(function(SimpleNamespace(app={}), "tiga")) == ("host_unavailable", 0)
