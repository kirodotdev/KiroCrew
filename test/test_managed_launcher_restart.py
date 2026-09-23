"""Managed restarts re-enter the stable launcher, not a retired Python bundle."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from installer_test_helpers import run_bounded

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import updates
from kiro_crew.gateway_restart import resolve_restart_launcher
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import PlatformCompositionError, set_context
from kiro_crew.slack.gateway import GatewayOrchestrator

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _compose(monkeypatch, launcher):
    provider = SimpleNamespace(restart_launcher=Mock(return_value=launcher))
    set_context(replace(build_default_context(KiroCrewConfig()), gateway_lifecycle=provider))
    return provider


def _state():
    return SimpleNamespace(
        _gateway_restart_in_progress=False,
        push_update_progress=Mock(),
        sessions=SimpleNamespace(close_all=AsyncMock()),
    )


class TestLauncherContract:
    def test_default_preserves_python_resolution(self):
        ctx = build_default_context(KiroCrewConfig())
        set_context(ctx)
        assert ctx.gateway_lifecycle.restart_launcher() is None
        assert resolve_restart_launcher() is None

    @pytest.mark.parametrize("target", ["", "relative-launcher", 123, "bad\0path"])
    def test_malformed_target_is_not_absence(self, monkeypatch, target):
        _compose(monkeypatch, target)
        with pytest.raises(ValueError, match="Cannot restart"):
            resolve_restart_launcher()

    @pytest.mark.parametrize("kind", ["missing", "directory", "non-executable"])
    @pytest.mark.asyncio
    async def test_bad_target_rejected_before_drain(self, monkeypatch, tmp_path, kind):
        launcher = tmp_path / "launcher.exe"
        if kind == "directory":
            launcher.mkdir()
        elif kind == "non-executable":
            launcher.write_text("not executable", encoding="utf-8")
            # Windows permissions do not express a POSIX executable bit.
            monkeypatch.setattr(os, "access", lambda *_: False)
        _compose(monkeypatch, str(launcher))
        state = _state()
        fallback = Mock(side_effect=AssertionError("must not fall back"))
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        assert await updates._restart_gateway(state, resolver=fallback) is False
        state.sessions.close_all.assert_not_awaited()
        fallback.assert_not_called()
        execv.assert_not_called()
        assert not state._gateway_restart_in_progress
        assert state.push_update_progress.call_args.args[0] == "error"

    @pytest.mark.parametrize(
        "error", [RuntimeError("provider failed"), PlatformCompositionError("bad composition")]
    )
    @pytest.mark.asyncio
    async def test_provider_failure_never_drains(self, monkeypatch, error):
        provider = _compose(monkeypatch, None)
        provider.restart_launcher.side_effect = error
        state = _state()
        with pytest.raises(type(error)):
            await updates._restart_gateway(state, resolver=Mock())
        state.sessions.close_all.assert_not_awaited()
        assert not state._gateway_restart_in_progress

    @pytest.mark.asyncio
    async def test_auto_restart_rejects_before_callback_fence(self, monkeypatch, tmp_path):
        _compose(monkeypatch, str(tmp_path / "missing"))
        orch = SimpleNamespace(
            _pending_update_respawn=None,
            dashboard_state=None,
            sessions=SimpleNamespace(close_all=AsyncMock(), fence_update_restart=Mock()),
            _drain_update_callback_work=AsyncMock(),
        )
        with pytest.raises(ValueError, match="Cannot restart"):
            await GatewayOrchestrator._restart_after_update(orch, Mock())
        orch._drain_update_callback_work.assert_not_awaited()
        orch.sessions.fence_update_restart.assert_not_called()
        orch.sessions.close_all.assert_not_awaited()

    @pytest.mark.parametrize("windows", [False, True])
    def test_exec_preserves_argv_environment_and_utf8(self, monkeypatch, tmp_path, windows):
        launcher = str(tmp_path / "space dir" / "launcher.exe")
        args = ["gateway", "--port", "6789", "space argument", 'quote"value', "", "雪"]
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        monkeypatch.setenv("PYTHONPATH", "stale-import-tree")
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance home"))
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "ascii")
        execv = Mock()
        monkeypatch.setattr(os, "execv", execv)
        platform_compat.reexec_launcher(launcher, args)
        expected = [launcher, *args]
        if windows:
            expected = [subprocess.list2cmdline([arg]) for arg in expected]
        execv.assert_called_once_with(launcher, expected)
        assert os.environ["PYTHONPATH"] == "stale-import-tree"
        assert os.environ["KIROCREW_HOME"] == str(tmp_path / "instance home")
        assert os.environ["PYTHONUTF8"] == "1"
        assert os.environ["PYTHONIOENCODING"] == "utf-8:backslashreplace"

    def test_windows_requires_native_launcher(self, monkeypatch, tmp_path):
        launcher = tmp_path / "launcher.cmd"
        launcher.write_text("@exit /b 0", encoding="utf-8")
        platform_compat.chmod_safe(launcher, 0o700)
        _compose(monkeypatch, str(launcher))
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(os, "access", lambda *_: True)
        with pytest.raises(ValueError, match="native Windows"):
            resolve_restart_launcher()


# The fixture runs only probes, never a gateway listener, update installer or LLM.
# Both synthetic bundle interpreters exec the same host Python through different
# paths; their import trees and the basename-dispatched stable launcher are real.
_PROBE = """
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core_probe
import companion_probe

root = Path(__file__).parent
print("PROBE=" + json.dumps({
    "core": core_probe.VERSION, "companion": companion_probe.VERSION,
    "core_path": core_probe.__file__, "companion_path": companion_probe.__file__,
    "executable": sys.executable, "args": sys.argv[1:],
    "home": os.environ["KIROCREW_HOME"], "port": os.environ["KIROCREW_PORT"],
    "pythonpath": os.environ["PYTHONPATH"],
    "utf8": os.environ.get("PYTHONUTF8"),
    "encoding": os.environ.get("PYTHONIOENCODING"),
    "dont_write_bytecode": sys.dont_write_bytecode,
}), flush=True)
if os.environ.pop("PROBE_FIRST", ""):
    sys.path.extend(DEPENDENCY_PATHS)
    from dataclasses import replace
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.context import set_context
    from kiro_crew.dashboard.handlers import updates
    from kiro_crew.dashboard import chat
    from kiro_crew.platform import update_provider
    from kiro_crew.slack import gateway

    launcher = str(root / "crew-launcher")
    set_context(replace(build_default_context(KiroCrewConfig()),
        gateway_lifecycle=SimpleNamespace(restart_launcher=lambda: launcher)))
    state = SimpleNamespace(_gateway_restart_in_progress=False,
        push_update_progress=lambda *a: None,
        sessions=SimpleNamespace(close_all=AsyncMock()))
    chat.save_all_slots_to_history = lambda state: None
    updates.flush_breadcrumb_writes = lambda *a: None
    gateway.flush_breadcrumb_writes = lambda *a: None

    async def apply():
        (root / "current").write_text("B\\n", encoding="utf-8")
        if REMOVE_A:
            shutil.rmtree(root / "A")
        # Do not repair this env in the core. Only the stable launcher can
        # know which tree and companion belong to its newly selected version.
        assert os.environ["PYTHONPATH"] == str(root / "A" / "imports")
        return True

    async def run():
        if MODE == "dashboard-update":
            update_provider.apply_policy_update = apply
            await updates.api_update_apply(SimpleNamespace(app={"state": state}))
        elif MODE == "automatic-update":
            await apply()
            orch = SimpleNamespace(_pending_update_respawn=None, dashboard_state=None,
                sessions=None, _UPDATE_DRAIN_TIMEOUT_SECS=1,
                _drain_update_callback_work=AsyncMock(return_value=True))
            await gateway.GatewayOrchestrator._restart_after_update(orch, lambda: sys.executable)
        else:
            await apply()
            await updates._restart_gateway(state, resolver=lambda: sys.executable)
        raise AssertionError("restart returned without replacing the probe")
    asyncio.run(run())
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX basename-dispatched shell launcher fixture")
@pytest.mark.parametrize("mode", ["manual", "dashboard-update", "automatic-update"])
@pytest.mark.parametrize("remove_a", [False, True], ids=["A-retained", "A-removed"])
def test_managed_bundle_switch(tmp_path, mode, remove_a):
    root = tmp_path / "managed install with spaces"
    root.mkdir()
    for version in ("A", "B"):
        bundle = root / version
        imports = bundle / "imports"
        imports.mkdir(parents=True)
        for name in ("core_probe", "companion_probe"):
            (imports / f"{name}.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
        package = imports / "kiro_crew"
        package.mkdir()
        # Execute real restart code through A's removable import path. The
        # synthetic module entry point only reports a probe even without the fix.
        for entry in (_SOURCE_ROOT / "kiro_crew").iterdir():
            if entry.name not in {"__main__.py", "__pycache__"}:
                (package / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        (package / "__main__.py").write_text(
            f"import runpy\nrunpy.run_path({str(root / 'probe.py')!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        (bundle / "python").symlink_to(sys.executable)
    (root / "current").write_text("A\n", encoding="utf-8")
    dispatcher = root / "dispatcher"
    dispatcher.write_text(
        "#!/bin/sh\n"
        'case "${0##*/}" in crew-launcher) ;; *) exit 93 ;; esac\n'
        f"root={shlex.quote(str(root))}\n"
        'IFS= read -r version < "$root/current"\n'
        'export PYTHONPATH="$root/$version/imports"\n'
        'exec "$root/$version/python" "$root/probe.py" "$@"\n',
        encoding="utf-8",
    )
    platform_compat.chmod_safe(dispatcher, 0o700)
    launcher = root / "crew-launcher"
    launcher.symlink_to(dispatcher)
    (root / "probe.py").write_text(
        f"DEPENDENCY_PATHS = {[p for p in sys.path if p.endswith(('site-packages', 'dist-packages'))]!r}\n"
        f"MODE = {mode!r}\nREMOVE_A = {remove_a!r}\n" + textwrap.dedent(_PROBE),
        encoding="utf-8",
    )
    home = root / "instance home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "KIRO_HOME": str(home / ".kiro"),
        "KIROCREW_HOME": str(home),
        "KIROCREW_PORT": "6789",
        "KIROCREW_PROFILE": "standalone",
        "KIROCREW_TELEMETRY": "0",
        "TMPDIR": str(root),
        "TMP": str(root),
        "TEMP": str(root),
        "PROBE_FIRST": "1",
        # Package directory symlinks lead into the real checkout.
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "ascii",
    }
    args = ["gateway", "--port", "6789", "--no-open", "space argument", "雪"]
    result = run_bounded([str(launcher), *args], env=env, cwd=str(root), timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    records = [
        json.loads(line[6:]) for line in result.stdout.splitlines() if line.startswith("PROBE=")
    ]
    assert len(records) == 2, result.stdout
    before, after = records
    assert before["dont_write_bytecode"] is after["dont_write_bytecode"] is True
    assert before["core"] == before["companion"] == "A"
    assert before["executable"] == str(root / "A" / "python")
    assert after["core"] == after["companion"] == "B"
    assert after["executable"] == str(root / "B" / "python")
    for name in ("core", "companion"):
        assert Path(after[f"{name}_path"]) == root / "B" / "imports" / f"{name}_probe.py"
    assert before["args"] == after["args"] == args
    assert before["home"] == after["home"] == str(home)
    assert before["port"] == after["port"] == "6789"
    assert after["pythonpath"] == str(root / "B" / "imports")
    assert after["utf8"] == "1"
    assert after["encoding"] == "utf-8:backslashreplace"
    assert (root / "A").exists() is not remove_a
