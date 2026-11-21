"""Each native session of a Bot chat keeps its own mount record until its close is confirmed.

A projection writes only its own session's entry, so a replacement session that
mounts nothing (the owner unticked the app while an Edit & Resend was projecting)
cannot hide the session it replaces from a stop. Each test names the change that
turns it red.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)
from test_bot_chat_swap_fence import _bounded

from kiro_crew import bot_chat_app_tools as tools
from kiro_crew.session import SessionManager

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
KEY = "dashboard:bot-chat-empty-replacement"


@pytest.mark.asyncio
async def test_an_empty_replacement_does_not_hide_the_session_it_replaced(cfg):
    """Select the Bots to stop from the current record alone and the stop finds
    none, reports success, and the replaced session keeps running the app."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await _bounded(mgr.get_or_create(KEY))
    provider._kas_shutdown_unconfirmed = False  # the chat's own close is confirmed
    mgr.release(mgr._fold_key(KEY))
    folded = mgr._fold_key(KEY)
    slug, app, runtime = "atlas", "team-app", object()
    # The replaced session, bound to its native id, still runs the app's server.
    tools._SESSION_APP_SNAPSHOT_PENDING[folded] = (slug, frozenset({app}))
    tools.bind_snapshot_to_native_sid(folded, "old-sid", runtime)
    # The replacement's projection mounted nothing and rewrote the chat's record.
    tools._OFFERED[slug] = ({}, {})
    tools._MOUNTED[slug] = {}
    tools._MOUNTED_SESSION_KEYS[slug] = folded

    assert tools.app_has_active_bot_sessions(app)
    assert app in tools.mounted_app_names()
    try:
        _stopped, failed = await _bounded(
            tools.stop_app_bot_chat_servers(SimpleNamespace(sessions=mgr), app)
        )
        assert slug in failed
        assert (runtime, "old-sid") in tools._PENDING_CLOSE[(slug, app)]
    finally:
        tools.cancel_pending_close_timers()


def test_a_confirmed_close_is_what_drops_a_session_record():
    """Let the record outlive its confirmed close and every later stop of the app
    discards the chat again; drop it anywhere else and a stop loses it."""
    from kiro_crew.acp.session_provider_shared import teardown_confirmed

    tools._SESSION_APP_SNAPSHOT["sid-1"] = ("atlas", frozenset({"team-app"}))
    rt = SimpleNamespace(_unconfirmed_teardowns={"sid-1"})
    assert teardown_confirmed(rt, "sid-1", killed=False) is False
    assert "sid-1" in tools._SESSION_APP_SNAPSHOT
    rt._unconfirmed_teardowns = set()
    assert teardown_confirmed(rt, "sid-1", killed=False) is True
    assert "sid-1" not in tools._SESSION_APP_SNAPSHOT


#: The only functions that may delete or rewrite a session's mount record, and why.
_RECORD_WRITERS = {
    "record_mounted",  # writes its own session's pending entry, by its own key
    "bind_snapshot_to_native_sid",  # moves that entry to the session's native id
    "note_unconfirmed_close",  # hands an unconfirmed close to the retry, which holds it
    "forget_closed_session",  # the confirmed close (teardown_confirmed)
    "_keep_left_sessions",  # after a confirmed stop: the chat's own pending entry, and
    # every other session handed to the retry
}
_RECORDS = {"_SESSION_APP_SNAPSHOT", "_SESSION_APP_SNAPSHOT_PENDING", "_SESSION_RUNTIMES"}


def _record_writes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in _RECORDS):
            continue
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                target = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"pop", "clear", "popitem", "update", "setdefault"}:
                        target = node.func.value
                elif isinstance(node, (ast.Assign, ast.Delete)):
                    for t in node.targets:
                        if isinstance(t, ast.Subscript):
                            target = t.value
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else getattr(target, "id", None)
                )
                if name in _RECORDS:
                    found.add((path.relative_to(SRC).as_posix(), fn.name))
    return found


def test_only_the_named_functions_write_or_drop_a_session_record():
    """A new writer could drop another session's record before its close is
    confirmed; add it here only if it writes its own session's entry, or runs
    after that close is confirmed."""
    writers = {fn for _path, fn in _record_writes()}
    assert writers <= _RECORD_WRITERS, writers - _RECORD_WRITERS
