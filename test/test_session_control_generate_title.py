"""``session_generate_title``: regenerate a created session's title from its transcript.

The verb runs ``chat_title.regenerate_title``, the sidebar's "Regenerate title"
code. The tests cover the creator-only reach, the per-target cooldown that keeps
a loop from spending titling calls, what the verb reports, the route, and the
MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_title, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _key(slot) -> str:
    return slot_history_key(slot)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.title = "Old title"
    if created:
        target._created_by = caller.key
    return state, caller, target


def _fake_regenerate(result: str, calls: list | None = None):
    async def _regen(state, slot, **_kw):
        if calls is not None:
            calls.append(slot.key)
        if result:
            slot.title = result
        return chat_title.RegeneratedTitle(result, True)

    return _regen


def _generate(state, caller, target: str = "chat-2") -> dict:
    return asyncio.run(
        sc.generate_title_target(state, caller_session_key=_key(caller), target=target)
    )


def test_a_created_session_is_retitled(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title", calls))

    out = _generate(state, caller)

    assert out == {"ok": True, "target": "chat-2", "title": "New title", "changed": True}
    assert calls == ["chat-2"]
    assert target.title == "New title"


def test_nothing_applied_reports_the_current_title_unchanged(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path)
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate(""))

    out = _generate(state, caller)

    assert out == {"ok": True, "target": "chat-2", "title": "Old title", "changed": False}


def test_a_session_the_caller_did_not_create_is_refused(tmp_path, monkeypatch):
    """An owner chat may stop any peer, but only retitle what it created.
    Mutation guard: dropping the creator check lets the model call run."""
    state, caller, target = _pair(tmp_path, created=False)
    calls: list[str] = []
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title", calls))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "not_creator"
    assert err.value.status == 403
    assert calls == []
    assert target.title == "Old title"


def test_a_session_created_by_someone_else_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path, created=False)
    target._created_by = "chat-9"
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title"))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "not_creator"


def test_the_caller_cannot_retitle_itself(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path)
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title"))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller, target="chat-1")

    assert err.value.code == "self_target"


def test_a_channel_linked_target_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    target.linked_session_key = "slack:1786300000.000100"
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title"))

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "linked_session_target"


def test_a_repeat_inside_the_window_is_refused_and_spends_nothing(tmp_path, monkeypatch):
    """Mutation guard: without the cooldown the second call runs the model again."""
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title", calls))
    _generate(state, caller)

    with pytest.raises(sc.SessionControlError) as err:
        _generate(state, caller)

    assert err.value.code == "title_rate_limited"
    assert err.value.status == 429
    assert calls == ["chat-2"]


def test_the_window_expires(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title", calls))
    clock = [1000.0]
    monkeypatch.setattr(create_rate_limit.time, "monotonic", lambda: clock[0])
    _generate(state, caller)

    clock[0] += create_rate_limit.WINDOW_SECS + 1
    _generate(state, caller)

    assert calls == ["chat-2", "chat-2"]


def test_a_refused_call_does_not_start_the_window(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path, created=False)
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title"))
    with pytest.raises(sc.SessionControlError):
        _generate(state, caller)

    target._created_by = caller.key
    assert _generate(state, caller)["changed"] is True


def test_a_concurrent_second_call_is_refused_while_the_first_runs(tmp_path, monkeypatch):
    """The window is stamped before the model call, not after it."""
    state, caller, _ = _pair(tmp_path)
    calls: list[str] = []

    async def _slow(state, slot, **_kw):
        calls.append(slot.key)
        await asyncio.sleep(0.05)
        return chat_title.RegeneratedTitle("New title", True)

    monkeypatch.setattr(sc, "regenerate_title", _slow)

    async def _both():
        return await asyncio.gather(
            sc.generate_title_target(state, caller_session_key=_key(caller), target="chat-2"),
            sc.generate_title_target(state, caller_session_key=_key(caller), target="chat-2"),
            return_exceptions=True,
        )

    results = asyncio.run(_both())

    assert calls == ["chat-2"]
    errors = [r for r in results if isinstance(r, sc.SessionControlError)]
    assert [e.code for e in errors] == ["title_rate_limited"]


def test_a_hand_set_title_is_replaced_as_the_sidebar_does(tmp_path):
    """Same core as the sidebar action: a manual title does not block it, and the
    result is an auto title the background refresh may revise."""
    state, caller, target = _pair(tmp_path)
    target.messages = [
        {"role": "user", "content": "Fix the flaky upload test"},
        {"role": "assistant", "content": "Looking at the upload test now."},
    ]
    target._titled = True
    target._title_origin = chat_title._TITLE_ORIGIN_USER

    async def _gen(state, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state, slot, **_kw):
        return True

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
    ):
        out = _generate(state, caller)

    assert out["title"] == "Flaky upload test fix"
    assert out["changed"] is True
    assert target._title_origin == chat_title._TITLE_ORIGIN_AUTO


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body):
    state, caller, _ = _pair(tmp_path)
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
    return request


def test_route_without_the_secret_is_forbidden(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title", calls))
    req = _request(tmp_path, internal=False, body={"target": "chat-2"})
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 403
    assert calls == []


def test_route_returns_the_verb_result(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "regenerate_title", _fake_regenerate("New title"))
    req = _request(tmp_path, internal=True, body={"target": "chat-2"})
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 200
    assert json.loads(resp.body)["title"] == "New title"


def test_route_renders_the_cooldown_as_429(tmp_path, monkeypatch):
    async def _limited(*_a, **_kw):
        raise sc.SessionControlError("wait", status=429, code="title_rate_limited")

    monkeypatch.setattr(sc, "generate_title_target", _limited)
    req = _request(tmp_path, internal=True, body={"target": "chat-2"})
    resp = asyncio.run(handlers_sc.api_session_control_generate_title(req))
    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "title_rate_limited"


def test_the_route_is_strict_internal():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/generate-title" in _STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


def test_tool_carries_the_verified_key_and_reports_the_new_title():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"ok": True, "target": "chat-2", "title": "New title", "changed": True},
        ) as post,
    ):
        out = _call_tool_inner("session_generate_title", {"target": "chat-2"})
    assert post.call_args.args[0] == "/api/session-control/generate-title"
    assert post.call_args.args[1] == {"target": "chat-2"}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert post.call_args.kwargs["timeout"] > 30, "the route waits for a model call"
    assert out == "Retitled `chat-2` to 'New title'."


def test_tool_reports_an_unchanged_title():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"ok": True, "target": "chat-2", "title": "Old title", "changed": False},
        ),
    ):
        out = _call_tool_inner("session_generate_title", {"target": "chat-2"})
    assert "keeps its title 'Old title'" in out


def test_tool_reports_a_refusal():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "try again in 90s"}),
    ):
        out = _call_tool_inner("session_generate_title", {"target": "chat-2"})
    assert out.startswith("Error: could not generate a title for that session: ")


def test_a_session_replaced_during_the_call_is_not_retitled(tmp_path):
    """The target closes and a new session opens under its key mid-call: neither
    the old slot's history nor the successor's sidebar row gets the title.
    Mutation guard: without the identity check the successor's row is pushed."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    successor = MagicMock()
    successor.title = "Successor"
    persisted: list[str] = []
    pushed: list[str] = []

    async def _gen(state_, messages, *, session_key=""):
        state._slots["chat-2"] = successor
        return "Flaky upload test fix"

    async def _persist(state_, slot, **_kw):
        persisted.append(slot.key)
        return True

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
        patch.object(state, "push_slot_title", lambda key, title, **kw: pushed.append(key)),
    ):
        with pytest.raises(sc.SessionControlError) as err:
            _generate(state, caller)

    assert err.value.code == "target_replaced"
    assert persisted == [] and pushed == []
    assert target.title == "Old title"
    assert successor.title == "Successor"


def test_a_target_linked_to_a_channel_during_the_call_is_not_retitled(tmp_path):
    """The gate is re-run after the model call. Mutation guard: without the
    re-check a session that became channel-linked mid-call is retitled."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    persisted: list[str] = []

    async def _gen(state_, messages, *, session_key=""):
        target.linked_session_key = "slack:1786300000.000100"
        return "Flaky upload test fix"

    async def _persist(state_, slot, **_kw):
        persisted.append(slot.key)
        return True

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
    ):
        with pytest.raises(sc.SessionControlError) as err:
            _generate(state, caller)

    assert err.value.code == "linked_session_target"
    assert persisted == []
    assert target.title == "Old title"


def test_a_failed_history_write_is_reported_not_claimed_as_success(tmp_path):
    """Mutation guard: ignoring the persist result answers ok for a title a
    restart would lose."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, **_kw):
        return False

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
    ):
        with pytest.raises(sc.SessionControlError) as err:
            _generate(state, caller)

    assert err.value.code == "title_persist_failed"
    assert err.value.status == 500
    assert target.title == "Flaky upload test fix", "the live slot keeps it"


def test_a_session_replaced_during_the_write_is_not_published(tmp_path):
    """The slot is closed and recreated under its key while the history write
    runs: the write guard refuses and the successor's row is not pushed."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    successor = MagicMock()
    pushed: list[str] = []
    guard_verdicts: list[bool] = []

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, *, still_current=None, title_fields=None):
        state._slots["chat-2"] = successor
        guard_verdicts.append(still_current())
        return False

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
        patch.object(state, "push_slot_title", lambda key, title, **kw: pushed.append(key)),
    ):
        with pytest.raises(sc.SessionControlError) as err:
            _generate(state, caller)

    assert guard_verdicts == [False]
    assert pushed == []
    assert err.value.code == "target_replaced"


def test_a_close_during_the_write_archives_the_previous_title(tmp_path):
    """A close that begins while the history write runs saves the slot as it
    finds it. The live title must still be the old one at that point, the write
    must be refused, and the call must not report the title as applied.
    Mutation guard: setting ``slot.title`` before the write lets the close's
    archival save keep the title the call then reports as discarded."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    pushed: list[str] = []
    archived_titles: list[str] = []
    guard_verdicts: list[bool] = []

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, *, still_current=None, title_fields=None):
        # A second caller's close starts here; its archival save reads the
        # slot's title under the transcript lock.
        slot.begin_close()
        archived_titles.append(slot.title)
        guard_verdicts.append(still_current())
        return False

    try:
        with (
            patch.object(chat_title, "_generate_title_via_kiro", _gen),
            patch.object(chat_title, "_persist_title", _persist),
            patch.object(state, "push_slot_title", lambda key, title, **kw: pushed.append(key)),
        ):
            with pytest.raises(sc.SessionControlError) as err:
                _generate(state, caller)
    finally:
        target.cancel_close()

    assert archived_titles == ["Old title"]
    assert guard_verdicts == [False]
    assert pushed == []
    assert err.value.code == "target_replaced"
    assert target.title == "Old title"


def test_a_close_begun_during_generation_is_not_retitled(tmp_path):
    """Mutation guard: without the closing check the title is written into a
    session whose close is already under way."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    persisted: list[str] = []

    async def _gen(state_, messages, *, session_key=""):
        target.begin_close()
        return "Flaky upload test fix"

    async def _persist(state_, slot, **_kw):
        persisted.append(slot.key)
        return True

    try:
        with (
            patch.object(chat_title, "_generate_title_via_kiro", _gen),
            patch.object(chat_title, "_persist_title", _persist),
        ):
            with pytest.raises(sc.SessionControlError) as err:
                _generate(state, caller)
    finally:
        target.cancel_close()

    assert err.value.code == "target_replaced"
    assert persisted == []
    assert target.title == "Old title"


def test_the_write_carries_the_pending_title_not_the_live_one(tmp_path):
    """``title_fields`` is what reaches the history line; the slot is untouched."""
    state, _, target = _pair(tmp_path)
    log = MagicMock()
    written: list[dict] = []

    def _update(key, fields, guard, **_kw):
        written.append(dict(fields))
        return guard({})

    log.update_metadata_if.side_effect = _update
    state.conversation_log = log
    pending = {"title": "New name", "title_origin": "auto", "title_low_signal": False}

    ok = asyncio.run(chat_title._persist_title(state, target, title_fields=pending))

    assert ok is True
    assert written[0]["title"] == "New name"
    assert written[0]["title_origin"] == "auto"
    assert target.title == "Old title"


def test_the_write_guard_refuses_the_commit(tmp_path):
    """``still_current`` runs inside the locked guard, so a False leaves the
    history line untouched."""
    state, _, target = _pair(tmp_path)
    log = MagicMock()
    seen: list[bool] = []

    def _update(key, fields, guard, **_kw):
        seen.append(guard({}))
        return seen[-1]

    log.update_metadata_if.side_effect = _update
    state.conversation_log = log

    ok = asyncio.run(chat_title._persist_title(state, target, still_current=lambda: False))

    assert ok is False
    assert seen == [False]


def test_a_mirror_link_added_during_the_write_refuses_the_commit(tmp_path):
    """The access re-check runs inside the locked write guard, not only before
    the write. Mutation guard: a guard that checks liveness alone commits and
    publishes the title of a session that became channel-linked mid-write."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    pushed: list[str] = []
    guard_verdicts: list[bool] = []

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, *, still_current=None, title_fields=None):
        target.linked_session_key = "slack:1786300000.000100"
        guard_verdicts.append(still_current())
        return guard_verdicts[-1]

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
        patch.object(state, "push_slot_title", lambda key, title, **kw: pushed.append(key)),
    ):
        with pytest.raises(sc.SessionControlError) as err:
            _generate(state, caller)

    assert guard_verdicts == [False]
    assert err.value.code == "linked_session_target"
    assert pushed == []
    assert target.title == "Old title"


def test_a_close_begun_after_the_commit_restores_the_previous_title(tmp_path):
    """The write commits, then a close begins before the call returns. The call
    discards the title, so it writes the slot's own (previous) title back: a
    close that aborts must not leave the discarded title on disk. Mutation
    guard: without the restore write the history line keeps the new title
    while the live session shows the old one."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    writes: list[dict | None] = []
    pushed: list[str] = []

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, *, still_current=None, title_fields=None):
        allowed = still_current() if still_current is not None else True
        writes.append(dict(title_fields) if title_fields else {"title": slot.title})
        if title_fields is not None:
            # Committed; a second caller's close starts right after.
            slot.begin_close()
        return allowed

    try:
        with (
            patch.object(chat_title, "_generate_title_via_kiro", _gen),
            patch.object(chat_title, "_persist_title", _persist),
            patch.object(state, "push_slot_title", lambda key, title, **kw: pushed.append(key)),
        ):
            with pytest.raises(sc.SessionControlError) as err:
                _generate(state, caller)
    finally:
        target.cancel_close()

    assert [w["title"] for w in writes] == ["Flaky upload test fix", "Old title"]
    assert err.value.code == "target_replaced"
    assert pushed == []
    assert target.title == "Old title"


def test_a_save_landing_between_commit_and_publish_is_overwritten(tmp_path):
    """An ordinary save of the slot can take the transcript lock after the title
    write commits and before the slot shows the title; it writes the slot's
    previous title. The call writes the slot's own values once more after
    publishing, so the history line ends on the new title. Mutation guard:
    without that write the line keeps the previous title while the call answers
    success."""
    state, caller, target = _pair(tmp_path)
    target.messages = [{"role": "user", "content": "Fix the flaky upload test"}]
    disk: list[str] = []

    async def _gen(state_, messages, *, session_key=""):
        return "Flaky upload test fix"

    async def _persist(state_, slot, *, still_current=None, title_fields=None):
        if still_current is not None and not still_current():
            return False
        disk.append(title_fields["title"] if title_fields else slot.title)
        if title_fields is not None:
            # An ordinary flush queued on the lock lands right after our commit
            # and reads the slot's title, which is still the previous one.
            disk.append(slot.title)
        return True

    with (
        patch.object(chat_title, "_generate_title_via_kiro", _gen),
        patch.object(chat_title, "_persist_title", _persist),
        patch.object(state, "push_slot_title", lambda key, title, **kw: None),
    ):
        out = _generate(state, caller)

    assert out["title"] == "Flaky upload test fix"
    assert disk[-1] == "Flaky upload test fix"
    assert target.title == "Flaky upload test fix"
