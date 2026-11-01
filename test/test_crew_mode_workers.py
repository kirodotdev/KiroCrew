"""Crew Mode workers: the swappable conductor, the conductor family, the worker stamps,
the person's session list, the folder no-ops and the worker list / steer routes.

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_mode

sys.path.insert(0, str(Path(__file__).parent))

from mcp_dashboard_folders_helpers import _FOLDERS, _call, _writes  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_crew_mode_agent(_floor_monkeypatch):
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    yield
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="")))


# ── the config key ─────────────────────────────────────────────────────────────


def test_crew_mode_agent_defaults_to_the_shipped_conductor():
    from kiro_crew.config.loader_builders import _build_agent_config

    assert _build_agent_config({}).crew_mode_agent == "kirocrew-conductor"


def test_crew_mode_agent_is_read_from_its_one_key():
    from kiro_crew.config.loader_builders import _build_agent_config

    assert _build_agent_config({"crew_mode_agent": " better "}).crew_mode_agent == "better"


@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_an_unusable_crew_mode_agent_falls_back_to_the_conductor(value):
    from kiro_crew.config.loader_builders import _build_agent_config

    assert _build_agent_config({"crew_mode_agent": value}).crew_mode_agent == "kirocrew-conductor"


def test_swapping_the_key_swaps_the_conductor_and_joins_the_family():
    cfg = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="better-conductor"))
    assert crew_mode.crew_mode_agent(cfg) == "better-conductor"
    # Remembered for readers that cannot load config (the slot serializer).
    assert crew_mode.crew_mode_agent() == "better-conductor"
    assert "better-conductor" in crew_mode.conductor_agents()


def test_every_conductor_is_crew_mode(monkeypatch):
    """Decision 3: every shipped conductor is Crew Mode, the two lists are one set,
    and a root on any of them (team lead and the scheduled pipeline conductor
    included) is a Crew Mode root whose workers leave the person's list."""
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    monkeypatch.setattr(crew_mode, "_configured", crew_mode.DEFAULT_CREW_MODE_AGENT)
    shipped = {
        "kirocrew-conductor",
        "kirocrew-ledger-conductor",
        "kirocrew-pipeline-conductor",
        "kirocrew-security-conductor",
        "kirocrew-team-lead",
    }
    assert crew_mode.conductor_agents() == shipped
    for name in shipped:
        assert crew_mode.runs_crew_mode(name, "template")
        rows = _stamped(_row("p", agent=name), _row("w", created_by="p"))
        assert rows["p"][crew_mode.ROOT_FIELD] is True
        assert rows["w"][crew_mode.WORKER_FIELD] == "p"
    custom = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="better-conductor"))
    assert crew_mode.conductor_agents(custom) == shipped | {"better-conductor"}


def test_the_stamp_marks_a_chat_crew_mode_until_it_is_migrated():
    """Decision 4: the stamp only marks the chat as Crew Mode. A root on a custom
    conductor absent from the key stays Crew Mode until the migration moves it;
    the stamp counts only while the chat is still on that agent."""
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="new-one")))
    rows = _stamped(
        _row("root", agent="old-one", crew_mode_agent="old-one"), _row("w", created_by="root")
    )
    assert rows["root"][crew_mode.ROOT_FIELD] is True
    assert rows["w"][crew_mode.WORKER_FIELD] == "root"
    moved = _stamped(_row("root", agent="kirocrew", crew_mode_agent="old-one"))
    assert moved["root"][crew_mode.ROOT_FIELD] is False
    assert crew_mode.stamp_for_switch("new-one", "template") == "new-one"
    assert crew_mode.stamp_for_switch("new-one", "member") == ""
    assert crew_mode.stamp_for_switch("kirocrew-pipeline-conductor", "template") == (
        "kirocrew-pipeline-conductor"
    )
    assert crew_mode.stamp_for_switch("kirocrew", "template") == ""


def test_a_worker_whose_root_tab_closed_stays_out_of_the_list():
    """Decision 5: a known closed root still holds its live workers, directly or
    through a closed middle worker by the persisted lineage."""
    crew_mode.note_roots(["closed-root"])
    rows = _stamped(_row("w", created_by="closed-root"))
    assert rows["w"][crew_mode.WORKER_FIELD] == "closed-root"
    out = [_row("g", created_by="closed-mid")]
    crew_mode.stamp_rows(out, parents={"closed-mid": "closed-root"})
    assert out[0][crew_mode.WORKER_FIELD] == "closed-root"


def test_saved_metadata_of_a_conductor_root_is_a_root():
    assert crew_mode.saved_row_is_root({"agent": "kirocrew-team-lead"})
    assert not crew_mode.saved_row_is_root({"agent": "kirocrew-team-lead", "created_by": "x"})
    assert not crew_mode.saved_row_is_root({"agent": "kirocrew"})
    assert not crew_mode.saved_row_is_root({"agent": "kirocrew-conductor", "agent_kind": "member"})


def test_a_crewmate_named_like_a_conductor_is_not_one():
    assert crew_mode.runs_crew_mode("kirocrew-conductor", "template")
    assert not crew_mode.runs_crew_mode("kirocrew-conductor", "member")
    assert not crew_mode.runs_crew_mode("kirocrew-conductor", "", "member")
    assert not crew_mode.runs_crew_mode("kirocrew", "template")


# ── row stamps ─────────────────────────────────────────────────────────────────


def _row(key: str, **over: Any) -> dict[str, Any]:
    return {"key": key, "agent": "kirocrew", "agent_kind": "template", "parent": None, **over}


def _stamped(*rows: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = [dict(r) for r in rows]
    crew_mode.stamp_rows(out)
    return {r["key"]: r for r in out}


def test_a_root_conductor_is_a_crew_mode_root_and_its_tree_are_its_workers():
    rows = _stamped(
        _row("root", agent="kirocrew-conductor"),
        _row("w1", created_by="root"),
        _row("w2", parent={"slot": "w1", "key": "w1"}),
        _row("plain"),
    )
    assert rows["root"][crew_mode.ROOT_FIELD] is True
    assert rows["w1"][crew_mode.WORKER_FIELD] == "root"
    # A grandchild belongs to the same root.
    assert rows["w2"][crew_mode.WORKER_FIELD] == "root"
    # Each worker names its direct dispatcher: the root, or the sub-conductor.
    assert rows["w1"][crew_mode.DISPATCHER_FIELD] == "root"
    assert rows["w2"][crew_mode.DISPATCHER_FIELD] == "w1"
    assert crew_mode.WORKER_FIELD not in rows["plain"]
    assert crew_mode.DISPATCHER_FIELD not in rows["plain"]
    assert crew_mode.DISPATCHER_FIELD not in rows["root"]
    assert rows["plain"][crew_mode.ROOT_FIELD] is False


@pytest.mark.parametrize(
    "over",
    [
        {"agent_kind": "member"},
        {"memory_mode": "incognito"},
        {"app": "some-app"},
        {"instance_id": "peer-1"},
        {"created_by": "someone"},
    ],
)
def test_only_a_plain_persistent_root_on_a_conductor_template_is_a_root(over):
    rows = _stamped(_row("root", agent="kirocrew-conductor", **over), _row("w", created_by="root"))
    assert rows["root"][crew_mode.ROOT_FIELD] is False
    assert crew_mode.WORKER_FIELD not in rows["w"]


def test_a_swapped_in_conductor_roots_crew_mode_too():
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="better-conductor")))
    rows = _stamped(_row("root", agent="better-conductor"), _row("w", created_by="root"))
    assert rows["w"][crew_mode.WORKER_FIELD] == "root"


def test_a_cycle_of_creators_ends_the_walk():
    rows = _stamped(_row("a", created_by="b"), _row("b", created_by="a"))
    assert crew_mode.WORKER_FIELD not in rows["a"]
    assert crew_mode.WORKER_FIELD not in rows["b"]


def test_a_worker_whose_root_closed_is_an_ordinary_row():
    rows = _stamped(_row("w", created_by="gone-root"))
    assert crew_mode.WORKER_FIELD not in rows["w"]


def test_a_worker_whose_own_creator_closed_stays_under_its_root():
    """R opened M, M opened W, then M closed. The frame resolves W's ``parent.key``
    to R (its nearest open ancestor), so W stays R's worker, not a loose session."""
    rows = _stamped(
        _row("R", agent="kirocrew-conductor"),
        _row("W", created_by="M", parent={"slot": "M", "key": "R", "ancestor": True}),
    )
    assert rows["W"][crew_mode.WORKER_FIELD] == "R"
    # With no open ancestor resolved, the closed creator ends the walk.
    loose = _stamped(
        _row("R", agent="kirocrew-conductor"),
        _row("W", created_by="M", parent={"slot": "M", "key": None}),
    )
    assert crew_mode.WORKER_FIELD not in loose["W"]
    # The live-slot test reads the same edges when it is given them.
    slots = {
        "R": SimpleNamespace(key="R", agent="kirocrew-conductor", agent_kind="template"),
        "W": SimpleNamespace(key="W", agent="kirocrew", agent_kind="template", _created_by="M"),
    }
    assert not crew_mode.slot_in_crew_tree(slots, "W")
    assert crew_mode.slot_in_crew_tree(slots, "W", {"W": {"slot": "M", "key": "R"}})


# ── a reopened worker of an old conductor ──────────────────────────────────────

#: A reopened slot: the resume leaves ``created_by`` blank and the settled frame
#: says ``parent: None``. Only its saved metadata line still names the creator.
_OLD_ROOT = "chat-9-1792000000"
_OLD_WORKER = "chat-10-1792000100"


def _reopened(**saved: str) -> dict[str, dict[str, Any]]:
    out = [
        _row(_OLD_ROOT, agent="kirocrew-security-conductor"),
        _row(_OLD_WORKER, agent="kirocrew-worker", created_by=""),
    ]
    crew_mode.stamp_rows(out, settled=True, saved_creators=saved, tree_slots=())
    return {r["key"]: r for r in out}


def test_a_reopened_old_conductor_worker_with_saved_lineage_is_hidden():
    rows = _reopened(**{_OLD_WORKER: _OLD_ROOT})
    assert rows[_OLD_ROOT][crew_mode.ROOT_FIELD] is True
    assert rows[_OLD_WORKER][crew_mode.WORKER_FIELD] == _OLD_ROOT
    # Out of the person's list, in the root's Workers panel.
    listed = crew_mode.list_view(list(rows.values()), "", agent=False)
    assert [r["key"] for r in listed] == [_OLD_ROOT]
    assert [r["key"] for r in crew_mode.workers_of(rows.values(), _OLD_ROOT)] == [_OLD_WORKER]


def test_a_worker_agent_no_session_created_stays_a_normal_session():
    """Lineage is never guessed from the agent: no saved creator, no root."""
    rows = _reopened()
    assert crew_mode.WORKER_FIELD not in rows[_OLD_WORKER]
    listed = crew_mode.list_view(list(rows.values()), "", agent=False)
    assert _OLD_WORKER in [r["key"] for r in listed]


def test_a_released_worker_is_not_put_back_by_its_saved_creator():
    """A tree node with no parent is a release: the saved line does not undo it."""
    out = [
        _row(_OLD_ROOT, agent="kirocrew-conductor"),
        _row(_OLD_WORKER, agent="kirocrew-worker"),
    ]
    crew_mode.stamp_rows(
        out, settled=True, saved_creators={_OLD_WORKER: _OLD_ROOT}, tree_slots=[_OLD_WORKER]
    )
    assert crew_mode.WORKER_FIELD not in out[1]


def test_a_live_reopened_worker_keeps_its_saved_root_in_slot_patches():
    """The ``slot_patch`` stamps (``live_stamps``) read the same saved creator."""
    slots = {
        _OLD_ROOT: SimpleNamespace(
            key=_OLD_ROOT, agent="kirocrew-security-conductor", agent_kind="template"
        ),
        _OLD_WORKER: SimpleNamespace(
            key=_OLD_WORKER,
            agent="kirocrew-worker",
            agent_kind="template",
            _created_by="",
            _saved_created_by=_OLD_ROOT,
        ),
    }
    assert crew_mode.live_stamps(slots)[_OLD_WORKER] == (_OLD_ROOT, False)
    plain = dict(slots)
    plain[_OLD_WORKER] = SimpleNamespace(
        key=_OLD_WORKER, agent="kirocrew-worker", agent_kind="template", _created_by=""
    )
    assert crew_mode.live_stamps(plain)[_OLD_WORKER] == ("", False)


def test_every_hydration_keeps_the_saved_creator_for_display_only():
    """Resume carries the line's creator onto ``_saved_created_by`` and never onto
    ``_created_by``, which session-control ownership reads."""
    from kiro_crew.dashboard.slot_persistence import metadata_codec

    slot = SimpleNamespace(autocompact_pct=None, _created_by="", _saved_created_by="")
    rows = [r for r in metadata_codec.FIELDS if r.key == "created_by"]
    state = SimpleNamespace(sessions=None)
    original = metadata_codec.FIELDS
    try:
        metadata_codec.FIELDS = tuple(rows)  # type: ignore[assignment]
        metadata_codec.apply(
            state,  # type: ignore[arg-type]
            slot,  # type: ignore[arg-type]
            {"created_by": _OLD_ROOT},
            metadata_codec.Resume(member=None),
        )
    finally:
        metadata_codec.FIELDS = original  # type: ignore[assignment]
    assert slot._saved_created_by == _OLD_ROOT
    assert slot._created_by == ""


# ── the session list API ───────────────────────────────────────────────────────


async def _slots_get(monkeypatch, query: str = "", internal: bool = False) -> list[str]:
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.handlers import source_providers

    payloads = [
        {"key": "root", crew_mode.ROOT_FIELD: True},
        {"key": "w1", crew_mode.ROOT_FIELD: False, crew_mode.WORKER_FIELD: "root"},
        {"key": "plain", crew_mode.ROOT_FIELD: False},
    ]
    state = SimpleNamespace(_slots={}, serialize_slots=lambda **_k: [dict(p) for p in payloads])

    async def _noop():
        return None

    monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _noop)
    monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: False)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("GET", "/api/chat/slots" + query, app=app)
    if internal:
        req["internal_auth"] = True
    resp = await chat_handlers.api_chat_slots(req)
    return [r["key"] for r in json.loads(resp.text)]


@pytest.mark.asyncio
async def test_a_persons_list_leaves_crew_mode_workers_out(monkeypatch):
    assert await _slots_get(monkeypatch) == ["root", "plain"]


@pytest.mark.asyncio
async def test_crew_all_keeps_the_workers(monkeypatch):
    assert await _slots_get(monkeypatch, "?crew=all") == ["root", "w1", "plain"]


@pytest.mark.asyncio
async def test_an_agents_read_keeps_the_workers_its_tools_resolve(monkeypatch):
    assert await _slots_get(monkeypatch, internal=True) == ["root", "w1", "plain"]


def test_serialize_slots_stamps_every_row():
    """The stamp rides the one serializer every slot list and broadcast reads."""
    from kiro_crew.dashboard.state_parts import slots_frame

    rows = {
        "root": {"key": "root", "agent": "kirocrew-conductor", "agent_kind": "template"},
        "w1": {
            "key": "w1",
            "agent": "kirocrew-worker",
            "agent_kind": "template",
            "created_by": "root",
        },
    }
    fake = SimpleNamespace(
        _slots={key: SimpleNamespace(key=key) for key in rows},
        subagents=None,
        _slots_under_construction=(),
        _drop_orphaned_mcp_report=lambda slot: None,
        serialize_slot=lambda slot, **_k: dict(rows[slot.key]),
    )
    out = {row["key"]: row for row in slots_frame.serialize_slots(fake)}  # type: ignore[arg-type]
    assert out["root"]["crew_mode_root"] is True
    assert out["w1"]["crew_root"] == "root"


# ── the agent catalog ──────────────────────────────────────────────────────────


def test_a_custom_conductor_is_in_force_from_the_live_config_without_a_catalog_read(
    monkeypatch,
):
    """F1: nothing has read the catalog; the live config watcher's snapshot decides."""
    from kiro_crew.config import live

    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="")))
    monkeypatch.setattr(
        live,
        "snapshot",
        lambda: SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="better-conductor")),
    )
    slots = {
        "root": SimpleNamespace(key="root", agent="better-conductor", agent_kind="template"),
        "w1": SimpleNamespace(key="w1", agent="kirocrew-worker", _created_by="root"),
    }
    assert crew_mode.crew_mode_agent() == "better-conductor"
    assert crew_mode.slot_in_crew_tree(slots, "w1") is True


def test_with_no_live_config_the_remembered_value_stands(monkeypatch):
    from kiro_crew.config import live

    monkeypatch.setattr(live, "snapshot", lambda: None)
    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="other-one")))
    assert crew_mode.crew_mode_agent() == "other-one"


def test_the_config_default_is_the_one_constant():
    from kiro_crew.config.loader_builders import _crew_mode_agent_or_default
    from kiro_crew.config.sections_agent import AgentConfig

    assert AgentConfig().crew_mode_agent == crew_mode.DEFAULT_CREW_MODE_AGENT
    assert _crew_mode_agent_or_default("  ") == crew_mode.DEFAULT_CREW_MODE_AGENT


# ── folder no-ops for a Crew Mode tree ────────────────────────────────────────


def _crew_slots(**over: Any) -> list[dict[str, Any]]:
    caller = {
        "key": "chat-1-100",
        "title": "Crew root",
        "folder_id": "",
        "created": "2026-09-14T05:00:00.000001+00:00",
        crew_mode.ROOT_FIELD: True,
        # A chat that turned Crew Mode on: the folder rule keys on the stamp.
        crew_mode.STAMP_FIELD: "kirocrew-conductor",
        **over,
    }
    return [caller]


def _routes(slots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "GET /api/chat/folders": list(_FOLDERS),
        "GET /api/chat/slots": slots,
        "POST /api/chat/folders": {"id": "newfolder000", "name": "x", "parent_id": ""},
        "PATCH /api/chat/slots/{slot}/folder": {"ok": True},
        "POST /api/session-control/create": {"key": "chat-9-900", "title": "w"},
    }


def test_file_self_leaves_a_crew_mode_root_where_it_is():
    out, dash = _call("chat_folder_file_self", {"folder": "Travel"}, _routes(_crew_slots()))
    assert _writes(dash) == []
    assert "folder ignored" in out


def test_file_self_leaves_a_crew_mode_worker_where_it_is():
    root = _crew_slots(key="chat-0-1")[0]
    slots = [
        root,
        *_crew_slots(
            **{
                crew_mode.ROOT_FIELD: False,
                crew_mode.STAMP_FIELD: "",
                crew_mode.WORKER_FIELD: "chat-0-1",
            }
        ),
    ]
    out, dash = _call("chat_folder_file_self", {"folder": "Goal/agent"}, _routes(slots))
    assert _writes(dash) == []
    assert "folder ignored" in out


def test_file_self_still_files_a_plain_session():
    slots = _crew_slots(**{crew_mode.ROOT_FIELD: False})
    _, dash = _call("chat_folder_file_self", {"folder": "Travel"}, _routes(slots))
    assert [w.route for w in _writes(dash)] == [
        "PATCH /api/chat/slots/{slot}/folder".replace("{slot}", "chat-1-100")
    ]


def test_session_create_from_a_crew_mode_root_files_nothing_and_says_so():
    out, dash = _call(
        "session_create", {"title": "haiku", "folder": "Goal/worker"}, _routes(_crew_slots())
    )
    writes = _writes(dash)
    # No folder was created and the create carries no folder.
    assert [w.route for w in writes] == ["POST /api/session-control/create"]
    assert "folder_id" not in writes[0].body
    assert "folder ignored" in out


def test_session_create_from_a_crew_mode_root_still_names_an_existing_folder():
    """An existing folder is passed on, so the server gives the worker that folder's
    project directory (and files nothing); no folder is created for it."""
    routes = {
        **_routes(_crew_slots()),
        "POST /api/session-control/create": {
            "target": "chat-9-900",
            "title": "w",
            "folder_ignored": "crew_mode",
        },
    }
    out, dash = _call("session_create", {"title": "haiku", "folder": "Travel"}, routes)
    writes = _writes(dash)
    assert [w.route for w in writes] == ["POST /api/session-control/create"]
    assert writes[0].body["folder_id"] == "cccccccccccc"
    assert "folder ignored" in out
    # The reply never says the worker was filed.
    assert "filed in" not in out


def test_session_fork_from_a_crew_mode_root_names_no_folder_even_an_existing_one():
    """Fork has no server-side Crew Mode rule, so it gets no folder at all."""
    routes = {**_routes(_crew_slots()), "POST /api/session-control/fork": {"key": "chat-9-901"}}
    out, dash = _call("session_fork", {"source": "chat-1-100", "folder": "Travel"}, routes)
    writes = _writes(dash)
    assert [w.route for w in writes] == ["POST /api/session-control/fork"]
    assert "folder_id" not in writes[0].body
    assert "folder ignored" in out


# ── the worker list and steer routes ──────────────────────────────────────────


def _state(rows: list[dict[str, Any]], live: dict[str, Any] | None = None) -> Any:
    def serialize_slots(**_k: Any) -> list[dict[str, Any]]:
        out = [dict(r) for r in rows]
        crew_mode.stamp_rows(out)
        return out

    live = live or {}
    return SimpleNamespace(
        serialize_slots=serialize_slots,
        get_slot=lambda key: live.get(key),
        push_slots_update=lambda: None,
    )


_TREE = [
    _row("root", agent="kirocrew-conductor"),
    _row("w1", created_by="root", title="Haiku one", running=True),
    _row(
        "w2",
        created_by="w1",
        title="Haiku two",
        pending_approval=True,
        last_message="**Two lines done**\nthird next",
    ),
    _row("other", title="Not a worker"),
]


@pytest.fixture
def _open_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _allow)
    monkeypatch.setattr(crew_workers, "_closed_workers", lambda root, live: [])
    monkeypatch.setattr(
        crew_workers,
        "_ledger_items",
        lambda root: {"w1": crew_workers.LedgerFacts("it_1", "Writing the haiku")},
    )
    return crew_workers


def _req(method: str, path: str, state: Any, match: dict[str, str], body: Any = None) -> Any:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app, match_info=match)
    if body is not None:

        async def _json() -> Any:
            return body

        req.json = _json  # type: ignore[method-assign]
    return req


@pytest.mark.asyncio
async def test_the_worker_list_is_the_roots_tree_with_status_and_item(_open_gate):
    resp = await _open_gate.api_crew_workers(
        _req("GET", "/api/chat/slots/root/crew-workers", _state(_TREE), {"slot": "root"})
    )
    body = json.loads(resp.text)
    by_key = {w["key"]: w for w in body["workers"]}
    assert set(by_key) == {"w1", "w2"}
    assert by_key["w1"]["status"] == "working"
    assert by_key["w2"]["status"] == "needs_approval"
    assert by_key["w1"]["ledger_item"] == "it_1"
    assert by_key["w1"]["title"] == "Haiku one"
    # The card's one line: the task line from the ledger, else the last reply.
    assert by_key["w1"]["summary"] == "Writing the haiku"
    assert by_key["w2"]["summary"] == "Two lines done"
    # Who dispatched whom: w2 was dispatched by the sub-conductor w1.
    assert by_key["w1"]["dispatched_by"] == "root"
    assert by_key["w2"]["dispatched_by"] == "w1"


@pytest.mark.asyncio
async def test_a_grandchild_is_grouped_by_the_ledger_its_sub_conductor_keeps(
    _open_gate, monkeypatch
):
    from kiro_crew.dashboard.handlers import crew_workers

    # w2 was dispatched by the sub-conductor w1, which bound it on w1's own ledger.
    ledgers = {
        "root": {"w1": crew_workers.LedgerFacts("it_1", "Haiku", "")},
        "w1": {"w2": crew_workers.LedgerFacts("it_9", "Second line", crew_workers.GROUP_DONE)},
    }
    monkeypatch.setattr(crew_workers, "_ledger_items", lambda key: ledgers.get(key, {}))
    rows = [{**r, "running": False, "pending_approval": False} for r in _TREE]
    resp = await _open_gate.api_crew_workers(_req("GET", "/x", _state(rows), {"slot": "root"}))
    by_key = {w["key"]: w for w in json.loads(resp.text)["workers"]}
    assert by_key["w2"]["ledger_item"] == "it_9"
    assert by_key["w2"]["group"] == crew_workers.GROUP_DONE


@pytest.mark.asyncio
async def test_the_worker_list_refuses_a_session_that_is_not_a_crew_mode_root(_open_gate):
    resp = await _open_gate.api_crew_workers(
        _req("GET", "/api/chat/slots/other/crew-workers", _state(_TREE), {"slot": "other"})
    )
    assert resp.status == 404
    assert json.loads(resp.text)["code"] == "not_crew_mode_root"


@pytest.mark.asyncio
async def test_Y3_a_closed_roots_list_is_read_from_saved_data(_open_gate, monkeypatch):
    """Edge review R2 Y3: a reload under a closed root keeps the saved root title
    and the worker's real state; the list is not a failed read."""
    from kiro_crew.dashboard.handlers import crew_workers

    crew_mode.note_roots(["root"])
    monkeypatch.setattr(
        crew_workers,
        "_ledger_items",
        lambda root: {"w1": crew_workers.LedgerFacts("it_1", "Haiku", crew_workers.GROUP_DONE)},
    )
    monkeypatch.setattr(
        crew_workers,
        "_closed_titles",
        lambda state, keys: {k: ("Toy Flag Review" if k == "root" else k, "") for k in keys},
    )
    # w1 finished (idle) and its item was accepted; the root's tab is closed.
    rows = [{**r, "running": False} for r in _TREE if r["key"] != "root"]
    resp = await _open_gate.api_crew_workers(_req("GET", "/x", _state(rows), {"slot": "root"}))
    assert resp.status == 200, resp.text
    body = json.loads(resp.text)
    assert body["root_closed"] is True
    assert body["root_title"] == "Toy Flag Review"
    by_key = {w["key"]: w for w in body["workers"]}
    assert by_key["w1"]["group"] == crew_workers.GROUP_DONE
    # An unknown session is still refused, closed or not.
    resp = await _open_gate.api_crew_workers(_req("GET", "/x", _state(rows), {"slot": "nope"}))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_the_routes_are_owner_only(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _allow)
    monkeypatch.setattr(crew_workers, "require_owner_dashboard_request", _deny)
    state = _state(_TREE)
    listed = await crew_workers.api_crew_workers(_req("GET", "/x", state, {"slot": "root"}))
    steered = await crew_workers.api_crew_worker_steer(
        _req("POST", "/x", state, {"slot": "root", "worker": "w1"}, {"message": "hi"})
    )
    assert listed.status == steered.status == 403


@pytest.mark.asyncio
async def test_the_routes_deny_an_app_token(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers

    async def _deny(*_a: Any, **_k: Any) -> web.Response:
        return web.json_response({"code": "not_found"}, status=404)

    monkeypatch.setattr(crew_workers, "_deny_app_caller", _deny)
    resp = await crew_workers.api_crew_workers(_req("GET", "/x", _state(_TREE), {"slot": "root"}))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_steer_refuses_a_session_outside_the_roots_tree(_open_gate):
    resp = await _open_gate.api_crew_worker_steer(
        _req("POST", "/x", _state(_TREE), {"slot": "root", "worker": "other"}, {"message": "hi"})
    )
    assert resp.status == 404
    assert json.loads(resp.text)["code"] == "not_a_worker"


@pytest.mark.asyncio
async def test_steer_refuses_an_empty_message(_open_gate):
    resp = await _open_gate.api_crew_worker_steer(
        _req("POST", "/x", _state(_TREE), {"slot": "root", "worker": "w1"}, {"message": "  "})
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_steer_cuts_into_a_running_turn_as_the_person(_open_gate, monkeypatch):
    from kiro_crew.dashboard import chat_delivery, session_control

    calls: dict[str, Any] = {}

    async def _steer(state, slot, message, **kw):
        calls["steer"] = (slot.key, message, kw)
        return chat_delivery.STEER_STEERED

    monkeypatch.setattr(chat_delivery, "steer_into_running_turn", _steer)
    monkeypatch.setattr(session_control, "containment_meta", lambda state, slot: {"c": 1})
    worker = SimpleNamespace(key="w1", turn_running=True, running=True, _app="")
    monkeypatch.setattr("kiro_crew.dashboard.relay_archive.is_relay_archive", lambda s: False)
    resp = await _open_gate.api_crew_worker_steer(
        _req(
            "POST",
            "/x",
            _state(_TREE, {"w1": worker}),
            {"slot": "root", "worker": "w1"},
            {"message": "shorter"},
        )
    )
    assert json.loads(resp.text)["steered"] is True
    key, message, kw = calls["steer"]
    assert (key, message) == ("w1", "shorter")
    # The person's own text: the composer's human origin, not a peer envelope.
    assert kw["user_origin"] is True


@pytest.mark.asyncio
async def test_steer_queues_and_starts_an_idle_worker(_open_gate, monkeypatch):
    from kiro_crew.dashboard import chat_delivery, chat_runner, session_control

    calls: dict[str, Any] = {}

    def _queue(state, slot, message, **kw):
        calls["queue"] = (slot.key, message, kw)
        return "q1"

    async def _start(state, slot, **kw):
        calls["start"] = kw
        return True

    monkeypatch.setattr(chat_delivery, "queue_for_next_turn", _queue)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _start)
    monkeypatch.setattr(session_control, "containment_meta", lambda state, slot: {})
    monkeypatch.setattr("kiro_crew.dashboard.relay_archive.is_relay_archive", lambda s: False)
    worker = SimpleNamespace(key="w1", turn_running=False, running=False, _app="")
    resp = await _open_gate.api_crew_worker_steer(
        _req(
            "POST",
            "/x",
            _state(_TREE, {"w1": worker}),
            {"slot": "root", "worker": "w1"},
            {"message": "next"},
        )
    )
    body = json.loads(resp.text)
    assert body["started"] is True
    assert calls["queue"][2]["directive_user_origin"] is True
    # The composer's own default: no Run-now override past running sub-agents.
    assert "allow_user_during_subagents" not in calls["start"]


@pytest.mark.asyncio
async def test_a_held_steer_is_reported_queued_persisted_and_shown(_open_gate, monkeypatch):
    """F3/F4: sub-agents hold the drain, so the text waits: durable, and on screen."""
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery, chat_runner, session_control

    persisted: list[str] = []
    frames: list[tuple[str, dict]] = []

    async def _held(state, slot, **kw):
        return False

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _held)
    monkeypatch.setattr(session_control, "containment_meta", lambda state, slot: {})
    monkeypatch.setattr(
        chat_delivery, "start_queue_persist", lambda st, sl: persisted.append(sl.key)
    )
    monkeypatch.setattr(chat_delivery, "warn_if_not_durable", lambda *a: None)
    monkeypatch.setattr(crew_log_emit, "on_message_queued", lambda *a, **k: None)
    monkeypatch.setattr("kiro_crew.dashboard.relay_archive.is_relay_archive", lambda s: False)
    queue: list[dict[str, Any]] = []

    def _append(message: str, *a: Any, **kw: Any) -> str:
        # As the slot's queue does: the entry stays queued while the drain is held.
        queue.append({"id": "q7", "content": message})
        return "q7"

    worker = SimpleNamespace(
        key="w1",
        turn_running=False,
        running=False,
        _app="",
        _queue=queue,
        _acp_client=None,
        queue_append=_append,
    )
    state = _state(_TREE, {"w1": worker})
    state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
    resp = await _open_gate.api_crew_worker_steer(
        _req("POST", "/x", state, {"slot": "root", "worker": "w1"}, {"message": "wait"})
    )
    body = json.loads(resp.text)
    assert (body["queued"], body["started"]) == (True, False)
    assert persisted == ["w1"]
    assert [kind for kind, _ in frames] == ["queue_push"]
    assert frames[0][1]["queue_id"] == "q7"


def test_the_routes_are_registered_under_the_slot():
    # Through the gateway's own registration (it binds the handler modules).
    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    resolved = {}
    for resource in app.router.resources():
        info = resource.get_info()
        canonical = info.get("formatter") or info.get("path")
        if canonical and canonical.startswith("/api/chat/slots/{slot}/crew-"):
            resolved[canonical] = sorted(r.method for r in resource if r.method != "HEAD")
    assert resolved == {
        "/api/chat/slots/{slot}/crew-workers": ["GET"],
        "/api/chat/slots/{slot}/crew-timeline": ["GET"],
        "/api/chat/slots/{slot}/crew-workers/{worker}/steer": ["POST"],
        # The conductor's approval card (owner decision B).
        "/api/chat/slots/{slot}/crew-workers/{worker}/approve": ["POST"],
    }


@pytest.mark.asyncio
async def test_a_closed_worker_keeps_its_saved_title(_open_gate, monkeypatch):
    """F9: a closed tab's card names it from its saved metadata, else its short key."""
    monkeypatch.setattr(
        _open_gate, "_closed_workers", lambda root, live: ["chat-8-1792540567", "chat-9-1792540599"]
    )
    log = SimpleNamespace(
        get_metadata=lambda key: (
            {"title": "Haiku three", "agent": "kirocrew-worker"}
            if key == "dashboard:chat-8-1792540567"
            else None
        )
    )
    state = _state(_TREE)
    state.conversation_log = log
    resp = await _open_gate.api_crew_workers(_req("GET", "/x", state, {"slot": "root"}))
    by_key = {w["key"]: w for w in json.loads(resp.text)["workers"]}
    assert by_key["chat-8-1792540567"]["title"] == "Haiku three"
    assert by_key["chat-8-1792540567"]["agent"] == "kirocrew-worker"
    assert by_key["chat-9-1792540599"]["title"] == "chat-9"


def test_a_worker_line_is_one_plain_line_cut_to_the_card():
    from kiro_crew.dashboard.handlers import crew_workers

    assert crew_workers._one_line("\n\n## Done!\nmore") == "Done!"
    assert len(crew_workers._one_line("x" * 500)) == crew_workers.WORKER_LINE_MAX
    assert crew_workers._worker_line({"last_message": 3}, "") == ""


def test_a_relayed_seed_is_not_a_worker_line_its_last_reply_is():
    from kiro_crew.dashboard.handlers import crew_workers

    slot = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "**Menu drafted**\nnext"},
            {"role": "user", "content": "[sent by session chat-1 via session_send]\n\nRetry"},
        ]
    )
    for seed in (
        "[sent by session chat-1 via session_send] Retry",
        "[quoted: sent by session chat-1 via session_send] Retry",
    ):
        assert crew_workers._worker_line({"last_message": seed}, "", slot) == "Menu drafted"
    assert (
        crew_workers._worker_line({"last_message": "[sent by session x via session_send]"}, "")
        == ""
    )


# ── the board and the per-turn block follow the family ───────────────────────


@pytest.mark.asyncio
async def test_the_board_is_adopted_for_a_swapped_in_conductor(monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import card_lifecycle

    crew_mode.remember(SimpleNamespace(agent=SimpleNamespace(crew_mode_agent="better-conductor")))
    adopted: list[str] = []
    monkeypatch.setattr(card_lifecycle, "is_root_session", lambda slot: True)
    monkeypatch.setattr(
        dashboard_agentic, "adopt_crew_mode_board", lambda key: adopted.append(key) or False
    )
    slot = SimpleNamespace(key="root", agent_kind="template", memory_mode="persistent")
    await dashboard_agentic.adopt_crew_mode_board_on_switch(slot, "better-conductor")
    await dashboard_agentic.adopt_crew_mode_board_on_switch(slot, "kirocrew")
    assert adopted == ["root"]


# A fake AWS key id, split so no scanner reads it as a real one.
_FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def test_a_long_last_reply_is_redacted_before_it_is_cut():
    from kiro_crew.dashboard.handlers import crew_workers

    secret = "AKIA" + "Q" * 16
    text = "x" * 140 + " **AKIA**" + secret[4:] + " done"
    slot = SimpleNamespace(messages=[{"role": "assistant", "content": text}])
    seed = "[sent by session c via session_send] go"
    line = crew_workers._worker_line({"last_message": seed}, "", slot)
    assert "Q" * 8 not in line


def test_a_last_reply_standing_in_for_a_seed_is_redacted():
    from kiro_crew.dashboard.handlers import crew_workers

    secret = "AKIA" + "Z" * 16
    slot = SimpleNamespace(messages=[{"role": "assistant", "content": f"key {secret} saved"}])
    seed = "[sent by session c via session_send] go"
    line = crew_workers._worker_line({"last_message": seed}, "", slot)
    assert line and secret not in line


def test_a_workers_item_comes_from_the_roots_own_ledger_through_the_board_seam(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_workers, work_ledger_board

    items = [
        SimpleNamespace(
            item_id="it_1",
            worker_session_key="dashboard:w1",
            title="Write it",
            summary=f"Draft done, key {_FAKE_KEY}",
        ),
        SimpleNamespace(item_id="it_2", worker_session_key=None, title="", summary=""),
    ]
    seen: list[str] = []

    def _records(slot_key):
        seen.append(slot_key)
        return None, items

    monkeypatch.setattr(work_ledger_board, "task_records", _records)
    out = crew_workers._ledger_items("root")
    assert list(out) == ["w1"]
    item, line, _progress = out["w1"]
    assert item == "it_1"
    # The latest report's line, through the board's egress redactor.
    assert line.startswith("Draft done") and _FAKE_KEY not in line
    assert seen == ["root"]


@pytest.mark.parametrize(
    ("status", "progress", "group"),
    [
        # A tool approval is the one thing the conductor cannot answer: the person
        # resolves it on the conductor's approval card.
        ("needs_approval", "", "approval"),
        ("working", "", "working"),
        ("closed", "", "done"),
        ("idle", "done", "done"),
        # A worker's question or blocker waits on its conductor, never the person.
        ("idle", "with_conductor", "with_conductor"),
        ("asking", "", "with_conductor"),
        # Replied but never said it finished: idle, not done (U8).
        ("idle", "", "waiting"),
    ],
)
def test_the_side_panel_group_of_a_worker(status, progress, group):
    from kiro_crew.dashboard.handlers import crew_workers

    assert crew_workers._group(status, progress) == group


@pytest.mark.parametrize(
    ("fields", "progress"),
    [
        ({"is_terminal": True, "status": None, "reason": None}, "done"),
        ({"is_terminal": False, "status": "done", "reason": None}, "done"),
        # Edge review R1 S1: whatever the reason, the conductor answers it (and asks
        # the person in its own chat when only they can decide).
        ({"is_terminal": False, "status": "question", "reason": "needs_human"}, "with_conductor"),
        ({"is_terminal": False, "status": "blocked", "reason": "approval"}, "with_conductor"),
        ({"is_terminal": False, "status": "question", "reason": None}, "with_conductor"),
        ({"is_terminal": False, "status": "blocked", "reason": None}, "with_conductor"),
        ({"is_terminal": False, "status": "progress", "reason": None}, ""),
    ],
)
def test_a_ledger_item_says_done_or_with_conductor_only_from_its_own_fields(fields, progress):
    from kiro_crew.dashboard.handlers import crew_workers

    assert crew_workers._progress_of(SimpleNamespace(**fields)) == progress


@pytest.mark.asyncio
async def test_the_worker_list_carries_each_workers_group(_open_gate):
    resp = await _open_gate.api_crew_workers(
        _req("GET", "/api/chat/slots/root/crew-workers", _state(_TREE), {"slot": "root"})
    )
    by_key = {w["key"]: w for w in json.loads(resp.text)["workers"]}
    assert by_key["w1"]["group"] == "working"
    assert by_key["w2"]["group"] == "approval"


def test_an_adopted_worker_belongs_to_the_root_that_adopted_it():
    """``session_adopt`` moves the crew log's parent edge; the birth stamp stays.
    Membership follows the current edge."""
    rows = _stamped(
        _row("A", agent="kirocrew-conductor"),
        _row("B", agent="kirocrew-conductor"),
        _row("w", created_by="A", parent={"slot": "B", "key": "B"}),
    )
    assert rows["w"][crew_mode.WORKER_FIELD] == "B"


def test_a_released_worker_stands_on_its_own_once_the_lineage_is_settled():
    """``session_release`` clears the crew log edge; the birth stamp stays. With the
    lineage settled a ``None`` edge is the answer, and the worker leaves the tree;
    while the lineage is not known, the birth stamp still nests it."""
    rows = [
        _row("A", agent="kirocrew-conductor"),
        _row("w", created_by="A", parent=None),
    ]
    settled = [dict(r) for r in rows]
    crew_mode.stamp_rows(settled, settled=True)
    assert crew_mode.WORKER_FIELD not in settled[1]
    unknown = [dict(r) for r in rows]
    crew_mode.stamp_rows(unknown)
    assert unknown[1][crew_mode.WORKER_FIELD] == "A"
    pending = [dict(r) for r in rows]
    pending[1]["lineage_pending"] = True
    crew_mode.stamp_rows(pending, settled=True)
    assert pending[1][crew_mode.WORKER_FIELD] == "A"
