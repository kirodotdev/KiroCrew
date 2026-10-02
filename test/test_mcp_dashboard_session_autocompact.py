"""``session_set_autocompact``: a session's compaction threshold over the dashboard MCP.

Pinned here: the scope (the caller's own session or one it created, for every
caller class), the range check shared with the per-slot route, the read shape
(``pct`` omitted), that a clear goes back to the global, that the write lands
through the route's own transaction (durable field, live override map), and
that the route is a strict internal-secret path.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    state.conversation_log.append(slot_history_key(target), "user", "hello")
    state.conversation_log.append(slot_history_key(caller), "user", "hi")
    if created:
        target._created_by = caller.key
    return state, caller, target


def _call(state, caller, target: str = "chat-2", **kw) -> dict:
    return asyncio.run(
        sc.autocompact_target(
            state, caller_session_key=slot_history_key(caller), target=target, **kw
        )
    )


# ── Scope ────────────────────────────────────────────────────────────────────


def test_sets_a_session_the_caller_created(tmp_path):
    state, caller, target = _pair(tmp_path)
    out = _call(state, caller, pct=40)
    assert out["ok"] is True and out["pct"] == 40.0
    assert target.autocompact_pct == 40.0
    state.sessions.set_autocompact_pct.assert_called_with(effective_session_key(target), 40.0)


def test_a_session_the_caller_did_not_create_is_refused_even_unfenced(tmp_path):
    """The creator check runs for every caller class, not only fenced ones."""
    state, caller, target = _pair(tmp_path, created=False)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=40)
    assert exc.value.code == "not_creator"
    assert target.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_not_called()


def test_the_read_is_scoped_like_the_write(tmp_path):
    state, caller, _target = _pair(tmp_path, created=False)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller)
    assert exc.value.code == "not_creator"


def test_the_caller_may_address_itself(tmp_path):
    state, caller, _target = _pair(tmp_path, created=False)
    out = _call(state, caller, target="chat-1", pct=85)
    assert out["target"] == "chat-1" and caller.autocompact_pct == 85.0


def test_self_addressing_mid_turn_does_not_touch_the_turn(tmp_path):
    """The override feeds the between-turn check only; nothing here stops a turn."""
    state, caller, _target = _pair(tmp_path)
    turn = MagicMock()
    turn.done.return_value = False
    caller.task = turn
    assert caller.running is True
    _call(state, caller, target="chat-1", pct=70)
    assert caller.running is True and caller.autocompact_pct == 70.0
    turn.cancel.assert_not_called()


def test_a_remote_target_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.executor = "remote"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=40)
    assert exc.value.code == "remote_target_unsupported"
    assert target.autocompact_pct is None


# ── Values ───────────────────────────────────────────────────────────────────


def test_read_reports_override_global_and_range(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.autocompact_pct = 33.0
    out = _call(state, caller)
    assert out["pct"] == 33.0
    assert out["min"] == AUTOCOMPACT_PCT_MIN and out["max"] == AUTOCOMPACT_PCT_MAX
    assert "global_pct" in out
    state.sessions.set_autocompact_pct.assert_not_called()


def test_null_clears_back_to_the_global(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.autocompact_pct = 33.0
    out = _call(state, caller, pct=None)
    assert out["pct"] is None and target.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_called_with(effective_session_key(target), None)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (AUTOCOMPACT_PCT_MAX + 1, "pct_out_of_range"),
        (AUTOCOMPACT_PCT_MIN - 1, "pct_out_of_range"),
        (True, "pct_not_a_number"),
        ("50", "pct_not_a_number"),
        (float("nan"), "pct_not_finite"),
    ],
)
def test_invalid_values_are_refused_before_the_gate(tmp_path, monkeypatch, raw, code):
    state, caller, target = _pair(tmp_path)
    gate = MagicMock()
    monkeypatch.setattr(sc, "authorize_target", gate)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=raw)
    assert exc.value.code == code and exc.value.status == 400
    gate.assert_not_called()
    assert target.autocompact_pct is None


def test_the_range_refusal_names_the_limits(tmp_path):
    state, caller, _target = _pair(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=AUTOCOMPACT_PCT_MAX + 1)
    assert f"{AUTOCOMPACT_PCT_MIN:g}" in exc.value.message
    assert f"{AUTOCOMPACT_PCT_MAX:g}" in exc.value.message


def test_a_refusal_on_the_post_persist_recheck_rolls_back(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    real = sc.authorize_target
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        # Call 1 is the up-front gate, 2 the transaction's pre-persist recheck,
        # 3 its post-persist recheck.
        if calls["n"] >= 3:
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, pct=40)
    assert exc.value.code == "linked_session_target"
    assert target.autocompact_pct is None
    state.sessions.set_autocompact_pct.assert_not_called()
    # The refusal came after the durable commit, so the commit is undone too:
    # a refused value left on disk would be restored at the next hydrate.
    meta = state.conversation_log._read_metadata(slot_history_key(target)) or {}
    assert meta.get("autocompact_pct") is None


def test_a_post_persist_refusal_restores_the_prior_durable_value(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    _call(state, caller, pct=30)
    real = sc.authorize_target
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, pct=60)
    assert target.autocompact_pct == 30.0
    meta = state.conversation_log._read_metadata(slot_history_key(target)) or {}
    assert meta.get("autocompact_pct") == 30.0


# ── The route ────────────────────────────────────────────────────────────────


def _request(state, body: dict, *, internal: bool = True):
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/autocompact"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}

    async def _json():
        return body

    request.json = _json
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )
    return request


def test_the_route_refuses_a_cookie_only_caller(tmp_path):
    state, _caller, _target = _pair(tmp_path)
    req = _request(state, {"target": "chat-2", "pct": 40}, internal=False)
    resp = asyncio.run(handlers_sc.api_session_control_autocompact(req))
    assert resp.status == 403


def test_the_route_reads_without_pct_and_writes_with_it(tmp_path):
    state, _caller, target = _pair(tmp_path)
    read = asyncio.run(
        handlers_sc.api_session_control_autocompact(_request(state, {"target": "chat-2"}))
    )
    assert read.status == 200 and json.loads(read.body.decode())["pct"] is None
    wrote = asyncio.run(
        handlers_sc.api_session_control_autocompact(
            _request(state, {"target": "chat-2", "pct": 55})
        )
    )
    assert wrote.status == 200 and target.autocompact_pct == 55.0


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/autocompact" in _STRICT_INTERNAL_API_PATHS


# ── The MCP tool ─────────────────────────────────────────────────────────────


def test_the_tool_omits_pct_to_read():
    resp = {
        "ok": True,
        "target": "chat-2",
        "pct": None,
        "global_pct": 80.0,
        "min": 5.0,
        "max": 90.0,
    }
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=resp) as post,
    ):
        out = _call_tool_inner("session_set_autocompact", {"target": "chat-2"})
    path, body = post.call_args.args
    assert path == "/api/session-control/autocompact"
    assert body == {"target": "chat-2"}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert "follows the global default (80%)" in out and "5-90" in out


def test_the_tool_sends_null_to_clear():
    resp = {"ok": True, "target": "chat-2", "pct": None, "global_pct": 80.0}
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=resp) as post,
    ):
        out = _call_tool_inner("session_set_autocompact", {"target": "chat-2", "pct": None})
    assert post.call_args.args[1] == {"target": "chat-2", "pct": None}
    assert out.startswith("Cleared")


def test_the_tool_reports_a_set():
    resp = {
        "ok": True,
        "target": "chat-2",
        "pct": 40.0,
        "global_pct": 80.0,
        "min": 5.0,
        "max": 90.0,
    }
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=resp),
    ):
        out = _call_tool_inner("session_set_autocompact", {"target": "chat-2", "pct": 40})
    assert out == "Set `chat-2`'s auto-compact threshold to 40% (allowed range 5-90)."


def test_the_tool_refuses_an_unverifiable_caller():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._post") as post,
    ):
        out = _call_tool_inner("session_set_autocompact", {"target": "chat-2", "pct": 40})
    assert out.startswith("Error")
    post.assert_not_called()


@pytest.mark.parametrize("args", [{"target": "chat-2", "pct": 40}, {"target": "chat-2"}])
def test_a_channel_caller_is_refused_at_dispatch_and_audited(args):
    """An auto-approved call never reaches the permission-prompt name match, and
    the route admits an owner-DM channel caller, so the refusal is here."""
    sel_obj = MagicMock()
    with (
        patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value="channel:slack:C1.100",
        ),
        patch("kiro_crew.mcp_dashboard._post") as post,
        patch("kiro_crew.mcp_dashboard.sel", return_value=sel_obj),
    ):
        out = _call_tool_inner("session_set_autocompact", args)
    assert out.startswith("Error:") and "channel agents" in out
    post.assert_not_called()
    sel_obj.log_tool_invocation.assert_called_once()
    assert sel_obj.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_blocked_tool"


def test_the_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "not yours"}),
    ):
        out = _call_tool_inner("session_set_autocompact", {"target": "chat-2", "pct": 40})
    assert out == "Error: could not change that session's threshold: not yours"


def test_an_unconfirmed_rollback_marks_nothing_dirty(tmp_path, monkeypatch):
    """An unconfirmed rollback restores the live field but never hands it to the
    periodic flush: that flush writes slot fields whole and skips the identity
    check for a line with no ``created_at``, so it could land on a same-key
    replacement transcript."""
    state, caller, target = _pair(tmp_path)
    log = state.conversation_log
    real = sc.authorize_target
    real_if = log.update_metadata_if
    calls = {"n": 0, "rollback": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            # The committing save has already landed; clear the flag so the
            # assertion below sees only what the rollback does.
            target._dirty = False
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    def _rollback_unconfirmed(*a, **kw):
        # Only the compensating write (prior value None, require_existing) is
        # unconfirmed; the committing save goes through normally.
        if len(a) >= 2 and a[1] == {"autocompact_pct": None} and kw.get("require_existing"):
            calls["rollback"] += 1
            return False
        return real_if(*a, **kw)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    monkeypatch.setattr(log, "update_metadata_if", _rollback_unconfirmed, raising=False)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, pct=60)
    assert calls["rollback"] >= 1
    assert target.autocompact_pct is None
    assert target._dirty is False


def test_an_unconfirmed_rollback_is_retried_before_the_refusal_returns(tmp_path, monkeypatch):
    """The dirty-slot flush skips a slot with no messages, so a transient
    unconfirmed write must be retried directly rather than left to the flush."""
    state, caller, target = _pair(tmp_path)
    key = slot_history_key(target)
    log = state.conversation_log
    real = sc.authorize_target
    real_if = log.update_metadata_if
    calls = {"n": 0, "if": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    def _first_rollback_unconfirmed(*a, **kw):
        if len(a) >= 2 and a[1] == {"autocompact_pct": None} and kw.get("require_existing"):
            calls["if"] += 1
            if calls["if"] == 1:
                return False
        return real_if(*a, **kw)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    monkeypatch.setattr(log, "update_metadata_if", _first_rollback_unconfirmed, raising=False)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, pct=60)
    assert calls["if"] == 2
    assert target.autocompact_pct is None
    assert (log._read_metadata(key) or {}).get("autocompact_pct") is None


def test_a_rollback_never_writes_into_a_same_key_replacement(tmp_path, monkeypatch):
    """A transcript deleted and recreated under the same key has a fresh
    ``created_at``; the rollback must leave that replacement's record alone."""
    state, caller, target = _pair(tmp_path)
    key = slot_history_key(target)
    log = state.conversation_log
    real = sc.authorize_target
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            # Stand in for delete+recreate landing ahead of the rollback: the
            # record at this key now belongs to another session.
            log.update_metadata_if(
                key,
                {"created_at": "2099-01-01T00:00:00Z", "autocompact_pct": 70.0},
                lambda _meta: True,
            )
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, pct=60)
    assert target.autocompact_pct is None
    assert target._dirty is False
    meta = log._read_metadata(key) or {}
    assert meta.get("created_at") == "2099-01-01T00:00:00Z"
    assert meta.get("autocompact_pct") == 70.0


def test_a_rollback_restores_a_legacy_record_with_no_created_at(tmp_path, monkeypatch):
    """A legacy or corrupt-healed metadata line carries no ``created_at``. That
    is a readable identity, not an unreadable one, so the rollback must still
    write the prior value back instead of leaving the refused one on disk."""
    state, caller, target = _pair(tmp_path)
    key = slot_history_key(target)
    log = state.conversation_log
    real = sc.authorize_target
    calls = {"n": 0}

    def _strip(meta):
        return {k: v for k, v in (meta or {}).items() if k != "created_at"}

    real_status = log.get_metadata_status
    real_if = log.update_metadata_if
    monkeypatch.setattr(
        log,
        "get_metadata_status",
        lambda k: (lambda m, ok: (_strip(m), ok))(*real_status(k)),
        raising=False,
    )
    monkeypatch.setattr(
        log,
        "update_metadata_if",
        lambda k, patch_, pred, **kw: real_if(k, patch_, lambda m: pred(_strip(m)), **kw),
        raising=False,
    )

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise sc.SessionControlError("gone", status=403, code="linked_session_target")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _flaky)
    with pytest.raises(sc.SessionControlError):
        _call(state, caller, pct=60)
    assert target.autocompact_pct is None
    assert (log._read_metadata(key) or {}).get("autocompact_pct") is None


def test_a_rollback_restores_a_divergent_alias_to_the_written_back_value():
    """An alias sibling that held a different value before the mirror must be
    restored to the value written back to disk, not its own prior, and no
    sibling is marked dirty: the guarded write-back is the only durable write."""
    from kiro_crew.dashboard.chat_handlers import _undo_committed_autocompact
    from kiro_crew.dashboard.state import _ChatSlot

    a = _ChatSlot("tab-a")
    b = _ChatSlot("tab-b")
    a.linked_session_key = "channel:shared:9"
    b.linked_session_key = "channel:shared:9"
    a.autocompact_pct = 60.0
    b.autocompact_pct = 60.0
    key = slot_history_key(a)
    state = MagicMock()
    state.conversation_log.update_metadata_if.return_value = True
    asyncio.run(
        _undo_committed_autocompact(
            state, a, "tab-a", None, [(b, 40.0)], key, 60.0, "2026-01-01T00:00:00Z"
        )
    )
    assert a.autocompact_pct is None
    assert b.autocompact_pct is None
    assert a._dirty is False
    assert b._dirty is False
    patch_ = state.conversation_log.update_metadata_if.call_args.args[1]
    assert patch_ == {"autocompact_pct": None}
