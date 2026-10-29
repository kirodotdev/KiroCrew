"""The work ledger's conducting depth cap is a setting, and nothing moves by default.

``work_ledger.max_depth`` (config) sets how many levels may conduct. The default stays 2, the range is 2 to 10, and a value
outside it is refused rather than clamped. ``may_conduct`` stays the one reader, so the
dispatch, ledger-open and item-create checks move together. The last block drives the
real routes at a cap of 3: a depth-2 conductor opens a ledger, creates items and binds a
worker whose report lands, and a depth-3 session is refused with the setting named.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import test_work_ledger_tools as tools
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR

from kiro_crew import work_ledger as wl
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.work_vocab import (
    WORK_MAX_DEPTH_DEFAULT,
    WORK_MAX_DEPTH_MAX,
    WORK_MAX_DEPTH_MIN,
)

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home
_open_route = tools._open_route
_clean_slots = tools._clean_slots


def _write_config(tmp_path: Path, section: object) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"work_ledger": section}), encoding="utf-8")
    # Rewritten within one mtime tick of the last write, so drop the cached parse.
    _invalidate_config_cache()


def _set_cap(tmp_path: Path, depth: int) -> None:
    _write_config(tmp_path, {"max_depth": depth})


# ── the default and the bounds ────────────────────────────────────────────


def test_the_default_cap_is_two_and_the_range_is_two_to_ten():
    assert (WORK_MAX_DEPTH_DEFAULT, WORK_MAX_DEPTH_MIN, WORK_MAX_DEPTH_MAX) == (2, 2, 10)
    assert wl.MAX_DEPTH == WORK_MAX_DEPTH_DEFAULT
    assert wl.max_depth() == 2
    assert KiroCrewConfig.load().work_ledger.max_depth == 2


@pytest.mark.parametrize("depth", [2, 3, 7, 10])
def test_config_accepts_every_value_in_range(tmp_path: Path, depth: int):
    _write_config(tmp_path, {"max_depth": depth})
    assert KiroCrewConfig.load().work_ledger.max_depth == depth
    assert wl.max_depth() == depth


@pytest.mark.parametrize("bad", [1, 0, -3, 11, 100, True, "abc", 2.5, None, [3]])
def test_config_refuses_an_out_of_range_value_and_keeps_the_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: object
):
    """Refused, not clamped: 11 does not become 10 and 1 does not become 2."""
    _write_config(tmp_path, {"max_depth": bad})
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        loaded = KiroCrewConfig.load().work_ledger.max_depth
    assert loaded == WORK_MAX_DEPTH_DEFAULT
    assert wl.max_depth() == WORK_MAX_DEPTH_DEFAULT
    assert any("work_ledger.max_depth" in record.getMessage() for record in caplog.records)


def test_the_setting_round_trips_through_to_dict(tmp_path: Path):
    _write_config(tmp_path, {"max_depth": 4})
    assert KiroCrewConfig.load().to_dict()["work_ledger"] == {
        "max_depth": 4,
        "remote_dispatch": False,
        "remote_dispatch_memory_gb": 2.0,
        "remote_dispatch_disk_gb": 10.0,
    }


# ── no environment override ───────────────────────────────────────────────


def test_the_environment_does_not_move_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The setting lives in config only; a stray variable changes nothing."""
    _set_cap(tmp_path, 4)
    monkeypatch.setenv("KIROCREW_WORK_LEDGER_MAX_DEPTH", "6")
    assert wl.max_depth() == 4


# ── one check point ───────────────────────────────────────────────────────


@pytest.mark.parametrize("cap", [2, 3, 5, 10])
def test_the_three_guards_follow_the_cap_together(tmp_path: Path, cap: int):
    """Dispatch, ledger open and item create all flip at ``cap``, never apart."""
    _set_cap(tmp_path, cap)
    for depth in range(0, cap + 2):
        allowed = depth < cap
        assert wl.may_conduct(depth) is allowed

        def refused(call) -> bool:
            try:
                call()
            except wl.WorkLedgerError as exc:
                assert exc.code == wl.CODE_DEPTH_EXCEEDED
                return True
            return False

        if depth > 0:
            assert refused(lambda: wl.child_depth(depth - 1)) is (not allowed)
        assert refused(lambda: wl.ensure_conductor(f"k{cap}-{depth}", depth=depth)) is (not allowed)


def test_a_refusal_names_the_setting_and_the_value_in_force(tmp_path: Path):
    _set_cap(tmp_path, 3)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.child_depth(2)
    message = str(caught.value)
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED
    assert "work_ledger.max_depth" in message
    assert "KIROCREW" not in message
    assert "the cap is 3 levels" in message


def test_a_deeper_conductor_creates_items_once_the_cap_is_raised(tmp_path: Path):
    wl.ensure_conductor("deep", goal="g", depth=1)
    with pytest.raises(wl.WorkLedgerError):
        wl.ensure_conductor("deeper", goal="g", depth=2)
    _set_cap(tmp_path, 3)
    wl.ensure_conductor("deeper", goal="g", depth=2)
    created = wl.apply_conductor_action(
        "deeper", "create", title="t", acceptance={"kind": "human_approval"}
    )
    assert created["item"].item_id


def test_lowering_the_cap_refuses_new_creates_and_leaves_the_ledger_readable(tmp_path: Path):
    _set_cap(tmp_path, 3)
    wl.ensure_conductor("deeper", goal="g", depth=2)
    made = wl.apply_conductor_action(
        "deeper", "create", title="t", acceptance={"kind": "human_approval"}
    )["item"].item_id
    _set_cap(tmp_path, 2)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(
            "deeper", "create", title="u", acceptance={"kind": "human_approval"}
        )
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED
    assert [item.item_id for item in wl.list_work_items("deeper")] == [made]


def test_the_default_still_refuses_a_conductor_dispatched_by_depth_one():
    """The behaviour every existing install keeps."""
    assert wl.child_depth(0) == 1
    with pytest.raises(wl.WorkLedgerError):
        wl.child_depth(1)
    wl.ensure_conductor(CONDUCTOR, goal="g", depth=1)


# ── the real routes at a cap of 3 ─────────────────────────────────────────

LEAD = "chat-depth-lead"
GROUP = "chat-depth-group"
LEAF = "chat-depth-leaf"
TOO_DEEP = "chat-depth-too-deep"


@pytest.mark.asyncio
async def test_a_depth_two_conductor_runs_a_team_and_a_depth_three_session_is_refused(
    tmp_path: Path,
):
    _set_cap(tmp_path, 3)
    # LEAD (depth 0) -> GROUP (depth 1) -> LEAF (depth 2, conducting) -> TOO_DEEP (depth 3).
    assert (await tools._record(LEAD, {"action": "goal", "goal": "top", "round": 1}))[0] == 200
    status, body = await tools._record(
        LEAD, {"action": "create", "title": "group", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    group_item = body["item"]["item_id"]
    tools._dispatched(GROUP, LEAD)
    assert (
        await tools._record(
            LEAD, {"action": "bind", "item_id": group_item, "worker_session_key": GROUP}
        )
    )[0] == 200

    assert (await tools._record(GROUP, {"action": "goal", "goal": "middle", "round": 1}))[0] == 200
    status, body = await tools._record(
        GROUP, {"action": "create", "title": "lead", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    lead_item = body["item"]["item_id"]
    tools._dispatched(LEAF, GROUP)
    assert (
        await tools._record(
            GROUP, {"action": "bind", "item_id": lead_item, "worker_session_key": LEAF}
        )
    )[0] == 200

    # Depth 2 opens a ledger, creates an item and binds a worker...
    status, body = await tools._record(LEAF, {"action": "goal", "goal": "bottom", "round": 1})
    assert status == 200, body
    status, body = await tools._read(LEAF)
    assert status == 200
    assert (body["conductor"]["depth"], body["conductor"]["max_depth"]) == (2, 3)
    status, body = await tools._record(
        LEAF, {"action": "create", "title": "work", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    work_item = body["item"]["item_id"]
    tools._dispatched(TOO_DEEP, LEAF)
    assert (
        await tools._record(
            LEAF, {"action": "bind", "item_id": work_item, "worker_session_key": TOO_DEEP}
        )
    )[0] == 200

    # ...its own report reaches the depth-1 parent's item...
    status, body = await tools._report(LEAF, {"status": "progress", "summary": "round 1 out"})
    assert status == 200, body
    assert wl.read_work_item(GROUP, lead_item).summary == "round 1 out"

    # ...and a depth-3 session may work but not conduct, with the setting named.
    status, body = await tools._record(TOO_DEEP, {"action": "goal", "goal": "no", "round": 1})
    assert status == 409, body
    assert body["code"] == wl.CODE_DEPTH_EXCEEDED
    assert "work_ledger.max_depth" in json.dumps(body)
    assert (await tools._report(TOO_DEEP, {"status": "progress", "summary": "still works"}))[
        0
    ] == 200


@pytest.mark.asyncio
async def test_lowering_the_cap_keeps_goal_updates_working_on_an_open_deeper_ledger(
    tmp_path: Path,
):
    """A depth-2 conductor made under cap 3 keeps its ledger when the cap drops to 2.

    ``goal`` re-enters the bootstrap on every call; it must not re-derive the depth
    of a ledger that already exists. Only creating items past the cap is refused.
    """
    _set_cap(tmp_path, 3)
    assert (await tools._record(LEAD, {"action": "goal", "goal": "top", "round": 1}))[0] == 200
    status, body = await tools._record(
        LEAD, {"action": "create", "title": "group", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    tools._dispatched(GROUP, LEAD)
    bind = {"action": "bind", "item_id": body["item"]["item_id"], "worker_session_key": GROUP}
    assert (await tools._record(LEAD, bind))[0] == 200
    assert (await tools._record(GROUP, {"action": "goal", "goal": "mid", "round": 1}))[0] == 200
    status, body = await tools._record(
        GROUP, {"action": "create", "title": "lead", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    lead_item = body["item"]["item_id"]
    tools._dispatched(LEAF, GROUP)
    bind = {"action": "bind", "item_id": lead_item, "worker_session_key": LEAF}
    assert (await tools._record(GROUP, bind))[0] == 200
    assert (await tools._record(LEAF, {"action": "goal", "goal": "bottom", "round": 1}))[0] == 200
    assert wl.read_conductor(LEAF).depth == 2

    _set_cap(tmp_path, 2)

    # The regression: this was 409 depth_exceeded, from child_depth(parent.depth).
    status, body = await tools._record(LEAF, {"action": "goal", "goal": "bottom 2", "round": 2})
    assert status == 200, body
    assert wl.read_conductor(LEAF).goal == "bottom 2"
    assert (await tools._read(LEAF))[0] == 200
    assert (await tools._report(LEAF, {"status": "progress", "summary": "still bound"}))[0] == 200
    # What the cap does refuse after lowering: new items at that level...
    status, body = await tools._record(
        LEAF, {"action": "create", "title": "more", "acceptance": {"kind": "human_approval"}}
    )
    assert (status, body["code"]) == (409, wl.CODE_DEPTH_EXCEEDED)
    # ...and a NEW ledger at that depth.
    status, body = await tools._record(
        GROUP, {"action": "create", "title": "other", "acceptance": {"kind": "human_approval"}}
    )
    assert status == 200, body
    tools._dispatched(TOO_DEEP, GROUP)
    bind = {"action": "bind", "item_id": body["item"]["item_id"], "worker_session_key": TOO_DEEP}
    assert (await tools._record(GROUP, bind))[0] == 200
    status, body = await tools._record(TOO_DEEP, {"action": "goal", "goal": "new", "round": 1})
    assert (status, body["code"]) == (409, wl.CODE_DEPTH_EXCEEDED)


def test_an_unreadable_config_falls_back_to_the_default_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An operator who set the cap to 3 must find out why the cap reads 2.

    The warning names the setting, the value in the file and the default used, and is
    logged once per reason, not on every ledger guard.
    """
    _set_cap(tmp_path, 3)
    monkeypatch.setattr(wl, "_WARNED_DEPTH_FALLBACKS", set())

    def boom(cls):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.work_ledger"):
        assert wl.max_depth() == WORK_MAX_DEPTH_DEFAULT
        assert wl.max_depth() == WORK_MAX_DEPTH_DEFAULT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "work_ledger.max_depth" in message
    assert "config exploded" in message
    assert "3" in message
    assert f"default depth cap {WORK_MAX_DEPTH_DEFAULT}" in message
