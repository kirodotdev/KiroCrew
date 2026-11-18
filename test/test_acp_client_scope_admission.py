"""Scope admission on the ``AcpClient`` spawn path, before ``initialize``.

The client's twin of ``test_acp_scope_admission.py``. ``AcpClient._spawn`` names its
scope ``kirocrew-cl-<process instance>.scope``, and both of its ``initialize`` sites
(the session handshake and the entitlement probe) first wait for systemd to move the
pid into that scope, under the 240 s cold-start window less a 30 s reserve. These tests
pin the name, the wait's budget and errors, and that ``ensure_ready`` does not retry an
admission timeout into a second transient unit.

No test reaches systemd or starts a process. Time is a :class:`ManualClock` that moves
only when the wait sleeps, the ``/proc`` reader is a fake, and the spawn tests mock the
process creation, so the stand-in ``systemd-run`` argv is never executed.
"""

from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain
from spawn_test_helpers import strip_spawn_shim
from test_acp_scope_admission import _IN, _OUT, _REAL_WAIT, _sleeper

from kiro_crew.acp import scope_admission
from kiro_crew.acp.client import AcpClient, AcpError, AcpProcessDied
from kiro_crew.acp.client_timeouts import _INIT_TIMEOUT
from kiro_crew.constants import KIROCREW_SPAWN_INSTANCE_ENV
from kiro_crew.sandbox_slice import _SCOPE_UNIT_PREFIX, name_scope_unit, scope_unit_name
from kiro_crew.shared_crew.containment import RUNTIME_SCOPE_PREFIX
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import finished

# ``AcpScopeNotAdmitted``, the client prefix and the client budget constants are
# imported inside the tests that use them, so on a tree that lacks them each test
# fails by itself instead of the module's collection (``prove.py`` counts failures).
_UNIT = "kirocrew-cl-0123456789abcdef.scope"
_STAND_IN_SCOPE = ["/usr/bin/systemd-run", "--user", "--scope", "-q"]
_TRANSPORT_LINE = "Failed to start transient scope unit: Transport endpoint is not connected"
# The scope wrap is systemd's, and the real ``_spawn`` below reaches the mocked
# ``asyncio.create_subprocess_exec`` only through the POSIX launcher. On Windows
# ``launch`` spawns through its job-object path, which would run the argv for real.
_POSIX_LAUNCHER_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


class _Stop(Exception):
    """Raised by a stubbed ``initialize`` wait to end the handshake there."""


def _reader(*answers: str | BaseException, then: str | BaseException | None = None) -> Any:
    """A fake ``/proc`` reader: each answer in turn, then *then*, with ``{unit}`` filled in."""
    queue = list(answers)

    async def _read(_pid: int) -> str:
        _read.reads += 1  # type: ignore[attr-defined]
        answer = queue.pop(0) if queue else then
        if isinstance(answer, BaseException):
            raise answer
        assert answer is not None, "the reader ran past its scripted answers"
        return answer.format(unit=_UNIT)

    _read.reads = 0  # type: ignore[attr-defined]
    return _read


def _reaped_with(proc: MagicMock, code: int) -> Any:
    """A reader whose pid is gone: the reap set *code* just before the read."""

    async def _read(_pid: int) -> str:
        proc.returncode = code
        raise FileNotFoundError("/proc/<pid>/cgroup")

    return _read


def _patch_wait(monkeypatch: pytest.MonkeyPatch, reader: Any, clock: ManualClock) -> None:
    monkeypatch.setattr(
        scope_admission,
        "wait_for_admission",
        functools.partial(
            _REAL_WAIT, read_cgroup=reader, clock=clock.monotonic, sleep=_sleeper(clock)
        ),
    )


def _fake_process() -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    return proc


def _scoped_client(tmp_path: Path) -> tuple[AcpClient, MagicMock]:
    """A client holding a fake, running process wrapped in a named scope."""
    client = AcpClient(work_dir=tmp_path)
    proc = _fake_process()
    client._process = proc
    client._pid = UNALLOCATABLE_PID
    client._scope_unit = _UNIT
    return client, proc


# --------------------------------------------------------------------------- #
# The stage's budget and errors.
# --------------------------------------------------------------------------- #


def test_admission_leaves_the_client_initialize_its_reserve() -> None:
    from kiro_crew.acp.client_timeouts import _SCOPE_ADMISSION_RESERVE, _SCOPE_ADMISSION_TIMEOUT

    assert _INIT_TIMEOUT == 240.0
    assert _SCOPE_ADMISSION_RESERVE == 30.0
    assert _SCOPE_ADMISSION_TIMEOUT == _INIT_TIMEOUT - _SCOPE_ADMISSION_RESERVE


@pytest.mark.asyncio
async def test_a_delayed_admission_leaves_initialize_the_rest_of_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, proc = _scoped_client(tmp_path)
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(_OUT, _OUT, _OUT, then=_IN)
    _patch_wait(monkeypatch, reader, clock)

    budget = await finished(client._admit_scope(), "the scope admission")

    assert reader.reads == 4
    assert budget == pytest.approx(_INIT_TIMEOUT - 3 * scope_admission.POLL_INTERVAL_S)
    proc.stdin.write.assert_not_called()


@pytest.mark.asyncio
async def test_a_spawn_with_no_scope_keeps_the_whole_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _scoped_client(tmp_path)
    client._scope_unit = ""
    reader = _reader()
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))

    budget = await finished(client._admit_scope(), "the scope admission")

    assert budget == _INIT_TIMEOUT
    assert reader.reads == 0


@pytest.mark.asyncio
async def test_an_admission_timeout_is_a_terminal_client_error_naming_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.acp.client_timeouts import _SCOPE_ADMISSION_TIMEOUT
    from kiro_crew.acp.scope_not_admitted import AcpScopeNotAdmitted
    from kiro_crew.llm_helpers import acp_error_is_pipe_death, acp_error_is_transient

    client, proc = _scoped_client(tmp_path)
    _patch_wait(monkeypatch, _reader(then=_OUT), ManualClock(monotonic_start=1000.0))

    with caplog.at_level(logging.INFO):
        with pytest.raises(AcpScopeNotAdmitted) as raised:
            await finished(client._admit_scope(), "the scope admission")

    exc = raised.value
    assert isinstance(exc, AcpError) and not isinstance(exc, AcpProcessDied)
    assert exc.transient is False and acp_error_is_transient(exc) is False
    assert acp_error_is_pipe_death(exc) is False
    text = str(exc)
    assert f"agent process (PID {UNALLOCATABLE_PID}) into its scope {_UNIT}" in text
    assert f"within {_SCOPE_ADMISSION_TIMEOUT:g}s" in text
    proc.stdin.write.assert_not_called()
    [record] = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert record.name == "kiro_crew.acp.client"
    assert record.levelno == logging.WARNING
    assert "outcome=timeout" in record.getMessage()
    assert f"scope={_UNIT}" in record.getMessage()


def test_a_chat_reply_shows_the_admission_timeout_diagnosis() -> None:
    """A Slack transport turn that fails on admission posts the diagnosis, not "try again".

    The message is fixed Kiro Crew wording, and a retry asks the slow manager for
    one more transient unit, so the generic retry reply would send the user back
    into the same failure.
    """
    from kiro_crew.acp.scope_not_admitted import AcpScopeNotAdmitted
    from kiro_crew.agent_sdk.drivers.acp_vocab import is_user_worded_failure
    from kiro_crew.slack import transport_dispatch

    exc = AcpScopeNotAdmitted(scope_admission.timeout_message(_UNIT, 4242, 210.0))

    text = transport_dispatch._transport_error_text(exc)
    assert text != transport_dispatch._TRANSPORT_ERROR_TEXT
    assert text.startswith("Error: ")
    assert f"(PID 4242) into its scope {_UNIT}" in text
    assert "within 210s, so the agent has not started" in text
    assert "systemctl --user list-units --state=failed --no-legend | wc -l" in text
    assert exc.user_worded and is_user_worded_failure(exc)


def test_a_chat_reply_shows_the_runtime_admission_timeout_diagnosis() -> None:
    """The runtime path's ``AcpScopeAdmissionTimeout`` reaches Slack and Telegram as itself.

    It is an ``AcpRuntimeError``, not an ``AcpError``, and the provider start path
    re-raises it unwrapped, so the chat reply must still show the fixed Kiro Crew
    diagnosis rather than the generic retry text.
    """
    from kiro_crew.agent_sdk.drivers.acp_vocab import (
        AcpScopeAdmissionTimeout,
        is_user_worded_failure,
    )
    from kiro_crew.slack import transport_dispatch
    from kiro_crew.telegram import transport_dispatch as telegram_dispatch

    exc = AcpScopeAdmissionTimeout(scope_admission.timeout_message(_UNIT, 4242, 210.0))

    assert is_user_worded_failure(exc)
    text = transport_dispatch._transport_error_text(exc)
    assert text != transport_dispatch._TRANSPORT_ERROR_TEXT
    assert text.startswith("Error: ")
    assert f"(PID 4242) into its scope {_UNIT}" in text
    assert "within 210s, so the agent has not started" in text
    assert "systemctl --user list-units --state=failed --no-legend | wc -l" in text
    reason = telegram_dispatch._user_safe_failure_reason(exc)
    assert reason is not None
    assert "within 210s, so the agent has not started" in reason


@pytest.mark.asyncio
async def test_an_exit_during_admission_is_a_death_naming_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, proc = _scoped_client(tmp_path)
    client._stderr_lines.append(_TRANSPORT_LINE)
    _patch_wait(monkeypatch, _reaped_with(proc, 1), ManualClock(monotonic_start=1000.0))

    with caplog.at_level(logging.INFO):
        with pytest.raises(AcpProcessDied) as raised:
            await finished(client._admit_scope(), "the scope admission")

    text = str(raised.value)
    assert "during the scope admission wait" in text
    assert "stage=scope_admission reads=0" in text
    assert f"scope={_UNIT}" in text
    assert "returncode=1" in text
    assert f"stderr_tail: {_TRANSPORT_LINE}" in text
    proc.stdin.write.assert_not_called()
    [record] = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert record.name == "kiro_crew.acp.client"
    assert "outcome=exited" in record.getMessage()
    assert "returncode=1" in record.getMessage()


@pytest.mark.asyncio
async def test_a_restricted_session_keeps_the_code_but_not_the_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, proc = _scoped_client(tmp_path)
    client.memory_mode = "incognito"
    client._stderr_lines.append(_TRANSPORT_LINE)
    _patch_wait(monkeypatch, _reaped_with(proc, 1), ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpProcessDied, match="stage=scope_admission") as raised:
        await finished(client._admit_scope(), "the scope admission")

    text = str(raised.value)
    assert "returncode=1" in text
    assert "stderr_tail: <not retained>" in text
    assert "Transport endpoint" not in text


# --------------------------------------------------------------------------- #
# The two initialize sites wait first and send what admission left.
# --------------------------------------------------------------------------- #


def _record_sends(client: AcpClient, reader: Any) -> list[tuple[str, int]]:
    """Make *client* record each request it sends, with the reads done by then."""
    sent: list[tuple[str, int]] = []

    async def _send(method: str, params: dict[str, Any]) -> int:
        sent.append((method, reader.reads))
        return len(sent)

    client._send_request = _send  # type: ignore[method-assign]
    return sent


def _stop_at_the_first_wait(client: AcpClient) -> list[float]:
    """Make *client*'s first response wait record its budget and end the handshake."""
    budgets: list[float] = []

    async def _wait(req_id: int, timeout: float = 0.0, **_kwargs: Any) -> dict[str, Any]:
        budgets.append(timeout)
        raise _Stop()

    client._wait_for_response = _wait  # type: ignore[method-assign]
    return budgets


@pytest.mark.asyncio
async def test_the_session_handshake_sends_initialize_after_admission_with_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _scoped_client(tmp_path)
    reader = _reader(_OUT, _OUT, _OUT, then=_IN)
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))
    sent = _record_sends(client, reader)
    budgets = _stop_at_the_first_wait(client)

    with pytest.raises(_Stop):
        await finished(client._initialize_session(), "the session handshake")

    assert sent == [("initialize", 4)]
    assert budgets == [pytest.approx(_INIT_TIMEOUT - 3 * scope_admission.POLL_INTERVAL_S)]


@pytest.mark.asyncio
async def test_the_entitlement_probe_sends_initialize_after_admission_with_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AcpClient(work_dir=tmp_path)

    async def _spawn() -> None:
        client._process = _fake_process()
        client._scope_unit = _UNIT

    client._spawn = _spawn  # type: ignore[method-assign]
    reader = _reader(_OUT, then=_IN)
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))
    sent = _record_sends(client, reader)
    budgets = _stop_at_the_first_wait(client)

    with pytest.raises(_Stop):
        await finished(client._entitlement_probe_answer(), "the probe handshake")

    assert sent == [("initialize", 2)]
    assert budgets == [pytest.approx(_INIT_TIMEOUT - scope_admission.POLL_INTERVAL_S)]


# --------------------------------------------------------------------------- #
# ensure_ready: a timeout ends the start, a death keeps its one retry.
# --------------------------------------------------------------------------- #


def _counted_start(client: AcpClient, make_reader: Any) -> dict[str, Any]:
    """Stub *client*'s spawn and teardown: each spawn is a fake scoped process."""
    seen: dict[str, Any] = {"spawns": 0, "procs": []}

    async def _spawn() -> None:
        proc = _fake_process()
        seen["spawns"] += 1
        seen["procs"].append(proc)
        seen["reader"] = make_reader(proc)
        client._process = proc
        client._scope_unit = _UNIT

    def _reset() -> None:
        client._process = None
        client._scope_unit = ""

    async def _read(pid: int) -> str:
        return await seen["reader"](pid)

    client._spawn = _spawn  # type: ignore[method-assign]
    client._cleanup_failed_live_spawn = AsyncMock()  # type: ignore[method-assign]
    client._reset_state = _reset  # type: ignore[method-assign]
    seen["read"] = _read
    return seen


@pytest.mark.asyncio
async def test_ensure_ready_does_not_retry_an_admission_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp.scope_not_admitted import AcpScopeNotAdmitted

    client = AcpClient(work_dir=tmp_path)
    seen = _counted_start(client, lambda _proc: _reader(then=_OUT))
    _patch_wait(monkeypatch, seen["read"], ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpScopeNotAdmitted):
        await finished(client.ensure_ready(), "the start")

    assert seen["spawns"] == 1
    client._cleanup_failed_live_spawn.assert_awaited_once()
    seen["procs"][0].stdin.write.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ready_retries_a_death_during_admission_once_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AcpClient(work_dir=tmp_path)
    seen = _counted_start(client, lambda proc: _reaped_with(proc, 1))
    _patch_wait(monkeypatch, seen["read"], ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpProcessDied, match="stage=scope_admission"):
        await finished(client.ensure_ready(), "the start")

    assert seen["spawns"] == 2
    for proc in seen["procs"]:
        proc.stdin.write.assert_not_called()


# --------------------------------------------------------------------------- #
# The scope's name.
# --------------------------------------------------------------------------- #


def test_the_client_prefix_names_a_unit_the_runtime_readers_skip() -> None:
    from kiro_crew.sandbox_slice import _CLIENT_SCOPE_UNIT_PREFIX

    token = "a1b2c3d4e5f6a7b8"
    assert scope_unit_name(token) == f"kirocrew-rt-{token}.scope"
    unit = scope_unit_name(token, _CLIENT_SCOPE_UNIT_PREFIX)
    assert unit == f"kirocrew-cl-{token}.scope"
    # ``kept_scopes`` globs the runtime prefix, and Shared Crew counts it.
    assert not unit.startswith(_SCOPE_UNIT_PREFIX)
    assert not unit.startswith(RUNTIME_SCOPE_PREFIX)
    named = name_scope_unit([*_STAND_IN_SCOPE, "--", "agent"], token, _CLIENT_SCOPE_UNIT_PREFIX)
    assert named == [*_STAND_IN_SCOPE, "--unit", unit, "--", "agent"]
    assert scope_unit_name("not a token", _CLIENT_SCOPE_UNIT_PREFIX) is None


async def _spawned_args(client: AcpClient, wrap: Any) -> tuple[tuple[str, ...], dict[str, str]]:
    """Run *client*'s real ``_spawn`` with the cgroup wrap *wrap* and a mocked process."""
    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)),
        patch("kiro_crew.acp.client.cgroup_scope_argv", wrap),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        mock_exec.return_value = _fake_process()
        await client._spawn()
    await _stop_stderr_drain(client)
    call = mock_exec.call_args
    return strip_spawn_shim(call.args), call.kwargs["env"]


@_POSIX_LAUNCHER_ONLY
@pytest.mark.asyncio
async def test_the_spawn_names_its_scope_after_its_process_instance(tmp_path: Path) -> None:
    client = AcpClient(work_dir=tmp_path)

    args, env = await _spawned_args(client, lambda argv: [*_STAND_IN_SCOPE, "--", *argv])

    instance = client.process_instance
    assert len(instance) == 16
    unit = f"kirocrew-cl-{instance}.scope"
    assert client._scope_unit == unit
    at = args.index("--")
    assert args[at - 2 : at] == ("--unit", unit)
    # The child carries the same token, so the reconciler can anchor a harness the
    # session's shell tool starts to this recorded process.
    assert env.get(KIROCREW_SPAWN_INSTANCE_ENV) == instance


@_POSIX_LAUNCHER_ONLY
@pytest.mark.asyncio
async def test_a_spawn_without_the_wrap_has_no_scope_to_wait_on(tmp_path: Path) -> None:
    client = AcpClient(work_dir=tmp_path)

    args, _env = await _spawned_args(client, lambda argv: list(argv))

    assert client._scope_unit == ""
    assert "--unit" not in args
    assert len(client.process_instance) == 16
