"""A Slack code channel's own session may conduct; nothing else it touches widens.

The code-channel dispatch exemption
(``session_control_parts/code_channel_exemption.py``) is the one predicate the three
channel-containment gates consult -- the creator gate, the session-control caller
half and the work-ledger entry gate -- for the one mirrored slot the owner-DM
exemption cannot admit. These tests pin both directions:

* the channel's recorded session, on an owner-driven turn, can create workers in
  its channel folder, drive and close them, and reach its own ledger -- which it
  reads as the published-fields projection only; and
* every non-qualifying case is still refused: the switch off, a slot the store does
  not name (or names falsely), a channel shared outside the workspace, a turn that
  carried a non-owner's text, a verb that reads a transcript, another session, a
  folder outside the channel's, and a worker trying to dispatch (depth 2).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from test_session_control_owner_dm import _key, _state

from kiro_crew import work_ledger as wl
from kiro_crew.config import live as live_config_mod
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import audience
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.client import parse_channel_info
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import Binding, CodeChannelStore, set_store

OWNER = "U0OWNER0001"
STRANGER = "U0STRANGER1"
CHANNEL = "C0CODECHAN1"
TEAM = "T0TEAM00001"
ANCHOR = "1786300000.000100"
CHANNEL_FOLDER = "fold-channel"
OTHER_FOLDER = "fold-elsewhere"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch, tmp_path):
    """Session control on, code channels on, the owner opted in to dispatch, owner-only Slack."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(cc_binding, "code_channels_on", lambda cfg: cfg is not None)
    _floor_monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=True))
    _floor_monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    _floor_monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    create_rate_limit.reset_for_tests()
    audience.clear()
    cce.forget_senders()
    # The dispatch switch's config-path reading is taken off the loop by every verb
    # before it judges; tests that call the judgement directly take it here. Its
    # freshness window is widened so a slow host never reads it as stale (the
    # staleness test sets the stamp itself).
    _floor_monkeypatch.setattr(cce, "_PATH_CHECK_TTL_S", 1e9)
    cce._forget_config_path_check()
    live_config_mod._clear_torn_files()
    asyncio.run(cce.refresh_config_path_check())
    yield
    set_store(None)
    audience.clear()
    cce.forget_senders()
    cce._forget_config_path_check()
    live_config_mod._clear_torn_files()
    create_rate_limit.reset_for_tests()


def _cfg(*, dispatch: bool) -> Any:
    return SimpleNamespace(slack=SimpleNamespace(code_channel_dispatch=dispatch))


def _channel(state, tmp_path, *, shared: bool | None = False, bind: bool = True):
    """A code channel's session as the runtime builds it: a dashboard slot filed in
    the channel's folder, linked to the kickoff anchor, recorded in the store."""
    state._folders = [
        {"id": "fold-root", "name": "Code channels", "parent_id": ""},
        {"id": CHANNEL_FOLDER, "name": "chan", "parent_id": "fold-root"},
        {"id": "fold-sub", "name": "kirocrew-worker", "parent_id": CHANNEL_FOLDER},
        {"id": OTHER_FOLDER, "name": "other", "parent_id": ""},
    ]
    slot = state.get_or_create_slot("chat-code-channel")
    slot.folder_id = CHANNEL_FOLDER
    state.link_slack(slot.key, ANCHOR, CHANNEL)
    store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    store.load()
    if bind:
        store.put_binding(
            Binding(channel_id=CHANNEL, team_id=TEAM, session_key=slot.key, anchor_ts=ANCHOR)
        )
    set_store(store)
    if shared is not None:
        audience.note_channel_info(
            ChannelInfo(channel_id=CHANNEL, is_code_channel=True, shared=shared)
        )
    return slot, store


def _owner_turn(slot) -> None:
    """The slot is running a turn the owner's Slack message opened."""
    cce.note_channel_sender(slot, OWNER)
    slot._turn_channel_origin = True


def _create(state, slot, **kw) -> dict[str, Any]:
    return asyncio.run(sc.create_session(state, caller_session_key=_key(slot), **kw))


def _refused_create(state, slot, **kw) -> sc.SessionControlError:
    with pytest.raises(sc.SessionControlError) as exc:
        _create(state, slot, **kw)
    return exc.value


# ── the ledger route harness, real state ─────────────────────────────────


@pytest.fixture
def ledger_routes_open(monkeypatch):
    """Bypass recognition/restriction and the crew log, whose own suites cover them."""

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognized)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)
    monkeypatch.setattr(routes.crew_log_emit, "on_crew_dispatch", lambda *a, **k: True)
    monkeypatch.setattr(
        routes.conductor_patrol, "ensure_patrol", AsyncMock(return_value=("existing", "test"))
    )
    monkeypatch.setattr(routes.conductor_patrol, "patrol_note", lambda *a: "")


def _req(state, method: str, path: str, sk: str, body: Any = ...) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app, headers={"X-Session-Key": sk})
    req["internal_auth"] = True
    if body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _record(state, sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = asyncio.run(
        routes.api_work_ledger_record(_req(state, "POST", "/api/work-ledger/record", sk, body))
    )
    return resp.status, json.loads(resp.text)


def _read(state, sk: str) -> tuple[int, dict[str, Any]]:
    resp = asyncio.run(routes.api_work_ledger_get(_req(state, "GET", "/api/work-ledger", sk)))
    return resp.status, json.loads(resp.text)


def _report(state, sk: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = asyncio.run(
        routes.api_work_report(_req(state, "POST", "/api/work-ledger/report", sk, body))
    )
    return resp.status, json.loads(resp.text)


# ── admitted: the channel's own session conducts ─────────────────────────


def test_the_channels_session_creates_a_worker_in_its_folder_and_drives_it(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)

    created = _create(state, slot)
    worker = state.get_slot(created["target"])
    assert worker._created_by == slot.key
    # Filed in the channel's folder, whose project is the channel's worktree.
    assert worker.folder_id == CHANNEL_FOLDER
    for op in ("send", "status", "stop", "end_wait"):
        target = sc.authorize_target(
            state, caller_session_key=_key(slot), target=worker.key, operation=op
        )
        assert target.key == worker.key, op


def test_workers_file_beside_the_conductor_never_in_an_agent_named_folder(tmp_path, monkeypatch):
    """Kyle's build-3 test filed a worker under ``Code channels/kirocrew-worker``.
    Workers now file in the conductor's own folder and nest under its session."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    exc = _refused_create(state, slot, folder_id="fold-sub")  # Code channels/chan/kirocrew-worker
    assert exc.code == "code_channel_folder" and "leave the folder out" in exc.message
    created = _create(state, slot)
    worker = state.get_slot(created["target"])
    assert worker.folder_id == slot.folder_id == CHANNEL_FOLDER
    assert worker._created_by == slot.key, "nests under the conductor's session"
    same = _create(state, slot, folder_id=CHANNEL_FOLDER)
    assert state.get_slot(same["target"]).folder_id == CHANNEL_FOLDER
    assert cce.CONDUCTOR_GUIDANCE == created["guidance"]


def test_a_dashboard_turn_and_a_patrol_wake_qualify_without_a_slack_author(tmp_path, monkeypatch):
    """Neither carries channel text, so no Slack author has to be on record."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    slot._turn_channel_origin = False
    slot._turn_channel_narrowed = False
    assert cce.admits(cce.judge_code_channel(state, slot, operation="create"))


def test_the_channels_conductor_runs_its_ledger_and_reads_published_fields_only(
    tmp_path, monkeypatch, ledger_routes_open
):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    sk = _key(slot)

    goal = "Fix the flaky test <!channel>\nprivate second line"
    status, body = _record(state, sk, {"action": "goal", "goal": goal, "round": 1})
    assert status == 200, body
    # The goal's first line only, defused (the amended published-fields rule).
    assert body["conductor"]["goal"] == "Fix the flaky test !channel"
    status, body = _record(
        state,
        sk,
        {
            "action": "create",
            "title": "Fix the flaky test <!here>",
            "acceptance": {"kind": "pr_checks", "pr": 41, "repo": "owner/name"},
        },
    )
    assert status == 200, body
    assert body.get("published_fields_only") is True
    assert "acceptance" not in body["item"]
    item_id = body["item"]["item_id"]
    # The ledger opened at the last conducting level: its workers may not conduct.
    record = wl.read_conductor(routes.session_ledger.ledger_key(sk))
    assert record is not None and record.depth == wl.MAX_DEPTH - 1

    worker = state.get_slot(_create(state, slot)["target"])
    status, body = _record(
        state, sk, {"action": "bind", "item_id": item_id, "worker_session_key": worker.key}
    )
    assert status == 200, body
    status, body = _record(
        state, sk, {"action": "decide", "item_id": item_id, "decision": "secret decision"}
    )
    assert status == 200, body
    assert "decision" not in body.get("item", {})

    long_line = "ghp_" + "A" * 36 + " pushed the fix <@U0SOMEONE> @channel " + "x" * 200
    status, _ = _report(
        state,
        slot_history_key(worker),
        {
            "status": "done",
            "summary": f"{long_line}\nsecond line never shown",
            "artifacts": {"branch": "fix/flaky", "log": "/tmp/private.log"},
            "pr": 41,
        },
    )
    assert status == 200

    status, board = _read(state, sk)
    assert status == 200, board
    assert board["published_fields_only"] is True
    assert "accept_batch" not in board
    (row,) = board["items"]
    assert set(row) == {
        "item_id",
        "title",
        "state",
        "summary",
        "branch",
        "pr",
        "ci",
        "worker_session_key",
    }
    assert row["title"] == "Fix the flaky test !here"  # markup defused, not deleted
    assert row["state"] == "done"
    assert row["branch"] == "fix/flaky" and row["pr"] == 41
    assert len(row["summary"]) <= cce.PUBLISHED_SUMMARY_MAX
    assert "second line" not in row["summary"]
    assert "ghp_" not in row["summary"]
    assert "<@" not in row["summary"] and "@channel" not in row["summary"]
    rendered = json.dumps(board)
    assert board["conductor"]["goal"] == "Fix the flaky test !channel"
    for secret in ("private second line", "secret decision", "pr_checks", "/tmp/private.log"):
        assert secret not in rendered, secret


# ── refused: everything that does not qualify ────────────────────────────


def test_a_transcript_read_of_its_own_worker_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker_key = _create(state, slot)["target"]
    for op in ("read", "summary", "broadcast", "retry", "set_model"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(slot), target=worker_key, operation=op
            )
        assert exc.value.code == "mirrored_caller", op
        assert "code-channel exemption is withheld" in exc.value.message


def test_the_channels_session_cannot_reach_a_session_it_did_not_create(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    state.get_or_create_slot("chat-the-persons-own-tab")
    for op in ("send", "status", "stop", "close"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=_key(slot),
                target="chat-the-persons-own-tab",
                operation=op,
            )
        assert exc.value.code == "not_creator", op
    assert sc._caller_is_ownership_fenced(state, slot.key) is True
    assert sc._delegation_lineage_fenced(state, slot.key) is True


def test_the_switch_off_restores_full_containment(tmp_path, monkeypatch, ledger_routes_open):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=False))

    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert '"Let the conductor hand off work"' in exc.message
    assert "(slack.code_channel_dispatch)" in exc.message
    status, body = _record(state, _key(slot), {"action": "goal", "goal": "g", "round": 1})
    assert status == 403 and body["code"] == "channel_session"


def test_a_switch_off_refusal_asks_for_the_hand_off_notice_and_on_forgets_it(tmp_path, monkeypatch):
    from kiro_crew.slack.code_channels import handoff

    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    asked: list[str] = []
    forgot: list[str] = []
    monkeypatch.setattr(handoff, "notice_soon", lambda c: asked.append(c) or True)
    monkeypatch.setattr(handoff, "forget", forgot.append)
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=False))
    verdict = cce.judge_code_channel(state, slot, operation=cce.CREATE_OPERATION)
    assert verdict.refusal == cce.DISPATCH_OFF_REFUSAL
    assert asked == [verdict.channel_id] and forgot == []
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=True))
    cce.judge_code_channel(state, slot, operation=cce.CREATE_OPERATION)
    assert forgot == [verdict.channel_id] and asked == [verdict.channel_id]


def test_code_channels_off_restores_full_containment(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cc_binding, "code_channels_on", lambda cfg: False)
    assert _refused_create(state, slot).code == "mirrored_caller"


def test_a_slack_linked_slot_the_store_does_not_name_is_refused(
    tmp_path, monkeypatch, ledger_routes_open
):
    """A non-code-channel mirrored session: exactly today's refusals."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path, bind=False)
    _owner_turn(slot)
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "code-channel" not in exc.message
    status, body = _record(state, _key(slot), {"action": "goal", "goal": "g", "round": 1})
    assert status == 403 and body["code"] == "channel_session"


def test_a_forged_binding_naming_a_slot_not_linked_to_the_anchor_is_not_a_code_channel(
    tmp_path, monkeypatch
):
    """The row names the slot and the slot's own channel, but the anchor resolves to
    the real channel's session: only the anchor clause can refuse it."""
    state = _state(tmp_path, monkeypatch)
    _slot, store = _channel(state, tmp_path)
    other = state.get_or_create_slot("chat-not-the-channel")
    state.link_slack(other.key, "1786300000.000999", "C0OTHERROOM")
    store.put_binding(
        Binding(channel_id="C0OTHERROOM", team_id=TEAM, session_key=other.key, anchor_ts=ANCHOR)
    )
    assert cce.code_channel_binding_of(state, other) is None
    _owner_turn(other)
    assert _refused_create(state, other).code == "mirrored_caller"


def test_a_binding_whose_channel_is_not_the_slots_own_link_is_not_a_code_channel(
    tmp_path, monkeypatch
):
    """The row names the slot and its anchor, but a channel the slot is not linked
    to: only the channel clause can refuse it."""
    state = _state(tmp_path, monkeypatch)
    _channel(state, tmp_path)
    other = state.get_or_create_slot("chat-not-the-channel")
    state.link_slack(other.key, "1786300000.000999", "C0OTHERROOM")
    from kiro_crew.slack.code_channels.store import store as live_store

    live_store().put_binding(
        Binding(
            channel_id="C0FORGED001",
            team_id=TEAM,
            session_key=other.key,
            anchor_ts="1786300000.000999",
        )
    )
    assert cce.code_channel_binding_of(state, other) is None


def test_an_unloaded_store_reads_as_no_code_channel(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    set_store(CodeChannelStore(path=tmp_path / "slack-code-channels.json"))  # never loaded
    assert cce.code_channel_binding_of(state, slot) is None


@pytest.mark.parametrize("shared", [True, None], ids=["slack-connect", "unknown"])
def test_a_shared_or_unconfirmed_channel_is_refused(tmp_path, monkeypatch, shared):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path, shared=shared)
    _owner_turn(slot)
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "shared outside this workspace" in exc.message


def test_a_turn_carrying_a_non_owners_text_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    cce.note_channel_sender(slot, STRANGER)
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "someone other than the owner" in exc.message


def test_a_channel_turn_with_no_author_on_record_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    slot._turn_channel_origin = True  # e.g. after a restart, before any message
    assert "not on record" in _refused_create(state, slot).message


def test_a_steer_of_channel_text_narrows_the_turn(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    slot._turn_channel_origin = False
    slot._turn_channel_narrowed = True
    cce.note_channel_sender(slot, STRANGER)
    assert _refused_create(state, slot).code == "mirrored_caller"


def test_a_second_room_beside_the_channel_refuses(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    state.sessions.set_mirror_link(
        slot_history_key(slot), ChannelLink("discord", channel_id="dm-elsewhere")
    )
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "somewhere other than its code channel" in exc.message


def test_an_archived_channel_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    _owner_turn(slot)
    store.update_binding(CHANNEL, archived_at=1.0)
    assert "archived" in _refused_create(state, slot).message


def test_a_worker_folder_outside_the_channels_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    assert _refused_create(state, slot, folder_id=OTHER_FOLDER).code == "code_channel_folder"


def test_a_fork_of_the_channels_transcript_is_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    with pytest.raises(sc.SessionControlError) as exc:
        sc._refuse_ineligible_creator(state, slot, operation="fork")
    assert exc.value.code == "mirrored_caller"


def test_a_worker_cannot_dispatch_depth_two(tmp_path, monkeypatch, ledger_routes_open):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_worker_caller"

    # And through the ledger: bound to the channel's conductor, it may not open one.
    sk = _key(slot)
    assert _record(state, sk, {"action": "goal", "goal": "g", "round": 1})[0] == 200
    status, body = _record(
        state, sk, {"action": "create", "title": "t", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    status, _ = _record(
        state, sk, {"action": "bind", "item_id": item_id, "worker_session_key": worker.key}
    )
    assert status == 200
    status, body = _record(state, slot_history_key(worker), {"action": "goal", "goal": "g2"})
    assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED


# ── the shared scope predicate ───────────────────────────────────────────


# ── the audience reading ─────────────────────────────────────────────────


def test_share_flags_parse_fail_closed():
    base = {"properties": {"record_channel": {"record_type": "agent_channel"}}}
    assert parse_channel_info(CHANNEL, {**base, "is_ext_shared": True}).shared is True
    assert parse_channel_info(CHANNEL, {**base, "is_pending_ext_shared": True}).shared is True
    assert parse_channel_info(CHANNEL, {**base, "is_org_shared": True}).shared is True
    internal = {**base, "is_ext_shared": False, "is_shared": False}
    assert parse_channel_info(CHANNEL, internal).shared is False
    assert parse_channel_info(CHANNEL, base).shared is None


def test_an_answer_without_share_flags_forgets_an_earlier_reading():
    audience.note_channel_info(ChannelInfo(channel_id=CHANNEL, is_code_channel=True, shared=False))
    assert audience.channel_shared(CHANNEL) is False
    audience.note_channel_info(ChannelInfo(channel_id=CHANNEL, is_code_channel=True, shared=None))
    assert audience.channel_shared(CHANNEL) is None


def test_published_summary_is_one_redacted_unpinging_line():
    line = cce.published_summary("<!channel> done: see <@U0X> @here\nmore")
    assert "<" not in line and ">" not in line and "@here" not in line
    assert line.startswith("!channel done: see")
    assert len(cce.published_summary("y" * 500)) == cce.PUBLISHED_SUMMARY_MAX


@pytest.mark.parametrize(
    "smuggled", ["<<@U0FAKE>!here>", "<<!here>@U0OWNER>", "<<@U0X>!subteam^S0FAKE>", "@@herehere"]
)
def test_mention_markup_cannot_be_reassembled_by_the_defusing(smuggled):
    """Removing a nested mention must not leave a new one behind (review finding)."""
    for text in (cce.published_summary(f"done {smuggled}"), cce._defuse(smuggled)):
        assert "<" not in text and ">" not in text
        assert "@here" not in text.lower()


# ── review fixes: depth that outlives the conductor, a stale audience, config ──


def test_a_worker_stays_one_level_deep_after_its_conductor_closes(
    tmp_path, monkeypatch, ledger_routes_open
):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    # The conductor's tab is gone; the worker's ``_created_by`` and the store remain.
    state._slots.pop(slot.key)
    assert state.get_slot(slot.key) is None

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_worker_caller"
    status, body = _record(state, slot_history_key(worker), {"action": "goal", "goal": "g"})
    assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED


def test_an_unbound_worker_cannot_open_a_ledger(tmp_path, monkeypatch, ledger_routes_open):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    for action in (
        {"action": "goal", "goal": "g"},
        {"action": "create", "title": "t", "acceptance": {"kind": "human_approval"}},
    ):
        status, body = _record(state, slot_history_key(worker), action)
        assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED, action


def test_the_depth_limit_holds_when_slack_never_loaded_the_store(
    tmp_path, monkeypatch, ledger_routes_open
):
    """A restart without Slack connected: the store is not loaded by the Slack
    runtime, the conductor's tab is gone, and the worker is still refused."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    state._slots.pop(slot.key)
    set_store(CodeChannelStore(path=tmp_path / "slack-code-channels.json"))  # not loaded

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_worker_caller"
    status, body = _record(state, slot_history_key(worker), {"action": "goal", "goal": "g"})
    assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED


def test_a_replaced_channel_session_keeps_its_workers_one_level_deep(tmp_path, monkeypatch):
    """A binding replaced by a new session retires the old key, persisted."""
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    store.replace_binding(
        Binding(channel_id=CHANNEL, team_id=TEAM, session_key="chat-new-one", anchor_ts=ANCHOR)
    )
    reloaded = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    reloaded.load()
    set_store(reloaded)
    state._slots.pop(slot.key)
    assert sc.code_channel_root(state, worker) == slot.key


def test_a_stale_share_reading_refuses_and_asks_for_a_fresh_one(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    asked: list[str] = []
    monkeypatch.setattr(audience, "refresh_soon", lambda ch: asked.append(ch) or True)
    monkeypatch.setattr(audience, "READING_TTL_SECS", -1.0)
    exc = _refused_create(state, slot)
    assert "being checked now" in exc.message
    assert asked == [CHANNEL]


def test_a_failed_lookup_drops_an_earlier_not_shared_reading():
    from kiro_crew.slack.code_channels import recognition
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    audience.note_channel_info(ChannelInfo(channel_id=CHANNEL, is_code_channel=True, shared=False))

    class _Failing:
        async def channel_info(self, channel_id):
            raise CodeChannelApiError("conversations.info", "ratelimited")

    asyncio.run(recognition.classify_channel(_Failing(), CHANNEL, "U0BOT"))
    assert audience.channel_shared(CHANNEL) is None


def test_refresh_soon_records_the_fresh_answer(monkeypatch):
    raw = {"is_ext_shared": True, "is_shared": True}

    class _Client:
        async def channel_info(self, channel_id):
            return parse_channel_info(channel_id, raw)

    from kiro_crew.slack.code_channels import client as cc_client

    monkeypatch.setattr(
        cc_client.CodeChannelsClient, "from_slack", classmethod(lambda c, s: _Client())
    )

    async def _go():
        assert audience.refresh_soon(CHANNEL) is True
        assert audience.refresh_soon(CHANNEL) is False  # one in flight per channel
        for _ in range(20):
            await asyncio.sleep(0)
        return audience.channel_shared(CHANNEL)

    assert asyncio.run(_go()) is True


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(..., False), (True, True), (False, False), ("false", False), ("true", False), (None, False)],
)
def test_the_dispatch_switch_reads_only_a_real_true_as_on(stored, expected):
    from kiro_crew.config.section_builders import _build_slack_config

    data: dict[str, Any] = {} if stored is ... else {"code_channel_dispatch": stored}
    assert _build_slack_config(data).code_channel_dispatch is expected


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None, {}])
def test_a_present_non_bool_switch_loads_as_off(bad):
    """Opt-in: whatever validation does with a malformed value, it reads as off."""
    from config_loader_helpers import _load_from_dict

    cfg = _load_from_dict({"slack": {"code_channel_dispatch": bad}})
    assert cfg.slack.code_channel_dispatch is False


def test_dispatch_is_off_by_default_and_code_channels_on_does_not_turn_it_on():
    """Kyle's ruling (plain A): opt-in, and no coupling to ``slack.code_channels``."""
    from config_loader_helpers import _load_from_dict

    from kiro_crew.config.sections_slack import SlackConfig

    assert SlackConfig().code_channel_dispatch is False
    assert _load_from_dict({}).slack.code_channel_dispatch is False
    on = _load_from_dict({"slack": {"code_channels": True}})
    assert on.slack.code_channels is True and on.slack.code_channel_dispatch is False


@pytest.mark.parametrize("section", ["on", ["code_channel_dispatch"], None, 1])
def test_a_malformed_slack_section_reads_dispatch_off(monkeypatch, section):
    """Validation drops a non-object ``slack`` section before anything records it;
    the opt-in default makes that loss read as off (round-8 verifier)."""
    from config_loader_helpers import _load_from_dict

    cfg = _load_from_dict({"slack": section})
    assert cfg.slack.code_channel_dispatch is False
    monkeypatch.setattr(cce, "_live_config", lambda: cfg)
    assert cce._switch_refusal() != ""


def test_an_unreadable_store_keeps_workers_one_level_deep(
    tmp_path, monkeypatch, ledger_routes_open
):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    state._slots.pop(slot.key)
    broken = tmp_path / "broken-store.json"
    broken.write_text("{not json", encoding="utf-8")
    corrupt = CodeChannelStore(path=broken)
    corrupt.load()  # starts empty, remembers the failure
    set_store(corrupt)
    # The worker edits its lineage away too: an unreadable store vouches for no one.
    worker._created_by = ""
    assert sc.code_channel_root(state, worker) == sc.LINEAGE_UNAVAILABLE
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_lineage_unavailable"
    status, body = _record(state, slot_history_key(worker), {"action": "goal", "goal": "g"})
    assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED


def test_an_unreadable_store_is_never_overwritten(tmp_path):
    """A write over a file that could not be read would erase every worker record
    and retired key it held (CI round-3 GPT finding)."""
    from kiro_crew.slack.code_channels.store import Route, StoreWriteError

    broken = tmp_path / "slack-code-channels.json"
    text = json.dumps({"bindings": {}, "workers": {"chat-w": 3}})  # loads, but not whole
    broken.write_text(text, encoding="utf-8")
    corrupt = CodeChannelStore(path=broken)
    corrupt.load()
    assert corrupt.unreadable is True
    with pytest.raises(StoreWriteError):
        corrupt.record_decision(
            Route(session_id="s", origin_channel_id="C0", origin_ts="1", decision="code")
        )
    assert broken.read_text(encoding="utf-8") == text


def test_the_lineage_lookup_never_reads_the_disk_itself(tmp_path):
    """The gate reads the loaded snapshot only; an unloaded store raises (refusal)."""
    from kiro_crew.slack.code_channels.store import StoreUnavailable

    unloaded = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    with pytest.raises(StoreUnavailable):
        unloaded.session_keys()
    asyncio.run(sc.load_code_channel_lineage())  # the async entry points' preload


def test_a_preflight_for_a_root_folder_path_is_refused_before_any_folder_exists(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    exc = _refused_create(state, slot, dry_run=True)
    assert exc.code == "code_channel_folder"
    # A walk that would create ``kirocrew-worker`` inside the channel's folder too.
    assert _refused_create(state, slot, dry_run=True, folder_id=CHANNEL_FOLDER).code == (
        "code_channel_folder"
    )


def test_the_guidance_answers_status_questions_from_the_state_read():
    """The conductor's code-channel instructions point at session_status, with the
    report shape Kyle asked for, and do not tell it to hedge about transcripts."""
    from kiro_crew.slack.code_channels import routing

    guidance = cce.CONDUCTOR_GUIDANCE
    assert "session_status" in guidance and "since_activity_s" in guidance
    assert "working, last activity 40s ago" in guidance
    assert "waiting on you: approve shell" in guidance
    assert "dashboard_link" in guidance and "leave the folder out" in guidance
    assert guidance in routing._seed_framing("C0ORIGIN01", "")


def test_the_guidance_is_not_seeded_while_hand_off_is_off(monkeypatch):
    """With the switch off (its default) the guidance would steer the conductor
    into a refused call (First Principles, round 4)."""
    from kiro_crew.slack.code_channels import routing

    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=False))
    assert cce.CONDUCTOR_GUIDANCE not in routing._seed_framing("C0ORIGIN01", "")


@pytest.mark.parametrize(
    "payload",
    [
        {"bindings": {CHANNEL: {"channel_id": CHANNEL}}},  # a row missing its fields
        {
            "bindings": {
                "C0OTHER": {
                    "channel_id": CHANNEL,
                    "team_id": "T",
                    "session_key": "s",
                    "anchor_ts": "1",
                }
            }
        },
        {"bindings": [], "retired_session_keys": []},
        {"bindings": {}, "retired_session_keys": "chat-1"},
        {"bindings": {}, "retired_session_keys": ["chat-1", 7]},
    ],
)
def test_a_structurally_malformed_store_reads_as_unavailable_not_empty(tmp_path, payload):
    """Rows that do not read back whole may have named a conductor; the lineage
    refuses rather than answering "no code channels" (verifier finding)."""
    from kiro_crew.slack.code_channels.store import StoreUnavailable

    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    with pytest.raises(StoreUnavailable):
        loaded.session_keys()


def test_a_well_formed_or_absent_store_answers(tmp_path):
    absent = CodeChannelStore(path=tmp_path / "missing.json")
    absent.load()
    assert absent.session_keys() == frozenset()
    path = tmp_path / "ok.json"
    path.write_text(
        json.dumps({"bindings": {}, "retired_session_keys": ["chat-1"]}), encoding="utf-8"
    )
    ok = CodeChannelStore(path=path)
    ok.load()
    assert ok.session_keys() == frozenset({"chat-1"})


def test_published_goal_is_one_defused_redacted_line_of_at_most_200():
    line = cce.published_goal("ship it <<@U0X>!here> @channel\nsecond line")
    assert line.startswith("ship it")
    assert "<" not in line and ">" not in line and "@channel" not in line
    assert "second" not in line
    assert len(cce.published_goal("g" * 500)) == cce.PUBLISHED_GOAL_MAX
    assert cce.published_goal("") == ""


def test_a_worker_that_erases_its_own_created_by_stays_one_level_deep(
    tmp_path, monkeypatch, ledger_routes_open
):
    """The transcript's ``created_by`` is editable from the worker's sandbox; the
    worker record in the write-protected store is not (CI GPT finding)."""
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    # Recorded at birth, and it survives a reload of the file.
    reloaded = CodeChannelStore(path=store.path)
    reloaded.load()
    assert reloaded.worker_conductor(worker.key) == slot.key
    set_store(reloaded)
    # The worker edits its lineage away and the gateway restarts without its conductor.
    worker._created_by = ""
    state._slots.pop(slot.key)

    assert sc.code_channel_root(state, worker) == slot.key
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(worker)))
    assert exc.value.code == "code_channel_worker_caller"
    status, body = _record(state, slot_history_key(worker), {"action": "goal", "goal": "g"})
    assert status == 409 and body["code"] == wl.CODE_DEPTH_EXCEEDED


def test_a_create_whose_worker_record_cannot_be_written_fails_and_leaves_no_worker(
    tmp_path, monkeypatch
):
    from kiro_crew.slack.code_channels.store import StoreWriteError

    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    _owner_turn(slot)
    before = set(state._slots)

    def _refuse(*a, **k):
        raise StoreWriteError("disk full")

    monkeypatch.setattr(store, "record_worker", _refuse)
    with pytest.raises(StoreWriteError):
        _create(state, slot)
    assert set(state._slots) == before


def test_an_unreadable_store_fences_every_caller_but_an_unloaded_one_does_not(
    tmp_path, monkeypatch
):
    """A worker can erase its own ``created_by``, so on a store whose FILE could not
    be read a session with none cannot be shown to be a person's tab: every caller
    is fenced until the file is repaired. A store merely not loaded yet (no file
    read has failed) keeps the transcript answer."""
    state = _state(tmp_path, monkeypatch)
    tab = state.get_or_create_slot("chat-the-persons-own-tab")
    set_store(CodeChannelStore(path=tmp_path / "never-loaded.json"))
    assert sc.code_channel_root(state, tab) == ""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    corrupt = CodeChannelStore(path=broken)
    corrupt.load()
    set_store(corrupt)
    assert sc.code_channel_root(state, tab) == sc.LINEAGE_UNAVAILABLE


def test_an_older_store_with_a_row_the_base_skipped_now_fences_callers(tmp_path, monkeypatch):
    """Accepted break (PR body, Backwards compatibility): a binding row that does
    not read back whole -- the base skipped it silently -- now makes the store
    unavailable, because that row may have named a conductor whose workers the
    one-level fence must still recognise. Every caller is refused with the
    store's name until the file is repaired."""
    state = _state(tmp_path, monkeypatch)
    tab = state.get_or_create_slot("chat-a-persons-tab")
    path = tmp_path / "slack-code-channels.json"
    path.write_text(
        json.dumps({"bindings": {"C0OLDROW01": {"channel_id": "C0ELSEWHERE"}}}),
        encoding="utf-8",
    )
    older = CodeChannelStore(path=path)
    older.load()
    set_store(older)
    assert sc.code_channel_root(state, tab) == sc.LINEAGE_UNAVAILABLE
    assert "slack-code-channels.json" in sc.LINEAGE_UNAVAILABLE_MESSAGE


def test_malformed_worker_records_read_as_unavailable(tmp_path):
    from kiro_crew.slack.code_channels.store import StoreUnavailable

    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"bindings": {}, "workers": {"chat-w": 3}}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    with pytest.raises(StoreUnavailable):
        loaded.worker_conductor("chat-w")


def test_a_worker_without_created_by_is_still_creator_fenced(tmp_path, monkeypatch):
    """Erasing ``created_by`` must not turn a worker into an unfenced tab (verifier)."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    state.get_or_create_slot("chat-the-persons-own-tab")
    worker._created_by = ""
    assert sc._caller_is_ownership_fenced(state, worker.key) is True
    for op in ("read", "send", "stop", "close"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(worker),
                target="chat-the-persons-own-tab",
                operation=op,
            )
        assert exc.value.code == "not_creator", op


def test_a_worker_presenting_another_sessions_key_is_still_a_worker(tmp_path, monkeypatch):
    """A slot that answers to the same caller key as a recorded worker carries the
    worker's depth limit and fence: resolution picks the first match (verifier)."""
    state = _state(tmp_path, monkeypatch)
    ordinary = state.get_or_create_slot("chat-aaa-ordinary")
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    worker.linked_session_key = slot_history_key(ordinary)
    assert slot_history_key(worker) == slot_history_key(ordinary)
    assert sc.caller_slot_key(state, slot_history_key(ordinary)) == ordinary.key

    assert sc.code_channel_root(state, ordinary) == slot.key
    assert sc._caller_is_ownership_fenced(state, ordinary.key) is True
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(ordinary)))
    assert exc.value.code == "code_channel_worker_caller"


def test_a_cold_restart_without_slack_keeps_an_erased_worker_fenced(tmp_path, monkeypatch):
    """Erased ``created_by``, conductor gone, store never loaded by Slack: the verb's
    own pre-gate prewarm loads the lineage, so the fence holds (round-5 verifier)."""
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    state.get_or_create_slot("chat-the-persons-own-tab")
    worker._created_by = ""
    state._slots.pop(slot.key)
    set_store(CodeChannelStore(path=store.path))  # cold: not loaded

    asyncio.run(sc.prewarm_enabled_check())  # what every session-control verb awaits
    assert sc._caller_is_ownership_fenced(state, worker.key) is True
    for op in ("read", "send", "stop", "close"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=slot_history_key(worker),
                target="chat-the-persons-own-tab",
                operation=op,
            )
        assert exc.value.code == "not_creator", op


def test_an_allow_listed_non_owner_author_is_still_refused(tmp_path, monkeypatch):
    """The author clause is owner identity, not the Slack allow-list (Design)."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda user: True)
    cce.note_channel_sender(slot, STRANGER)
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "someone other than the owner" in exc.message


@pytest.mark.parametrize("degraded", [{"*"}, {"*", "*config.local.json"}, {"slack"}])
def test_a_degraded_config_refuses_the_exemption(tmp_path, monkeypatch, degraded):
    """An unreadable overlay can have held the opt-out; its loss must not read as ON (GPT)."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    cfg = SimpleNamespace(
        slack=SimpleNamespace(code_channel_dispatch=True), degraded_sections=frozenset(degraded)
    )
    monkeypatch.setattr(cce, "_live_config", lambda: cfg)
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "could not be read whole" in exc.message


def test_a_degradation_the_live_config_did_not_carry_still_refuses(tmp_path, monkeypatch):
    """The live Slack config is a cached object a reload copies fields onto; it does
    not carry ``degraded_sections``. The process-wide record every read writes is
    consulted too (verifier: watcher reload path)."""
    from pathlib import Path as _Path

    from kiro_crew.config import resolution

    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    assert cce.admits(cce.judge_code_channel(state, slot, operation="create"))
    resolution._mark_file_degraded(_Path("config.local.json"))  # what a failed read records
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg(dispatch=True))  # no degradation on it
    exc = _refused_create(state, slot)
    assert "could not be read whole" in exc.message


# ── an opt-out the loader or the watcher could lose reads as off ─────────────


@pytest.fixture
def config_files(tmp_path):
    """A real ``config.json`` + ``config.local.json`` pair the loader reads."""
    from unittest.mock import patch

    from kiro_crew.config import live

    base = tmp_path / "cfg" / "config.json"
    local = tmp_path / "cfg" / "config.local.json"
    base.parent.mkdir()
    live.reset_for_tests()
    with (
        patch("kiro_crew.config.loader.config_path", return_value=base),
        patch("kiro_crew.config.loader.config_local_path", return_value=local),
    ):
        yield base, local
    live.reset_for_tests()


_OPTED_IN = {"slack": {"code_channels": True, "code_channel_dispatch": True}}


_NOT_A_FILE = "could not be read as a whole file"


def _read_paths() -> None:
    """Take the switch's off-loop config-path reading, as every verb does first."""
    asyncio.run(cce.refresh_config_path_check())


def test_a_torn_read_under_the_live_watcher_refuses(config_files, monkeypatch):
    """The owner withdraws dispatch in the overlay; the watcher's load reads it, but
    its torn-file probe then fails transiently, so the watcher keeps the previous
    (opted-in) snapshot. The probe lists the torn file and the exemption refuses on
    it rather than on the stale value (round-8 verifier, live.py probe). The tear
    is NOT written to the loader's process-wide degradation record, which other
    gates read (CI round-3 First Principles / Design). A later whole probe does
    NOT clear it: the watcher may never adopt that result (a held settings
    transaction discards it), so the kept snapshot can still predate the opt-out
    (round-10 security review)."""
    from kiro_crew.config.live import ConfigWatch, torn_config_files
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import observed_degraded_sections

    base, local = config_files
    base.write_text(json.dumps(_OPTED_IN), encoding="utf-8")
    w = ConfigWatch()
    w.prime(KiroCrewConfig.load())
    local.write_text(json.dumps({"slack": {"code_channel_dispatch": False}}), encoding="utf-8")
    before = observed_degraded_sections()

    def load_then_tear() -> Any:
        cfg = ConfigWatch._load()
        local.write_text("{ torn", encoding="utf-8")  # the probe meets a transient tear
        try:
            return cfg, cfg.to_dict(), ConfigWatch._document_is_torn()
        finally:
            local.write_text(json.dumps({"slack": {"code_channel_dispatch": False}}))

    monkeypatch.setattr(ConfigWatch, "_load_with_doc", staticmethod(load_then_tear))
    asyncio.run(w.refresh_now())
    snap = w.snapshot()
    assert snap is not None and snap.slack.code_channel_dispatch is True, "previous snapshot kept"
    assert str(local) in torn_config_files()
    assert observed_degraded_sections() == before, "no process-wide mark"
    monkeypatch.setattr(cce, "_live_config", lambda: snap)
    _read_paths()
    assert _NOT_A_FILE in cce._switch_refusal()
    # The recovery probe reads the files whole; the kept snapshot is still the
    # opted-in one, and the switch must stay off.
    assert ConfigWatch._document_is_torn() is False
    assert str(local) in torn_config_files()
    _read_paths()
    assert _NOT_A_FILE in cce._switch_refusal()
    assert observed_degraded_sections() == before


def test_an_overlay_path_that_is_a_directory_refuses(config_files, monkeypatch):
    """A ``config.local.json`` that exists but is not a file may stand where the
    opt-out was; the switch reads it as unknown. The loader keeps reading it as
    absent, so no other gate moves (round-8; CI round-3 First Principles)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import observed_degraded_sections

    base, local = config_files
    base.write_text(json.dumps(_OPTED_IN), encoding="utf-8")
    local.mkdir()
    before = observed_degraded_sections()
    cfg = KiroCrewConfig.load()
    assert observed_degraded_sections() == before
    monkeypatch.setattr(cce, "_live_config", lambda: cfg)
    _read_paths()
    assert _NOT_A_FILE in cce._switch_refusal()


def _symlink_or_skip(link, target) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("this host cannot create symlinks")


def test_a_dangling_overlay_symlink_refuses(config_files, monkeypatch):
    """An overlay symlink whose target is gone may stand where the opt-out was
    (round-9 security review)."""
    from kiro_crew.config.loader import KiroCrewConfig

    base, local = config_files
    base.write_text(json.dumps(_OPTED_IN), encoding="utf-8")
    _symlink_or_skip(local, local.parent / "gone.json")
    cfg = KiroCrewConfig.load()
    monkeypatch.setattr(cce, "_live_config", lambda: cfg)
    _read_paths()
    assert _NOT_A_FILE in cce._switch_refusal()


def test_a_dangling_overlay_after_a_cached_load_still_refuses(config_files, monkeypatch):
    """A healthy load with no overlay is cached; a dangling overlay appearing next
    reuses that cache, and the switch's own path check still refuses."""
    from kiro_crew.config.loader import KiroCrewConfig

    base, local = config_files
    base.write_text(json.dumps(_OPTED_IN), encoding="utf-8")
    assert KiroCrewConfig.load().slack.code_channel_dispatch is True
    assert KiroCrewConfig.load().slack.code_channel_dispatch is True  # a cache hit
    _symlink_or_skip(local, local.parent / "gone.json")
    cfg = KiroCrewConfig.load()
    monkeypatch.setattr(cce, "_live_config", lambda: cfg)
    _read_paths()
    assert _NOT_A_FILE in cce._switch_refusal()


def test_a_lost_base_config_can_never_widen_dispatch(config_files, monkeypatch):
    """The overlay wins over ``config.json``, so a base value that is lost can only
    have been an opt-in (default off) or one the overlay overrides anyway. A
    dangling ``config.json`` link is materialised by the loader's own first-run
    write, and dispatch then follows the overlay alone."""
    from kiro_crew.config.loader import KiroCrewConfig

    base, local = config_files
    _symlink_or_skip(base, base.parent / "gone.json")
    assert KiroCrewConfig.load().slack.code_channel_dispatch is False
    local.write_text(json.dumps(_OPTED_IN), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert cfg.slack.code_channel_dispatch is True  # the overlay's own choice
    local.write_text(
        json.dumps({"slack": {"code_channels": True, "code_channel_dispatch": False}}),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().slack.code_channel_dispatch is False


# ── round-10 security review ──────────────────────────────────────────────


def test_the_config_path_reading_runs_off_the_loop_and_errs_off_until_taken(monkeypatch):
    """The switch's synchronous gate never stats a file: with no fresh reading it
    refuses and starts one in a thread, which the next judgement reads."""
    import threading

    threads: list[str] = []
    real = cce._check_config_paths_blocking

    def recorded() -> str:
        threads.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(cce, "_check_config_paths_blocking", recorded)

    async def body() -> None:
        cce._forget_config_path_check()
        assert "not been checked yet" in cce._switch_refusal()
        assert threads == [], "the gate itself read nothing"
        for _ in range(50):
            await asyncio.sleep(0.01)
            if cce._path_check is not None:
                break
        assert cce._switch_refusal() == ""

    asyncio.run(body())
    assert threads and all(name != threading.main_thread().name for name in threads)


def test_a_stale_config_path_reading_errs_off(monkeypatch):
    import time

    monkeypatch.setattr(cce, "_PATH_CHECK_TTL_S", 10.0)
    with cce._path_check_lock:
        cce._path_check = (time.monotonic() - 11.0, "")
    assert "not been checked yet" in cce._switch_refusal()


def test_verbs_take_the_config_path_reading_before_judging(monkeypatch):
    """``load_code_channel_lineage`` -- awaited by every session-control verb's
    prewarm and by every work-ledger route -- refreshes the reading."""
    cce._forget_config_path_check()
    asyncio.run(sc.load_code_channel_lineage())
    assert cce._path_check is not None and cce._switch_refusal() == ""


def test_a_store_whose_routes_are_not_an_object_reads_as_unavailable(tmp_path, monkeypatch):
    """A structural error anywhere in the document -- here ``routes`` as a list
    beside an intact worker record -- used to escape the load and leave the store
    unloaded, so the lineage fell back to the editable ``created_by`` (round-10)."""
    state = _state(tmp_path, monkeypatch)
    worker = state.get_or_create_slot("chat-a-worker")
    worker._created_by = ""
    path = tmp_path / "slack-code-channels.json"
    path.write_text(
        json.dumps({"routes": ["x"], "bindings": {}, "workers": {worker.key: "chat-cc"}}),
        encoding="utf-8",
    )
    loaded = CodeChannelStore(path=path)
    asyncio.run(asyncio.to_thread(loaded.load))
    set_store(loaded)
    assert loaded.unreadable is True
    assert sc.code_channel_root(state, worker) == sc.LINEAGE_UNAVAILABLE
    assert sc._caller_is_ownership_fenced(state, worker.key) is True


def test_a_dangling_store_symlink_is_unavailable_and_never_overwritten(tmp_path, monkeypatch):
    """A store path whose symlink target is gone is not an absent store: it may
    have held worker records. Read as unavailable, and no write replaces the link
    with a fresh file that has lost them (round-10)."""
    from kiro_crew.slack.code_channels.store import Route, StoreWriteError

    state = _state(tmp_path, monkeypatch)
    worker = state.get_or_create_slot("chat-a-worker")
    link = tmp_path / "slack-code-channels.json"
    _symlink_or_skip(link, tmp_path / "gone.json")
    loaded = CodeChannelStore(path=link)
    loaded.load()
    set_store(loaded)
    assert loaded.unreadable is True
    assert sc.code_channel_root(state, worker) == sc.LINEAGE_UNAVAILABLE
    with pytest.raises(StoreWriteError):
        loaded.record_decision(Route(session_id="s1", origin_channel_id="C1", origin_ts="1.0"))
    assert link.is_symlink() and not link.exists(), "the link is left for a person to repair"


def test_an_absent_store_still_loads_empty_and_writable(tmp_path):
    from kiro_crew.slack.code_channels.store import Route

    loaded = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    loaded.load()
    assert loaded.unreadable is False
    loaded.record_decision(Route(session_id="s1", origin_channel_id="C1", origin_ts="1.0"))
    assert (tmp_path / "slack-code-channels.json").is_file()


# ── its own workers' STATE, and a link to each ───────────────────────────


def _cfg_with_dashboard(url: str = "http://127.0.0.1:7777") -> Any:
    return SimpleNamespace(
        slack=SimpleNamespace(code_channel_dispatch=True, use_tunnel_url=False),
        dashboard=SimpleNamespace(url=url),
    )


def test_the_conductor_reads_its_own_workers_state_and_link(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cce, "_live_config", _cfg_with_dashboard)
    created = _create(state, slot)
    worker = state.get_slot(created["target"])
    state.get_or_create_slot("chat-the-persons-own-tab")
    assert created["dashboard_link"] == (
        "http://127.0.0.1:7777/chat?sid=" + created["target"].replace(":", "%3A")
    )
    worker.messages.append(
        {"role": "user", "content": "SECRET BRIEF", "ts": "2026-10-19T10:00:00+00:00"}
    )
    status = asyncio.run(sc.created_session_status(state, caller_session_key=_key(slot)))
    rows = {row["target"]: row for row in status["sessions"]}
    assert set(rows) == {worker.key}, "only its own workers"
    row = rows[worker.key]
    assert row["status"] in {"idle", "queued", "working"}
    assert row["last_activity_at"].startswith("2026-10-19T10:00:00")
    assert isinstance(row["since_activity_s"], int) and row["since_activity_s"] >= 0
    assert row["dashboard_link"] == created["dashboard_link"]
    assert "SECRET BRIEF" not in json.dumps(status), "state only, never transcript text"


def test_the_worker_link_carries_no_credential_and_survives_the_outbound_filter(monkeypatch):
    from kiro_crew.dashboard.chat_delivery import sanitize_outbound

    monkeypatch.setattr(cce, "_live_config", _cfg_with_dashboard)
    link = cce.worker_dashboard_link("chat-worker-1")
    assert link == "http://127.0.0.1:7777/chat?sid=chat-worker-1"
    assert sanitize_outbound(link) == link
    assert "token" not in link.lower() and "@" not in link
    # A dashboard URL that itself carries a credential yields no link at all.
    monkeypatch.setattr(
        cce, "_live_config", lambda: _cfg_with_dashboard("http://user:pw@127.0.0.1:7777")
    )
    assert "pw" not in cce.worker_dashboard_link("chat-worker-1")
    monkeypatch.setattr(cce, "_live_config", lambda: _cfg_with_dashboard(""))
    assert cce.worker_dashboard_link("chat-worker-1") == ""
    # Whatever builds the link, a credential-shaped one is never handed out.
    import kiro_crew.dashboard.chat_backfill as backfill

    monkeypatch.setattr(cce, "_live_config", _cfg_with_dashboard)
    monkeypatch.setattr(
        backfill, "session_deep_link", lambda *a, **k: "http://127.0.0.1:7777/chat?sid=x&token=t0"
    )
    assert cce.worker_dashboard_link("chat-worker-1") == ""


def test_a_transcript_read_points_at_status_and_the_tab_but_stays_refused(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    monkeypatch.setattr(cce, "_live_config", _cfg_with_dashboard)
    worker_key = _create(state, slot)["target"]
    other = state.get_or_create_slot("chat-the-persons-own-tab")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(slot), target=worker_key)
    assert exc.value.code == "mirrored_caller"
    assert "session_status" in exc.value.message
    assert cce.worker_dashboard_link(worker_key) in exc.value.message
    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(slot), target=other.key)
    assert exc.value.code == "mirrored_caller"
    assert "/chat?sid=" not in exc.value.message, "no link to a session it did not create"


def test_status_on_a_restored_worker_never_loads_its_transcript(tmp_path, monkeypatch):
    """A worker restored as a lazy sidebar row: the activity stamp comes from the
    tail read at restore, and the deferred transcript reader never runs on the
    loop (round-11 review)."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    worker = state.get_slot(_create(state, slot)["target"])
    loads: list[Any] = []

    def _deferred_loader(*a: Any) -> None:
        loads.append(a)
        raise AssertionError("the transcript was loaded")

    worker._lazy_window = _deferred_loader
    worker._lazy_tail = [{"role": "assistant", "content": "x", "ts": "2026-10-19T11:00:00+00:00"}]
    assert worker.window_pending
    status = asyncio.run(sc.created_session_status(state, caller_session_key=_key(slot)))
    row = {r["target"]: r for r in status["sessions"]}[worker.key]
    assert row["last_activity_at"].startswith("2026-10-19T11:00:00")
    assert loads == [] and worker.window_pending


# ── what the conductor actually reads through MCP ────────────────────────


def _mcp(path: str, reply: object, tool: str, args: dict[str, Any]) -> str:
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    dash = InMemoryDashboardClient({path: reply})
    return TABLE.call(tool, args, ToolContext(dash, Caller.strict("dashboard:chat-code-channel")))


def test_the_mcp_status_reply_carries_state_age_and_link():
    reply = {
        "ok": True,
        "caller": "chat-code-channel",
        "tree": "readable",
        "history": "readable",
        "history_omitted": 0,
        "roster_omitted": 0,
        "sessions": [
            {
                "target": "chat-w1",
                "title": "Fix #21964",
                "status": "working",
                "running": True,
                "queue_depth": 0,
                "since_activity_s": 40,
                "dashboard_link": "http://127.0.0.1:7777/chat?sid=chat-w1",
                "source": "live",
            },
            {
                "target": "chat-w2",
                "title": "Docs",
                "status": "working",
                "running": True,
                "queue_depth": 0,
                "pending_approval": True,
                "pending_approval_tool": "shell",
                "since_activity_s": 300,
                "source": "live",
            },
        ],
    }
    out = _mcp("GET /api/session-control/status", reply, "session_status", {})
    assert "`chat-w1` (Fix #21964) — working, last activity 40s ago" in out
    assert "http://127.0.0.1:7777/chat?sid=chat-w1" in out
    assert "shell" in out and "last activity 5m ago" in out


def test_the_mcp_create_reply_gives_a_code_channel_the_guidance_not_a_transcript_read():
    reply = {
        "ok": True,
        "target": "chat-w1",
        "title": "Fix #21964",
        "dashboard_link": "http://127.0.0.1:7777/chat?sid=chat-w1",
        "guidance": cce.CONDUCTOR_GUIDANCE,
    }
    out = _mcp("POST /api/session-control/create", reply, "session_create", {"title": "x"})
    assert "session_read_message" not in out
    assert "session_status" in out and "http://127.0.0.1:7777/chat?sid=chat-w1" in out
    ordinary = _mcp(
        "POST /api/session-control/create",
        {"ok": True, "target": "chat-w1", "title": "t"},
        "session_create",
        {"title": "x"},
    )
    assert "watch it with session_read_message" in ordinary


def test_a_conductor_refiled_mid_create_is_refused(tmp_path, monkeypatch):
    """The worker files beside the conductor where it is NOW (round-13 review)."""
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    from kiro_crew.dashboard import session_control as sc_facade

    real = sc_facade._refuse_moved_caller_identity

    def _refile_then_check(*a: Any, **k: Any) -> Any:
        slot.folder_id = OTHER_FOLDER  # refiled while the create waited
        return real(*a, **k)

    # A shared name: patched through the facade, which reaches every holder.
    monkeypatch.setattr(sc_facade, "_refuse_moved_caller_identity", _refile_then_check)
    exc = _refused_create(state, slot)
    assert exc.code == "code_channel_folder" and "moved folder" in exc.message


def test_an_ordinary_callers_status_rows_are_unchanged(tmp_path, monkeypatch):
    """Only a code channel's conductor gets the activity age and link (round-14
    verifier); a person's own tab sees the rows it always did."""
    state = _state(tmp_path, monkeypatch)
    tab = state.get_or_create_slot("chat-the-persons-own-tab")
    child = state.get_or_create_slot("chat-its-child")
    child._created_by = tab.key
    child.messages.append({"role": "user", "content": "hi", "ts": "2026-10-19T10:00:00+00:00"})
    monkeypatch.setattr(cce, "_live_config", _cfg_with_dashboard)
    status = asyncio.run(sc.created_session_status(state, caller_session_key=_key(tab)))
    for row in status["sessions"]:
        assert "since_activity_s" not in row and "last_activity_at" not in row
        assert "dashboard_link" not in row
