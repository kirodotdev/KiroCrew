"""A signed-out kiro-cli-chat must surface as a sign-in error, not a retried 5xx.

The bundled ``kiro-cli-chat`` starts, answers ``initialize`` and ``session/new``,
then fails ``session/prompt`` with the frame below and writes nothing to stderr
(captured from kiro-cli 2.31.0 under an empty ``HOME``; its ``whoami`` printed
``Not logged in`` and exited 1). ``saw_not_logged_in`` never latches, so the
frame was classified as a transient connection failure and the dashboard ran its
retry ladder before showing "transient error (HTTP 5xx)".

These tests drive the real frame through the real classifier
(``_raise_acp_error``) and the real ``AcpSessionProvider.stream``, with a real
``KiroPrerequisiteService`` whose kiro-cli runs are faked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import kiro_prerequisite
from kiro_crew.acp.client import AcpAuthRequired, AcpError
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.transport_errors import _raise_acp_error
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, AcpEvent
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult
from kiro_crew.llm_helpers import acp_error_is_connection_failure, acp_error_is_transient
from kiro_crew.providers.base import EVENT_TEXT_CHUNK

# The exact error a signed-out kiro-cli-chat returns for session/prompt.
SIGNED_OUT_PROMPT_FRAME = {
    "code": -32603,
    "message": "Internal error",
    "data": "Encountered an error in the response stream: An unknown error occurred: "
    "dispatch failure",
}


class _Clock:
    """A stepped monotonic clock for the service's probe staleness."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(tmp_path: Path, whoami: Any, clock: _Clock) -> tuple[Any, list[str]]:
    """A real service over a fake kiro-cli; returns it and the argv log.

    *whoami* is the ``whoami`` result, or a callable returning it per run.
    """
    executable = tmp_path / ".local" / "bin" / "kiro-cli"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    calls: list[str] = []

    async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
        calls.append(" ".join(args))
        if args == ["--version"]:
            return ProcessResult(ok=True, output="kiro-cli 2.31.0", returncode=0)
        if args == ["whoami"]:
            return whoami() if callable(whoami) else whoami
        return ProcessResult(ok=False, returncode=1)

    async def audit(**_kwargs: Any) -> None:
        return None

    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
        home=tmp_path,
        data_home=tmp_path / "data-home",
        process_runner=run,
        audit_writer=audit,
        clock=clock,
        spec_lister=lambda: [],
    )
    return service, calls


async def _boot(service: Any, calls: list[str]) -> None:
    """Run the gateway's boot probe, which verifies the binary, then forget its calls."""
    await service.snapshot(force=True)
    calls.clear()


SIGNED_OUT = ProcessResult(ok=False, returncode=1, output="Not logged in")
SIGNED_IN = ProcessResult(ok=True, returncode=0, output="Logged in with Builder ID")
WHOAMI_TIMED_OUT = ProcessResult(ok=False, timed_out=True)


def _provider(backend: str = ACP_BACKEND_KIRO, *, output_first: bool = False) -> Any:
    """A real provider over a handle whose prompt fails with the signed-out frame."""
    handle = MagicMock()
    handle.session_id = "s-1"
    handle.memory_mode = "persistent"

    async def prompt(_message: str):
        if output_first:
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
        _raise_acp_error(SIGNED_OUT_PROMPT_FRAME, backend=backend)

    handle.prompt = prompt
    runtime = MagicMock()
    runtime.is_alive.return_value = True
    runtime._process = MagicMock(returncode=None)
    runtime._last_activity = 0.0
    runtime.acp_backend = backend
    runtime.saw_not_logged_in = lambda: False  # the frame comes with no stderr
    return AcpSessionProvider(handle, runtime)


def _register(monkeypatch: Any, service: Any) -> None:
    """Register *service* for this test only; the module global is restored after.

    Tolerates a tree without the registry so that, on unfixed code, the defect
    test fails on its assertion (a retried AcpError) rather than on a missing name.
    """
    monkeypatch.setattr(kiro_prerequisite, "_turn_auth_service", None, raising=False)
    register = getattr(kiro_prerequisite, "register_turn_auth_service", None)
    if register is not None:
        register(service)


async def _drain(provider: Any) -> list[Any]:
    events = []
    async for event in provider.stream("hello"):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_signed_out_prompt_failure_raises_auth_required(tmp_path, monkeypatch):
    """The defect: red on main (a transient AcpError), green with the fix."""
    clock = _Clock()
    service, _calls = _service(tmp_path, SIGNED_OUT, clock)
    await _boot(service, _calls)
    _register(monkeypatch, service)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider())

    exc = caught.value
    assert isinstance(exc, AcpAuthRequired), f"got {type(exc).__name__}: {exc}"
    assert acp_error_is_transient(exc) is False
    assert acp_error_is_connection_failure(exc) is False
    assert "kiro-cli login" in str(exc)
    assert "5xx" not in str(exc)


@pytest.mark.asyncio
async def test_signed_in_host_keeps_the_connection_retry(tmp_path, monkeypatch):
    """A real network drop on a signed-in host is still a retryable error."""
    service, _calls = _service(tmp_path, SIGNED_IN, _Clock())
    await _boot(service, _calls)
    _register(monkeypatch, service)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider())

    assert not isinstance(caught.value, AcpAuthRequired)
    assert caught.value.transient is True
    assert caught.value.connection_failure is True


@pytest.mark.asyncio
async def test_a_whoami_that_did_not_answer_is_not_a_sign_out(tmp_path, monkeypatch):
    """A timed-out whoami is "unknown"; the turn keeps its ordinary retry."""
    service, _calls = _service(tmp_path, WHOAMI_TIMED_OUT, _Clock())
    await _boot(service, _calls)
    _register(monkeypatch, service)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider())

    assert not isinstance(caught.value, AcpAuthRequired)
    assert caught.value.transient is True


@pytest.mark.asyncio
async def test_no_registered_service_changes_nothing(monkeypatch):
    """The CLI and other callers with no gateway service keep today's behaviour."""
    _register(monkeypatch, None)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider())

    assert not isinstance(caught.value, AcpAuthRequired)
    assert caught.value.transient is True


@pytest.mark.asyncio
async def test_failure_after_output_is_not_rechecked(tmp_path, monkeypatch):
    """Once the turn produced output it was not a sign-in failure; no probe runs."""
    service, calls = _service(tmp_path, SIGNED_OUT, _Clock())
    await _boot(service, calls)
    _register(monkeypatch, service)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider(output_first=True))

    assert not isinstance(caught.value, AcpAuthRequired)
    assert "whoami" not in calls


@pytest.mark.asyncio
async def test_kas_backend_is_not_rechecked(tmp_path, monkeypatch):
    """KAS has its own sign-in owner; kiro-cli's whoami does not speak for it."""
    service, calls = _service(tmp_path, SIGNED_OUT, _Clock())
    await _boot(service, calls)
    _register(monkeypatch, service)

    with pytest.raises(AcpError) as caught:
        await _drain(_provider(backend=ACP_BACKEND_KAS))

    assert not isinstance(caught.value, AcpAuthRequired)
    assert "whoami" not in calls


@pytest.mark.asyncio
async def test_a_stale_verdict_is_reprobed(tmp_path, monkeypatch):
    """A sign-in made after the last probe is seen once the verdict ages out."""
    clock = _Clock()
    state = {"whoami": SIGNED_OUT}
    service, calls = _service(tmp_path, lambda: state["whoami"], clock)
    await _boot(service, calls)

    assert await service.signed_out_confirmed() is True
    probes = calls.count("whoami")

    # Inside the window the latched verdict answers with no new probe.
    clock.now += kiro_prerequisite._TURN_AUTH_MAX_AGE_SECS - 1
    assert await service.signed_out_confirmed() is True
    assert calls.count("whoami") == probes

    # The user signs in from a terminal; past the window the probe sees it.
    state["whoami"] = SIGNED_IN
    clock.now += 2
    assert await service.signed_out_confirmed() is False
    assert calls.count("whoami") == probes + 1


@pytest.mark.asyncio
async def test_concurrent_failures_share_one_probe(tmp_path):
    """A burst of failing turns waits on one whoami, not one each."""
    service, calls = _service(tmp_path, SIGNED_OUT, _Clock())
    await _boot(service, calls)

    results = await asyncio.gather(*(service.signed_out_confirmed() for _ in range(5)))

    assert results == [True] * 5
    assert calls.count("whoami") == 1


@pytest.mark.asyncio
async def test_the_check_never_rewrites_the_readiness_latch(tmp_path):
    """A failing whoami during a network blip must not flip the gateway not-ready."""
    clock = _Clock()
    state = {"whoami": SIGNED_IN}
    service, calls = _service(tmp_path, lambda: state["whoami"], clock)
    await _boot(service, calls)
    before = service._status
    ready_before = await service.session_ready()

    # The network drops: whoami cannot refresh the token and fails.
    state["whoami"] = ProcessResult(ok=False, returncode=1, output="dispatch failure")
    clock.now += kiro_prerequisite._TURN_AUTH_MAX_AGE_SECS + 1
    assert await service.signed_out_confirmed() is False

    assert calls == ["whoami"], "only whoami runs, not the full readiness probe"
    assert service._status is before
    assert await service.session_ready() is ready_before


@pytest.mark.asyncio
async def test_no_verified_binary_runs_nothing(tmp_path):
    """Before the boot probe found a binary there is nothing safe to run."""
    service, calls = _service(tmp_path, SIGNED_OUT, _Clock())

    assert await service.signed_out_confirmed() is False
    assert calls == []


@pytest.mark.asyncio
async def test_close_unregisters_the_service(tmp_path, monkeypatch):
    service, _calls = _service(tmp_path, SIGNED_OUT, _Clock())
    await _boot(service, _calls)
    _register(monkeypatch, None)
    kiro_prerequisite.register_turn_auth_service(service)
    assert await kiro_prerequisite.kiro_signed_out_confirmed() is True

    await service.close()

    assert kiro_prerequisite._turn_auth_service is None
    assert await kiro_prerequisite.kiro_signed_out_confirmed() is False


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ProcessResult(ok=False, returncode=1, output="Not logged in"), True),
        (
            ProcessResult(
                ok=False,
                returncode=1,
                output="error: You are not logged in, please log in with kiro-cli login",
            ),
            True,
        ),
        (ProcessResult(ok=False, timed_out=True, output="Not logged in"), False),
        (ProcessResult(ok=False, output="Not logged in"), False),  # never exited
        (ProcessResult(ok=False, returncode=1, output="network unreachable"), False),
        (ProcessResult(ok=True, returncode=0, output="Logged in"), False),
    ],
)
def test_whoami_signed_out_verdict(result, expected):
    assert kiro_prerequisite._whoami_says_signed_out(result) is expected
