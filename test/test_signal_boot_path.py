"""Signal's modules stay off the gateway boot path while the channel is off.

``channels.py`` and the dashboard handlers package are imported at gateway
start, and the channel is opt-in, so importing them must not load the channel's
runtime (``no-new-work-on-gateway-boot-path``, clause 5). The runtime loads when
the registry starts the channel, which it does only while the channel is
enabled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew

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

    seen: list[Any] = []
    handle = object()

    async def _fake_start(orch: Any) -> Any:
        seen.append(orch)
        return handle

    monkeypatch.setattr(signal_gateway, "maybe_start_signal", _fake_start)
    (desc,) = [d for d in builtin_channel_descriptors() if d.channel_type == "signal"]
    assert desc.start is not None
    orch = SimpleNamespace()
    assert await desc.start(orch) is handle
    assert seen == [orch]
