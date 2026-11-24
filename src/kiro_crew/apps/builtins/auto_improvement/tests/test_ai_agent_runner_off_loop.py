"""The runner's audited writes and governance gate stay off the event loop.

In a spawned app backend ``sel()`` is a route client: a critical write is network I/O and
is refused on a thread that runs an event loop. The runner awaits on that loop, so its
approval write and its governance gate (which can write a critical deny-exception row)
run on a worker thread. These tests pin that, and that a failure or a cancel on the way
never ends in an approval. The shared fakes are those of ``test_ai_agent_runner_coverage``.
"""

# flake8: noqa: F811  -- ``fake_sel`` is a fixture imported from the sibling suite
from __future__ import annotations

import asyncio
import sys

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as R
from kiro_crew.apps.builtins.auto_improvement.tests.test_ai_agent_runner_coverage import (  # noqa: F401
    _drive,
    _ev,
    _FakeProvider,
    fake_sel,
)

#: ``s3_direction._s3_download_only`` reads no command on Windows (no POSIX shell reads it
#: there), so the deny stands and no exception is granted: the S3 cases are POSIX-only.
_NO_S3_EXEMPTION_ON_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the S3 download exemption is not read on Windows (no POSIX shell reads the command)",
)


class _Rows:
    """A healthy gateway route: one token, 204 for every row; the rows it took."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __call__(self, req, timeout):
        import json

        class _Reply:
            def __init__(self, status: int, body: bytes = b"") -> None:
                self.status, self._body = status, body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def read(self) -> bytes:
                return self._body

        if req.full_url.endswith("/token"):
            return _Reply(200, b'{"token": "tok"}')
        self.rows.append(json.loads(req.data))
        return _Reply(204)


@pytest.fixture
def route_client(monkeypatch):
    """The process is a spawned backend with a working route; ``sel()`` is the route client."""
    from kiro_crew.apps import sel_client

    rows = _Rows()
    monkeypatch.setattr(sel_client, "loopback_urlopen", rows)
    for name, value in {
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "improve-app",
        "KIROCREW_GATEWAY_ORIGIN": "http://127.0.0.1:8123",
        "KIROCREW_PROXY_SECRET": "per-app-secret",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KIROCREW_SEL_CLIENT_PID", raising=False)
    return rows


_S3_DOWNLOAD = "aws s3 cp --no-sign-request s3://public-bucket/input.json ./input.json"


def _s3_download_events():
    return [
        _ev(
            kind=EVENT_PERMISSION_REQUEST,
            tool_kind="bash",
            request_id="r1",
            raw_tool_params={"command": _S3_DOWNLOAD},
        ),
        _ev(kind=EVENT_COMPLETE),
    ]


def _deny_exception_rows(rows: _Rows) -> list[dict]:
    return [r for r in rows.rows if r["kwargs"].get("operation") == "security.deny_exception"]


@pytest.mark.asyncio
@_NO_S3_EXEMPTION_ON_WINDOWS
async def test_run_async_approves_an_s3_download_the_deny_exception_allows(route_client):
    provider = _FakeProvider(_s3_download_events())
    res = await _drive(R.SessionAgentRunner(), provider)

    assert res.ok is True
    assert provider.approved == ["r1"] and provider.rejected == []
    rows = _deny_exception_rows(route_client)
    assert rows and all(r["kwargs"]["critical"] is True for r in rows)  # the exception rows landed


@pytest.mark.asyncio
@_NO_S3_EXEMPTION_ON_WINDOWS
async def test_run_async_denies_when_the_deny_exception_is_written_on_the_loop(
    route_client, monkeypatch
):
    """A missed offload fails closed: the write is refused on the loop, so the tool is denied."""

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(R.asyncio, "to_thread", _inline)
    provider = _FakeProvider(_s3_download_events())
    res = await _drive(R.SessionAgentRunner(), provider)

    assert res.ok is True
    assert provider.approved == [] and provider.rejected == ["r1"]
    assert _deny_exception_rows(route_client) == []


@pytest.mark.asyncio
async def test_run_async_denies_when_the_governance_call_itself_raises(monkeypatch, fake_sel):
    """A failure of the offloaded call (not a hook verdict) is a deny, never an approval."""

    async def _no_thread(func, /, *args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(R.asyncio, "to_thread", _no_thread)
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_PERMISSION_REQUEST, tool_kind="fsRead", request_id="r1"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    res = await _drive(R.SessionAgentRunner(), provider)

    assert res.ok is True
    assert provider.rejected == ["r1"] and provider.approved == []


@pytest.mark.asyncio
async def test_run_async_cancelled_while_awaiting_the_governance_call_never_approves(
    monkeypatch, fake_sel
):
    import threading

    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

    entered, release = threading.Event(), threading.Event()

    def _held_gate(ev, **_kw) -> str:
        entered.set()
        release.wait(FALLBACK_TIMEOUT_SECS)
        return ""

    monkeypatch.setattr(R, "_governance_denial", _held_gate)
    provider = _FakeProvider(
        [
            _ev(kind=EVENT_PERMISSION_REQUEST, tool_kind="fsRead", request_id="r1"),
            _ev(kind=EVENT_COMPLETE),
        ]
    )
    task = asyncio.ensure_future(_drive(R.SessionAgentRunner(), provider))
    await async_wait_until(entered.is_set)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.approved == []  # the request was never approved
    assert provider.rejected == []  # it ends unanswered with the run: denied by omission


@pytest.mark.asyncio
async def test_approve_writes_its_audit_row_off_the_event_loop(monkeypatch):
    """The critical write is synchronous (network I/O in a spawned backend): not on the loop."""
    seen: list[bool] = []

    class _Sel:
        def log_tool_invocation(self, **_kw) -> None:
            try:
                asyncio.get_running_loop()
                seen.append(True)
            except RuntimeError:
                seen.append(False)

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    provider = _FakeProvider()
    await R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite")

    assert seen[0] is False  # the first row is the critical one
    assert provider.approved == ["r1"]


@pytest.mark.asyncio
async def test_a_critical_write_left_on_the_loop_is_refused_and_the_tool_is_denied(monkeypatch):
    """A caller that missed the offload is refused, never stalled and never allowed."""
    from kiro_crew.apps import sel_client

    def _no_network(*_a, **_kw):
        raise AssertionError("a critical write on the loop reached the network")

    monkeypatch.setattr(sel_client, "loopback_urlopen", _no_network)
    client = sel_client.SelClient("deny-app", "http://127.0.0.1:1", "per-app-secret")
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: client)
    with pytest.raises(sel_client.SelClientError, match="event loop"):
        client.log_tool_invocation(session_key="s", tool_name="t", outcome="x", critical=True)

    async def _inline(func, /, *args, **kwargs):  # the offload, missed
        return func(*args, **kwargs)

    monkeypatch.setattr(R.asyncio, "to_thread", _inline)
    provider = _FakeProvider()
    await R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite")

    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_approve_cancelled_while_its_audit_write_is_in_flight_never_approves(monkeypatch):
    """A cancel during the write leaves the tool denied; the write may still land (over-report)."""
    import threading

    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

    entered, release, landed = threading.Event(), threading.Event(), []

    class _Sel:
        def log_tool_invocation(self, **kw) -> None:
            entered.set()
            release.wait(FALLBACK_TIMEOUT_SECS)
            landed.append(kw["request_id"])

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    provider = _FakeProvider()
    task = asyncio.ensure_future(R.SessionAgentRunner._approve(provider, "r1", tool="fsWrite"))
    await async_wait_until(entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()

    assert provider.approved == []  # the tool is never approved: denied by omission
    await async_wait_until(lambda: bool(landed))
    assert landed == ["r1"]  # the write still landed: an over-report, the safe direction
