"""Section 3.9.6: an unbacked SHA or time is tagged unverified and the brief
posts, unless the tag cannot sit beside the value or fit the card."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_warm_task_brief import _WAITING_ITEM, _claimed, _posted, _Slot, _snapshot, _State

from kiro_crew.dashboard import warm_task_brief


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "logged", "shown"),
    [
        (
            {"headline": "Back. Merged at 17:05", "body": "- built at 7:05"},
            ["07:05", "17:05"],
            ["17:05", "7:05"],
        ),
        ({"headline": "Back.", "body": "- landed as a1b2c3d4"}, ["a1b2c3d4"], ["a1b2c3d4"]),
    ],
    ids=["time", "sha"],
)
async def test_an_unbacked_sha_or_time_is_kept_and_tagged_unverified(
    monkeypatch, caplog, result, logged, shown
):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = replace(_snapshot(), work_items=(_WAITING_ITEM,))

    async def read_snapshot(*args, **kwargs):
        return _claimed(snapshot) if slot.flushed else snapshot

    async def run_model(*args, **kwargs):
        return json.dumps(result)

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(warm_task_brief.inputs, "clock", lambda *args, **kwargs: "12:00")
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    with caplog.at_level("INFO", logger=warm_task_brief.logger.name):
        await warm_task_brief._prepare_warm_task_brief(state, slot)
    (row,) = _posted(slot)
    meta = row["meta"]
    tag = warm_task_brief._UNVERIFIED_TEXT["en"]
    posted = meta["brief"]["headline"] + "\n" + meta["brief"]["body"]
    assert meta["brief"]["headline"] != warm_task_brief._FAILURE_TEXT["en"]
    for value in shown:
        assert f"{value} {tag}" in posted and f"{value} {tag}" in row["content"]
    assert meta["brief"]["waiting"] == [{"title": "Pick a layout", "ask": "Grid or list?"}]
    # Still a miss: the next return is not read as caught up.
    assert meta["grounding_miss"] == "unbacked_fact"
    assert warm_task_brief._claim_digest(slot.rows) == ""
    assert f"marked unbacked_fact: {logged}" in caplog.text


def test_a_tag_never_hides_or_rejoins_a_secret():
    brief = warm_task_brief._parse_result(
        json.dumps({"headline": "Back at 23:47", "body": "| key AK<b></b>IAIOSFODNN7EXAMPLE |"}),
        "",
    )
    assert brief is not None
    marked = warm_task_brief._marked(brief, ["23:47"], "en")
    assert marked is not None
    content = warm_task_brief._brief_text(marked, "en")
    tag = warm_task_brief._UNVERIFIED_TEXT["en"]
    assert "23:47 " + tag in marked.headline
    miss = warm_task_brief._grounding_miss(marked, content, set(), "", set(), tag)
    assert miss == "secret"


def test_a_tag_the_card_would_cut_off_is_no_tag():
    # The card keeps 240 characters of a field: a tag past them would vanish.
    headline = "x" * 226 + " at 17:05"
    brief = warm_task_brief._parse_result(json.dumps({"headline": headline}), "")
    assert brief is not None and brief.headline == headline
    assert warm_task_brief._marked(brief, ["17:05"], "en") is None
    # An emoji is two UTF-16 units: 238 code points, 253 units on the card.
    wide = replace(brief, headline="\U0001f680" * 15 + "x" * 204 + " 17:05")
    assert warm_task_brief._marked(wide, ["17:05"], "en") is None
    short = replace(brief, headline="Back at 17:05")
    assert warm_task_brief._marked(short, ["17:05"], "en") is not None


def test_a_wake_checks_words_are_tagged_and_gateway_facts_are_not():
    wake_fact = {
        "id": "f2",
        "kind": "deploy",
        "label": "Build at 17:05",
        "source": "wake",
        "values": {"result": "finished 17:05"},
    }
    pr_fact = {
        "id": "f1",
        "kind": "pr",
        "label": "#12 at 17:05",
        "source": "github",
        "values": {"state": "open"},
    }
    brief = warm_task_brief._parse_result(json.dumps({"headline": "Back."}), "")
    assert brief is not None
    brief = replace(brief, checked=(pr_fact, wake_fact))
    marked = warm_task_brief._marked(brief, ["17:05"], "en")
    assert marked is not None
    tag = warm_task_brief._UNVERIFIED_TEXT["en"]
    assert marked.checked[0] == pr_fact
    assert marked.checked[1]["label"] == f"Build at 17:05 {tag}"
    assert marked.checked[1]["values"]["result"] == f"finished 17:05 {tag}"
    miss = warm_task_brief._grounding_miss(marked, "", set(), "", set(), tag)
    assert miss == ""


@pytest.mark.parametrize(
    "text",
    [
        "done at 17:05:30 today",
        "see [x](a1b2c3d4) here",
        "landed as `a1b2c3d4`",
        "landed as **a1b2c3d4**def",
        "(a1b2c3d4) landed",
    ],
)
def test_a_value_without_a_clean_boundary_is_left_for_the_recheck(text):
    tag = warm_task_brief._UNVERIFIED_TEXT["en"]
    assert tag not in warm_task_brief._mark(text, {"17:05", "a1b2c3d4"}, tag)


def test_a_body_with_a_picture_is_never_tagged():
    body = "- note at 17:05\n- ```mermaid\n  gantt\n  task :a, 17:05, 30m\n  ```"
    tag = warm_task_brief._UNVERIFIED_TEXT["en"]
    assert warm_task_brief._mark_body(body, {"17:05"}, tag) == body
    assert tag in warm_task_brief._mark_body("- note at 17:05", {"17:05"}, tag)
