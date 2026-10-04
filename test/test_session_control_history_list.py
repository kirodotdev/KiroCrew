"""Session control: listing the archived sessions a caller could revive.

``list_archived_sessions`` is the finding half of ``session_revive``: every row it
returns must be a target revive would accept for the same caller, and nothing it
would refuse may appear (a title is the person's private work). So the
containment tests archive real sessions through the real close verb, mark them
with the same metadata fields the revive tests use, and assert each one is
absent; the round-trip test revives what the list returned.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_windows():
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()
    yield
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _archive(state, caller, name: str, *, title: str = "", age: int = 0) -> str:
    """Close a fresh session *name* through the real close verb.

    *age* backdates its transcript mtime by that many minutes, which is what the
    catalog sorts on, so a test can pin the newest-first order deterministically.
    """
    peer = _slot(state, name)
    peer.messages.append({"role": "user", "content": "hi"})
    if title:
        peer.title = title
        peer._titled = True
    peer._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=peer.key))
    assert peer.key not in state._slots
    path = state.conversation_log._dir / f"dashboard_{peer.key}.jsonl"
    stamp = 1_800_000_000 - age * 60
    os.utime(path, (stamp, stamp))
    return peer.key


def _list(state, caller, **kwargs):
    return asyncio.run(sc.list_archived_sessions(state, caller_session_key=_key(caller), **kwargs))


def _targets(result) -> list[str]:
    return [row["target"] for row in result["sessions"]]


def _lineage(monkeypatch, parents: dict, *, known: bool = True):
    monkeypatch.setattr(
        sc, "_slot_tree_parent", lambda slot_key: (known, parents.get(slot_key, ""), {})
    )


# ── Happy path ───────────────────────────────────────────────────────────────


def test_lists_archived_sessions_newest_first_and_skips_live_ones(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    older = _archive(state, caller, "chat-2", title="Older", age=10)
    newer = _archive(state, caller, "chat-3", title="Newer", age=1)
    _slot(state, "chat-4")  # live: not history

    result = _list(state, caller)

    assert result["ok"] is True
    assert _targets(result) == [newer, older]
    assert result["sessions"][0]["title"] == "Newer"
    assert result["sessions"][0]["last_active"].endswith("Z")
    assert result["more"] is False
    assert result["lineage_unknown"] is False


def test_every_listed_row_is_a_target_revive_accepts(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2", title="Lookup")

    [row] = _list(state, caller)["sessions"]
    revived = asyncio.run(
        sc.revive_session(state, caller_session_key=_key(caller), target=row["target"])
    )

    assert revived["target"] == row["target"]
    assert _list(state, caller)["sessions"] == []


def test_limit_caps_rows_and_reports_the_exact_overflow(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(5):
        _archive(state, caller, f"chat-{i + 2}", age=i)

    result = _list(state, caller, limit=2)

    assert len(result["sessions"]) == 2
    assert result["more"] is True
    assert result["omitted"] == 3
    full = _list(state, caller, limit=5)
    assert full["more"] is False and full["omitted"] == 0


def test_the_overflow_count_skips_rows_revive_would_refuse(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(4):
        _archive(state, caller, f"chat-{i + 2}", age=i)
    # The oldest one is an app session, which revive refuses.
    state.conversation_log.update_metadata("dashboard:chat-5", {"app": "some-app"})

    assert _list(state, caller, limit=1)["omitted"] == 2


def test_an_overflow_row_revived_during_the_scan_is_not_counted(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2", age=0)
    older = _archive(state, caller, "chat-3", age=1)
    real_scan = sc._scan_revivable_history

    def _scan_then_revive(*args, **kwargs):
        out = real_scan(*args, **kwargs)
        _slot(state, older)
        return out

    monkeypatch.setattr(sc, "_scan_revivable_history", _scan_then_revive)
    result = _list(state, caller, limit=1)
    assert len(result["sessions"]) == 1
    assert result["omitted"] == 0 and result["more"] is False


def test_every_overflow_key_is_kept_within_the_scan_cap(tmp_path, monkeypatch):
    # The scan cap is the one bound on overflow keys, so the revive correction
    # sees every one of them: a revive of the OLDEST overflow row mid-scan is
    # still taken out of the count, and the key list never outgrows the walk.
    monkeypatch.setattr(sc, "MAX_HISTORY_SCAN_ENTRIES", 50)
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(6):
        _archive(state, caller, f"chat-{i + 2}", age=i)
    oldest = "chat-7"
    captured = {}
    real_scan = sc._scan_revivable_history

    def _scan_then_revive_oldest(*args, **kwargs):
        out = real_scan(*args, **kwargs)
        captured["out"] = out
        _slot(state, oldest)
        return out

    monkeypatch.setattr(sc, "_scan_revivable_history", _scan_then_revive_oldest)
    result = _list(state, caller, limit=1)

    _rows, keys, _lineage, truncated = captured["out"]
    assert oldest in keys
    assert len(keys) == 5 <= sc.MAX_HISTORY_SCAN_ENTRIES
    assert truncated is False
    assert result["omitted"] == 4 and result["more"] is True
    assert result["scan_truncated"] is False


def test_the_walk_stops_at_the_scan_cap_and_says_so(tmp_path, monkeypatch):
    # The catalog walk is bounded too, so the `seen` set cannot grow with the
    # archive; a truncated walk reports it and `omitted` becomes a lower bound.
    monkeypatch.setattr(sc, "MAX_HISTORY_SCAN_ENTRIES", 3)
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(6):
        _archive(state, caller, f"chat-{i + 2}", age=i)

    result = _list(state, caller, limit=1)

    assert result["scan_truncated"] is True
    assert result["more"] is True
    assert len(result["sessions"]) == 1
    assert result["omitted"] < 5
    monkeypatch.setattr(sc, "MAX_HISTORY_SCAN_ENTRIES", 100)
    full = _list(state, caller, limit=1)
    assert full["scan_truncated"] is False
    assert full["omitted"] == 5


def test_the_scan_reads_a_bounded_catalog_not_list_sessions(tmp_path, monkeypatch):
    # `list_sessions` builds a full row, title included, for every transcript
    # before any cap applies; the scan must start from the bounded stem read.
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(4):
        _archive(state, caller, f"chat-{i + 2}", age=i)
    log = state.conversation_log

    def _refuse():
        raise AssertionError("the history scan must not materialize list_sessions")

    monkeypatch.setattr(log, "list_sessions", _refuse)
    result = _list(state, caller, limit=2)

    assert _targets(result) == ["chat-2", "chat-3"]
    assert result["omitted"] == 2 and result["scan_truncated"] is False


def test_newest_session_stems_keeps_only_the_newest_entries(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for i in range(5):
        _archive(state, caller, f"chat-{i + 2}", age=i)
    log = state.conversation_log

    capped, more = log.newest_session_stems(2)
    assert [stem for stem, _ in capped] == ["dashboard_chat-2", "dashboard_chat-3"]
    assert more is True
    everything, more_all = log.newest_session_stems(1000)
    assert more_all is False
    assert [stem for stem, _ in everything][:5] == [f"dashboard_chat-{i + 2}" for i in range(5)]
    assert log.newest_session_stems(0) == ([], False)


def test_limit_is_clamped_to_the_ceiling(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2")

    assert len(_list(state, caller, limit=10_000)["sessions"]) == 1
    assert len(_list(state, caller, limit=0)["sessions"]) == 1


# ── Folder filter ────────────────────────────────────────────────────────────


def _folder(state, fid: str, name: str):
    state._folders.append({"id": fid, "name": name, "parent_id": None, "position": 0})


def test_folder_filter_keeps_only_sessions_filed_there(tmp_path):
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    filed = _archive(state, caller, "chat-2")
    _archive(state, caller, "chat-3")
    state.conversation_log.update_metadata(f"dashboard:{filed}", {"folder_id": "f1"})

    result = _list(state, caller, folder_id="f1")

    assert _targets(result) == [filed]
    assert result["sessions"][0]["folder_id"] == "f1"


def test_an_unknown_folder_is_refused_not_answered_empty(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _list(state, caller, folder_id="nope")

    assert exc.value.code == "folder_not_found"


# ── Containment: what revive would refuse is not listed ─────────────────────


@pytest.mark.parametrize(
    "fields",
    [
        {"workspace": "other"},
        {"app": "some-app"},
        {"linked_session_key": "slack:123.456"},
        {"channel_origin": "slack"},
        {"memory_mode": "incognito"},
        {"memory_mode": "temporary"},
    ],
    ids=["workspace", "app", "linked", "channel-origin", "incognito", "temporary"],
)
def test_sessions_revive_would_refuse_are_not_listed(tmp_path, fields):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    keep = _archive(state, caller, "chat-2")
    hidden = _archive(state, caller, "chat-3", title="Private work")
    state.conversation_log.update_metadata(f"dashboard:{hidden}", fields)

    result = _list(state, caller)

    assert _targets(result) == [keep]
    assert "Private work" not in repr(result)


def test_scheduled_runs_and_member_threads_are_not_listed(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    log = state.conversation_log
    for key in ("dashboard:cron-job1", "dashboard:member-alpha", "dashboard:dashboard:cron-x"):
        log.append(key, "user", "hi")
        log.update_metadata(key, {"closed": True})

    assert _list(state, caller)["sessions"] == []


def test_channel_linked_and_mirrored_sessions_are_not_listed(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    keep = _archive(state, caller, "chat-2")
    origin = _archive(state, caller, "chat-3")
    slack = _archive(state, caller, "chat-4")
    mirrored = _archive(state, caller, "chat-5")
    state.sessions.set_origin_link(f"dashboard:{origin}", MagicMock(channel_type="telegram"))
    state.sessions.set_slack_link(f"dashboard:{slack}", "1712793600.1", "C1")
    link = MagicMock(channel_type="slack", channel_id="C1", thread_id="1.2")
    state.sessions.get_mirror_link = lambda key: link if key == f"dashboard:{mirrored}" else None

    assert _targets(_list(state, caller)) == [keep]


def test_a_store_that_cannot_answer_hides_the_row(tmp_path):
    """The link probe fails closed for revive, so the listing must not offer a
    session revive would then refuse."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2")

    def _boom(key):
        raise RuntimeError("store down")

    state.sessions.get_origin_link = _boom
    assert _list(state, caller)["sessions"] == []


# ── Ownership fence ──────────────────────────────────────────────────────────


def test_fenced_caller_sees_only_what_it_created_and_the_lineage_confirms(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _archive(state, caller, "chat-2", age=3)
    forged = _archive(state, caller, "chat-3", age=2)
    foreign = _archive(state, caller, "chat-4", age=1)
    log = state.conversation_log
    log.update_metadata(f"dashboard:{own}", {"created_by": caller.key})
    log.update_metadata(f"dashboard:{forged}", {"created_by": caller.key})  # lineage disagrees
    log.update_metadata(f"dashboard:{foreign}", {"created_by": "chat-9"})
    _lineage(monkeypatch, {own: caller.key, forged: "chat-9", foreign: "chat-9"})

    result = _list(state, caller, caller_fenced=True)

    assert _targets(result) == [own]
    assert result["lineage_unknown"] is False


def test_fenced_caller_gets_nothing_and_a_flag_when_the_lineage_is_unreadable(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _archive(state, caller, "chat-2")
    state.conversation_log.update_metadata(f"dashboard:{own}", {"created_by": caller.key})
    _lineage(monkeypatch, {}, known=False)

    result = _list(state, caller, caller_fenced=True)

    assert result["sessions"] == []
    assert result["lineage_unknown"] is True


def test_unfenced_caller_does_not_consult_the_lineage(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, "chat-2")

    def _never(slot_key):
        raise AssertionError("lineage read for an unfenced caller")

    monkeypatch.setattr(sc, "_slot_tree_parent", _never)
    assert _targets(_list(state, caller, caller_fenced=False)) == [key]


# ── Caller side ──────────────────────────────────────────────────────────────


def test_a_caller_with_no_live_slot_is_refused_before_history_is_read(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2")
    state._slots.pop(caller.key)

    with patch.object(sc, "_scan_revivable_history") as scan:
        with pytest.raises(sc.SessionControlError):
            _list(state, caller)
    scan.assert_not_called()


def test_a_session_revived_during_the_scan_is_dropped(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, "chat-2")
    real_scan = sc._scan_revivable_history

    def _scan_then_revive(*args, **kwargs):
        rows = real_scan(*args, **kwargs)
        _slot(state, key)  # a human click published it meanwhile
        return rows

    monkeypatch.setattr(sc, "_scan_revivable_history", _scan_then_revive)
    assert _list(state, caller)["sessions"] == []


def test_an_oversized_folder_id_is_reported_as_unfiled(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, "chat-2")
    huge = "f" * (sc.MAX_HISTORY_FOLDER_ID_CHARS + 1)
    state.conversation_log.update_metadata(f"dashboard:{key}", {"folder_id": huge})

    (row,) = _list(state, caller)["sessions"]

    assert row["target"] == key
    assert row["folder_id"] == ""


def test_a_fence_that_tightens_during_the_scan_withholds_the_rows(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    foreign = _archive(state, caller, "chat-2", title="Someone else's work")
    state.conversation_log.update_metadata(f"dashboard:{foreign}", {"created_by": "chat-9"})
    fenced = {"now": False}
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda state, key: fenced["now"])
    real_scan = sc._scan_revivable_history

    def _scan_then_bind_dm(*args, **kwargs):
        rows = real_scan(*args, **kwargs)
        fenced["now"] = True  # a channel-DM link lands on the caller meanwhile
        return rows

    monkeypatch.setattr(sc, "_scan_revivable_history", _scan_then_bind_dm)
    with pytest.raises(sc.SessionControlError) as exc:
        _list(state, caller)
    assert exc.value.code == "caller_changed_mid_read"


def test_post_scan_caller_recheck_runs_off_the_event_loop(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, "chat-2")
    real_check = sc.refuse_caller_surface
    on_loop: list[bool] = []

    def _record(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(sc, "refuse_caller_surface", _record)
    _list(state, caller)
    assert on_loop == [False, False]


# ── HTTP wrapper ─────────────────────────────────────────────────────────────


def _none_async():
    async def _f(request):
        return None

    return _f


def test_route_forwards_folder_limit_and_fence_to_the_core(monkeypatch):
    from kiro_crew.dashboard.handlers import session_control as handlers_sc

    seen: dict = {}

    async def _ok(state, **kw):
        seen.update(kw)
        return {"ok": True, "sessions": [], "more": False, "lineage_unknown": False}

    monkeypatch.setattr(sc, "list_archived_sessions", _ok)
    monkeypatch.setattr(handlers_sc, "_require_internal", _none_async())
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda request: "dashboard:chat-1")
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda request: True)
    request = MagicMock()
    request.app = {"state": object()}
    request.query = {"folder_id": " f1 ", "limit": "7"}

    resp = asyncio.run(handlers_sc.api_session_control_history(request))

    assert resp.status == 200
    assert seen == {
        "caller_session_key": "dashboard:chat-1",
        "folder_id": "f1",
        "limit": 7,
        "caller_fenced": True,
    }


def test_route_refuses_a_non_integer_limit(monkeypatch):
    from kiro_crew.dashboard.handlers import session_control as handlers_sc

    monkeypatch.setattr(handlers_sc, "_require_internal", _none_async())
    request = MagicMock()
    request.app = {"state": object()}
    request.query = {"limit": "lots"}

    resp = asyncio.run(handlers_sc.api_session_control_history(request))

    assert resp.status == 400


def test_route_is_registered_and_strict():
    from kiro_crew.dashboard import server

    assert "/api/session-control/history" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────


def _strict(monkeypatch):
    from kiro_crew import mcp_dashboard

    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("dashboard:chat-1", "")
    )
    return mcp_dashboard


def test_mcp_tool_gets_the_history_route_and_renders_rows(monkeypatch):
    mcp_dashboard = _strict(monkeypatch)
    seen: dict = {}

    def _get(path, session_key=""):
        seen.update(path=path, session_key=session_key)
        return {
            "ok": True,
            "sessions": [
                {
                    "target": "chat-2",
                    "title": "Lookup",
                    "last_active": "2026-09-30T12:00:00Z",
                    "folder_id": "f1",
                }
            ],
            "more": True,
            "omitted": 7,
            "lineage_unknown": False,
        }

    monkeypatch.setattr(mcp_dashboard, "_get", _get)

    out = mcp_dashboard._call_tool_inner("session_history_list", {})

    assert seen == {
        "path": "/api/session-control/history?limit=20",
        "session_key": "dashboard:chat-1",
    }
    assert "`chat-2` (Lookup)" in out and "2026-09-30T12:00:00Z" in out
    assert "folder f1" in out and "7 more not shown" in out
    assert "session_revive" in out

    def _get_truncated(path, session_key=""):
        resp = _get(path, session_key)
        resp["scan_truncated"] = True
        return resp

    monkeypatch.setattr(mcp_dashboard, "_get", _get_truncated)
    out = mcp_dashboard._call_tool_inner("session_history_list", {})
    assert "at least 7 more not shown" in out


def test_mcp_tool_resolves_a_folder_path_without_creating_it(monkeypatch):
    mcp_dashboard = _strict(monkeypatch)
    folders = [{"id": "f1", "name": "Gamma", "parent_id": None, "position": 0}]
    monkeypatch.setattr(mcp_dashboard, "_get_rows", lambda path: (folders, None))
    seen: dict = {}

    def _get(path, session_key=""):
        seen["path"] = path
        return {"ok": True, "sessions": [], "more": False, "lineage_unknown": False}

    monkeypatch.setattr(mcp_dashboard, "_get", _get)
    with patch.object(mcp_dashboard, "_post") as post:
        out = mcp_dashboard._call_tool_inner(
            "session_history_list", {"folder": "Gamma", "limit": 5}
        )

    assert seen["path"] == "/api/session-control/history?limit=5&folder_id=f1"
    assert "No archived sessions you can revive in `Gamma`" in out
    post.assert_not_called()


def test_mcp_tool_refuses_an_unknown_folder_without_calling_the_route(monkeypatch):
    mcp_dashboard = _strict(monkeypatch)
    monkeypatch.setattr(mcp_dashboard, "_get_rows", lambda path: ([], None))
    with patch.object(mcp_dashboard, "_get") as get:
        out = mcp_dashboard._call_tool_inner("session_history_list", {"folder": "Nowhere"})
    assert out.startswith("Error:")
    get.assert_not_called()


def test_mcp_tool_says_when_the_lineage_hid_rows(monkeypatch):
    mcp_dashboard = _strict(monkeypatch)
    monkeypatch.setattr(
        mcp_dashboard,
        "_get",
        lambda *a, **k: {"ok": True, "sessions": [], "more": False, "lineage_unknown": True},
    )
    out = mcp_dashboard._call_tool_inner("session_history_list", {})
    assert "lineage" in out


def test_mcp_tool_does_not_call_an_empty_page_an_empty_archive(monkeypatch):
    # The kept rows can be revived mid-scan while counted overflow rows remain.
    mcp_dashboard = _strict(monkeypatch)
    monkeypatch.setattr(
        mcp_dashboard,
        "_get",
        lambda *a, **k: {"ok": True, "sessions": [], "more": True, "omitted": 3},
    )
    out = mcp_dashboard._call_tool_inner("session_history_list", {})
    assert "you can revive" not in out
    assert "3 more not shown" in out


def test_mcp_tool_refuses_a_caller_without_a_strict_key(monkeypatch):
    from kiro_crew import mcp_dashboard

    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("", "Error: nope")
    )
    with patch.object(mcp_dashboard, "_get") as get:
        out = mcp_dashboard._call_tool_inner("session_history_list", {})
    assert out == "Error: nope"
    get.assert_not_called()
