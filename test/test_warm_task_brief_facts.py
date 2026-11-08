"""Checked facts and the strict line rule of the warm welcome
(RFC ``rfc-delayed-task-brief.md`` section 3.9.1, "Checked facts").

The gateway numbers what it read (``{fN}``), deletes body lines that state a
PR, task, file, deploy or ticket without citing one, keeps progress counts to
the ledger's, reads every ledger PR before the brief may name it, and does
not send a brief again when nothing it stood on changed.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_warm_task_brief import (
    _arm_model,
    _claimed,
    _foreground_slot,
    _iso,
    _posted,
    _snapshot,
    _State,
)

from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_facts as facts
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.session_summary import TranscriptTurn

_LIVE = (
    {
        "pr": "#12",
        "repo": "kirodotdev/KiroCrew",
        "checked": True,
        "state": "merged",
        "review": "APPROVED",
        "checks": None,
        "read_at": "09:30",
    },
    {"pr": "#13", "repo": "kirodotdev/KiroCrew", "checked": False, "not_checked_since": "never"},
)
_ITEMS = ({"item_id": "it_0000000a", "title": "Ship the importer", "state": "open"},)


def _facts():
    return facts.checked_facts(_LIVE, _ITEMS)


def test_each_read_gets_an_id_and_an_unread_pr_says_only_not_checked():
    found = _facts()
    assert [(f["id"], f["kind"], f["label"]) for f in found] == [
        ("f1", "pr", "kirodotdev/KiroCrew#12"),
        ("f2", "pr", "kirodotdev/KiroCrew#13"),
        ("f3", "task", "Ship the importer"),
    ]
    # Machine values, as read; the card translates them.
    assert found[0]["values"] == {"state": "merged", "review": "APPROVED"}
    assert found[0]["source"] == "github"
    assert found[1]["values"] == {"not_checked_since": "never"}
    assert facts.card_facts(found)[2] == {
        "id": "f3",
        "kind": "task",
        "label": "Ship the importer",
        "source": "tasks",
    }


def _line(kind, refs, text="a line"):
    return {"text": text, "kind": kind, "facts": refs}


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        (_line("pr", ["f1"]), True),
        (_line("pr", []), False),
        # An id is looked up exactly; an unknown one drops the line.
        (_line("pr", ["f9"]), False),
        (_line("pr", ["f1", "f9"]), False),
        # The kind must match: a task fact does not back a pr line.
        (_line("pr", ["f3"]), False),
        (_line("task", ["f3"]), True),
        (_line("task", ["f1"]), False),
        # A PR the read could not reach still has its own fact.
        (_line("pr", ["f2"]), True),
        # No fact covers files, deploys or tickets yet, cited or not.
        (_line("file", []), False),
        (_line("deploy", ["f1"]), False),
        (_line("ticket", ["f3"]), False),
        (_line("none", []), True),
        (_line("none", ["f1"]), True),
        (_line("none", ["f7"]), False),
    ],
)
def test_a_typed_line_stays_only_when_its_fact_ids_back_its_kind(line, kept):
    assert facts.typed_lines([line], _facts()) == ((line,) if kept else ())


def test_raw_lines_are_typed_bounded_and_an_unknown_kind_is_none():
    raw = [{"text": " x ", "kind": "pr", "facts": ["f1"]}, {"text": "y", "kind": "boss"}, "z"]
    raw += [{"text": ""}, *({"text": str(n)} for n in range(20))]
    lines = facts.raw_lines(raw, lambda v: str(v or "").strip())
    assert lines[:2] == (_line("pr", ["f1"], "x"), _line("none", [], "y"))
    assert len(lines) == facts.MAX_LINES


def test_a_three_digit_fact_id_is_looked_up_and_the_count_is_bounded():
    items = [{"item_id": f"it_{n:08x}", "title": f"B{n:03d}", "state": "open"} for n in range(250)]
    found = facts.checked_facts((), items)
    assert len(found) == facts.MAX_FACTS and found[-1]["id"] == "f200"
    assert facts.typed_lines([_line("task", ["f100"])], found) == (_line("task", ["f100"]),)
    assert facts.marks(["f1", "f100"]) == "¹,¹⁰⁰"


def test_the_body_is_kept_as_written_with_no_picture_count():
    body = "- words\n```mermaid\nA --> B\n```\n| a | b |\n<mcwidget><p>x</p></mcwidget>"
    brief = facts.checked_brief(
        warm_task_brief.Brief("on_track", "h", "", "", (), (), (), (), body=body), _facts(), ()
    )
    assert brief.body == body


def test_check_counts_keep_an_unknown_result_and_read_as_words():
    entry = {**_LIVE[0], "state": "open", "mergeable": "MERGEABLE", "review": ""}
    entry["checks"] = {"passed": 1, "failed": 0, "pending": 2, "unknown": 1}
    (fact,) = facts.checked_facts((entry,), ())
    words = ["open", "no conflicts", "checks 1 passed, 0 failed, 2 running, 1 unknown"]
    assert facts.fact_words(fact["values"]) == words
    task = {"item_id": "it_0000000b", "title": "Ship", "state": "open", "status": "progress"}
    assert facts.fact_words(facts.checked_facts((), (task,))[0]["values"]) == ["ongoing"]


def test_every_open_items_ledger_pr_is_read_before_it_may_be_named():
    def item(n, status):
        acceptance = {"kind": "pr_checks", "pr": n, "repo": "kirodotdev/KiroCrew"}
        return {"state": "open", "status": status, "acceptance": acceptance}

    def terminal(state):
        return state == "accepted"

    items = [item(1, "progress"), item(2, "question"), {**item(3, "done"), "state": "accepted"}]
    waiting = [{"text": "see https://github.com/acme/x/pull/9", "options": []}]
    refs = facts.live_refs(items, waiting, terminal)
    # The ask first, its URL, then the other open item; a closed item is not read.
    assert [(r.repo, r.number) for r in refs] == [
        ("kirodotdev/KiroCrew", 2),
        ("acme/x", 9),
        ("kirodotdev/KiroCrew", 1),
    ]
    many = [item(n, "progress") for n in range(1, 9)]
    assert len(facts.live_refs(many, [], terminal)) == inputs.MAX_LIVE_PRS


def test_the_digest_ignores_read_times_only():
    moved = ({**_LIVE[0], "read_at": "10:00", "_read_epoch": 1.0}, _LIVE[1])
    assert facts.facts_digest("d", [], _LIVE) == facts.facts_digest("d", [], moved)
    merged = ({**_LIVE[0], "state": "open"}, _LIVE[1])
    assert facts.facts_digest("d", [], _LIVE) != facts.facts_digest("d", [], merged)
    assert facts.facts_digest("d", [], _LIVE) != facts.facts_digest("e", [], _LIVE)


@pytest.mark.asyncio
async def test_a_posted_brief_drops_uncited_lines_and_lists_what_was_checked(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(inputs, "probe_github", lambda urls: {})
    state = _State()
    slot = _foreground_slot(state)
    item = {"item_id": "it_0000000a", "title": "Fix the test", "state": "open"}
    base = replace(_snapshot(), work_items=(item,))
    lines = [
        {"text": "Fix the test is open", "kind": "task", "facts": ["f1"]},
        {"text": "edited src/app/main.py", "kind": "file", "facts": []},
        {"text": "Fix the test is done", "kind": "task", "facts": []},
    ]

    async def run_model(*args, **kwargs):
        return json.dumps({"headline": "Back.", "lines": lines, "body": "```\nA --> B\n```"})

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    row = slot.rows[-1]
    brief = row["meta"]["brief"]
    # The file line and the uncited task line go; the picture stays as written.
    assert brief["lines"] == [lines[0]]
    assert brief["body"] == "```\nA --> B\n```"
    # Copy and reply threads read ``content``: the marks keep their rows.
    assert (
        "- Fix the test is open¹\nChecked just now:\n¹ Fix the test (task list)" in row["content"]
    )
    assert brief["checked"] == [
        {
            "id": "f1",
            "kind": "task",
            "label": "Fix the test",
            "source": "tasks",
        }
    ]


@pytest.mark.asyncio
async def test_nothing_is_sent_again_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(inputs, "probe_github", lambda urls: {})
    calls: list[str] = []

    async def run_model(*args, **kwargs):
        calls.append("model")
        return json.dumps({"headline": "Back."})

    # Only the owner spoke: nothing new in the thread since they left.
    quiet_turns = (TranscriptTurn(index=1, role="user", text="Fix it", ts=_iso(0.0)),)
    base = replace(_snapshot(), turns=quiet_turns)
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    digest = slot.rows[-1]["meta"][facts.FACTS_DIGEST_KEY]

    again = replace(base, fingerprint="fp2", previous_facts=digest)
    state2 = _State()
    slot2 = _foreground_slot(state2)
    _arm_model(monkeypatch, slot2, [again, _claimed(again), _claimed(again)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state2, slot2)
    assert calls == ["model"]
    assert _posted(slot2) == []
    # No "preparing" indicator shows for a return that sends nothing.
    assert state2.preparing_pushes == []
    # A changed ledger is sent.
    changed = replace(again, ledger_digest="other")
    slot3 = _foreground_slot(state2)
    _arm_model(monkeypatch, slot3, [changed, _claimed(changed), _claimed(changed)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state2, slot3)
    assert calls == ["model", "model"] and len(_posted(slot3)) == 1


def test_a_secret_in_a_checked_fact_is_caught_and_the_fallback_drops_the_facts():
    brief = warm_task_brief.Brief(
        status="on_track",
        headline="Back.",
        asked="",
        remaining="",
        done=(),
        needs_you=(),
        next=(),
        extra=(),
        checked=({"id": "f1", "kind": "task", "label": "key AKIA&#73;OSFODNN7EXAMPLE"},),
    )
    content = warm_task_brief._brief_text(brief, "en")
    assert warm_task_brief._grounding_miss(brief, content, set(), "", set()) == "secret"
    assert warm_task_brief._gateway_only(brief, "en").checked == ()


@pytest.mark.asyncio
async def test_an_owner_who_leaves_during_the_first_read_gets_no_brief(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    state = _State()
    slot = _foreground_slot(state)
    calls: list[str] = []

    def probe(urls):
        state._foreground_slots.clear()  # the tab closes while GitHub is read
        return {}

    async def run_model(*args, **kwargs):
        calls.append("model")
        return json.dumps({"headline": "Back."})

    monkeypatch.setattr(inputs, "probe_github", probe)
    acceptance = {"kind": "pr_checks", "pr": 12, "repo": "kirodotdev/KiroCrew"}
    item = {"item_id": "it_0000000a", "state": "open", "status": "progress"}
    base = replace(_snapshot(), work_items=({**item, "acceptance": acceptance},))
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert calls == [] and _posted(slot) == [] and state.preparing_pushes == []


@pytest.mark.asyncio
async def test_a_turn_started_during_the_first_read_defers_the_brief_not_drops_it(monkeypatch):
    import asyncio

    monkeypatch.setattr(inputs, "_live_cache", {})
    state = _State()
    slot = _foreground_slot(state)
    loop = asyncio.get_running_loop()

    read: list[list[str]] = []

    def probe(urls):
        read.append(list(urls))
        if len(read) == 1:
            slot.running = True  # a patrol turn is admitted while GitHub is read

            def end_turn():
                slot.running = False

            loop.call_soon_threadsafe(loop.call_later, 0.05, end_turn)
        return {}

    monkeypatch.setattr(inputs, "probe_github", probe)
    acceptance = {"kind": "pr_checks", "pr": 12, "repo": "kirodotdev/KiroCrew"}
    item = {"item_id": "it_0000000a", "state": "open", "status": "progress"}
    base = replace(_snapshot(), work_items=({**item, "acceptance": acceptance},))
    # The turn adds a work item with its own PR: the brief reads it too.
    added = {**item, "item_id": "it_0000000b", "acceptance": {**acceptance, "pr": 13}}
    after = replace(_claimed(base), work_items=(*base.work_items, added))
    _arm_model(monkeypatch, slot, [base, _claimed(base), after, after], None)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert [row["meta"]["kind"] for row in _posted(slot)] == ["warm_task_brief_result"]
    assert [url.rsplit("/", 1)[1] for url in read[-1]] == ["12", "13"]
