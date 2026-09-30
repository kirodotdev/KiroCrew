"""``session_set_project``: set the project directory of a session the caller created.

The verb writes the target's project through the same path check and commit the
in-turn ``set_project`` directive uses, and arms the deferred reset so the target
cold-starts in the new CWD at its next turn boundary. Its reach is narrower than
the other session verbs: only an unpinned, idle session the caller created.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE, _list_tools
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _no_recent_projects(_floor_monkeypatch):
    """Keep the best-effort recent-projects write off the real home directory."""
    saved: list[str] = []
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._save_recent_project", lambda p: saved.append(p)
    )
    return saved


def _key(slot) -> str:
    return slot_history_key(slot)


def _pair(tmp_path, *, created: bool = True):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    if created:
        target._created_by = caller.key
    return state, caller, target


def _set(state, caller, target: str = "chat-2", *, path: str = "") -> dict:
    return asyncio.run(
        sc.set_project_target(state, caller_session_key=_key(caller), target=target, path=path)
    )


def _refused(state, caller, **kw) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, **kw)
    return exc.value


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


# ── the write ────────────────────────────────────────────────────────────────


def test_a_created_idle_target_takes_the_project_and_arms_the_reset(tmp_path, _no_recent_projects):
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()

    out = _set(state, caller, path=str(project))

    real = str(project.resolve())
    assert out == {"ok": True, "target": "chat-2", "project": real, "changed": True}
    assert target.project == real
    assert target._pending_reset_history_key == effective_session_key(target)
    assert _no_recent_projects == [real]
    assert caller.project != real, "the caller's own project is untouched"


def test_a_change_is_persisted_before_it_is_acknowledged(tmp_path, monkeypatch):
    """An idle target may not save again before a restart. Mutation guard:
    dropping the forced save leaves the acknowledged project only in memory."""
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()
    saves: list[tuple[str, dict]] = []

    async def _save(_state, slot, *a, **kw):
        saves.append((slot.project, kw))
        return True

    monkeypatch.setattr(sc, "save_slot_off_loop", _save)

    _set(state, caller, path=str(project))

    assert len(saves) == 1
    saved_project, kw = saves[0]
    assert saved_project == str(project.resolve())
    assert kw["force"] is True
    assert kw["best_effort"] is False
    assert kw["expected_history_key"] == _key(target)
    assert kw["expected_slot_name"] == "chat-2"


def test_an_unchanged_project_is_not_saved(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    target.project = str(tmp_path.resolve())
    saves: list[str] = []

    async def _save(_state, slot, *a, **kw):
        saves.append(slot.project)
        return True

    monkeypatch.setattr(sc, "save_slot_off_loop", _save)

    assert _set(state, caller, path=str(tmp_path))["changed"] is False
    assert saves == []


def test_a_refused_save_rolls_the_change_back(tmp_path, monkeypatch):
    """A session deleted or rebound mid-persist keeps its old project and no
    reset, and the caller is told nothing changed."""
    state, caller, target = _pair(tmp_path)
    target.project = "/before"
    project = tmp_path / "wt"
    project.mkdir()

    async def _refused_save(*_a, **_kw):
        return False

    monkeypatch.setattr(sc, "save_slot_off_loop", _refused_save)

    exc = _refused(state, caller, path=str(project))

    assert (exc.code, exc.status) == ("session_gone", 409)
    assert target.project == "/before"
    assert target._pending_reset_history_key is None
    assert target._dirty is True


@pytest.mark.parametrize(
    "save_outcome", [False, OSError("disk is full")], ids=["refused", "failed"]
)
def test_a_save_failure_after_reset_consumption_keeps_the_new_project(
    tmp_path, monkeypatch, save_outcome
):
    state, caller, target = _pair(tmp_path)
    target.project = "/before"
    project = tmp_path / "wt"
    project.mkdir()

    async def _consume_reset_then_fail(_state, slot, *a, **kw):
        slot._pending_reset_history_key = None
        if isinstance(save_outcome, Exception):
            raise save_outcome
        return save_outcome

    monkeypatch.setattr(sc, "save_slot_off_loop", _consume_reset_then_fail)

    exc = _refused(state, caller, path=str(project))

    expected = "persist_failed" if isinstance(save_outcome, Exception) else "session_gone"
    assert exc.code == expected
    assert "already started on the new project" in exc.message
    assert "will be retried" in exc.message
    assert target.project == str(project.resolve())
    assert target._pending_reset_history_key is None
    assert target._dirty is True


def test_a_failed_save_rolls_the_change_back(tmp_path, monkeypatch):
    """A lock or I/O failure must not be acknowledged as a landed change."""
    state, caller, target = _pair(tmp_path)
    target.project = "/before"
    project = tmp_path / "wt"
    project.mkdir()

    async def _failing_save(*_a, **_kw):
        raise OSError("disk is full")

    monkeypatch.setattr(sc, "save_slot_off_loop", _failing_save)

    exc = _refused(state, caller, path=str(project))

    assert (exc.code, exc.status) == ("persist_failed", 503)
    assert target.project == "/before"
    assert target._pending_reset_history_key is None
    assert target._dirty is True


def _audits(monkeypatch) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: seen.append(kw))
    return seen


def test_a_cancel_during_the_save_still_records_the_decision(tmp_path, monkeypatch):
    """A gateway shutdown can cancel the handler after the commit. The committed
    project must still carry a terminal SEL record, and the slot must be left
    for the dirty flush. Mutation guard: without the post-commit handler the
    CancelledError leaves no audit line at all."""
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()
    audits = _audits(monkeypatch)

    async def _cancelled_save(*_a, **_kw):
        raise asyncio.CancelledError

    monkeypatch.setattr(sc, "save_slot_off_loop", _cancelled_save)

    with pytest.raises(asyncio.CancelledError):
        _set(state, caller, path=str(project))

    real = str(project.resolve())
    assert target.project == real
    assert target._dirty is True
    assert [(a["operation"], a["outcome"]) for a in audits] == [("set_project", "allowed")]
    assert audits[0]["detail"] == {
        "project": real,
        "changed": True,
        "interrupted": "CancelledError",
    }


def test_a_cancel_during_the_recent_projects_write_still_records_the_decision(
    tmp_path, monkeypatch
):
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()
    audits = _audits(monkeypatch)

    async def _saved(*_a, **_kw):
        return True

    async def _cancelled_recent(_project):
        raise asyncio.CancelledError

    monkeypatch.setattr(sc, "save_slot_off_loop", _saved)
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_directive_apply.save_recent_project", _cancelled_recent
    )

    with pytest.raises(asyncio.CancelledError):
        _set(state, caller, path=str(project))

    assert [(a["outcome"], a["detail"].get("interrupted")) for a in audits] == [
        ("allowed", "CancelledError")
    ]


def test_a_refusal_after_the_commit_is_audited_once_as_denied(tmp_path, monkeypatch):
    """The rollback paths raise SessionControlError, which `_audit_denials`
    records; the post-commit handler must not add a second record."""
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()
    audits = _audits(monkeypatch)

    async def _refused_save(*_a, **_kw):
        return False

    monkeypatch.setattr(sc, "save_slot_off_loop", _refused_save)

    _refused(state, caller, path=str(project))

    assert [a["outcome"] for a in audits] == ["denied"]


def test_a_refused_save_keeps_a_newer_writers_project(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()

    async def _newer_write_then_refuse(_state, slot, *a, **kw):
        slot.project = "/newer"
        return False

    monkeypatch.setattr(sc, "save_slot_off_loop", _newer_write_then_refuse)

    _refused(state, caller, path=str(project))

    assert target.project == "/newer"


def test_a_refused_save_keeps_a_same_text_write_from_another_writer(tmp_path, monkeypatch):
    """The dashboard's project route writes ``slot.project`` under ``slot._lock``,
    which this verb does not hold. If it writes the SAME path while the save
    awaits, that write arms no reset, so a value compare cannot tell it from this
    call's commit. Mutation guard: a value-equality rollback reverts the other
    writer's acknowledged change to ``/before``."""
    state, caller, target = _pair(tmp_path)
    target.project = "/before"
    project = tmp_path / "wt"
    project.mkdir()
    real = str(project.resolve())

    async def _same_text_write_then_refuse(_state, slot, *a, **kw):
        slot.project = real[:1] + real[1:]  # equal text, a distinct object
        return False

    monkeypatch.setattr(sc, "save_slot_off_loop", _same_text_write_then_refuse)

    exc = _refused(state, caller, path=str(project))

    assert target.project == real
    assert target._dirty is True
    assert exc.code == "session_gone"
    assert "already on the new project" in exc.message


def test_the_same_project_again_changes_nothing_and_arms_no_reset(tmp_path):
    state, caller, target = _pair(tmp_path)
    project = tmp_path / "wt"
    project.mkdir()
    target.project = str(project.resolve())

    out = _set(state, caller, path=str(project))

    assert out["changed"] is False
    assert target._pending_reset_history_key is None


# ── scope ────────────────────────────────────────────────────────────────────


def test_a_session_the_caller_did_not_create_is_refused(tmp_path):
    """The person's own sessions have no creator; the fence must hold even when
    the global switch lets an unfenced caller reach them for the other verbs.
    Mutation guard: relying on authorize_target's fence alone lets this through."""
    state, caller, target = _pair(tmp_path, created=False)
    target.project = "/before"

    exc = _refused(state, caller, path=str(tmp_path))

    assert exc.code == "not_creator"
    assert target.project == "/before"
    assert target._pending_reset_history_key is None


def test_a_session_another_agent_created_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path, created=False)
    target._created_by = "chat-99"

    assert _refused(state, caller, path=str(tmp_path)).code == "not_creator"


def test_a_pinned_session_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.pinned = True

    exc = _refused(state, caller, path=str(tmp_path))

    assert exc.code == "pinned_target"
    assert target._pending_reset_history_key is None


def test_a_closed_session_is_not_found(tmp_path):
    """Only open sessions resolve; an archived one is gone from the live set."""
    state, caller, _ = _pair(tmp_path)
    state._slots.pop("chat-2")

    assert _refused(state, caller, path=str(tmp_path)).code == "target_not_found"


def test_the_caller_cannot_target_itself(tmp_path):
    """Its own project goes through set_project, which resets at its own boundary."""
    state, caller, _ = _pair(tmp_path)

    assert _refused(state, caller, target="chat-1", path=str(tmp_path)).code == "self_target"


def test_a_channel_linked_target_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.linked_session_key = "slack:1786300000.000100"

    assert _refused(state, caller, path=str(tmp_path)).code == "linked_session_target"


def test_a_relay_archive_target_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.executor = "remote"

    assert _refused(state, caller, path=str(tmp_path)).code == "relay_archive_read_only"


# ── busy ─────────────────────────────────────────────────────────────────────


def test_a_target_with_a_turn_in_flight_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    _busy(target)

    exc = _refused(state, caller, path=str(tmp_path))

    assert exc.code == "target_busy"
    assert "stop" in exc.message.lower() and "wait" in exc.message.lower()
    assert target._pending_reset_history_key is None


def test_a_target_with_queued_messages_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target._queue.append({"id": "q1", "content": "next"})

    assert _refused(state, caller, path=str(tmp_path)).code == "target_busy"


def test_a_target_held_by_a_switch_handler_is_refused(tmp_path):
    """A model/agent/workspace switch holds the slot lock across its own reset."""
    state, caller, target = _pair(tmp_path)

    async def _run() -> None:
        async with target._lock:
            await sc.set_project_target(
                state, caller_session_key=_key(caller), target="chat-2", path=str(tmp_path)
            )

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(_run())
    assert exc.value.code == "target_busy"


def test_a_target_with_attached_subagents_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)

    async def _children(*_a, **_kw):
        return True

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.subagents_attached_async", _children)

    assert _refused(state, caller, path=str(tmp_path)).code == "target_busy"
    assert target._pending_reset_history_key is None


def test_attached_subagents_are_rechecked_after_the_metadata_lock(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    prior_project = target.project
    probes = 0

    async def _children(*_a, **_kw):
        nonlocal probes
        probes += 1
        return probes == 2

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.subagents_attached_async", _children)

    assert _refused(state, caller, path=str(tmp_path)).code == "target_busy"
    assert probes == 2
    assert target.project == prior_project
    assert target._pending_reset_history_key is None


def test_a_turn_starting_while_the_path_resolves_is_refused(tmp_path, monkeypatch):
    """The idle check re-runs after the awaits. Mutation guard: dropping the
    second check commits a reset under a turn that just started."""
    state, caller, target = _pair(tmp_path)

    async def _turn_starts_meanwhile(*_a, **_kw):
        _busy(target)
        return False

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.subagents_attached_async", _turn_starts_meanwhile
    )

    assert _refused(state, caller, path=str(tmp_path)).code == "target_busy"
    assert target._pending_reset_history_key is None


def test_a_target_pinned_while_the_path_resolves_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)

    async def _pinned_meanwhile(*_a, **_kw):
        target.pinned = True
        return False

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.subagents_attached_async", _pinned_meanwhile
    )

    assert _refused(state, caller, path=str(tmp_path)).code == "pinned_target"
    assert target._pending_reset_history_key is None


def test_a_target_linked_while_the_path_resolves_is_refused(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)

    async def _link_meanwhile(*_a, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return False

    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.subagents_attached_async", _link_meanwhile)

    assert _refused(state, caller, path=str(tmp_path)).code == "linked_session_target"


# ── the path ─────────────────────────────────────────────────────────────────


def test_a_sensitive_path_is_refused_with_403(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.security.sensitive_path_refusal",
        lambda p: "sensitive path" if p.endswith(".ssh") else None,
    )
    secret = tmp_path / ".ssh"
    secret.mkdir()

    exc = _refused(state, caller, path=str(secret))

    assert (exc.code, exc.status) == ("sensitive_path", 403)
    assert target.project != str(secret)


def test_a_symlink_to_a_sensitive_path_is_refused(tmp_path, monkeypatch):
    """The check runs on the realpath too, the same as set_project."""
    state, caller, _ = _pair(tmp_path)
    real = tmp_path / ".aws"
    real.mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(real)
    monkeypatch.setattr(
        "kiro_crew.security.sensitive_path_refusal",
        lambda p: "sensitive path" if p.endswith(".aws") else None,
    )

    assert _refused(state, caller, path=str(link)).code == "sensitive_path"


def test_a_missing_directory_is_refused_with_400(tmp_path):
    state, caller, target = _pair(tmp_path)

    exc = _refused(state, caller, path=str(tmp_path / "nope"))

    assert (exc.code, exc.status) == ("not_a_directory", 400)
    assert target._pending_reset_history_key is None


def test_a_data_home_overlap_is_refused(tmp_path, monkeypatch):
    state, caller, _ = _pair(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.sandbox.voice_runtime_workspace_conflict", lambda _p: "overlaps the data home"
    )

    exc = _refused(state, caller, path=str(tmp_path))

    assert exc.code == "workspace_overlaps_data_home"
    assert "overlaps the data home" in exc.message


def test_an_unreachable_target_learns_nothing_about_the_path(tmp_path, monkeypatch):
    """The path is resolved only after the gates, so a refused caller cannot use
    the verb as a filesystem existence probe."""
    state, caller, _ = _pair(tmp_path, created=False)
    probed: list[str] = []

    async def _probe(p):
        probed.append(p)
        return p

    monkeypatch.setattr("kiro_crew.dashboard.session_directive_apply.resolve_project_path", _probe)

    assert _refused(state, caller, path="/etc").code == "not_creator"
    assert probed == []


def test_an_empty_path_is_refused(tmp_path):
    state, caller, _ = _pair(tmp_path)

    assert _refused(state, caller, path="").code == "bad_request"


def test_the_directive_and_the_verb_share_one_path_check():
    """Two copies of the sensitive-path check is how they would drift."""
    import inspect

    from kiro_crew.dashboard import session_directive_apply as sda

    assert "resolve_project_path" in inspect.getsource(sda._set_project)
    assert "resolve_project_path" in inspect.getsource(sc.set_project_target)


# ── route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body: dict):
    state, caller, _ = _pair(tmp_path)
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/set-project"
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


def test_route_without_the_secret_is_forbidden(tmp_path):
    req = _request(tmp_path, internal=False, body={"target": "chat-2", "path": str(tmp_path)})
    resp = asyncio.run(handlers_sc.api_session_control_set_project(req))
    assert resp.status == 403


def test_route_refuses_a_wrongly_typed_path(tmp_path):
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "path": 5})
    resp = asyncio.run(handlers_sc.api_session_control_set_project(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_route_sets_the_project(tmp_path):
    project = tmp_path / "wt"
    project.mkdir()
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "path": str(project)})

    resp = asyncio.run(handlers_sc.api_session_control_set_project(req))

    assert resp.status == 200
    assert json.loads(resp.body)["project"] == str(project.resolve())
    assert req.app["state"]._slots["chat-2"].project == str(project.resolve())


def test_route_is_on_the_strict_internal_set():
    from kiro_crew.dashboard import server

    assert "/api/session-control/set-project" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"
_SET_PROJECT = "POST /api/session-control/set-project"


def _call_tool(args: dict, reply: object = None) -> tuple[str | None, InMemoryDashboardClient]:
    """One tools/call frame for session_set_project against an in-memory gateway.

    Returns ``(None, dash)`` when argument validation raised, so a caller can
    assert that nothing reached the gateway either way.
    """
    dash = InMemoryDashboardClient({} if reply is None else {_SET_PROJECT: reply})
    try:
        out = TABLE.call("session_set_project", args, ToolContext(dash, Caller.strict(_VERIFIED)))
    except Exception:
        return None, dash
    return out, dash


def _writes(dash: InMemoryDashboardClient) -> list:
    return [r for r in dash.requests if r.method != "GET"]


def test_tool_refuses_a_call_without_path_before_calling_the_gateway():
    out, dash = _call_tool({"target": "chat-2"})
    assert out is None or out.startswith("Error")
    assert _writes(dash) == []


def test_tool_carries_the_verified_key_and_says_the_target_resets():
    out, dash = _call_tool(
        {"target": "chat-2", "path": "/w/wt"},
        {"ok": True, "target": "chat-2", "project": "/w/wt", "changed": True},
    )
    (req,) = _writes(dash)
    assert req.route == _SET_PROJECT
    assert req.body == {"target": "chat-2", "path": "/w/wt"}
    assert req.session_key == _VERIFIED
    assert out is not None
    assert "`chat-2` project set to `/w/wt`" in out
    assert "resets at its next turn boundary" in out


def test_tool_reports_an_unchanged_project_without_a_reset():
    out, _ = _call_tool(
        {"target": "chat-2", "path": "/w/wt"},
        {"ok": True, "target": "chat-2", "project": "/w/wt", "changed": False},
    )
    assert out is not None
    assert "nothing changed" in out
    assert "not reset" in out


def test_tool_reports_a_refusal_as_an_error():
    out, _ = _call_tool(
        {"target": "chat-2", "path": "/w/wt"},
        {"error": "session busy", "code": "target_busy"},
    )
    assert out is not None
    assert out.startswith("Error:") and "session busy" in out


def test_tool_refuses_a_relative_path_before_calling_the_gateway():
    out, dash = _call_tool({"target": "chat-2", "path": "rel/dir"})
    assert out is None or out.startswith("Error")
    assert _writes(dash) == []


def test_the_tool_description_says_the_conversation_resets():
    (tool,) = [t for t in _list_tools() if t["name"] == "session_set_project"]
    assert "RESETS at its next turn boundary" in tool["description"]
    assert tool["inputSchema"]["required"] == ["target", "path"]
