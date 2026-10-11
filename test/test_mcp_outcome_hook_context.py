"""A response-outcome hook reports with its call's identity, and outlives a prompt exit.

The hook runs on its own thread after the dispatcher answered the call. By then
the worker that ran the call has cleared its caller, and a pooled backend has no
per-session identity in its environment, so the commit or drop the hook sends is
accepted only if it runs under the context captured when it was registered. It
also reports AFTER the response is written, so a client that ends the server as
soon as it has its answer (EOF) must not kill the report. A SIGTERM does not wait
for it: the report it cuts off leaves its claim to expire, a duplicate delivery,
never a lost one.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any

import pytest

from kiro_crew import mcp_core, mcp_shared
from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller
from kiro_crew.mcp_tools import spawn as spawn_tools

PARENT = "dashboard:pooled-parent"
TOKEN = "signed-token-for-parent"


@pytest.fixture(autouse=True)
def _pooled_backend_env(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # A pooled backend is spawned from gatewayd's env: no per-session identity.
    for var in ("KIROCREW_SESSION_KEY", "KIROCREW_SESSION_TOKEN", "KIROCREW_HOST_PID"):
        _floor_monkeypatch.delenv(var, raising=False)
    # Only mcp_core's retry backoff skips its sleep: ``mcp_core.time`` is the
    # stdlib module, so patching its ``sleep`` would reach every thread.
    _floor_monkeypatch.setattr(mcp_core, "time", _NoSleepTime())


class _NoSleepTime:
    """``time`` for ``mcp_core`` alone, with ``sleep`` a no-op."""

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    @staticmethod
    def sleep(_secs: float) -> None:
        return None


def _pooled_caller() -> CallerContext:
    return CallerContext(session_key=PARENT, from_gateway=True, session_token=TOKEN)


def _run_as_dispatched_worker(body) -> None:
    """Model ``_run_tool``: caller installed, call armed, caller cleared after."""

    def _worker() -> None:
        set_current_caller(_pooled_caller())
        mcp_shared._arm_response_outcome("req-1")
        try:
            body()
        finally:
            set_current_caller(None)

    worker = threading.Thread(target=_worker)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()


def _hook_recording_caller(seen: list[Any], done: threading.Event):
    def _hook(_delivered: bool) -> None:
        seen.append(current_caller())
        done.set()

    return _hook


def test_the_settled_hook_runs_under_the_registering_callers_context() -> None:
    seen: list[Any] = []
    done = threading.Event()
    hook = _hook_recording_caller(seen, done)
    _run_as_dispatched_worker(lambda: mcp_shared.on_response_outcome(hook))
    # Settled from this thread, which carries no caller.
    assert current_caller() is None
    mcp_shared._settle_response_outcome("req-1", True)
    assert done.wait(5)
    assert seen[0] is not None and seen[0].session_key == PARENT
    assert seen[0].session_token == TOKEN


def test_an_unanswered_calls_drop_runs_under_its_callers_context() -> None:
    seen: list[Any] = []
    done = threading.Event()
    hook = _hook_recording_caller(seen, done)
    _run_as_dispatched_worker(lambda: mcp_shared.on_response_outcome(hook))
    # The loop ends without answering req-1.
    mcp_shared._drop_unsettled_arms()
    assert done.wait(5)
    assert seen[0] is not None and seen[0].session_key == PARENT


def _gateway_like_post(posts: list[dict]):
    """A ``_post`` built on the REAL identity headers and the gateway's member
    scope rule: no token, or a key that is not the claimed parent, is refused."""

    def _post(path: str, body: dict | None = None, **_kw: Any) -> dict:
        token_hdr = mcp_core._session_token_header()
        sk = mcp_core._resolve_session_key()
        posts.append({"phase": (body or {}).get("phase"), "sk": sk, "token": bool(token_hdr)})
        if not token_hdr or sk != (body or {}).get("parent_session"):
            return {
                "error": "The execution identity is unavailable",
                "code": "member_identity_unavailable",
            }
        return {"status": "ok"}

    return _post


@pytest.mark.parametrize("written", [True, False])
def test_a_pooled_backends_outcome_report_is_accepted(
    monkeypatch: pytest.MonkeyPatch, written: bool
) -> None:
    posts: list[dict] = []
    monkeypatch.setattr(mcp_core, "_post", _gateway_like_post(posts))
    # A pooled backend has no env-token or pid rung for PARENT.
    monkeypatch.setattr(
        mcp_core, "resolve_own_identity", lambda **_k: type("I", (), {"session_key": ""})()
    )
    done = threading.Event()
    real_close = spawn_tools._close_collection

    def _close(*a: Any, **k: Any) -> None:
        real_close(*a, **k)
        if (a[3] if len(a) > 3 else k.get("phase")) in ("commit", "drop"):
            done.set()

    monkeypatch.setattr(spawn_tools, "_close_collection", _close)
    _run_as_dispatched_worker(lambda: spawn_tools._commit_when_answered(PARENT, ["a1"], ["a1"]))
    mcp_shared._settle_response_outcome("req-1", written)
    assert done.wait(5)
    phase = "commit" if written else "drop"
    # Accepted on the first attempt: exactly one request, carrying the identity.
    assert posts == [{"phase": phase, "sk": PARENT, "token": True}]


def test_a_report_in_flight_survives_the_server_exiting(tmp_path) -> None:
    """The stdio loop returns right after it writes the response (client EOF);
    interpreter exit waits for the report the hook is still sending."""
    marker = tmp_path / "committed"
    script = textwrap.dedent(f"""
        import time
        from kiro_crew import mcp_shared
        def hook(delivered):
            time.sleep(0.3)  # one loopback POST
            open({str(marker)!r}, "w").write(str(delivered))
        mcp_shared._arm_response_outcome("r")
        assert mcp_shared.on_response_outcome(hook) is None
        mcp_shared._settle_response_outcome("r", True)
        """)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    subprocess.run([sys.executable, "-c", script], check=True, env=env, timeout=60, cwd=tmp_path)
    assert marker.read_text() == "True"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM has no handler on Windows")
def test_sigterm_exits_without_joining_a_running_report(tmp_path) -> None:
    """A real SIGTERM to a real server process: the handler does NOT wait for
    a report still running, so the process exits at once and the report never
    lands. Its claim then expires and the result is delivered again
    (``test_an_unconfirmed_claim_expires_into_a_delivery``): a duplicate,
    never a loss. Nothing in this process is patched."""
    started = tmp_path / "started"
    marker = tmp_path / "committed"
    script = textwrap.dedent(f"""
        import time
        from kiro_crew import mcp_shared
        mcp_shared._install_hard_exit_handlers()
        def hook(delivered):
            open({str(started)!r}, "w").write("1")
            time.sleep(120)  # a report that has not landed when the signal does
            open({str(marker)!r}, "w").write(str(delivered))
        mcp_shared._arm_response_outcome("r")
        assert mcp_shared.on_response_outcome(hook) is None
        mcp_shared._settle_response_outcome("r", True)
        time.sleep(120)  # the server idles until the client ends it
        """)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(sys.path),
        "HOME": str(tmp_path),
        "KIROCREW_HOME": str(tmp_path / "kirocrew"),
    }
    proc = subprocess.Popen([sys.executable, "-c", script], env=env, cwd=tmp_path)
    try:
        deadline = time.monotonic() + 30
        while not started.exists():
            assert proc.poll() is None, "the server exited before its report started"
            assert time.monotonic() < deadline, "the report never started"
            time.sleep(0.01)
        os.kill(proc.pid, signal.SIGTERM)
        # The handler's os._exit(0): it neither idles out nor waits on the hook.
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert not marker.exists()


def test_the_signal_handler_has_no_report_join() -> None:
    """The SIGTERM path is ``logging.shutdown``, a stderr flush and ``os._exit``:
    nothing in it reaches the outcome-hook set or its lock."""
    import inspect

    source = inspect.getsource(mcp_shared._hard_exit_on_signal)
    assert "_join_outcome_hooks" not in source
    assert "_outcome_hook_threads" not in source


def test_an_unconfirmed_claim_expires_into_a_delivery() -> None:
    """What a report cut off by SIGTERM leaves: a claim nobody commits. Once
    its time is up the held result is released for ordinary delivery."""
    import asyncio

    from kiro_crew.subagent_inline_collection import CLAIM_TTL_SECS, InlineCollections

    now = [0.0]
    reg = InlineCollections(clock=lambda: now[0])
    released: list[str] = []

    async def scenario() -> None:
        assert reg.reserve(PARENT, "a1", 60, call="c1")
        assert reg.hold(PARENT, "a1")
        reg.finish(PARENT, ["a1"], ["a1"], call="c1")  # claimed; no commit follows
        reg._release = lambda rec, returned: released.append(rec.aid)  # type: ignore[method-assign]
        now[0] += CLAIM_TTL_SECS + 1
        reg._expire(PARENT)

    asyncio.run(scenario())
    assert released == ["a1"]


def test_a_failing_collection_report_tries_three_times_with_its_pauses(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt fails: three attempts, and the pauses between them go
    through the module's own pause, never the process-wide ``time.sleep``."""
    pauses: list[float] = []
    posts: list[dict[str, Any]] = []
    _floor_monkeypatch.setattr(spawn_tools, "_collection_retry_pause", pauses.append)
    _floor_monkeypatch.setattr(
        mcp_core, "_post", lambda _p, body, timeout=0.0: posts.append(body) or {"error": "down"}
    )
    spawn_tools._close_collection(PARENT, ["a1"], ["a1"], "commit", call="c1")
    assert len(posts) == 3 and all(b["call"] == "c1" for b in posts)
    assert pauses == list(spawn_tools.COLLECTION_RETRY_PAUSES)
    # Every attempt and pause fits the normal exit's join of a running report.
    assert 3 * 5.0 + sum(pauses) < mcp_shared.OUTCOME_HOOK_EXIT_WAIT_SECS


def test_the_exit_wait_is_bounded_by_its_budget() -> None:
    release = threading.Event()
    mcp_shared._start_outcome_hook(lambda _d: release.wait(10), True)
    try:
        assert mcp_shared._join_outcome_hooks(0.05) == 1
    finally:
        release.set()
    assert mcp_shared._join_outcome_hooks(5) == 0
    assert not mcp_shared._outcome_hook_threads
