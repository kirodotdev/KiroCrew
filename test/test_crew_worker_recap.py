"""``GET .../crew-workers/{worker}/recap``: one session's recap from records only.

The crewmate Sessions tab expands a row in place and shows this
(``docs/request-for-change/rfc-mate-sessions-folder-or-lineage.md``, "Owner decision:
a click shows the recap in place"). Every line comes from the work item and the
session's own crew-log folds; no model call. Every case names the rule it guards.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode
from kiro_crew.crew_log.projection_catalogue import Projection
from kiro_crew.members_parts.member_space import DM_SLOT_MODE
from kiro_crew.work_ledger_records import WorkItem

MATE = "dm-ada"


@pytest.fixture(autouse=True)
def _fresh_roots(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "_known_mate_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: None)
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew", "agent_kind": "template", "parent": None, **over}


_TREE = [
    _row(MATE, agent="ada", agent_kind="member", mode=DM_SLOT_MODE),
    _row("m1", created_by=MATE, title="Slot title one"),
    _row("m2", created_by="m1", title="Slot title two"),
    _row("croot", agent="kirocrew-conductor"),
    _row("cw", created_by="croot"),
    _row("plain"),
]

_ITEM = WorkItem(
    item_id="it_aaaa1111",
    title="Write the release notes",
    acceptance={"kind": "pr_checks", "pr": 42, "repo": "kirodotdev/KiroCrew"},
    state="open",
    decision="Ship the short version first.",
    worker_session_key="m1",
    status="blocked",
    reason="needs_human",
    summary="Draft done; waiting on the changelog owner.",
    artifacts={"branch": "docs/notes"},
    pr=42,
    last_report_at="2026-11-03T05:00:00Z",
)


def _state(rows: list[dict[str, Any]]) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    return SimpleNamespace(
        serialize_slots=serialize_slots,
        get_slot=lambda key: None,
        _slots={r["key"]: object() for r in rows},
    )


@pytest.fixture
def recap(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_worker_recap, crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(
        crew_workers, "_ledger_work_items", lambda board: {"m1": _ITEM} if board == MATE else {}
    )
    monkeypatch.setattr(
        crew_worker_recap,
        "_session_folds",
        lambda unit: {"last_turn": "", "approvals": [], "approvals_count": 0, "questions": 0},
    )
    return crew_worker_recap


async def _get(module: Any, state: Any, root: str, worker: str) -> web.Response:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/x", app=app, match_info={"slot": root, "worker": worker})
    return await module.api_crew_worker_recap(req)


@pytest.mark.asyncio
async def test_the_recap_is_the_work_items_own_lines(recap):
    resp = await _get(recap, _state(_TREE), MATE, "m1")
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["title"] == "Write the release notes"
    assert (body["status"], body["reason"], body["state"]) == ("blocked", "needs_human", "open")
    assert body["summary"] == "Draft done; waiting on the changelog owner."
    assert body["decision"] == "Ship the short version first."
    assert body["pr"] == 42
    assert body["pr_url"] == "https://github.com/kirodotdev/KiroCrew/pull/42"
    assert body["artifacts"] == {"branch": "docs/notes"}
    assert body["last_update"] == "2026-11-03T05:00:00Z"
    assert body["ledger_item"] == "it_aaaa1111"


@pytest.mark.asyncio
async def test_a_session_with_no_item_is_its_title_and_nothing_else(recap):
    """The empty state: the slot title, no report lines."""
    resp = await _get(recap, _state(_TREE), MATE, "m2")
    body = json.loads(resp.text)
    assert body["title"] == "Slot title two"
    assert body["summary"] == body["status"] == body["decision"] == body["pr_url"] == ""
    assert body["pr"] is None and body["ledger_item"] == ""


@pytest.mark.asyncio
async def test_a_grandchilds_item_on_its_sub_conductors_ledger_is_found(recap, monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    item = WorkItem(item_id="it_bbbb2222", title="Check links", worker_session_key="m2")
    monkeypatch.setattr(
        crew_workers, "_ledger_work_items", lambda board: {"m2": item} if board == "m1" else {}
    )
    body = json.loads((await _get(recap, _state(_TREE), MATE, "m2")).text)
    assert body["ledger_item"] == "it_bbbb2222" and body["title"] == "Check links"


@pytest.mark.asyncio
async def test_a_session_outside_the_tree_and_a_non_root_are_refused(recap):
    for root, worker, code in (
        (MATE, "cw", "not_a_worker"),
        (MATE, "plain", "not_a_worker"),
        ("croot", "m1", "not_a_worker"),
        ("m1", "m2", "not_crew_mode_root"),
    ):
        resp = await _get(recap, _state(_TREE), root, worker)
        assert resp.status == 404, (root, worker)
        assert json.loads(resp.text)["code"] == code


@pytest.mark.asyncio
async def test_the_recap_keeps_the_owner_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_worker_recap, crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    assert (await _get(crew_worker_recap, _state(_TREE), MATE, "m1")).status == 403
    monkeypatch.setattr(crew_workers, "_deny_app_caller", _deny)
    assert (await _get(crew_worker_recap, _state(_TREE), MATE, "m1")).status == 403


@pytest.mark.asyncio
async def test_the_recap_makes_no_model_call(recap, monkeypatch):
    """Records only: the model-written intent summary and its generator are never reached."""
    from kiro_crew.dashboard import chat_summary

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the recap must not read or make a model summary")

    for name in dir(chat_summary):
        if not name.startswith("__") and callable(getattr(chat_summary, name)):
            monkeypatch.setattr(chat_summary, name, _boom, raising=False)
    resp = await _get(recap, _state(_TREE), MATE, "m1")
    assert resp.status == 200
    source = open(recap.__file__, encoding="utf-8").read()
    assert "chat_summary" not in source.split('"""', 2)[2]


@pytest.mark.asyncio
async def test_report_text_is_redacted(recap, monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    item = WorkItem(
        item_id="it_cccc3333", title="t", worker_session_key="m1", summary=f"key {secret}"
    )
    monkeypatch.setattr(crew_workers, "_ledger_work_items", lambda board: {"m1": item})
    body = json.loads((await _get(recap, _state(_TREE), MATE, "m1")).text)
    assert secret not in body["summary"]


@pytest.mark.asyncio
async def test_text_is_redacted_before_it_is_cut(recap, monkeypatch):
    """A secret that straddles a length cap is removed whole: the cut runs on the
    redacted text, so no prefix of the secret survives it."""
    from kiro_crew.dashboard.handlers import crew_worker_recap, crew_workers

    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    pad = "x" * (crew_worker_recap.RECAP_TEXT_MAX - 4)
    art_pad = "y" * (crew_worker_recap.RECAP_ARTIFACT_VALUE_MAX - 4)
    item = WorkItem(
        item_id="it_dddd4444",
        title="t",
        worker_session_key="m1",
        summary=pad + secret,
        decision=pad + secret,
        artifacts={"note": art_pad + secret},
    )
    monkeypatch.setattr(crew_workers, "_ledger_work_items", lambda board: {"m1": item})
    body = json.loads((await _get(recap, _state(_TREE), MATE, "m1")).text)
    for text in (body["summary"], body["decision"], body["artifacts"]["note"]):
        assert "AKIA" not in text
    assert len(body["summary"]) <= crew_worker_recap.RECAP_TEXT_MAX
    assert len(body["artifacts"]["note"]) <= crew_worker_recap.RECAP_ARTIFACT_VALUE_MAX


def test_the_pr_link_comes_from_typed_fields_and_never_from_a_bad_repo():
    from kiro_crew.dashboard.handlers import crew_worker_recap as r

    # The typed fields win: the link is built from ``pr`` and the acceptance repo.
    linked = WorkItem(
        artifacts={"pr": "https://github.com/o/n/pull/7"}, pr=9, acceptance={"repo": "o/n"}
    )
    assert r._github_pr_url(linked) == "https://github.com/o/n/pull/9"
    # With no typed number, a reported pull-request link is used.
    assert r._github_pr_url(WorkItem(artifacts={"pr": "https://github.com/o/n/pull/7"})) == (
        "https://github.com/o/n/pull/7"
    )
    for bad in (
        "https://github.com/o/n/tree/pull/7",
        "https://github.com//n/pull/7",
        "https://user@github.com/o/n/pull/7",
        "https://github.com:8443/o/n/pull/7",
        "https://github.com/o/x/../n/pull/7",
        "https://github.com/o/n!/pull/7",
        "https://github.com/o/n/pull/7/files/more",
        "http://github.com/o/n/pull/7",
        "https://github.com.evil/o/n/pull/7",
        "https://github.com/o/n/pull/07",
        "https://github.com/o/n/pull/7?x=1",
        "https://github.com/o/n/pull/7/other",
    ):
        assert r._github_pr_url(WorkItem(artifacts={"pr": bad})) == "", bad
    for repo in ("o/n/x", "evil.com/../x", "", None, "o/"):
        assert r._github_pr_url(WorkItem(pr=9, acceptance={"repo": repo})) == ""
    assert (
        r._github_pr_url(WorkItem(pr=9, acceptance={"repo": "o/n"}))
        == "https://github.com/o/n/pull/9"
    )
    assert r._github_pr_url(WorkItem(artifacts={"x": "https://evil.example/pull/1"})) == ""
    # A branch whose name holds "pull/7" is not a pull request: the real PR wins.
    branch = WorkItem(
        artifacts={"branch": "https://github.com/o/n/tree/pull/7"},
        pr=42,
        acceptance={"repo": "o/n"},
    )
    assert r._github_pr_url(branch) == "https://github.com/o/n/pull/42"
    # The repository's own PR parser takes only the canonical pull-request URL.
    assert r._github_pr_url(WorkItem(artifacts={"pr": "https://github.com/o/n/pull/7/files"})) == ""


def test_the_session_folds_give_last_turn_pending_approvals_and_open_questions(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import projection_session_memo
    from kiro_crew.dashboard.handlers import crew_worker_recap as r

    values = {
        "timeline": {
            "moments": [
                {"type": "turn/completed", "time": 1_700_000_000_000},
                {"type": "turn/started", "time": 1_700_000_100_000},
            ]
        },
        "approvals": {
            "pending": 1,
            "pending_requests": [{"tool": "Shell", "time": 1_700_000_050_000}],
        },
        "tools": {
            "open_calls": [
                {"name": "mcp_kirocrew_core_ask_question"},
                {"name": "@kirocrew-core/ask_question"},
                {"name": "execute_bash"},
            ]
        },
    }
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(
        projection_session_memo,
        "read_projection",
        lambda unit, name: Projection(name=name, seq=1, value=values[name]),
    )
    out = r._session_folds("unit-1")
    assert out["last_turn"].startswith("2023-11-14T22:13:20")
    assert out["approvals_count"] == 1 and out["approvals"][0]["tool"] == "Shell"
    assert out["questions"] == 2
    # No unit, or the crew log off: nothing is read and nothing is claimed.
    assert r._session_folds("")["questions"] == 0
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
    assert r._session_folds("unit-1")["approvals_count"] == 0


def test_a_closed_session_reads_its_newest_saved_unit(monkeypatch):
    """A closed tab has no serving provider: its folds come from the newest unit the
    store holds for its slot, so its last turn and open calls still show."""
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import projection_readers
    from kiro_crew.dashboard.handlers import crew_worker_recap as r
    from kiro_crew.dashboard.handlers import crew_workers

    seen: list[str] = []
    monkeypatch.setattr(projection_readers, "newest_unit_in_succession", lambda slot: f"u-{slot}")
    monkeypatch.setattr(
        session_ledger, "crew_log_units", lambda slot, *a, **k: ("u-old", f"u-{slot}")
    )
    monkeypatch.setattr(
        r,
        "_session_folds",
        lambda unit: seen.append(unit)
        or {
            "last_turn": "2026-11-03T04:00:00+00:00",
            "approvals": [],
            "approvals_count": 0,
            "questions": 1,
        },
    )
    monkeypatch.setattr(crew_workers, "_ledger_work_items", lambda board: {})
    monkeypatch.setattr(
        crew_workers, "_closed_titles", lambda state, keys: {k: ("Closed one", "") for k in keys}
    )
    out = r.build_recap(SimpleNamespace(), MATE, "m9", None, [])
    assert seen == ["u-m9"]
    assert out["title"] == "Closed one" and out["live"] is False
    assert out["last_update"] == "2026-11-03T04:00:00+00:00"
    assert out["open_questions"] == 1


def test_a_permanently_deleted_sessions_units_are_never_read(monkeypatch):
    """The store keeps a deleted session's files; the ledger seam excludes them. A
    unit the seam does not list is never folded, so nothing of it reaches the recap."""
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import projection_readers
    from kiro_crew.dashboard.handlers import crew_worker_recap as r

    monkeypatch.setattr(projection_readers, "newest_unit_in_succession", lambda slot: "u-deleted")
    monkeypatch.setattr(session_ledger, "crew_log_units", lambda slot, *a, **k: ())
    assert r._unit_of(SimpleNamespace(), None, "m9") == ""
    # A deleted newest unit falls back to the newest one the seam still lists.
    monkeypatch.setattr(session_ledger, "crew_log_units", lambda slot, *a, **k: ("u-a", "u-b"))
    assert r._unit_of(SimpleNamespace(), None, "m9") == "u-b"


def test_a_peer_crews_item_never_stands_for_a_local_session(monkeypatch):
    """A remote bind names the peer's slot key, which can equal a local key; the
    local session's own item is the one read, whatever order the board lists them."""
    from kiro_crew.dashboard.handlers import crew_workers, work_ledger_board

    local = WorkItem(item_id="it_local001", title="Local task", worker_session_key="m1")
    peer = WorkItem(
        item_id="it_peer0001",
        title="Peer task",
        worker_session_key="m1",
        worker_instance_id="peer-crew",
    )
    monkeypatch.setattr(work_ledger_board, "task_records", lambda key: (None, [local, peer]))
    assert crew_workers._ledger_work_items(MATE)["m1"].item_id == "it_local001"
    monkeypatch.setattr(work_ledger_board, "task_records", lambda key: (None, [peer]))
    assert crew_workers._ledger_work_items(MATE) == {}
