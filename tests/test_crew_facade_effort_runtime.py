"""Prevent BYO effort endpoint HTTP-500 regressions after Crew 0.12 facade compose.

The upstream facade rebinds slot_effort owner functions to chat_handlers' globals.
Imports and helpers defined only on the owner module disappear from that global
namespace. Tests exercise the rebound *real generated endpoint code* rather
than assuming direct owner imports prove runtime behavior.
"""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
import sys
from types import FunctionType, ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from kiro_byo.crew_slot_patch import _patch_slot_effort


def _real_crew_slot_effort() -> Path:
    root = os.environ.get("KIROCREW_012_SOURCE_DIR", "")
    if not root:
        pytest.skip("set KIROCREW_012_SOURCE_DIR to validate actual Crew 0.12 owner")
    file = Path(root) / "src/kiro_crew/dashboard/chat_api/slot_effort.py"
    if not file.is_file():
        pytest.skip("Crew 0.12 slot_effort.py not available at supplied root")
    return file


def _extracted_func(text: str, name: str) -> str:
    tree = ast.parse(text)
    fn = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == name)
    return ast.unparse(fn)


def _local_imports(text: str, name: str) -> set[str]:
    tree = ast.parse(text)
    fn = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == name)
    imports: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            imports.update(alias.name for alias in node.names)
    return imports


def test_real_crew_owner_composition_binding_is_stable() -> None:
    source = _real_crew_slot_effort().read_text(encoding="utf-8")
    transformed = _patch_slot_effort(source)
    assert transformed == _patch_slot_effort(transformed)
    compile(transformed, "slot_effort.py", "exec")
    assert {
        "ACP_BACKEND_KIRO_BYO", "_kiro_byo_cold_selection_capabilities",
        "_kiro_byo_type_selection_active", "_kiro_byo_reconcile_slot_routing",
    } <= _local_imports(transformed, "api_chat_slot_selection_capabilities")
    assert {
        "ACP_BACKEND_KIRO_BYO", "_kiro_byo_type_selection_active",
    } <= _local_imports(transformed, "api_chat_slot_reasoning_effort")
    # The helpers themselves are rebound too. All backend constants they use
    # must be looked up locally, not only in slot_effort's module globals.
    for name in ("_kiro_byo_cold_selection_capabilities", "_kiro_byo_reconcile_slot_routing"):
        assert "ACP_BACKEND_KIRO_BYO" in _local_imports(transformed, name)


def test_generated_real_endpoint_survives_facade_rebind() -> None:
    source = _real_crew_slot_effort().read_text(encoding="utf-8")
    transformed = _patch_slot_effort(source)
    owner = ModuleType("kiro_crew.dashboard.chat_api.slot_effort")
    backend_mod = ModuleType("kiro_crew.agent_sdk.backends")
    backend_mod.ACP_BACKEND_KIRO_BYO = "kiro-byo"
    # Compile the actual generated endpoint, not a manually simplified copy.
    exec("from __future__ import annotations\n" + _extracted_func(transformed, "api_chat_slot_selection_capabilities"), owner.__dict__)
    calls: list[str] = []

    async def reconcile(_state, _slot):
        calls.append("reconcile")
        return False

    async def cold(_slot):
        calls.append("cold")
        return {"known": True, "backend": "kiro-byo", "effort_supported": True,
                "effort_levels": ["low", "high", "max"]}

    async def configured(_slot):
        return "kiro-byo"

    owner._kiro_byo_reconcile_slot_routing = reconcile
    owner._kiro_byo_cold_selection_capabilities = cold
    owner._kiro_byo_type_selection_active = lambda slot: False
    # The facade deliberately does NOT have any of the BYO-specific symbols.
    # This is the exact regression from the Crew owner→facade rebind contract.
    facade_globals = {
        "__name__": "kiro_crew.dashboard.chat_handlers",
        "AcpProvider": type("AcpProvider", (), {}),
        "deny_app_slot_access": lambda *_args: None,
        "_configured_backend_for_slot": configured,
        "effective_session_key": lambda slot: slot.key,
        "web": SimpleNamespace(json_response=lambda result, **_kwargs: result),
    }
    rebound = FunctionType(owner.api_chat_slot_selection_capabilities.__code__, facade_globals)
    slot = SimpleNamespace(key="slot-1", model="DeepSeek-V4-Pro")
    request = SimpleNamespace(app={"state": SimpleNamespace(
        _slots={"slot-1": slot},
        sessions=SimpleNamespace(get_provider=lambda _key: None),
    )}, match_info={"slot": "slot-1"}, get=lambda _key, default="": default)
    fake_modules = {
        "kiro_crew": ModuleType("kiro_crew"),
        "kiro_crew.agent_sdk": ModuleType("kiro_crew.agent_sdk"),
        "kiro_crew.agent_sdk.backends": backend_mod,
        "kiro_crew.dashboard": ModuleType("kiro_crew.dashboard"),
        "kiro_crew.crewmate_harness": ModuleType("kiro_crew.crewmate_harness"),
        "kiro_crew.dashboard.chat_api": ModuleType("kiro_crew.dashboard.chat_api"),
        "kiro_crew.dashboard.chat_api.slot_effort": owner,
    }
    fake_modules["kiro_crew.crewmate_harness"].CrewmateHarnessUnavailable = type(
        "CrewmateHarnessUnavailable", (Exception,), {}
    )
    with patch.dict(sys.modules, fake_modules):
        result = asyncio.run(rebound(request))
    assert result == {"known": True, "backend": "kiro-byo", "effort_supported": True,
                      "effort_levels": ["low", "high", "max"]}
    assert calls == ["reconcile", "cold"]


def test_upgraded_old_owner_gets_missing_local_bindings() -> None:
    source = _real_crew_slot_effort().read_text(encoding="utf-8")
    transformed = _patch_slot_effort(source)
    # Simulate the old installed file, where the only symbols were in the
    # owner's globals. The new transform must migrate it without touching
    # upstream handler logic or accumulating duplicate imports.
    old_lines = transformed.splitlines(keepends=True)
    start = [i for i, line in enumerate(old_lines) if "# BYO: local bindings survive Crew facade composition." in line]
    assert len(start) == 2
    for i in reversed(start):
        end = next(j for j in range(i, len(old_lines)) if old_lines[j].strip() == ")")
        del old_lines[i:end+1]
    legacy = "".join(old_lines)
    ast.parse(legacy)
    migrated = _patch_slot_effort(legacy)
    assert migrated == transformed


def test_verified_old_034_effort_source_is_upgradeable_without_other_changes() -> None:
    from kiro_byo import crew12_patch

    relative = "src/kiro_crew/dashboard/chat_api/slot_effort.py"
    source = _real_crew_slot_effort().read_text(encoding="utf-8")
    current = _patch_slot_effort(source)
    # Reconstruct prior 0.3.4 output (six new imports are missing).
    lines = current.splitlines(keepends=True)
    for i in reversed([i for i, line in enumerate(lines)
                       if "# BYO: local bindings survive Crew facade composition." in line]):
        end = next(j for j in range(i, len(lines)) if lines[j].strip() == ")")
        del lines[i:end + 1]
    prior = "".join(lines)
    for helper in ("_kiro_byo_cold_selection_capabilities", "_kiro_byo_reconcile_slot_routing"):
        head, body = prior.split(f"async def {helper}(", 1)
        body = body.replace(
            "    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n", "", 1
        )
        prior = head + f"async def {helper}(" + body
    # This is a migration of old BYO-owned code, NOT an invitation to modify
    # unknown source. The inspector proves only those six local bindings differ.
    assert crew12_patch.inspect(relative, prior).state == "upgradeable"
    assert crew12_patch.patch_text(relative, prior) == current
    assert crew12_patch.inspect(relative, current).state == "patched"
