"""``session_generate_title``: regenerate a created session's title from its transcript.

The verb runs the sidebar's "Regenerate title" code
(``chat_title.generate_manual_title`` + ``chat_title.apply_regenerated_title``).
The tests cover the creator-only reach, the per-target window that keeps a loop
from spending titling calls, the re-check after the model call, what the verb
reports, the route, and the MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_title, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext

_LINK = "slack:1786300000.000100"

#: Only turns a hang into a named failure; every step finishes in milliseconds.
_HANG_CEILING_S = 60.0


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_window():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def persisted(monkeypatch) -> list[str]:
    """Record every history write as succeeding, without touching the disk."""
    seen: list[str] = []

    async def _persist(_state, slot, **_kw):
        seen.append(slot.key)
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _persist)
    return seen


def _key(slot) -> str:
    return slot_history_key(slot)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "Old title"
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    if created:
        target._created_by = caller.key
    return state, caller, target


def _model(monkeypatch, result: str = "New title", calls: list | None = None, during=None):
    """Stand in for the titling call the verb makes, optionally acting mid-call."""

    async def _gen(_state, slot):
        if calls is not None:
            calls.append(slot.key)
        if during is not None:
            during()
        return result

    monkeypatch.setattr(sc, "generate_manual_title", _gen)


def _generate(state, caller, target: str = "chat-2") -> dict:
    return asyncio.run(
        sc.generate_title_target(state, caller_session_key=_key(caller), target=target)
    )


def test_a_created_session_is_retitled_as_an_auto_title(tmp_path, monkeypatch, persisted):
    state, caller, target = _pair(tmp_path)
    calls: list[str] = []
    _model(monkeypatch, calls=calls)

    out = _generate(state, caller)

    assert out == {"ok": True, "target": "chat-2", "title": "New title", "changed": True}
    assert calls == ["chat-2"]
    assert persisted == ["chat-2"]
    assert target.title == "New title"
    assert target._title_origin == chat_title._TITLE_ORIGIN_AUTO


def test_nothing_from_the_model_reports_the_current_title(tmp_path, monkeypatch, persisted):
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch, result="")

    out = _generate(state, caller)

    assert out == {"ok": True, "target": "chat-2", "title": "Old title", "changed": False}
    assert persisted == []
    assert target.title == "Old title"


def test_a_session_the_caller_did_not_create_is_refused(tmp_path, monkeypatch, persisted):
    """An owner chat may stop any peer, but only retitle what it created.
    Mutation guard: dropping the creator check lets the model call run."""
    state, caller, target = _pair(tmp_path, created=False)
    calls: list[str] = []
    _model(monkeypatch, calls=calls)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert (err.value.code, err.value.status) == ("not_creator", 403)
    assert calls == [] and persisted == []
    assert target.title == "Old title"


def test_a_session_created_by_someone_else_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path, created=False)
    target._created_by = "chat-9"
    _model(monkeypatch)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "not_creator"


def test_the_caller_cannot_retitle_itself(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path)
    _model(monkeypatch)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller, target="chat-1")

    assert err.value.code == "self_target"


def test_a_channel_linked_target_is_refused_before_the_model_call(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    target.linked_session_key = _LINK
    calls: list[str] = []
    _model(monkeypatch, calls=calls)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "linked_session_target"
    assert calls == []


def test_a_refusal_is_audited_as_denied(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path, created=False)
    _model(monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: seen.append(kw))

    with pytest.raises(sc.SessionControlError):
        _generate(state, caller)

    assert [(r["operation"], r["outcome"], r["detail"]) for r in seen] == [
        ("generate_title", "denied", {"code": "not_creator"})
    ]


# ── The per-target window ────────────────────────────────────────────────────


def test_a_repeat_inside_the_window_is_refused_and_spends_nothing(tmp_path, monkeypatch, persisted):
    """Mutation guard: without the window the second call runs the model again."""
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []
    _model(monkeypatch, calls=calls)
    _generate(state, caller)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert (err.value.code, err.value.status) == ("title_rate_limited", 429)
    assert calls == ["chat-2"]


def test_the_window_expires(tmp_path, monkeypatch, persisted, manual_clock):
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []
    _model(monkeypatch, calls=calls)
    manual_clock.install(monkeypatch, create_rate_limit)
    _generate(state, caller)

    manual_clock.advance(create_rate_limit.WINDOW_SECS + 1)
    _generate(state, caller)

    assert calls == ["chat-2", "chat-2"]


def test_a_refused_call_does_not_start_the_window(tmp_path, monkeypatch, persisted):
    state, caller, target = _pair(tmp_path, created=False)
    _model(monkeypatch)
    with pytest.raises(sc.SessionControlError):
        _generate(state, caller)

    target._created_by = caller.key
    assert _generate(state, caller)["changed"] is True


def test_a_concurrent_second_call_is_refused_while_the_first_runs(tmp_path, monkeypatch, persisted):
    """The window is stamped before the model call, not after it."""
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []

    async def _both():
        parked = asyncio.Event()
        release = asyncio.Event()

        async def _held(_state, slot):
            calls.append(slot.key)
            parked.set()
            await release.wait()
            return "New title"

        monkeypatch.setattr(sc, "generate_manual_title", _held)
        first = asyncio.create_task(
            sc.generate_title_target(state, caller_session_key=_key(caller), target="chat-2")
        )
        await asyncio.wait_for(parked.wait(), _HANG_CEILING_S)
        try:
            with pytest.raises(sc.SessionControlError) as err:
                await sc.generate_title_target(
                    state, caller_session_key=_key(caller), target="chat-2"
                )
        finally:
            release.set()
            await asyncio.wait_for(first, _HANG_CEILING_S)
        return err.value.code

    assert asyncio.run(_both()) == "title_rate_limited"
    assert calls == ["chat-2"]


# ── The real titling code ────────────────────────────────────────────────────


def test_a_hand_set_title_is_replaced_as_the_sidebar_does(tmp_path, monkeypatch, persisted):
    """Same code as the sidebar action: a manual title does not block it, and the
    result is an auto title the background refresh may revise."""
    state, caller, target = _pair(tmp_path)
    target._titled = True
    target._title_origin = chat_title._TITLE_ORIGIN_USER

    async def _gen(_state, messages, *, session_key=""):
        assert messages[-1]["content"] == "Fix the flaky upload test"
        return "Flaky upload test fix"

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _gen)

    out = _generate(state, caller)

    assert out["title"] == "Flaky upload test fix"
    assert out["changed"] is True
    assert target._title_origin == chat_title._TITLE_ORIGIN_AUTO


# ── Changes while the model call runs ────────────────────────────────────────


def test_a_session_replaced_during_the_call_is_not_retitled(tmp_path, monkeypatch, persisted):
    """The target closes and a new session opens under its key mid-call.
    Mutation guard: without the identity check the old slot is written and the
    successor's row is pushed."""
    state, caller, target = _pair(tmp_path)
    successor = MagicMock()
    successor.title = "Successor"
    pushed: list[str] = []
    monkeypatch.setattr(state, "push_slot_title", lambda key, title, **kw: pushed.append(key))
    _model(monkeypatch, during=lambda: state._slots.__setitem__("chat-2", successor))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert (err.value.code, err.value.status) == ("target_replaced", 409)
    assert persisted == [] and pushed == []
    assert target.title == "Old title"
    assert successor.title == "Successor"


def test_a_close_begun_during_the_call_is_not_retitled(tmp_path, monkeypatch, persisted):
    """Mutation guard: without the closing check the archived line gets the title."""
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch, during=target.begin_close)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "target_replaced"
    assert persisted == []
    assert target.title == "Old title"


def test_a_target_linked_to_a_channel_during_the_call_is_not_retitled(
    tmp_path, monkeypatch, persisted
):
    """The gate is re-run after the model call. Mutation guard: without the
    re-check a session that became channel-linked mid-call is retitled."""
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch, during=lambda: setattr(target, "linked_session_key", _LINK))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "linked_session_target"
    assert persisted == []
    assert target.title == "Old title"


def test_a_rename_during_the_call_wins(tmp_path, monkeypatch, persisted):
    """Same contract as the sidebar: an explicit title outranks a generated one."""
    state, caller, target = _pair(tmp_path)

    def _rename():
        target.title = "Person's name"
        target._title_origin = chat_title._TITLE_ORIGIN_USER
        target._title_epoch += 1

    _model(monkeypatch, during=_rename)

    out = _generate(state, caller)

    assert out == {"ok": True, "target": "chat-2", "title": "Person's name", "changed": False}
    assert persisted == []
    assert target._title_origin == chat_title._TITLE_ORIGIN_USER


# ── The save ─────────────────────────────────────────────────────────────────


def test_a_failed_history_write_is_reported_not_claimed_as_success(tmp_path, monkeypatch):
    """Mutation guard: ignoring the persist result answers ok for a title a
    restart would lose."""
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch)

    async def _persist(_state, _slot, **_kw):
        return False

    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert (err.value.code, err.value.status) == ("title_persist_failed", 500)
    assert target.title == "New title", "the live slot keeps it"


def test_the_save_is_guarded_by_the_slot_still_owning_its_key(tmp_path, monkeypatch):
    """The history write re-checks identity under its lock, so a slot closed
    and reopened under the same key while the write was queued cannot stamp
    its title over the successor's saved rename.
    Mutation guard: dropping ``still_current`` from the verb's
    ``apply_regenerated_title`` call leaves the write unguarded."""
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch)
    guards: list = []

    async def _persist(_state, _slot, *, still_current=None):
        guards.append(still_current)
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    _generate(state, caller)

    (guard,) = guards
    assert guard is not None and guard() is True
    state._slots["chat-2"] = MagicMock()
    assert guard() is False


def test_a_save_refused_because_the_session_was_replaced_reports_it(tmp_path, monkeypatch):
    """A write the lock-held guard refused is not a disk failure: the session
    was replaced, so the call answers ``target_replaced``, not
    ``title_persist_failed``.
    Mutation guard: dropping the identity branch reports a write failure."""
    state, caller, _ = _pair(tmp_path)
    _model(monkeypatch)

    async def _persist(_state, _slot, *, still_current=None):
        state._slots["chat-2"] = MagicMock()
        return bool(still_current and still_current())

    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert (err.value.code, err.value.status) == ("target_replaced", 409)


def test_the_title_is_on_the_slot_before_the_save_suspends(tmp_path, monkeypatch):
    """The change happens synchronously after the re-check, as in session_rename."""
    state, caller, target = _pair(tmp_path)
    _model(monkeypatch)
    seen: list[str] = []

    async def _persist(_state, slot, **_kw):
        seen.append(slot.title)
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    _generate(state, caller)

    assert seen == ["New title"]


def test_a_session_replaced_during_the_save_is_not_pushed(tmp_path, monkeypatch):
    """The successor opened under the same key must not show this title.
    Mutation guard: dropping the identity check in ``apply_regenerated_title``
    pushes the title onto the successor's row."""
    state, caller, _ = _pair(tmp_path)
    _model(monkeypatch)
    successor = MagicMock()
    pushed: list[str] = []
    monkeypatch.setattr(state, "push_slot_title", lambda key, title, **kw: pushed.append(key))

    async def _persist(_state, _slot, **_kw):
        state._slots["chat-2"] = successor
        return True

    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    _generate(state, caller)

    assert pushed == []


def test_a_completed_call_pushes_the_title(tmp_path, monkeypatch, persisted):
    state, caller, _ = _pair(tmp_path)
    _model(monkeypatch)
    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "push_slot_title", lambda key, title, **kw: pushed.append((key, title))
    )

    _generate(state, caller)

    assert pushed == [("chat-2", "New title")]


def test_an_allowed_call_writes_its_audit_record(tmp_path, monkeypatch, persisted):
    state, caller, _ = _pair(tmp_path)
    _model(monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: seen.append(kw))

    _generate(state, caller)

    assert [r for r in seen if r.get("outcome") == "allowed"] == [
        {
            "caller_session_key": _key(caller),
            "operation": "generate_title",
            "slot_key": "chat-2",
            "outcome": "allowed",
            "detail": {"applied": True},
        }
    ]


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body: dict):
    state, caller, target = _pair(tmp_path)
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/generate-title"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request, target


def test_route_without_the_secret_is_forbidden(tmp_path, monkeypatch):
    calls: list[str] = []
    _model(monkeypatch, calls=calls)
    req, _ = _request(tmp_path, internal=False, body={"target": "chat-2"})
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 403
    assert calls == []


def test_route_returns_the_verb_result(tmp_path, monkeypatch, persisted):
    _model(monkeypatch)
    req, target = _request(tmp_path, internal=True, body={"target": "chat-2"})
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 200
    assert json.loads(resp.body)["title"] == "New title"
    assert target.title == "New title"


def test_route_renders_the_window_as_429(tmp_path, monkeypatch, persisted):
    _model(monkeypatch)
    req, _ = _request(tmp_path, internal=True, body={"target": "chat-2"})
    asyncio.run(handlers_sc.api_session_control_generate_title(req))
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "title_rate_limited"


def test_the_route_is_registered_strict_internal():
    """An unlisted session-control path falls through to cookie auth, and the
    MCP caller's secret is then ignored in production."""
    from kiro_crew.dashboard import server

    assert "/api/session-control/generate-title" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = Caller.strict("dashboard:chat-verified")


def _call(reply: object, caller: Caller = _VERIFIED) -> tuple[str, list[DashboardRequest]]:
    dash = InMemoryDashboardClient({"POST /api/session-control/generate-title": reply})
    out = TABLE.call("session_generate_title", {"target": "chat-2"}, ToolContext(dash, caller))
    return out, [r for r in dash.requests if r.method != "GET"]


def test_tool_carries_the_verified_key_and_reports_the_new_title():
    out, writes = _call({"ok": True, "target": "chat-2", "title": "New title", "changed": True})
    (req,) = writes
    assert (req.method, req.path) == ("POST", "/api/session-control/generate-title")
    assert req.body == {"target": "chat-2"}
    assert req.session_key == "dashboard:chat-verified"
    assert out == "Retitled `chat-2` to 'New title'."


def test_tool_waits_longer_than_the_default_for_the_model_call():
    """Mutation guard: the default 30 s wait would time out a slow titling call
    the route still completes."""
    from kiro_crew import mcp_dashboard

    assert mcp_dashboard.GENERATE_TITLE_TIMEOUT_SECS > 30
    assert mcp_dashboard.GENERATE_TITLE_TIMEOUT_SECS < create_rate_limit.WINDOW_SECS


def test_tool_reports_an_unchanged_title():
    out, _ = _call({"ok": True, "target": "chat-2", "title": "Old title", "changed": False})
    assert "keeps its title 'Old title'" in out


def test_tool_reports_a_refusal():
    out, _ = _call({"error": "try again later", "code": "title_rate_limited"})
    assert out.startswith("Error: could not generate a title for that session: ")


def test_tool_refuses_an_unverifiable_caller_without_a_request():
    dash = InMemoryDashboardClient({"POST /api/session-control/generate-title": {"ok": True}})
    out = TABLE.call(
        "session_generate_title",
        {"target": "chat-2"},
        ToolContext(dash, Caller.unverified("", "no session")),
    )
    assert out.startswith("Error:")
    assert list(dash.requests) == []


# ── The sidebar route keeps its behaviour ────────────────────────────────────


def _sidebar_request(state, name: str):
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"slot": name}
    request.get = lambda key, default=None: default
    return request


def test_the_sidebar_route_still_regenerates_through_the_shared_helper(
    tmp_path, monkeypatch, persisted
):
    state, _, target = _pair(tmp_path)

    async def _gen(_state, messages, *, session_key=""):
        return "Flaky upload test fix"

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _gen)

    resp = asyncio.run(chat_title.api_chat_slot_generate_title(_sidebar_request(state, "chat-2")))

    assert json.loads(resp.body) == {"ok": True, "title": "Flaky upload test fix"}
    assert target.title == "Flaky upload test fix"
    assert persisted == ["chat-2"]


def test_the_sidebar_route_does_not_push_onto_a_successor(tmp_path, monkeypatch):
    state, _, _ = _pair(tmp_path)
    successor = MagicMock()
    pushed: list[str] = []
    monkeypatch.setattr(state, "push_slot_title", lambda key, title, **kw: pushed.append(key))

    async def _gen(_state, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(_state, _slot, **_kw):
        state._slots["chat-2"] = successor
        return True

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _gen)
    monkeypatch.setattr(chat_title, "_persist_title", _persist)

    asyncio.run(chat_title.api_chat_slot_generate_title(_sidebar_request(state, "chat-2")))

    assert pushed == []
