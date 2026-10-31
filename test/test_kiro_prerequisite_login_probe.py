"""The enterprise sign-in check runs through the first-party carve-out on a no-backend host.

Owner decision (enterprise smoke test P9): the sandbox must never block the
sign-in check, so an enterprise build boots, and its login gate stays open, on
a host with no user namespaces. The two fixed probes (``kiro-cli --version`` and
``whoami``) re-run through ``wrap_argv``'s ONE first-party carve-out: only a
``no_backend`` host with no governance floor runs them unconfined, and every
other refusal still fails closed.

These tests spawn a real stand-in ``kiro-cli`` through the real
``_run_process`` and the real ``wrap_argv``, with the host pinned to "no sandbox
backend" the way ``test_sandbox_first_party_exec.py`` pins it.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import enterprise_boot
from kiro_crew.enterprise_gate import EnterpriseGate, LoginWatch
from kiro_crew.kiro_prerequisite_process import _host_can_reap, _run_process
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows skips the sandbox wrap entirely"
)

_SIGNED_IN = 'echo "Logged in as tester"'


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture
def sel_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """SEL events the sandbox chokepoint writes."""
    events: list[dict[str, Any]] = []
    writer = MagicMock()
    writer.log_tool_invocation.side_effect = lambda **event: events.append(event)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: writer)
    return events


@pytest.fixture
def no_backend(monkeypatch: pytest.MonkeyPatch, sel_events: list[dict[str, Any]]) -> None:
    """A host with genuinely no sandbox backend and the default fail-close config."""
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.delenv("KIROCREW_ALLOW_UNSANDBOXED", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    monkeypatch.setattr(sandbox_mod, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sandbox_mod, "_allow_unsandboxed_exec", lambda: False)
    monkeypatch.setattr(sandbox_mod, "_inside_macos_sandbox", lambda: False)
    monkeypatch.setattr(sandbox_mod, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(
        sandbox_mod,
        "_last_unshare_failure",
        (False, "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)", "enable user namespaces"),
    )
    for func, attr in (
        (sandbox_mod._warn_first_party_unconfined_once, "_warned"),
        (sandbox_mod.wrap_argv, "_warned"),
    ):
        if hasattr(func, attr):
            monkeypatch.delattr(func, attr)


class _Host:
    """A stand-in ``kiro-cli`` that logs every run, and the service that probes it."""

    def __init__(self, tmp_path: Path, whoami: str) -> None:
        from kiro_prerequisite_helpers import _no_audit

        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        self.log = tmp_path / "runs.log"
        self.pidfile = tmp_path / "child.pid"
        self.executable = tmp_path / ".local" / "bin" / "kiro-cli"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text(
            "#!/bin/sh\n"
            f'echo "$1" >> "{self.log}"\n'
            'case "$1" in\n'
            '  --version) echo "kiro-cli 9.9.9" ;;\n'
            f"  whoami) {whoami.format(pidfile=self.pidfile)} ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)
        self.service = KiroPrerequisiteService(
            platform_name=sys.platform,
            environ={
                "HOME": str(tmp_path),
                "PATH": f"{self.executable.parent}:/usr/bin:/bin",
            },
            home=tmp_path,
            audit_writer=_no_audit,
        )

    def runs(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").split() if self.log.exists() else []


def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whoami: str = _SIGNED_IN) -> _Host:
    host = _Host(tmp_path, whoami)
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.KiroPrerequisiteService",
        lambda **_kwargs: host.service,
    )
    return host


@pytest.mark.asyncio
async def test_a_no_backend_host_boots_and_the_gate_stays_open(
    enterprise: None,
    no_backend: None,
    sel_events: list[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _host(tmp_path, monkeypatch)
    service = await enterprise_boot.require_kiro_login()
    assert service is host.service and service.authenticated

    gate = EnterpriseGate(closed_for=())
    watch = LoginWatch(gate, service)
    assert await watch.check() is True
    assert gate.is_open
    watch.sync()
    assert gate.is_open

    # Readiness still says the sandbox is unavailable, so no session starts,
    # and the setup-complete marker is left for a sandboxed probe to write.
    status = service._status
    assert status.sandbox_unavailable and status.authenticated and not status.ready
    assert not service.initial_setup_complete
    # Boot and the watch tick each ran exactly the two fixed probes.
    assert host.runs() == ["--version", "whoami"] * 2
    unconfined = [e for e in sel_events if e.get("outcome") == "unconfined"]
    assert len(unconfined) == 4
    assert all("first-party" in e["resources"] for e in unconfined)


@pytest.mark.asyncio
async def test_require_sandbox_does_not_block_the_sign_in_check(
    enterprise: None, no_backend: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``boot.require_sandbox: true`` governs agent tools only (owner decision);
    ``sandbox_allow_unsandboxed_exec`` is pinned False by ``no_backend``."""
    policy = SimpleNamespace(governance=SimpleNamespace(boot=SimpleNamespace(require_sandbox=True)))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: policy)
    host = _host(tmp_path, monkeypatch)
    assert await enterprise_boot.require_kiro_login() is host.service
    assert host.runs() == ["--version", "whoami"]


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param({"_last_unshare_failure": (True, "fork EAGAIN", "retry")}, id="transient"),
        pytest.param({"_inside_macos_sandbox": lambda: True}, id="foreign_sandbox"),
        pytest.param({"_governance_sandbox_floor": lambda: "strict"}, id="governance_floor"),
    ],
)
@pytest.mark.asyncio
async def test_every_other_refusal_keeps_the_probe_sandboxed(
    refusal: dict[str, Any],
    enterprise: None,
    no_backend: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in refusal.items():
        monkeypatch.setattr(sandbox_mod, name, value)
    host = _host(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "Fix the sandbox error above" in capsys.readouterr().err
    assert host.runs() == []


@pytest.mark.asyncio
async def test_signed_out_still_refuses_boot(
    enterprise: None,
    no_backend: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host(tmp_path, monkeypatch, whoami='echo "error: Not logged in" >&2; exit 1')
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "no Kiro sign-in was found" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_full_probe_logs_the_confirmed_sign_in(
    enterprise: None,
    no_backend: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Smoke test N1: the carve-out confirmed the sign-in, so the log says so."""
    host = _host(tmp_path, monkeypatch)
    with caplog.at_level("WARNING", logger="kiro_crew.kiro_prerequisite"):
        status = await host.service._probe(force=True)
    assert status.sandbox_unavailable and status.authenticated and not status.ready
    messages = [r.getMessage() for r in caplog.records]
    assert not any("could not be verified" in m for m in messages)
    assert any("is signed in" in m and "agent sessions still need a sandbox" in m for m in messages)


@pytest.mark.asyncio
async def test_the_full_probe_keeps_the_old_line_when_signed_out(
    enterprise: None,
    no_backend: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = _host(tmp_path, monkeypatch, whoami='echo "error: Not logged in" >&2; exit 1')
    with caplog.at_level("WARNING", logger="kiro_crew.kiro_prerequisite"):
        status = await host.service._probe(force=True)
    assert status.sandbox_unavailable and not status.authenticated and not status.ready
    assert any("could not be verified" in r.getMessage() for r in caplog.records)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.asyncio
async def test_a_hung_whoami_is_killed_and_boot_refused(
    enterprise: None,
    no_backend: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("kiro_crew.kiro_prerequisite._IDENTITY_PROBE_TIMEOUT_SECS", 2)
    host = _host(tmp_path, monkeypatch, whoami="echo $$ > {pidfile}; exec sleep 60")
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "whoami timed out" in err
    assert "no Kiro sign-in was found" not in err
    pid = int(host.pidfile.read_text(encoding="utf-8"))
    await async_wait_until(lambda: _gone(pid), describe=lambda: f"pid {pid} still alive")


@pytest.mark.skipif(not _host_can_reap(), reason="host cannot reap survivors (no pidfd)")
@pytest.mark.asyncio
async def test_a_helper_left_behind_does_not_hold_the_sign_in_check_open(
    enterprise: None, no_backend: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(
        tmp_path,
        monkeypatch,
        whoami="(sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}); " + _SIGNED_IN,
    )
    # Without reaping, the helper would hold whoami to its timeout and refuse boot.
    assert await enterprise_boot.require_kiro_login() is host.service
    pid = int(host.pidfile.read_text(encoding="utf-8"))
    await async_wait_until(lambda: _gone(pid), describe=lambda: f"pid {pid} still alive")


@pytest.mark.asyncio
async def test_a_relative_cli_override_reaches_the_probe_absolute(
    enterprise: None, no_backend: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    monkeypatch.chdir(host.executable.parent)
    host.service._environ["KIROCREW_KIRO_BIN"] = "./kiro-cli"
    assert await enterprise_boot.require_kiro_login() is host.service
    assert host.runs() == ["--version", "whoami"]


@pytest.mark.asyncio
async def test_oss_never_takes_the_carve_out(
    no_backend: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    status = await host.service.snapshot()
    assert status["sandbox_unavailable"] is True
    assert status["authenticated"] is False
    assert host.runs() == []


# --------------------------------------------------------------------------- #
# Reaping is opt-in on _run_process, default off


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "kiro-cli"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def passthrough_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the sandbox out of the picture so only the reaping choice is tested."""
    from kiro_crew import kiro_prerequisite_process as process

    async def passthrough(argv: list[str], *, env: dict[str, str], **_kwargs: Any) -> Any:
        return argv, env, None

    monkeypatch.setattr(process, "_prepare_sandboxed_spawn", passthrough)


@pytest.mark.skipif(not _host_can_reap(), reason="host cannot reap survivors (no pidfd)")
@pytest.mark.parametrize("reap", [False, True])
@pytest.mark.asyncio
async def test_reaping_survivors_is_opt_in(
    reap: bool, passthrough_sandbox: None, tmp_path: Path
) -> None:
    exe = _script(tmp_path, "(sleep 60 >/dev/null 2>&1 &) ; echo signed-in")
    result = await _run_process(
        exe, ["whoami"], env={"PATH": "/usr/bin:/bin"}, timeout_secs=3, reap_survivors=reap
    )
    if reap:
        assert result.ok and "signed-in" in result.output
    else:
        # The default is unchanged for every other caller: the helper holds the
        # supervisor until the timeout.
        assert result.timed_out


@pytest.mark.asyncio
async def test_the_sandboxed_spawn_still_refuses_without_the_flag(no_backend: None) -> None:
    """Without ``first_party_fixed_argv`` a no-backend host spawns nothing."""
    result = await _run_process("/usr/bin/true", [], env={}, timeout_secs=5)
    assert result.sandbox_failure is not None and not result.ok
    assert result.sandbox_failure[0] == "no_backend"
