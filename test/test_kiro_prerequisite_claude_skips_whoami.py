"""The prerequisite probe does not run ``kiro-cli whoami`` when the backend is claude.

``whoami`` is not a local read: with an expired cached token it refreshes an OIDC
token and writes Kiro CLI's sign-in store. A gateway whose sessions run on the
Claude harness never uses that sign-in, so neither the readiness probe nor the
boot login check may spawn it there. ``--version`` still runs: it is a local
read and it is how the probe resolves the binary it reports on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult


def _service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> tuple[KiroPrerequisiteService, list[list[str]]]:
    monkeypatch.setattr(
        "kiro_crew.config.KiroCrewConfig.load",
        lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, kas_bundle=False)),
    )
    _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
    calls: list[list[str]] = []

    async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
        calls.append(args)
        return ProcessResult(ok=True, output="ok")

    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        home=tmp_path,
        process_runner=run,
        audit_writer=_no_audit,
    )
    return service, calls


@pytest.mark.asyncio
async def test_claude_backend_probe_never_runs_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)

    status = await service.snapshot(force=True)

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

    status = await service.snapshot(force=True)

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
    await service.snapshot(force=True)
    assert ["whoami"] not in calls

    # The status probe just ran, well inside max_age_secs, yet it skipped whoami.
    assert await service.verified_ready(max_age_secs=3600.0) is True

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_failed_turn_sign_in_check_runs_whoami_on_a_claude_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    await service.snapshot(force=True)

    await service.signed_out_confirmed()

    assert ["whoami"] in calls


@pytest.mark.asyncio
async def test_a_failed_turn_sign_in_check_does_not_make_a_skipped_probe_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, calls = _service(tmp_path, monkeypatch, ACP_BACKEND_CLAUDE)
    await service.snapshot(force=True)
    await service.signed_out_confirmed()
    whoami_runs = calls.count(["whoami"])

    # The latch still holds the skipped probe's ready=False; the check above must
    # not have made it look like a real verdict.
    assert await service.verified_ready(max_age_secs=3600.0) is True

    assert calls.count(["whoami"]) == whoami_runs + 1
