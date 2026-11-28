"""The prerequisite probe does not run ``kiro-cli whoami`` when the backend is claude.

``whoami`` is not a local read: with an expired cached token it refreshes an OIDC
token and writes Kiro CLI's sign-in store. A gateway whose sessions run on the
Claude harness never uses that sign-in, so neither the readiness probe nor the
boot login check may spawn it there. ``--version`` still runs: it is a local
read and it is how the probe resolves the binary it reports on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew import members
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult
from kiro_crew.testing.wait import await_until


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    before_reply: Callable[[list[str]], Awaitable[None]] | None = None,
    agents: dict[str, Any] | None = None,
) -> tuple[KiroPrerequisiteService, list[list[str]]]:
    config = SimpleNamespace(
        agent=SimpleNamespace(acp_backend=backend, kas_bundle=False, member_acp_backend=backend),
        agents=agents or {},
    )
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", lambda: config)
    picks = members._SESSION_BACKEND_PICKS
    for key in list(picks):
        monkeypatch.delitem(picks, key)
    _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
    calls: list[list[str]] = []

    async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
        calls.append(args)
        if before_reply is not None:
            await before_reply(args)
        return ProcessResult(ok=True, output="ok")

    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        process_runner=run,
        audit_writer=_no_audit,
        clock=_Clock(),
    )
    return service, calls


class _Clock:
    """A monotonic clock the test moves by hand, so no poll waits on real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _status_poll(service: KiroPrerequisiteService) -> dict[str, Any]:
    """The dashboard's machine poll (``refresh=auto``), past its floor and cache."""
    clock = service._clock
    assert isinstance(clock, _Clock)
    clock.now += 10.0
    return await service.snapshot(force=True, coalesce=True)


@pytest.mark.asyncio
async def test_claude_backend_probe_never_runs_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)

    status = await _status_poll(service)

    assert status["installed"] is True
    assert ["--version"] in calls
    assert ["whoami"] not in calls


@pytest.mark.asyncio
async def test_claude_backend_boot_login_check_never_runs_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)

    await service.boot_login_status()

    assert calls == [["--version"]]


@pytest.mark.asyncio
async def test_kas_backend_probe_still_runs_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_KAS)

    status = await _status_poll(service)

    assert status["authenticated"] is True
    assert calls[:2] == [["--version"], ["whoami"]]


@pytest.mark.asyncio
async def test_claude_default_still_probes_whoami_for_a_kiro_routed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # regenerate / edit-resend / rewind on a Kiro CLI session ask verified_ready;
    # the default backend being claude must not turn that into a signed-out 503.
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)

    assert await service.verified_ready(max_age_secs=3600.0) is True

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_skipped_status_probe_is_not_reused_as_a_kiro_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    await _status_poll(service)
    assert ["whoami"] not in calls

    # The status probe just ran, well inside max_age_secs, yet it skipped whoami.
    assert await service.verified_ready(max_age_secs=3600.0) is True

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_failed_turn_sign_in_check_runs_whoami_on_a_claude_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    await _status_poll(service)

    await service.signed_out_confirmed()

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_failed_turn_sign_in_check_does_not_make_a_skipped_probe_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    await _status_poll(service)
    await service.signed_out_confirmed()
    whoami_runs = calls.count(["whoami"])

    # The latch still holds the skipped probe's ready=False; the check above must
    # not have made it look like a real verdict.
    assert await service.verified_ready(max_age_secs=3600.0) is True

    assert calls.count(["whoami"]) == whoami_runs + 1


@pytest.mark.asyncio
async def test_a_status_probe_in_flight_does_not_make_a_skipped_latch_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The skip flag describes the latch it was written with. A second status probe
    # that is still running has not replaced that latch, so a Kiro-routed check
    # made meanwhile must still re-probe, not trust the skipped ready=False.
    blocking = False
    in_version = asyncio.Event()
    release = asyncio.Event()

    async def hold_version(args: list[str]) -> None:
        if blocking and args == ["--version"]:
            in_version.set()
            await await_until(release, what="the test releasing --version")

    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE, hold_version)
    await _status_poll(service)
    assert ["whoami"] not in calls

    blocking = True
    status_refresh = asyncio.create_task(_status_poll(service))
    check: asyncio.Task[bool] | None = None
    try:
        await await_until(in_version, what="the status refresh reaching --version")
        check = asyncio.create_task(service.verified_ready(max_age_secs=3600.0))
        # One loop turn, not a timed wait: the new task runs its first step now,
        # while the refresh still holds the probe lock inside --version.
        await asyncio.sleep(0)
        blocking = False
        release.set()
        await await_until(status_refresh, what="the status refresh")

        assert await await_until(check, what="the Kiro-routed check") is True
        assert ["whoami"] in calls
    finally:
        release.set()
        for task in (status_refresh, check):
            if task is not None and not task.done():
                task.cancel()


@pytest.mark.asyncio
async def test_a_human_check_again_runs_whoami_on_a_claude_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Check again (refresh=explicit) is the person asking for the sign-in answer.
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)

    status = await service.snapshot(force=True)

    assert ["whoami"] in calls
    assert status["authenticated"] is True


@pytest.mark.asyncio
async def test_a_crewmate_pinned_to_kiro_cli_keeps_whoami_on_the_status_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A slot this gateway routes to Kiro CLI runs on kiro-cli's sign-in, so the
    # status the dashboard shows for it must come from a real whoami.
    pin = SimpleNamespace(acp_backend="kiro")
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE, agents={"helper": pin})

    status = await _status_poll(service)

    assert ["whoami"] in calls
    assert status["authenticated"] is True


@pytest.mark.asyncio
async def test_a_session_created_on_kiro_cli_keeps_whoami_on_the_status_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    # The table session_create writes (members.note_session_backend_pick), set and
    # restored through monkeypatch so no other test sees the pick.
    monkeypatch.setitem(members._SESSION_BACKEND_PICKS, "kiro-chat", "kiro")

    await _status_poll(service)

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_skipped_probe_keeps_the_last_real_sign_in_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    assert await service.verified_ready(max_age_secs=3600.0) is True
    whoami_runs = calls.count(["whoami"])

    status = await _status_poll(service)

    assert calls.count(["whoami"]) == whoami_runs
    assert status["authenticated"] is True


@pytest.mark.asyncio
async def test_a_crewmate_pinned_to_a_harness_bots_cannot_run_on_keeps_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Such a pin runs the Bot on KAS (CrewBackendPin.checked), which signs in via
    # kiro-cli, though the stored pin names another harness.
    from kiro_crew import crewmate_harness

    monkeypatch.setattr(crewmate_harness, "bots_cannot_run_on", lambda backend: True)
    pin = SimpleNamespace(acp_backend="goose")
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE, agents={"helper": pin})

    await _status_poll(service)

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_an_inherited_bot_harness_on_an_all_claude_gateway_keeps_the_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An omitted agent.member_acp_backend loads as inherited; with every crewmate on
    # claude nothing runs on kiro-cli, so the status poll still skips whoami.
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader_builders import MEMBER_BACKEND_INHERITED_ATTR

    pin = SimpleNamespace(acp_backend="claude")
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE, agents={"helper": pin})
    setattr(KiroCrewConfig.load().agent, MEMBER_BACKEND_INHERITED_ATTR, True)

    await _status_poll(service)

    assert ["whoami"] not in calls
