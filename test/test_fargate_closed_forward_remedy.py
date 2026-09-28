"""A closed fargate forward names its cause and a real remedy.

The SSM tunnel child prints every session-close notice to STDOUT and its own
diagnostics to a rolling log file, and it exits 0 whether AWS closed the session
cleanly, a transport drop ended it, or it never started. So the exit code cannot
separate those and stderr carries none of them. These pins hold the two halves of
the fix together: stdout is captured concurrently and bounded, and only an
anchored close shape is allowed to name a cause.

The wording guards are as load-bearing as the classification. A message that
states a duration, promises traffic keeps a session alive, or offers a reconnect
is wrong even when the classification is right.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.instances.ssh_tunnel_manager import (
    _MAX_STDOUT_CHARS,
    _sanitize_ssm_stdout,
    _SshTunnel,
    _ssm_close_reason,
)

# The exact literals session-manager-plugin writes to stdout, from its source:
# datachannel HandleChannelClosedMessage and sessionhandler ResumeSessionHandler.
SESSION_ID = "user-0123456789abcdef"
BANNER = f"\nStarting session with SessionId: {SESSION_ID}\n"
IDLE_REASON = "Your session timed out due to inactivity and has been terminated."
CLOSED_IDLE = f"{BANNER}\n\nSessionId: {SESSION_ID} : {IDLE_REASON}\n\n"
CLOSED_NO_REASON = f"{BANNER}\n\nExiting session with sessionId: {SESSION_ID}.\n\n"
CLOSED_OTHER_REASON = f"{BANNER}\n\nSessionId: {SESSION_ID} : Session terminated by operator.\n\n"
RESUME_TIMED_OUT = f"{BANNER}Session: {SESSION_ID} timed out.\n"
START_FAILED = "Cannot perform start session: some transport error\n"


def _tunnel(*, stdout: str = "", stderr: str = "", port: int = 7777) -> _SshTunnel:
    t = _SshTunnel("crew", "", port, 7777, transport="ssm", ssm_target="ecs:c_a_b")
    t._stdout_buf = stdout
    t._stderr_buf = stderr
    return t


class TestCloseReasonShapes:
    """Only an anchored shape names a cause; anything else stays unclaimed."""

    @pytest.mark.parametrize(
        "stdout,kind",
        [
            (CLOSED_IDLE, "idle"),
            (CLOSED_NO_REASON, "closed"),
            (CLOSED_OTHER_REASON, "closed"),
            (RESUME_TIMED_OUT, "resume_timeout"),
            (START_FAILED, "start_failed"),
            ("", ""),
            (BANNER, ""),
            # A line that merely mentions a session is not a close notice.
            (f"{BANNER}Connection accepted for session {SESSION_ID}.\n", ""),
        ],
    )
    def test_shape(self, stdout: str, kind: str) -> None:
        assert _ssm_close_reason(stdout) == kind

    def test_resume_timeout_is_not_read_as_idle(self) -> None:
        """ "timed out" in the resume shape is a transport outcome, not idleness.

        The two shapes differ only in their anchor (``Session:`` against
        ``SessionId:``), which is exactly why the anchor is matched and the word
        is not.
        """
        assert _ssm_close_reason(RESUME_TIMED_OUT) == "resume_timeout"

    def test_id_without_a_reason_is_not_claimed(self) -> None:
        """The reason-carrying anchor needs its separator to mean anything."""
        assert _ssm_close_reason(f"SessionId: {SESSION_ID}\n") == ""


class TestClosedForwardMessage:
    """What an operator is told, per case."""

    def test_idle_names_cause_and_preference(self) -> None:
        msg = _tunnel(stdout=CLOSED_IDLE)._exit_error(0)
        assert "no activity" in msg
        assert "idle-timeout preference" in msg

    def test_unknown_exit_zero_stays_generic(self) -> None:
        """An unrecognised reason must NOT be reported as idle."""
        msg = _tunnel(stdout=CLOSED_OTHER_REASON)._exit_error(0)
        assert "AWS ended the SSM session" in msg
        assert "no activity" not in msg
        assert "idle-timeout preference" not in msg

    def test_no_reason_stays_generic(self) -> None:
        msg = _tunnel(stdout=CLOSED_NO_REASON)._exit_error(0)
        assert "AWS ended the SSM session" in msg
        assert "idle-timeout preference" not in msg

    def test_transport_failure_is_not_idle(self) -> None:
        msg = _tunnel(stdout=RESUME_TIMED_OUT)._exit_error(0)
        assert "was lost" in msg
        assert "no activity" not in msg
        assert "idle-timeout preference" not in msg

    def test_never_started_is_not_a_close(self) -> None:
        msg = _tunnel(stdout=START_FAILED)._exit_error(0)
        assert "never opened" in msg
        assert "AWS ended" not in msg

    def test_bare_exit_zero_unchanged(self) -> None:
        """No stdout, no stderr: the pre-existing wording still applies."""
        assert _tunnel()._exit_error(0) == "SSM session exited with code 0"

    def test_stderr_signal_outranks_a_close_notice(self) -> None:
        """An actionable stderr failure is still the better answer."""
        msg = _tunnel(
            stdout=CLOSED_IDLE, stderr="An error occurred (AccessDeniedException) ..."
        )._exit_error(255)
        assert "IAM denied ssm:StartSession" in msg
        assert "idle-timeout preference" not in msg

    @pytest.mark.parametrize(
        "stdout",
        [CLOSED_IDLE, CLOSED_NO_REASON, CLOSED_OTHER_REASON, RESUME_TIMED_OUT, START_FAILED],
    )
    def test_wording_constraints(self, stdout: str) -> None:
        """No duration, no keep-alive promise, no reconnect offer, no CLI verb.

        Each is a conclusion the issue records: the AWS documentation does not
        settle which idle limit governs a port forward at an ``ecs:`` target, the
        documented activity list is terminal-centric, re-opening is a human
        action, and a fargate crew has no CLI connect verb.
        """
        msg = _tunnel(stdout=stdout)._exit_error(0)
        low = msg.lower()
        for promise in ("automatically", "will reconnect", "retrying", "keep using"):
            assert promise not in low, promise
        for verb in ("kirocrew cloud", "kirocrew connect", "aws ssm start-session"):
            assert verb not in low, verb
        # A digit may only appear as the local port, never as a duration.
        assert "minute" not in low and "second" not in low
        digits = "".join(ch for ch in msg if ch.isdigit())
        assert digits == "7777", digits
        # Points at the surface that actually reaches a fargate crew.
        assert "card in Settings" in msg

    def test_own_stop_never_reaches_the_classifier(self) -> None:
        """A deliberate teardown composes no error at all."""
        t = _tunnel(stdout=CLOSED_IDLE)
        t._stopping = True

        class _Exited:
            returncode = 0

            async def wait(self) -> int:
                return 0

        t._proc = _Exited()  # type: ignore[assignment]
        asyncio.run(t._monitor())
        assert t.status.error == ""


class TestStdoutCaptureHygiene:
    """The stdout buffer is bounded, control-stripped and credential-redacted."""

    def test_reason_text_is_never_surfaced(self) -> None:
        """Service-controlled text classifies; it is never shown or logged."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        stdout = f"{BANNER}\n\nSessionId: {SESSION_ID} : {IDLE_REASON} {secret}\n\n"
        msg = _tunnel(stdout=stdout)._exit_error(0)
        assert secret not in msg
        assert SESSION_ID not in msg
        # The reason's OWN words must be absent too, not merely its secrets.
        # Redaction alone would let the rest of a service string through, and the
        # operator would then be reading text this code cannot vouch for.
        for fragment in ("Your session timed out", "has been terminated"):
            assert fragment not in msg, fragment
        # Still classified as idle despite the trailing junk.
        assert "idle-timeout preference" in msg

    def test_credentials_redacted_before_matching(self) -> None:
        """Sanitizing happens at read, so matching never sees a raw credential.

        Pinned on the sanitizer directly: it is the only thing standing between
        a service-controlled stream and the matcher, and the classifier hands
        the reason text back to nobody, so there is no return value to observe
        it through.
        """
        raw = f"SessionId: {SESSION_ID} : closed AKIAIOSFODNN7EXAMPLE\n"
        assert "AKIAIOSFODNN7EXAMPLE" not in _sanitize_ssm_stdout(raw)
        # The notice is still classifiable with the credential gone.
        assert _ssm_close_reason(raw) == "closed"

    def test_control_sequences_cannot_hide_the_anchor(self) -> None:
        """An ANSI-wrapped notice is still recognised, not smuggled past."""
        stdout = f"\x1b[31mSessionId: {SESSION_ID} : {IDLE_REASON}\x1b[0m\n"
        assert _ssm_close_reason(stdout) == "idle"

    def test_control_chars_stripped(self) -> None:
        stdout = f"SessionId:\x00 {SESSION_ID} : {IDLE_REASON}\r\n"
        assert _ssm_close_reason(stdout) == "idle"

    def test_oversized_stdout_is_bounded_and_still_classified(self) -> None:
        """A flood cannot grow memory, and the newest notice survives it."""
        flood = "x" * (_MAX_STDOUT_CHARS * 4)

        async def _drive() -> _SshTunnel:
            t = _tunnel()
            payload = (flood + CLOSED_IDLE).encode()
            t._proc = _FakeProc(payload)  # type: ignore[assignment]
            t._stdout_task = asyncio.create_task(t._drain_stdout())
            await t._finish_stdout_drain()
            return t

        t = asyncio.run(_drive())
        assert len(t._stdout_buf) <= _MAX_STDOUT_CHARS
        # The tail is kept, so the close notice written last is what remains.
        assert t._exit_error(0).count("idle-timeout preference") == 1

    def test_drain_survives_a_read_failure(self) -> None:
        """A failing pipe must not raise out of an unobserved background task."""

        async def _drive() -> str:
            t = _tunnel()
            t._proc = _FakeProc(b"", raise_on_read=True)  # type: ignore[assignment]
            t._stdout_task = asyncio.create_task(t._drain_stdout())
            await t._finish_stdout_drain()
            return t._stdout_buf

        assert asyncio.run(_drive()) == ""


class _FakeStream:
    """Feeds bytes in chunks, then EOF — a stand-in for the child's stdout pipe."""

    def __init__(self, payload: bytes, raise_on_read: bool = False) -> None:
        self._payload = payload
        self._raise = raise_on_read

    async def read(self, n: int) -> bytes:
        if self._raise:
            raise OSError("pipe went away")
        chunk, self._payload = self._payload[:n], self._payload[n:]
        return chunk


class _FakeProc:
    returncode = 0

    def __init__(self, payload: bytes, raise_on_read: bool = False) -> None:
        self.stdout = _FakeStream(payload, raise_on_read)
        self.stderr = None

    async def wait(self) -> int:
        return 0


class TestSpawnWiring:
    """The SSM child's stdout must actually be piped, or nothing above can work.

    Without this pin the stdio wiring could go back to DEVNULL and every
    classification test would still pass, because those set the buffer directly.
    """

    def _spawn_kwargs(self, monkeypatch: pytest.MonkeyPatch, **kw: object) -> dict:
        seen: dict = {}

        async def _fake_exec(*_argv: str, **kwargs: object):
            seen.update(kwargs)
            return _FakeProc(b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        t = _SshTunnel("crew", "host", 7777, 7777, connect_timeout_secs=0.01, **kw)  # type: ignore[arg-type]
        asyncio.run(t.start())
        return seen

    def test_ssm_stdout_is_piped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._spawn_kwargs(monkeypatch, transport="ssm", ssm_target="ecs:c_a_b")
        assert seen["stdout"] == asyncio.subprocess.PIPE
        assert seen["stderr"] == asyncio.subprocess.PIPE

    def test_ssh_stdout_still_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssh writes nothing useful there; the new pipe is SSM-only."""
        seen = self._spawn_kwargs(monkeypatch)
        assert seen["stdout"] == asyncio.subprocess.DEVNULL


class TestMonitorPath:
    """The unexpected-exit path settles the drain before it composes the error.

    The drain is concurrent, so the notice the child writes immediately before
    exiting can still be unread when ``wait()`` returns. Without the settle this
    reports the old bare exit code.
    """

    def test_monitor_reports_idle(self) -> None:
        async def _drive() -> _SshTunnel:
            t = _tunnel()
            t._proc = _FakeProc(CLOSED_IDLE.encode())  # type: ignore[assignment]
            t._stdout_task = asyncio.create_task(t._drain_stdout())
            await t._monitor()
            return t

        t = asyncio.run(_drive())
        assert "idle-timeout preference" in t.status.error
        assert t.status.state.value == "error"
