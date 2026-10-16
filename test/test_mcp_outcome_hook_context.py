"""A call's commit carries its caller's identity, and a lost commit is a duplicate.

``spawn_sub_agents`` commits its collection from ``DeferredTool.on_answered``,
synchronously on the dispatch thread with the call's caller installed. A pooled
backend has no per-session identity in its environment, so the commit is
accepted only if it is sent while that identity is installed. A commit that
never lands (the request failed, or a SIGTERM ended the server first) leaves
the members held until the collection expires; they are then delivered as
ordinary completions: a duplicate of what the reply carried, never a loss.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_caller import CallerContext, current_caller, set_current_caller
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent_inline_collection import COLLECTION_GRACE_SECS, InlineCollections

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


def _as_the_driver(body) -> None:
    """Model ``_tell_answered``: caller installed for the hook, cleared after."""
    set_current_caller(_pooled_caller())
    try:
        body()
    finally:
        set_current_caller(None)


def _collected_step(collected: list[str]) -> Any:
    """A ``spawn_sub_agents`` call whose collect step carried *collected*."""
    step = spawn_tools._SubAgentsStep(
        sa_ids=list(collected),
        sa_deferred=set(),
        sa_errors=[],
        parent_session=PARENT,
        max_wait=60.0,
        inline_result=lambda _aid, raw: raw,
    )
    step._collected = list(collected)
    return step


def _gateway_like_post(posts: list[dict]):
    """A ``_post`` built on the REAL identity headers and the gateway's member
    scope rule: no token, or a key that is not the claimed parent, is refused."""

    def _post(path: str, body: dict | None = None, **_kw: Any) -> dict:
        token_hdr = mcp_core._session_token_header()
        sk = mcp_core._resolve_session_key()
        posts.append(
            {
                "ids": (body or {}).get("ids"),
                "sk": sk,
                "token": bool(token_hdr),
            }
        )
        if not token_hdr or sk != (body or {}).get("parent_session"):
            return {
                "error": "The execution identity is unavailable",
                "code": "member_identity_unavailable",
            }
        return {"status": "ok"}

    return _post


@pytest.mark.parametrize("written", [True, False])
def test_a_pooled_backends_commit_is_sent_at_once_under_the_calls_identity(
    monkeypatch: pytest.MonkeyPatch, written: bool
) -> None:
    posts: list[dict] = []
    monkeypatch.setattr(mcp_core, "_post", _gateway_like_post(posts))
    # A pooled backend has no env-token or pid rung for PARENT.
    monkeypatch.setattr(
        mcp_core, "resolve_own_identity", lambda **_k: type("I", (), {"session_key": ""})()
    )
    step = _collected_step(["a1"])
    _as_the_driver(lambda: step.on_answered(written))
    # Sent before the hook returned, on the driver's thread, which carries no
    # caller once it did. Accepted on the one attempt: one request.
    assert current_caller() is None
    ids = ["a1"] if written else []
    assert posts == [{"ids": ids, "sk": PARENT, "token": True}]
    # The call commits once: a later hook sends nothing more.
    step.cancel()
    assert len(posts) == 1


def test_a_failing_commit_is_attempted_once_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_core, "_post", lambda _p, body, timeout=0.0: posts.append(body) or {"error": "down"}
    )
    step = _collected_step(["a1"])
    with caplog.at_level(logging.WARNING, logger=spawn_tools.logger.name):
        step.on_answered(True)
    assert len(posts) == 1 and posts[0]["call"] == step.call_id
    assert any("could not commit" in r.getMessage() for r in caplog.records)


def test_an_uncommitted_collection_expires_into_a_delivery() -> None:
    """What a commit that never landed leaves: held members nobody settles.
    Once the collection's time is up each held result is released for
    ordinary delivery, a duplicate of what the reply carried."""
    now = [0.0]
    reg = InlineCollections(clock=lambda: now[0])
    released: list[str] = []

    async def scenario() -> None:
        assert reg.reserve(PARENT, "a1", 60, call="c1")
        assert reg.hold(PARENT, "a1")
        reg._release = lambda rec, returned: released.append(rec.aid)  # type: ignore[method-assign]
        now[0] += 60 + COLLECTION_GRACE_SECS - 1
        reg._expire(PARENT)
        assert released == []  # still within the collection's lifetime
        now[0] += 2
        reg._expire(PARENT)

    asyncio.run(scenario())
    assert released == ["a1"]
