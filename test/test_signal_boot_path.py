"""Signal's modules stay off the gateway boot path while the channel is off.

``channels.py`` and the dashboard handlers package are imported at gateway
start, and the channel is opt-in, so importing them must not load the channel's
runtime (``no-new-work-on-gateway-boot-path``, clause 5). The runtime loads when
the registry starts the channel, which it does only while the channel is
enabled.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

import kiro_crew
from kiro_crew import channels
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.slack.gateway import GatewayOrchestrator

_SRC = str(Path(kiro_crew.__file__).resolve().parents[1])

#: What may load at import time: the package (a docstring) and the
#: dependency-free identity helpers config loading reads.
_ALLOWED_AT_IMPORT = ["kiro_crew.signal_cli", "kiro_crew.signal_cli.identifiers"]


def _signal_modules_after(import_line: str, home: Path) -> list[str]:
    """``kiro_crew.signal_cli*`` modules a fresh interpreter holds after *import_line*.

    A fresh process, because pytest has already imported most of the package, so
    ``sys.modules`` in-process says nothing about what one import pulls.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (_SRC, env.get("PYTHONPATH"))))
    env["KIROCREW_HOME"] = str(home)
    env.pop("COV_CORE_SOURCE", None)
    env.pop("COVERAGE_PROCESS_START", None)
    snippet = (
        "import json, sys\n"
        f"{import_line}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('kiro_crew.signal_cli'))))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_the_boot_modules_loads_no_signal_runtime(tmp_path: Path) -> None:
    """One interpreter, every boot-path importer: what loads only ever grows, so the
    set after the last import bounds the set after each one."""
    loaded = _signal_modules_after(
        "import kiro_crew.config.loader\n"
        "import kiro_crew.channels\n"
        "import kiro_crew.dashboard.handlers\n"
        "import kiro_crew.dashboard.handlers.signal_settings\n"
        "import kiro_crew.dashboard.server\n"
        "import kiro_crew.slack.gateway",
        tmp_path,
    )
    assert loaded == _ALLOWED_AT_IMPORT


@pytest.mark.asyncio
async def test_the_registry_start_imports_the_channel_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.channels import builtin_channel_descriptors
    from kiro_crew.signal_cli import gateway as signal_gateway

    events: list[Any] = []
    handle = object()

    async def _fake_start(orch: Any) -> Any:
        events.append(("start", orch))
        return handle

    monkeypatch.setattr(signal_gateway, "maybe_start_signal", _fake_start)
    (desc,) = [d for d in builtin_channel_descriptors() if d.channel_type == "signal"]
    assert desc.start is not None
    orch = SimpleNamespace(
        _adopt_channel_section_from_watcher=lambda d, creds: events.append(
            ("adopt", d.channel_type, creds)
        )
    )
    assert await desc.start(orch) is handle
    assert events == [("adopt", "signal", {}), ("start", orch)]


# -- A write landing while the channel's modules import ----------------------
#
# The import is awaited on a worker thread, after the registry's before_start
# adoption and before the dispatcher subscribes to the watcher, so the factory
# adopts the watcher's snapshot again once it returns.

_OPERATOR = "+15555550100"
_REVOKED = "+15555550101"


def _cfg(*, enabled: bool = True, allowed: list[str]) -> KiroCrewConfig:
    base = KiroCrewConfig()
    section = replace(base.signal, enabled=enabled, account=_OPERATOR, allowed_numbers=allowed)
    return replace(base, signal=section)


def _orch(cfg: KiroCrewConfig) -> Any:
    """The two gateway methods the adoption runs, bound to a minimal host."""
    orch = SimpleNamespace(_cfg=cfg, _signal_enabled=bool(cfg.signal.enabled))
    orch._adopt_channel_section_from_watcher = MethodType(
        GatewayOrchestrator._adopt_channel_section_from_watcher, orch
    )
    orch._hoist_signal = MethodType(GatewayOrchestrator._hoist_signal, orch)
    return orch


async def _start_with_a_write_during_the_import(
    monkeypatch: pytest.MonkeyPatch, boot: KiroCrewConfig, written: KiroCrewConfig
) -> tuple[list[str], bool]:
    """Run Signal's start factory while *written* lands mid-import.

    Returns the allow-list and enablement ``maybe_start_signal`` was handed.
    """
    from kiro_crew.signal_cli import gateway as signal_gateway

    current: dict[str, KiroCrewConfig] = {}
    seen: list[tuple[list[str], bool]] = []

    def _import(name: str) -> Any:
        current["snapshot"] = written
        return importlib.import_module(name)

    async def _fake_start(orch: Any) -> None:
        seen.append((list(orch._cfg.signal.allowed_numbers), orch._signal_enabled))

    monkeypatch.setattr(channels, "importlib", SimpleNamespace(import_module=_import))
    monkeypatch.setattr(live, "snapshot", lambda: current.get("snapshot"))
    monkeypatch.setattr(signal_gateway, "maybe_start_signal", _fake_start)
    await channels._start_signal(_orch(boot))
    (result,) = seen
    return result


@pytest.mark.asyncio
async def test_a_revocation_during_the_import_reaches_the_first_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed, enabled = await _start_with_a_write_during_the_import(
        monkeypatch, _cfg(allowed=[_OPERATOR, _REVOKED]), _cfg(allowed=[_OPERATOR])
    )
    assert (allowed, enabled) == ([_OPERATOR], True)


@pytest.mark.asyncio
async def test_a_disable_during_the_import_keeps_the_channel_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, enabled = await _start_with_a_write_during_the_import(
        monkeypatch, _cfg(allowed=[_OPERATOR]), _cfg(enabled=False, allowed=[_OPERATOR])
    )
    assert enabled is False


@pytest.mark.asyncio
async def test_a_degraded_section_during_the_import_keeps_the_boot_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torn = replace(_cfg(allowed=[]), _degraded_sections=frozenset({"signal"}))
    allowed, _ = await _start_with_a_write_during_the_import(
        monkeypatch, _cfg(allowed=[_OPERATOR, _REVOKED]), torn
    )
    assert allowed == [_OPERATOR, _REVOKED]
