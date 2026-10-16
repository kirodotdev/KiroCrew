"""Crewmate goals: the record, the owner's Goals tab routes, the crewmate's tools,
the ``crewmate.goal`` card, the Daily Goal Reminder and the context block.

The rules the RFC sets and these tests pin:

* a crewmate changes only its OWN goals (the caller's slot decides, never a body);
* readings and reports are a direct write (no card), the long-term goal is a card;
* the record holds no short-term goals (the work ledger says what the crewmate
  is doing now) and is name-scoped like the rules file;
* each check-in switch (daily, weekly, monthly) keeps exactly its own one
  schedule, and none can turn on with no goal;
* key results are proposed as a whole set on a card, readings are direct and kept;
* a weekly or monthly report is filed as an artifact and linked, not pasted;
* nothing about a goal is ever written to ``member-rules/``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from guide_route_helpers import FakeState, agent, run_guide_app
from member_goals_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    KR,
    ORDINARY,
    PROPOSE,
    READ,
    REPORT,
    TOKEN,
    _call,
    _gid,
    _goal,
    _jobs_by_cadence,
    _owner,
    _owner_ok,
    _quiet_sel,
    _reminder_state,
    _rules_dir,
    _schedules,
    _seed,
    _slug,
    _thread,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_mate, member_goals
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.paths import data_home
from kiro_crew.cron import CronService
from kiro_crew.dashboard import goal_reminder, mate_welcome
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.handlers import member_goals as goal_routes
from kiro_crew.dashboard.handlers import member_self as self_routes

# ── the record ──


def test_a_record_holding_short_term_goals_reads_past_them_and_drops_them_on_write() -> None:
    """An older record's ``short_term`` list and its undo row are ignored, never refused."""
    _seed()
    slug = _slug(ORDINARY)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {"id": "g00000001", "text": "draft", "status": "open", "updated_at": 1.0}
    old = {
        "member": ORDINARY,
        "slug": slug,
        "long_term": "Keep the release notes current",
        "short_term": [{**item, "updated_by": "user"}],
        "last_crewmate_change": {"id": "c000000000001", "op": "add", "goal_id": "g00000001"},
    }
    path.write_text(json.dumps(old), encoding="utf-8")
    rec = member_goals.read_goals(slug, ORDINARY)
    # The one ``long_term`` string reads as a one-goal list with a fixed id.
    assert [(g["id"], g["text"]) for g in rec["goals"]] == [
        (member_goals.LEGACY_GOAL_ID, "Keep the release notes current")
    ]
    assert "short_term" not in rec and "last_crewmate_change" not in rec
    view = member_goals.public_view(rec)
    assert "short_term" not in view and "last_crewmate_change" not in view
    assert "draft" not in member_goals.context_block(rec)
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "short_term" not in stored and "last_crewmate_change" not in stored
    # Written in the list shape: the legacy goal kept its id, the new one added.
    assert "long_term" not in stored
    assert [g["text"] for g in stored["goals"]] == ["Keep the release notes current", "Ship it"]
    assert stored["goals"][0]["id"] == member_goals.LEGACY_GOAL_ID


def test_a_pre_change_record_with_key_results_migrates_on_its_next_write() -> None:
    """A record from before goal lists, key results and all, keeps everything."""
    _seed()
    slug = _slug(ORDINARY)
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {
        "member": ORDINARY,
        "slug": slug,
        "long_term": "Ship clean releases",
        "long_term_updated_at": 5.0,
        "long_term_updated_by": "crewmate",
        "key_results": [
            {"id": "k00000001", "text": "Green CI", "target": "yes", "current": "", "updated_at": 0}
        ],
        "schedules": {"daily": True, "time": "08:00"},
    }
    path.write_text(json.dumps(old), encoding="utf-8")
    rec = member_goals.read_goals(slug, ORDINARY)
    (goal,) = rec["goals"]
    assert (goal["updated_at"], goal["updated_by"]) == (5.0, "crewmate")
    assert rec["key_results"][0]["goal_ids"] == []
    assert rec["schedules"]["daily"] is True
    member_goals.set_key_results(
        slug,
        ORDINARY,
        [{"text": "Green CI", "target": "yes", "goal_ids": [member_goals.LEGACY_GOAL_ID]}],
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["key_results"][0]["goal_ids"] == [member_goals.LEGACY_GOAL_ID]
    assert stored["key_results"][0]["id"] == "k00000001"
    assert "long_term" not in stored and stored["schedules"]["daily"] is True


def test_a_reading_is_free_text_up_to_its_own_cap() -> None:
    """A reading can be a qualitative note, longer than a target may be."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "Docs ready", "target": "yes"}])
    (kr,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    note = "x" * member_goals.KR_READING_MAX_CHARS
    assert member_goals.KR_READING_MAX_CHARS > member_goals.KR_VALUE_MAX_CHARS
    member_goals.record_key_result(slug, ORDINARY, kr["id"], note)
    assert member_goals.read_goals(slug, ORDINARY)["key_results"][0]["current"] == note
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.record_key_result(slug, ORDINARY, kr["id"], note + "x")
    assert exc.value.code == "too_long"
    long_target = "y" * (member_goals.KR_VALUE_MAX_CHARS + 1)
    with pytest.raises(member_goals.GoalError):
        member_goals.set_key_results(
            slug, ORDINARY, [{"text": "Docs ready", "target": long_target}]
        )
    member_goals.set_key_results(
        slug, ORDINARY, [{"text": "Docs ready", "target": "yes", "current": note}]
    )
    limits = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))["limits"]
    assert limits["key_result_reading_max_chars"] == member_goals.KR_READING_MAX_CHARS


def test_the_record_is_name_scoped_and_never_written_over_for_another_name() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Keep the release notes current", by="user")
    # Another exact name that lands on the same slug reads "no goal" ...
    assert _goal(member_goals.read_goals(slug, "Scout!")) == ""
    # ... and cannot write over this member's record.
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_goal(slug, "Scout!", "something else", by="user")
    assert exc.value.code == "slug_collision"
    assert _goal(member_goals.read_goals(slug, ORDINARY)) == "Keep the release notes current"


def test_the_goal_lives_under_trust_and_never_in_the_rules_file() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    path = member_goals.member_goals_path(slug)
    assert path.parent == (data_home() / "trust" / member_goals.GOALS_DIR_NAME).resolve()
    assert path.is_file()
    assert not _rules_dir().exists() or not any(_rules_dir().iterdir())
    assert members_mod.member_dir(slug) not in path.parents


@pytest.mark.parametrize("linked", ["member-goals", "trust"])
@pytest.mark.parametrize("pinned", [True, False], ids=["openat", "lstat-fallback"])
def test_a_symlinked_goal_directory_never_redirects_the_gateways_write(
    linked: str, pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandboxed shell can plant a link in ``trust/``; the gateway must not follow it."""
    if pinned and not member_goals._PINNED:
        pytest.skip("no openat/O_NOFOLLOW on this platform")
    monkeypatch.setattr(member_goals, "_PINNED", pinned)
    _seed()
    slug = _slug(ORDINARY)
    home = data_home()
    sealed = home / "profiles"
    sealed.mkdir(parents=True, exist_ok=True)
    (home / "trust").mkdir(parents=True, exist_ok=True)
    if linked == "trust":
        real_trust = home / "trust"
        for child in list(real_trust.iterdir()):
            child.rename(home / f"moved-{child.name}")
        real_trust.rmdir()
        (sealed / member_goals.GOALS_DIR_NAME).mkdir()
        (home / "trust").symlink_to(sealed, target_is_directory=True)
    else:
        (home / "trust" / member_goals.GOALS_DIR_NAME).symlink_to(sealed, target_is_directory=True)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.record_report(slug, ORDINARY, kind="weekly", artifact="w", title="Week")
    assert not (sealed / f"{slug}.json").exists()
    assert not (sealed / member_goals.GOALS_DIR_NAME / f"{slug}.json").exists()


def test_the_fallback_treats_a_junction_as_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows junctions are not symlinks to ``is_symlink``; the fallback must refuse them."""
    from kiro_crew import platform_compat

    monkeypatch.setattr(member_goals, "_PINNED", False)
    _seed()
    slug = _slug(ORDINARY)
    home = data_home()
    sealed = home / "profiles"
    sealed.mkdir(parents=True, exist_ok=True)
    junction = home / "trust" / member_goals.GOALS_DIR_NAME
    junction.mkdir(parents=True, exist_ok=True)
    real = platform_compat.is_link_or_junction
    monkeypatch.setattr(
        platform_compat,
        "is_link_or_junction",
        lambda p: Path(p) == junction or real(p),
    )
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.record_report(slug, ORDINARY, kind="weekly", artifact="w", title="Week")
    assert not (junction / f"{slug}.json").exists()


@pytest.mark.parametrize("pinned", [True, False], ids=["openat", "lstat-fallback"])
def test_a_symlinked_goal_record_is_neither_read_nor_written_through(
    pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if pinned and not member_goals._PINNED:
        pytest.skip("no openat/O_NOFOLLOW on this platform")
    monkeypatch.setattr(member_goals, "_PINNED", pinned)
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    path = member_goals.member_goals_path(slug)
    outside = data_home() / "profiles" / "victim.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"member": ORDINARY, "long_term": "planted"}))
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.read_goals(slug, ORDINARY)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.set_goal(slug, ORDINARY, "Changed", by="user")
    assert json.loads(outside.read_text())["long_term"] == "planted"


@pytest.mark.skipif(not member_goals._PINNED or not hasattr(os, "mkfifo"), reason="POSIX only")
def test_a_fifo_at_the_goal_record_is_refused_without_hanging() -> None:
    """A sandboxed shell can mkfifo the record; the read must fail, not block."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    path = member_goals.member_goals_path(slug)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.read_goals(slug, ORDINARY)
    with pytest.raises(member_goals.GoalsUnreadable):
        member_goals.set_goal(slug, ORDINARY, "Changed", by="user")


def test_a_long_term_write_is_a_compare_and_set() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "first", by="user")
    gid = _gid(slug, ORDINARY)
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_goal(slug, ORDINARY, "second", by="user", goal_id=gid, expected="stale")
    assert exc.value.code == "changed_meanwhile"
    member_goals.set_goal(slug, ORDINARY, "second", by="user", goal_id=gid, expected="first")
    assert _goal(member_goals.read_goals(slug, ORDINARY)) == "second"
    # "No goal yet" is a compare-and-set too: refused once there is one.
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_goal(slug, ORDINARY, "third", by="user", expected="")
    assert exc.value.code == "changed_meanwhile"


def test_goals_are_a_list_with_stable_ids_and_no_count_cap() -> None:
    _seed()
    slug = _slug(ORDINARY)
    ids = [
        member_goals.set_goal(slug, ORDINARY, f"goal {n}", by="user")["goal_id"] for n in range(7)
    ]
    rec = member_goals.read_goals(slug, ORDINARY)
    assert [g["id"] for g in rec["goals"]] == ids and len(set(ids)) == 7
    assert all(member_goals.is_goal_id(i) for i in ids)
    # Replacing one keeps its id and its place.
    member_goals.set_goal(slug, ORDINARY, "goal two, reworded", by="user", goal_id=ids[2])
    rec = member_goals.read_goals(slug, ORDINARY)
    assert rec["goals"][2] == {**rec["goals"][2], "id": ids[2], "text": "goal two, reworded"}
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_goal(slug, ORDINARY, "goal 3", by="user")
    assert exc.value.code == "duplicate_goal"
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_goal(slug, ORDINARY, "x", by="user", goal_id="g0000beef")
    assert exc.value.code == "goal_not_found"
    view = member_goals.public_view(rec)
    assert [g["number"] for g in view["goals"]] == list(range(1, 8))
    assert "max_goals" not in view["limits"] and "max_key_results" not in view["limits"]


@pytest.mark.parametrize("cadence", member_goals.CADENCES)
def test_no_check_in_turns_on_without_a_goal_and_clearing_the_goal_turns_all_off(
    cadence: str,
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, {cadence: True})
    assert exc.value.code == "no_goal"
    member_goals.set_goal(slug, ORDINARY, "Ship it", by="user")
    member_goals.set_schedules(slug, ORDINARY, {c: True for c in member_goals.CADENCES})
    member_goals.set_goal(slug, ORDINARY, "", by="user", goal_id=_gid(slug, ORDINARY))
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert not any(sched[c] for c in member_goals.CADENCES)


def test_key_results_have_no_count_cap_and_keep_ninety_days_of_readings(
    _floor_monkeypatch,
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship clean releases", by="user")
    four = [{"text": f"kr {n}", "target": "10"} for n in range(4)]
    member_goals.set_key_results(slug, ORDINARY, four)
    assert len(member_goals.read_goals(slug, ORDINARY)["key_results"]) == 4
    member_goals.set_key_results(slug, ORDINARY, four[:2], expected=four)
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    clock = [1_000_000_000.0]
    _floor_monkeypatch.setattr(member_goals, "_now", lambda: clock[0])
    member_goals.record_key_result(slug, ORDINARY, kid, "3")
    clock[0] += member_goals.READINGS_KEEP_SECS + 1
    member_goals.record_key_result(slug, ORDINARY, kid, "5")
    kr, _other = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert kr["current"] == "5"
    assert [r["value"] for r in kr["readings"]] == ["5"]
    # Re-proposing the same text and target keeps the id and its readings.
    member_goals.set_key_results(slug, ORDINARY, four[:2], expected=four[:2])
    assert member_goals.read_goals(slug, ORDINARY)["key_results"][0]["readings"] == kr["readings"]
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(slug, ORDINARY, four[:1], expected=four[2:])
    assert exc.value.code == "changed_meanwhile"


def test_a_key_result_links_only_to_goals_in_the_list() -> None:
    _seed()
    slug = _slug(ORDINARY)
    a = member_goals.set_goal(slug, ORDINARY, "Ship", by="user")["goal_id"]
    b = member_goals.set_goal(slug, ORDINARY, "Hire", by="user")["goal_id"]
    member_goals.set_key_results(
        slug,
        ORDINARY,
        [
            {"text": "Both", "target": "yes", "goal_ids": [a, b, a]},
            {"text": "None", "target": "yes", "goal_ids": []},
        ],
    )
    both, none = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (both["goal_ids"], none["goal_ids"]) == ([a, b], [])
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(
            slug, ORDINARY, [{"text": "x", "target": "1", "goal_ids": ["g0000beef"]}]
        )
    assert exc.value.code == "unknown_goal"
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(
            slug, ORDINARY, [{"text": "x", "target": "1", "goal_ids": ["not-an-id"]}]
        )
    assert exc.value.code == "invalid_key_results"
    # An item without goal_ids keeps the links of the key result it matches.
    member_goals.set_key_results(slug, ORDINARY, [{"text": "Both", "target": "yes"}])
    assert member_goals.read_goals(slug, ORDINARY)["key_results"][0]["goal_ids"] == [a, b]
    rec = member_goals.read_goals(slug, ORDINARY)
    assert "(serves goals 1, 2)" in member_goals.context_block(rec)


def test_removing_a_goal_strips_it_from_key_results_and_undo_puts_both_back() -> None:
    _seed()
    slug = _slug(ORDINARY)
    a = member_goals.set_goal(slug, ORDINARY, "Ship", by="user")["goal_id"]
    b = member_goals.set_goal(slug, ORDINARY, "Hire", by="user")["goal_id"]
    member_goals.set_key_results(
        slug, ORDINARY, [{"text": "Both", "target": "yes", "goal_ids": [a, b]}]
    )
    member_goals.remove_goal(slug, ORDINARY, a)
    rec = member_goals.read_goals(slug, ORDINARY)
    assert [g["id"] for g in rec["goals"]] == [b]
    assert rec["key_results"][0]["goal_ids"] == [b]
    assert member_goals.public_view(rec)["removed_goal"]["id"] == a
    member_goals.restore_goal(slug, ORDINARY, a)
    rec = member_goals.read_goals(slug, ORDINARY)
    assert [g["id"] for g in rec["goals"]] == [a, b]
    assert sorted(rec["key_results"][0]["goal_ids"]) == sorted([a, b])
    assert rec["removed_goal"] is None
    # Clearing a goal's text through the write route strips it the same way.
    member_goals.set_goal(slug, ORDINARY, "", by="user", goal_id=b)
    rec = member_goals.read_goals(slug, ORDINARY)
    assert [g["id"] for g in rec["goals"]] == [a] and rec["key_results"][0]["goal_ids"] == [a]
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.restore_goal(slug, ORDINARY, b)
    assert exc.value.code == "undo_unavailable"


def test_key_result_movement_reads_the_window_from_readings(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    clock = [1_000_000.0]
    _floor_monkeypatch.setattr(member_goals, "_now", lambda: clock[0])
    member_goals.record_key_result(slug, ORDINARY, kid, "10")
    clock[0] += 10 * 86400
    member_goals.record_key_result(slug, ORDINARY, kid, "14")
    clock[0] += 86400
    member_goals.record_key_result(slug, ORDINARY, kid, "19")
    rec = member_goals.read_goals(slug, ORDINARY)
    (m,) = member_goals.key_result_movement(rec, since=clock[0] - 7 * 86400)
    assert (m["start"], m["now"], m["readings_in_window"]) == ("10", "19", 2)


def test_key_result_movement_starts_at_the_first_reading_when_none_precedes_the_window() -> None:
    # Stored out of order: the start is the earliest reading inside the window.
    readings = [{"value": "4", "at": 300.0}, {"value": "2", "at": 200.0}]
    late = {"id": "k00000001", "text": "late", "target": "5", "current": "4", "readings": readings}
    unread = {"id": "k00000002", "text": "unread", "target": "5", "current": "", "readings": []}
    moved, still = member_goals.key_result_movement({"key_results": [late, unread]}, since=100.0)
    assert (moved["start"], moved["now"], moved["readings_in_window"]) == ("2", "4", 2)
    assert (still["start"], still["now"], still["readings_in_window"]) == (None, None, 0)


# ── the crewmate's tools ──


def _seed_key_result(member: str) -> str:
    slug = _slug(member)
    member_goals.set_goal(slug, member, "Ship", by="user")
    member_goals.set_key_results(slug, member, [{"text": "tests", "target": "yes"}])
    return member_goals.read_goals(slug, member)["key_results"][0]["id"]


def test_the_crewmate_reads_only_its_own_goal() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    member_goals.set_goal(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME, "mine", by="user")
    member_goals.set_goal(_slug(ORDINARY), ORDINARY, "theirs", by="user")
    # A name in the body is ignored: the caller's slot names the crewmate.
    status, body = _call(state, READ, {"member": ORDINARY}, slot.key)
    assert status == 200, body
    assert _goal(body) == "mine"
    assert "short_term" not in body and "last_crewmate_change" not in body


def test_a_thread_the_binding_does_not_name_is_refused() -> None:
    _seed()
    state = FakeState()
    kid = _seed_key_result(ORDINARY)
    slot = state.open_slot("member-other")
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = ORDINARY
    status, body = _call(state, KR, {"id": kid, "current": "x"}, slot.key)
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert member_goals.read_goals(_slug(ORDINARY), ORDINARY)["key_results"][0]["current"] == ""


def test_a_subagent_cannot_change_goals() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    method, path, _ = KR

    async def go(c):
        body = {"id": "k00000001", "current": "x"}
        r = await c.request(method, path, json=body, headers=agent("subagent:abc"))
        return r.status, await r.json()

    status, body = run_guide_app(go, state, extra_routes=[KR])
    assert (status, body["code"]) == (403, "subagent_caller")


def test_the_crewmate_records_a_reading_directly(_floor_monkeypatch) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    slug = _slug(ASSISTANT_MEMBER_NAME)
    member_goals.set_goal(slug, ASSISTANT_MEMBER_NAME, "Ship", by="user")
    member_goals.set_key_results(slug, ASSISTANT_MEMBER_NAME, [{"text": "tests", "target": "yes"}])
    kid = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["key_results"][0]["id"]
    status, body = _call(state, KR, {"id": kid, "current": "no"}, slot.key)
    assert status == 200, body
    (kr,) = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["key_results"]
    assert kr["current"] == "no" and len(kr["readings"]) == 1


def test_a_report_is_filed_under_the_crewmates_reports_folder_and_linked(
    _floor_monkeypatch,
) -> None:
    from kiro_crew import artifacts as artifacts_mod

    _seed()
    state = FakeState()
    pushed: list[Any] = []

    class Bus:
        def push(self, payload):
            pushed.append(payload)

    state.notification_bus = Bus()  # type: ignore[attr-defined]
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    slug = _slug(ASSISTANT_MEMBER_NAME)
    member_goals.set_goal(slug, ASSISTANT_MEMBER_NAME, "Ship", by="user")
    md = "# Week 41\n\nPRs described: 14 -> 19 of 27."
    status, body = _call(
        state, REPORT, {"kind": "weekly", "title": "Week 41", "markdown": md}, slot.key
    )
    assert status == 200, body
    art = artifacts_mod.get_default_store().get(body["slug"])
    assert art.kind == "markdown"
    folders = artifacts_mod.get_default_folder_store()
    label = ASSISTANT_MEMBER_NAME.replace("/", "-")
    assert art.folder_id == folders.resolve_path(f"Crewmates/{label}/Reports")
    assert body["url"] == f"/artifacts/{body['slug']}"
    (note,) = pushed
    assert note.url == body["url"]
    assert md not in (note.body or "")
    (report,) = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["reports"]
    assert (report["kind"], report["slug"]) == ("weekly", body["slug"])
    status, body = _call(state, REPORT, {"kind": "daily", "title": "x", "markdown": "y"}, slot.key)
    assert (status, body["code"]) == (400, "invalid_report_kind")


def test_the_reminders_hidden_turn_may_record_a_reading_and_only_its_goal_cards(
    _floor_monkeypatch,
) -> None:
    """Readings and the crewmate's own goal cards are admitted on a turn the user did not send."""
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    kid = _seed_key_result(ASSISTANT_MEMBER_NAME)
    status, body = _call(state, KR, {"id": kid, "current": "half, two suites left"}, slot.key)
    assert status == 200, body
    status, body = _call(state, READ, {}, slot.key)
    assert status == 200 and body["key_results"][0]["current"] == "half, two suites left"
    status, body = _call(state, PROPOSE, {"request": "model", "params": {"model": "x"}}, slot.key)
    assert (status, body["code"]) == (403, "not_user_turn")


def _capture_proposals(monkeypatch) -> list[dict]:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True})

    monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    return seen


GOAL_ASK = {"request": "goal", "params": {"long_term": "Ship the docs site"}}
KR_ASK = {"request": "key_results", "params": {"items": [{"text": "pages", "target": "40"}]}}
CHECK_INS_ASK = {"request": "check_ins", "params": {"daily": True, "time": "09:30"}}


def test_a_turn_in_its_own_thread_raises_its_own_goal_cards(_floor_monkeypatch) -> None:
    """A check-in, a sub-agent's result or the goal-setup nudge: not sent by the user."""
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    for ask in (GOAL_ASK, KR_ASK, CHECK_INS_ASK):
        assert _call(state, PROPOSE, ask, slot.key)[0] == 200
    assert [c["kind"] for c in seen] == [
        catalog.KIND_CREWMATE_GOAL,
        catalog.KIND_CREWMATE_KEY_RESULTS,
        catalog.KIND_CREWMATE_CHECK_INS,
    ]
    assert all(c["params"]["member"] == ORDINARY for c in seen)
    assert seen[2]["params"] == {"member": ORDINARY, "daily": True, "time": "09:30"}
    # Nothing else a card could ask for: the model, the approval mode, the tools.
    status, body = _call(state, PROPOSE, {"request": "model", "params": {"model": "x"}}, slot.key)
    assert (status, body["code"]) == (403, "not_user_turn")
    assert len(seen) == 3


def test_a_channel_turn_raises_no_goal_card(_floor_monkeypatch) -> None:
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot._turn_channel_origin = True
    status, body = _call(state, PROPOSE, GOAL_ASK, slot.key)
    assert (status, body["code"]) == (403, "channel_caller")
    assert seen == []


def test_a_turn_outside_a_crewmate_thread_raises_no_goal_card(_floor_monkeypatch) -> None:
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot.mode = "chat"
    for ask in (GOAL_ASK, KR_ASK, CHECK_INS_ASK):
        status, body = _call(state, PROPOSE, ask, slot.key)
        assert (status, body["code"]) == (403, "not_user_turn")
    assert seen == []


def test_check_ins_takes_only_its_own_fields(_floor_monkeypatch) -> None:
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY)
    for params in ({}, {"daily": True, "member": "someone-else"}):
        status, body = _call(state, PROPOSE, {"request": "check_ins", "params": params}, slot.key)
        assert (status, body["code"]) == (400, "invalid_params")
    assert seen == []


@pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly"])
def test_each_check_in_is_dispatched_as_a_plain_hidden_turn(
    _floor_monkeypatch, cadence: str
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot.task = None
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_schedules(slug, ORDINARY, {cadence: True})
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    class Job:
        managed_by = goal_reminder.managed_key(slug, cadence)

    assert asyncio.run(goal_reminder.fire(state, Job())) == "started"
    ((args, kwargs),) = sent
    assert args[1] is slot
    assert kwargs == {"human_seen": False, "heartbeat": False}


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ("turn", "skipped: the crewmate is busy"),
        ("queue", "skipped: the crewmate is busy"),
        ("reopen", "skipped: the crewmate's thread is not open"),
    ],
    ids=["a-turn-started", "a-message-queued", "the-thread-was-reopened"],
)
def test_the_monthly_review_is_not_dispatched_when_the_thread_changed_during_the_ledger_read(
    _floor_monkeypatch, change: str, outcome: str
) -> None:
    """A turn that starts while the work ledger is read makes the review wait."""
    from guide_route_helpers import DashboardTurn

    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot.task = None
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"monthly": True})
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    def ledger_read(_key: str, _cadence: str) -> list:
        # Runs inside the await that fire() makes before dispatching.
        if change == "turn":
            slot.task = DashboardTurn()  # type: ignore[assignment]
        elif change == "queue":
            slot._queue.append({"id": "q1", "content": "[Cron notification]"})
        else:
            fresh = state.open_slot(slot.key)
            fresh.task = None
            fresh.mode = slot.mode
            fresh.agent = slot.agent
        return []

    _floor_monkeypatch.setattr(goal_reminder, "finished_tasks", ledger_read)

    class Job:
        managed_by = goal_reminder.managed_key(slug, "monthly")

    assert asyncio.run(goal_reminder.fire(state, Job())) == outcome
    assert sent == []


def test_a_turn_the_user_did_not_send_cannot_raise_a_card_for_another_crewmate_by_name(
    _floor_monkeypatch,
) -> None:
    """``propose_change`` names its member in the body, so it stays user-turn only."""
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    route = ("POST", "/api/cards/agent/propose", card_routes.api_cards_agent_propose)
    body = {
        "kind": catalog.KIND_CREWMATE_GOAL,
        "params": {"member": ASSISTANT_MEMBER_NAME, "long_term": "Something else"},
    }
    status, out = _call(state, route, body, slot.key)
    assert (status, out["code"]) == (403, "not_user_turn")
    assert seen == []


def test_set_goal_becomes_a_card_for_the_callers_own_member(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        from aiohttp import web

        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True, "card": {"id": "c1"}})

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, _ = _call(
        state, PROPOSE, {"request": "goal", "params": {"long_term": "Help Ray ship"}}, slot.key
    )
    assert status == 200
    assert seen == [
        {
            "kind": catalog.KIND_CREWMATE_GOAL,
            "params": {"member": ASSISTANT_MEMBER_NAME, "long_term": "Help Ray ship"},
        }
    ]
    # Proposing wrote nothing: the goal changes only when the card is applied.
    assert _goal(member_goals.read_goals(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME)) == ""
    # A goal id passes through, to replace that goal.
    status, _ = _call(
        state,
        PROPOSE,
        {"request": "goal", "params": {"long_term": "Help Ray ship", "goal_id": "g00000001"}},
        slot.key,
    )
    assert status == 200 and seen[-1]["params"]["goal_id"] == "g00000001"
    status, _ = _call(
        state, PROPOSE, {"request": "goal", "params": {"goal_id": "g00000001"}}, slot.key
    )
    assert status == 400


def test_the_mate_server_lists_the_goal_tools_and_routes_set_goal_to_a_card() -> None:
    names = {t["name"] for t in mcp_mate._list_tools()}
    assert {
        "goal_read",
        "set_goal",
        "set_key_results",
        "key_result_progress",
        "save_goal_report",
    } <= names
    assert not {"short_goal_add", "short_goal_update"} & names
    assert mcp_mate._PROPOSALS["set_goal"] == "goal"
    assert mcp_mate._PROPOSALS["set_key_results"] == "key_results"
    assert "key_result_progress" not in mcp_mate._PROPOSALS
    assert self_routes.SELF_PROPOSALS["key_results"] == catalog.KIND_CREWMATE_KEY_RESULTS
    assert self_routes.SELF_PROPOSALS["goal"] == catalog.KIND_CREWMATE_GOAL
    assert goal_routes.OP_GOAL_WRITE not in guide_routes._USER_TURN_OPERATIONS
    assert "member.propose_self" in guide_routes._USER_TURN_OPERATIONS


# ── the card ──


GOAL_SNAPSHOT = {
    "exists": True,
    "slug": "scout",
    "member_id": "",
    "goals": [{"id": "g00000001", "text": "old"}, {"id": "g00000002", "text": "other"}],
}


def test_the_goal_card_adds_a_goal_and_its_undo_removes_exactly_that_one() -> None:
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_GOAL, {"member": ORDINARY, "long_term": " Ship it "}
    )
    assert params == {"member": ORDINARY, "long_term": "Ship it"}
    preview = catalog.build_preview(catalog.KIND_CREWMATE_GOAL, params, GOAL_SNAPSHOT, {})
    assert preview["changes"][0]["label"] == "Goal 3" and preview["changes"][0]["before"] is None
    (step,) = preview["apply"]
    assert (step["method"], step["path"]) == ("PUT", "/api/members/scout/goals/long-term")
    assert "member-rules" not in step["path"]
    assert step["body"] == {"member": ORDINARY, "long_term": "Ship it"}
    assert ("PUT", "/api/members/{slug}/goals/long-term") in catalog.HOOKED_ROUTES
    # The apply's answer names the new goal; the Undo removes that one only.
    ev = catalog.extract_evidence(
        catalog.KIND_CREWMATE_GOAL, catalog.OP_APPLY, 0, {"goal_id": "g000000aa", "goals": []}
    )
    assert ev == {"goal_id": "g000000aa"}
    plan, reason = catalog.build_undo(catalog.KIND_CREWMATE_GOAL, params, GOAL_SNAPSHOT, [ev], 1)
    assert reason is None
    (undo,) = plan
    assert undo["path"] == step["path"]
    assert undo["body"] == {
        "member": ORDINARY,
        "goal_id": "g000000aa",
        "long_term": "",
        "expected": "Ship it",
    }
    assert catalog.build_undo(catalog.KIND_CREWMATE_GOAL, params, GOAL_SNAPSHOT, [{}], 1) == (
        None,
        "identity_unknown",
    )
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_GOAL, {**params, "long_term": "other"}, GOAL_SNAPSHOT, {}
        )
    assert exc.value.code == "no_change"


def test_the_goal_card_replaces_one_goal_by_id_with_a_compare_and_set() -> None:
    import json

    params = catalog.validate_params(
        catalog.KIND_CREWMATE_GOAL,
        {"member": ORDINARY, "long_term": "new", "goal_id": "g00000001"},
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_GOAL, params, GOAL_SNAPSHOT, {})
    assert preview["changes"][0]["label"] == "Goal 1"
    (step,) = preview["apply"]
    body = step["body"] if isinstance(step["body"], dict) else json.loads(step["body"])
    assert (body["goal_id"], body["expected"]) == ("g00000001", "old")
    (undo,) = catalog.build_undo(catalog.KIND_CREWMATE_GOAL, params, GOAL_SNAPSHOT, [{}], 1)[0]
    ubody = undo["body"] if isinstance(undo["body"], dict) else json.loads(undo["body"])
    assert (ubody["goal_id"], ubody["long_term"], ubody["expected"]) == ("g00000001", "old", "new")
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_GOAL, {**params, "goal_id": "g0000beef"}, GOAL_SNAPSHOT, {}
        )
    assert exc.value.code == "not_found"
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_CREWMATE_GOAL, {"member": ORDINARY, "long_term": "x", "goal_id": "bad"}
        )


def test_the_key_results_card_replaces_the_set_with_a_compare_and_set() -> None:
    import json

    items = [{"text": "PRs described", "target": "27"}]
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items}
    )
    snapshot = {"exists": True, "slug": "scout", "has_goal": True, "key_results": []}
    (step,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, snapshot, {})[
        "apply"
    ]
    assert (step["method"], step["path"]) == ("PUT", "/api/members/scout/goals/key-results")
    body = step["body"] if isinstance(step["body"], dict) else json.loads(step["body"])
    assert (body["items"], body["expected"]) == (items, [])
    (undo,) = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, snapshot, [{}], 1)[0]
    ubody = undo["body"] if isinstance(undo["body"], dict) else json.loads(undo["body"])
    assert (ubody["items"], ubody["expected"]) == ([], items)
    assert ("PUT", "/api/members/{slug}/goals/key-results") in catalog.HOOKED_ROUTES
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_KEY_RESULTS, params, {**snapshot, "has_goal": False}, {}
        )
    assert exc.value.code == "no_goal"
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items * 4}
        )


def test_set_key_results_becomes_a_card_for_the_callers_own_member(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        from aiohttp import web

        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True})

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    items = [{"text": "tests pass", "target": "yes"}]
    status, _ = _call(
        state, PROPOSE, {"request": "key_results", "params": {"items": items}}, slot.key
    )
    assert status == 200
    assert seen == [
        {
            "kind": catalog.KIND_CREWMATE_KEY_RESULTS,
            "params": {"member": ASSISTANT_MEMBER_NAME, "items": items},
        }
    ]


# ── the owner's routes and the check-ins ──


@pytest.mark.parametrize("cadence", member_goals.CADENCES)
def test_each_switch_creates_and_removes_exactly_its_own_schedule(
    tmp_path: Path, cadence: str
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/schedules"
    status, body = _owner(state, "PUT", path, {"member": ORDINARY, cadence: True})
    assert (status, body["code"]) == (409, "no_goal")
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())
    _owner(
        state,
        "PUT",
        f"/api/members/{slug}/goals/long-term",
        {"member": ORDINARY, "long_term": "Ship"},
    )
    for when in ("09:00", "07:15"):
        status, body = _owner(
            state,
            "PUT",
            path,
            {
                "member": ORDINARY,
                cadence: True,
                "time": when,
                "timezone": "UTC",
                "weekday": 3,
                "month_day": 12,
            },
        )
        assert status == 200, body
        assert body["schedules"][cadence] is True
    jobs = _jobs_by_cadence(state, slug)
    (job,) = jobs[cadence]
    assert all(v == [] for c, v in jobs.items() if c != cadence)
    expected = {
        "daily": "15 7 * * *",
        "weekly": "15 7 * * 3",
        "monthly": "15 7 12 * *",
        # The heartbeat ignores the time of day and runs on its interval.
        "heartbeat": "*/30 * * * *",
    }[cadence]
    assert job.schedule.cron_expr == expected
    status, body = _owner(state, "PUT", path, {"member": ORDINARY, cadence: False})
    assert status == 200 and body["schedules"][cadence] is False
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())


def test_turning_one_switch_off_leaves_the_others(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    _owner(
        state,
        "PUT",
        base + "/schedules",
        {"member": ORDINARY, "daily": True, "weekly": True, "monthly": True},
    )
    first = {c: [j.id for j in v] for c, v in _jobs_by_cadence(state, slug).items()}
    assert all(len(first[c]) == 1 for c in ("daily", "weekly", "monthly"))
    assert first["heartbeat"] == []
    _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "weekly": False})
    after = _jobs_by_cadence(state, slug)
    assert after["weekly"] == []
    assert len(after["daily"]) == 1 and len(after["monthly"]) == 1
    # The unchanged jobs are kept, not recreated: same ids, so their run
    # history (keyed on the job id) survives another switch's save.
    assert [j.id for j in after["daily"]] == first["daily"]
    assert [j.id for j in after["monthly"]] == first["monthly"]


def test_saving_one_switch_keeps_the_other_check_ins_and_their_history(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "daily": True})
    (daily,) = _schedules(state, slug, "daily")
    removed: list[str] = []
    real_remove = state.crons.svc.remove_job_async

    async def spy(job_id: str, **kw: Any) -> Any:
        removed.append(job_id)
        return await real_remove(job_id, **kw)

    state.crons.svc.remove_job_async = spy  # type: ignore[method-assign]
    _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "weekly": True})
    assert daily.id not in removed
    assert [j.id for j in _schedules(state, slug, "daily")] == [daily.id]
    assert len(_schedules(state, slug, "weekly")) == 1
    # A changed cadence is still replaced: moving the time recreates the job.
    _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "time": "07:30"})
    assert daily.id in removed
    assert [j.id for j in _schedules(state, slug, "daily")] != [daily.id]


def test_clearing_the_goal_removes_every_check_in_schedule(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    _owner(
        state,
        "PUT",
        base + "/schedules",
        {"member": ORDINARY, "daily": True, "weekly": True, "monthly": True},
    )
    gid = _gid(slug, ORDINARY)
    status, body = _owner(state, "DELETE", base + f"/items/{gid}?member={ORDINARY}", None)
    assert status == 200, body
    assert body["goals"] == [] and body["removed_goal"]["id"] == gid
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())
    status, body = _owner(state, "POST", base + f"/items/{gid}/restore", {"member": ORDINARY})
    assert status == 200 and [g["id"] for g in body["goals"]] == [gid]
    # An empty text with a goal_id removes the goal through the write route too.
    status, body = _owner(
        state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "", "goal_id": gid}
    )
    assert status == 200 and body["goals"] == []
    status, body = _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": ""})
    assert (status, body["code"]) == (400, "empty_text")


def test_the_owner_edits_and_removes_key_results(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    one = [{"text": "PRs described", "target": "27"}]
    status, body = _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": one})
    assert status == 200 and [k["text"] for k in body["key_results"]] == ["PRs described"]
    status, body = _owner(
        state, "PUT", base + "/key-results", {"member": ORDINARY, "items": [], "expected": one}
    )
    assert status == 200 and body["key_results"] == []


def test_rewording_a_key_result_in_the_editor_keeps_its_id_and_readings(tmp_path: Path) -> None:
    """The Goals editor names each key result by id, so new wording keeps its readings."""
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    two = [{"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": two})
    first, second = member_goals.read_goals(slug, ORDINARY)["key_results"]
    member_goals.record_key_result(slug, ORDINARY, first["id"], "12")
    member_goals.record_key_result(slug, ORDINARY, first["id"], "19")
    edited = [
        {"id": first["id"], "text": "PRs with a description", "target": "30"},
        {"id": second["id"], "text": "notes out", "target": "yes"},
    ]
    status, _body = _owner(
        state, "PUT", base + "/key-results", {"member": ORDINARY, "items": edited, "expected": two}
    )
    assert status == 200
    after = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert [k["id"] for k in after] == [first["id"], second["id"]]
    assert (after[0]["text"], after[0]["target"]) == ("PRs with a description", "30")
    assert after[0]["current"] == "19"
    assert [r["value"] for r in after[0]["readings"]] == ["12", "19"]


@pytest.mark.parametrize("kid", ["k00000000", "not-an-id", 7])
def test_an_editor_write_naming_a_key_result_outside_the_set_is_refused(
    tmp_path: Path, kid: Any
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    one = [{"text": "PRs described", "target": "27"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": one})
    before = member_goals.read_goals(slug, ORDINARY)["key_results"]
    status, body = _owner(
        state,
        "PUT",
        base + "/key-results",
        {"member": ORDINARY, "items": [{"id": kid, "text": "other", "target": "1"}]},
    )
    assert status == 400
    assert body["code"] == ("unknown_key_result" if kid == "k00000000" else "invalid_key_results")
    assert member_goals.read_goals(slug, ORDINARY)["key_results"] == before


def test_a_removed_key_result_comes_back_with_its_readings(tmp_path: Path) -> None:
    """The Goals tab's remove keeps the key result for one Undo, readings and place."""
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    two = [{"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": two})
    first = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    member_goals.record_key_result(slug, ORDINARY, first["id"], "12")
    member_goals.record_key_result(slug, ORDINARY, first["id"], "19")
    kept = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    status, body = _owner(state, "DELETE", f"{base}/key-results/{kept['id']}?member={ORDINARY}")
    assert status == 200, body
    assert [k["text"] for k in body["key_results"]] == ["notes out"]
    assert body["removed_key_result"]["id"] == kept["id"]
    status, body = _owner(
        state, "POST", f"{base}/key-results/{kept['id']}/restore", {"member": ORDINARY}
    )
    assert status == 200, body
    assert body["removed_key_result"] is None
    assert body["key_results"][0] == kept
    assert [r["value"] for r in body["key_results"][0]["readings"]] == ["12", "19"]
    status, body = _owner(
        state, "POST", f"{base}/key-results/{kept['id']}/restore", {"member": ORDINARY}
    )
    assert (status, body["code"]) == (410, "undo_unavailable")


def test_a_stale_removal_undo_is_cleared_when_refused() -> None:
    """Refused as stale, the Undo is gone from disk, so it does not refuse forever."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.remove_key_result(slug, ORDINARY, kid)
    member_goals.set_goal(slug, ORDINARY, "", by="user", goal_id=_gid(slug, ORDINARY))
    assert member_goals.read_goals(slug, ORDINARY)["removed_key_result"] is not None
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.restore_key_result(slug, ORDINARY, kid)
    assert exc.value.code == "stale_undo"
    assert member_goals.read_goals(slug, ORDINARY)["removed_key_result"] is None
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.restore_key_result(slug, ORDINARY, kid)
    assert exc.value.code == "undo_unavailable"


def test_a_later_write_to_the_key_results_ends_the_removal_undo(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    two = [{"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": two})
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    _owner(state, "DELETE", f"{base}/key-results/{kid}?member={ORDINARY}")
    status, body = _owner(
        state,
        "PUT",
        base + "/key-results",
        {"member": ORDINARY, "items": [{"text": "PRs described", "target": "27"}]},
    )
    assert status == 200 and body["removed_key_result"] is None
    status, body = _owner(state, "POST", f"{base}/key-results/{kid}/restore", {"member": ORDINARY})
    assert (status, body["code"]) == (410, "undo_unavailable")
    assert [k["text"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]] == [
        "PRs described"
    ]


def test_the_schedule_list_leaves_out_goal_check_ins_and_keeps_other_jobs(tmp_path: Path) -> None:
    """GET /api/crons (Schedule page, a crewmate's Schedules tab) omits only check-ins."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import cron as cron_routes

    svc = CronService(base_dir=tmp_path / "crons")
    slug = "scout"

    async def go() -> list[str]:
        await svc.add_job_async("ordinary", "do it", every_secs=3600)
        await svc.add_managed_job_async(
            "app:mochi:feed", name="app job", message="feed", every_secs=3600
        )
        for cadence in member_goals.CADENCES:
            await svc.add_managed_job_async(
                goal_reminder.managed_key(slug, cadence),
                name=f"check-in {cadence}",
                message="x",
                cron_expr="0 9 * * *",
            )
        app = web.Application()
        app["state"] = SimpleNamespace(
            crons=svc,
            owner_id="",
            conversation_log=None,
            push_slots_update=MagicMock(),
            has_slot=lambda _key: False,
        )
        app.router.add_get("/api/crons", cron_routes.api_crons)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/crons")
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        return sorted(j["name"] for j in data["jobs"])

    assert asyncio.run(go()) == ["app job", "ordinary"]
    # Still in the store, still scheduled.
    stored = {j.managed_by for j in svc.list_jobs(include_disabled=True)}
    assert {goal_reminder.managed_key(slug, c) for c in member_goals.CADENCES} <= stored


def test_the_owner_long_term_route_refuses_a_stale_expected(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/long-term"
    status, body = _owner(state, "PUT", path, {"member": ORDINARY, "long_term": "first"})
    gid = body["goal_id"]
    assert [g["id"] for g in body["goals"]] == [gid]
    status, body = _owner(
        state,
        "PUT",
        path,
        {"member": ORDINARY, "long_term": "second", "goal_id": gid, "expected": "other"},
    )
    assert (status, body["code"]) == (409, "changed_meanwhile")
    # A new goal replaces nothing, so it cannot carry a stale text to compare.
    status, body = _owner(
        state, "PUT", path, {"member": ORDINARY, "long_term": "second", "expected": "other"}
    )
    assert (status, body["code"]) == (400, "invalid_expected")


def test_the_short_term_and_undo_routes_are_gone() -> None:
    from kiro_crew.dashboard.server_runtime import mcp_routes

    assert not hasattr(goal_routes, "api_member_goals_undo")
    assert not hasattr(goal_routes, "api_member_self_short_goal")
    assert not hasattr(member_goals, "add_short_term")
    source = Path(mcp_routes.__file__).read_text(encoding="utf-8")
    assert "short-term" not in source and "/goals/undo" not in source


# ── the reminder turn and the context ──


def test_the_reminder_fires_one_hidden_turn_in_the_crewmates_own_thread(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY)
    slot.task = None
    slot._turn_user_sent = False
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    class Job:
        managed_by = goal_reminder.managed_key(slug)

    assert asyncio.run(goal_reminder.fire(state, Job())) == "skipped: switch off or no goal"
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "pages reviewed", "target": "40"}])
    member_goals.set_schedules(slug, ORDINARY, {"daily": True})
    assert asyncio.run(goal_reminder.fire(state, Job())) == "started"
    ((args, kwargs),) = sent
    assert args[1] is slot and kwargs == {"human_seen": False, "heartbeat": False}
    prompt = args[2]
    assert "Keep docs current" in prompt
    assert "pages reviewed" in prompt and "key_result_progress" in prompt
    assert "short_goal" not in prompt and "next task" in prompt


def _ledger_item(title: str, state: str, closed_at: str | None, summary: str = ""):
    from kiro_crew import work_ledger

    return work_ledger.WorkItem(
        item_id="it_0000000" + str(len(title) % 10),
        title=title,
        state=state,
        summary=summary,
        closed_at=closed_at,
    )


def test_a_report_prompt_lists_the_tasks_finished_in_its_window(_floor_monkeypatch) -> None:
    """The weekly report reads finished work-ledger tasks, not a short-term list."""
    from datetime import datetime, timedelta, timezone

    from kiro_crew import work_ledger

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    items = [
        _ledger_item("Fix the flaky test", "accepted", recent, f"done {TOKEN}"),
        _ledger_item("Old migration", "accepted", old),
        _ledger_item("Still open", "open", None),
        # Reopened after a close: its stale closed_at does not make it finished.
        _ledger_item("Reopened", "open", recent),
        _ledger_item("Dropped spike", "abandoned", recent),
    ]
    seen: list[str] = []

    def listing(key: str):
        seen.append(key)
        return items

    _floor_monkeypatch.setattr(work_ledger, "list_work_items", listing)
    tasks = goal_reminder.finished_tasks("dashboard:member-scout", "weekly")
    assert seen == ["member-scout"]
    assert [(t["title"], t["state"]) for t in tasks] == [
        ("Fix the flaky test", "accepted"),
        ("Dropped spike", "abandoned"),
    ]
    assert TOKEN not in json.dumps(tasks)
    assert len(goal_reminder.finished_tasks("dashboard:member-scout", "monthly")) == 3
    rec = member_goals.empty_record()
    rec["long_term"] = "Keep docs current"
    prompt = goal_reminder.reminder_prompt(rec, "weekly", finished=tasks)
    assert "Fix the flaky test" in prompt and "Old migration" not in prompt
    assert "short-term" not in prompt.lower()
    empty = goal_reminder.reminder_prompt(rec, "monthly", finished=[])
    assert "from your own work log" in empty


def test_a_thread_with_no_ledger_has_finished_nothing(_floor_monkeypatch) -> None:
    from kiro_crew import work_ledger

    def refuse(_key: str):
        raise work_ledger.WorkLedgerError("no such slot", code=work_ledger.CODE_INVALID_VALUE)

    _floor_monkeypatch.setattr(work_ledger, "list_work_items", refuse)
    assert goal_reminder.finished_tasks("weird key", "weekly") == []


@pytest.mark.parametrize(
    ("cadence", "marker"),
    [
        ("daily", "[Daily goal reminder]"),
        ("weekly", "[Weekly report]"),
        ("monthly", "[Monthly review]"),
    ],
)
def test_each_check_in_prompt_is_written_as_principles(cadence: str, marker: str) -> None:
    rec = member_goals.empty_record()
    rec["long_term"] = "Keep docs current"
    prompt = goal_reminder.reminder_prompt(rec, cadence)
    assert prompt.startswith(marker)
    for word in ("must", "never", "exactly"):
        assert word not in prompt.lower()
    if cadence != "daily":
        assert "save_goal_report" in prompt and f'kind "{cadence}"' in prompt
    # Only the Monthly review is admitted to raise the goal cards, so only it asks.
    asks = "set_goal" in prompt and "set_key_results" in prompt
    assert asks is (cadence == "monthly")


def test_the_goal_is_injected_after_the_rules_and_before_the_briefing() -> None:
    from kiro_crew.context_assembly.member import build_member_section

    _seed()
    slug = _slug(ORDINARY)
    members_mod.write_member_rules(slug, member=ORDINARY, text="Ask before deleting anything.")
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "pages reviewed", "target": "40"}])
    section = build_member_section(ORDINARY)
    rules_at = section.index("\n\n[PERMANENT RULES")
    goal_at = section.index("\n\n[YOUR GOAL")
    assert rules_at < goal_at
    assignment = "\n\n[CURRENT ASSIGNMENT"
    assert assignment in section
    assert goal_at < section.index(assignment)
    assert "Keep docs current" in section and "pages reviewed" in section
    assert "short_goal" not in section and "short-term" not in section


def test_the_goal_section_carries_no_reading_so_progress_does_not_resend_it() -> None:
    """A reading must not change the per-turn section; the check-in prompt has it."""
    from kiro_crew.context_assembly.member import build_member_section

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_goal(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "pages reviewed", "target": "40"}])
    before = build_member_section(ORDINARY)
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, "17 pages")
    after = build_member_section(ORDINARY)
    assert after == before
    assert "17 pages" not in after and "pages reviewed" in after and "40" in after
    prompt = goal_reminder.reminder_prompt(member_goals.read_goals(slug, ORDINARY), "daily")
    assert "17 pages" in prompt
