"""The warm welcome redesign, one test group per root cause of the analysis.

RC1 the payload was sliced at 40,000 characters and lost the transcript; RC2
assistant turns were cut to 400+400 characters and the pending ask with them;
RC3 the reply language followed a guessed "original request"; RC6 a
conductor's own crewmate thread got the crewmate prompt; RC8 a separate opener
could be orphaned. RC4 and RC5 (live PR state, buttons, links) come in a
follow-up change. RC7 (the whole-board count) is pinned in
``test_warm_task_brief_shape.py``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_warm_task_brief import (
    _arm_model,
    _claimed,
    _focus,
    _foreground_slot,
    _iso,
    _snapshot,
    _State,
    _thread,
)

from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.session_summary import TranscriptTurn

_ASK = (
    "Two PRs wait on you.\n"
    + "Detail line. " * 120
    + "\n#26100 is green; I recommend merging it.\n"
    "[OPTIONS: Merge #26100 now | Hold #26100 until Monday]"
)


def _turn(index: int, role: str, text: str, ago: float, *, injected: bool = False):
    return TranscriptTurn(
        index=index, role=role, text=text, ts=_iso(time.time() - ago), injected=injected
    )


def _item(n: int, **fields) -> dict:
    item = {
        "item_id": f"it_{n:08x}",
        "title": f"Item {n}",
        "state": "accepted",
        "status": "done",
        "summary": "s" * 500,
        "decision": "d" * 2000,
        "acceptance": {"kind": "pr_checks", "pr": n, "repo": "kirodotdev/KiroCrew"},
        "artifacts": {},
        "created_at": "2026-10-10T00:00:00+00:00",
        "last_report_at": "2026-10-10T00:00:00+00:00",
    }
    item.update(fields)
    return item


class _FixedToday(_dt.datetime):
    """``datetime`` whose ``now`` never moves, so clock() picks the same
    form (with or without the date) on both sides of an assertion."""

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 10, 31, 12, 0, tzinfo=_dt.timezone.utc).astimezone(tz)


@pytest.fixture(autouse=True)
def _fixed_today(_floor_monkeypatch):
    _floor_monkeypatch.setattr(inputs, "datetime", _FixedToday)


def _payload_of(prompt: str) -> dict:
    body = prompt.split("UNTRUSTED TASK SNAPSHOT\n", 1)[1].split("\nEND UNTRUSTED", 1)[0]
    return json.loads(body)


def _snap(**fields) -> warm_task_brief.TaskSnapshot:
    return replace(_snapshot(), **fields)


# ---------------------------------------------------------------- RC1


def test_rc1_each_section_keeps_whole_entries_within_its_budget():
    entries = [{"text": "x" * 900} for _ in range(20)]
    kept = inputs.fit(entries, 5_000)
    assert 0 < len(kept) < 20 and all(entry == entries[0] for entry in kept)
    items = tuple(_item(n, state="open", status="progress") for n in range(100))
    open_items = inputs.open_item_entries(items, None, warm_task_brief.work_item_is_terminal)
    size = len(json.dumps(open_items, ensure_ascii=False, separators=(",", ":")))
    assert 0 < len(open_items) < 100 and size <= inputs.OPEN_ITEMS_CHARS + 2 * len(open_items)


# ---------------------------------------------------------------- RC2


# ---------------------------------------------------------------- RC3


# ---------------------------------------------------------------- RC6


# ---------------------------------------------------------------- quiet, RC8


@pytest.mark.asyncio
async def test_a_quiet_return_posts_one_line_and_spends_nothing(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        turns=(_turn(1, "user", "Fix it", 9000),),
        work_items=(),
        ledger_digest="d1",
        previous_digest="d1",
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("a quiet return makes no model call")

    _arm_model(monkeypatch, slot, [base, _claimed(base)], unexpected)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    rows = [row for row in slot.rows if row["role"] == "assistant"]
    assert len(rows) == 1
    assert rows[0]["content"] == warm_task_brief._QUIET_TEXT["en"].format(
        at=inputs.clock(base.last_user_at)
    )
    assert slot._preparing_count == 0 and state.preparing_pushes == []
    # A plain line, no card: it must not claim "on track" for a blocked task.
    assert "brief" not in rows[0]["meta"]


@pytest.mark.asyncio
async def test_a_plain_chat_is_never_briefed_even_with_pending_choices(monkeypatch):
    state, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    slot.mode = "chat"
    log.rows.append({"role": "assistant", "content": _ASK, "ts": _iso(time.time() - 60)})
    await _focus(state, 1, slot.key)
    assert [row for row in slot.rows if (row.get("meta") or {}).get("kind")] == []
    assert log.reads == 0


# ---------------------------------------------------------------- review fixes


@pytest.mark.asyncio
async def test_session_ledger_progress_while_away_is_not_a_quiet_return(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    recent = _iso(time.time() - 120)
    base = _snap(
        turns=(_turn(1, "user", "Fix it", 9000),),
        work_items=(),
        session_state={
            "phase": "implementing",
            "last_progress_at": recent,
            "events": [{"ts": recent, "kind": "progress", "text": "Rebased onto main"}],
        },
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    # Not the quiet line: the full welcome, which starts with the goal.
    assert slot.rows[-1]["content"].startswith("**Goal**")


@pytest.mark.asyncio
async def test_a_question_reported_while_the_claim_settles_is_not_missed(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    quiet = _snap(turns=(_turn(1, "user", "Fix it", 9000),), work_items=())
    # A worker asks a question between the first read and the settled re-read.
    asked = replace(
        _claimed(quiet),
        work_items=(_item(4, state="open", status="question", title="Pick a layout"),),
    )
    _arm_model(monkeypatch, slot, [quiet, asked, asked])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert "Pick a layout" in slot.rows[-1]["content"]
    # The claim and the posted row keep the first snapshot's fingerprint.
    assert slot.rows[-1]["meta"]["fingerprint"] == quiet.fingerprint


def test_the_quiet_line_claims_only_nothing_new_and_dates_an_older_time(monkeypatch):
    from datetime import datetime as real_datetime
    from datetime import timezone

    noon = real_datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    class _Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return noon.astimezone(tz) if tz else noon.replace(tzinfo=None)

    # A frozen "now", in UTC, so the same-day test never straddles midnight.
    monkeypatch.setattr(inputs, "datetime", _Frozen)
    monkeypatch.setattr(
        "kiro_crew.cron_service.schedule.get_local_tz", lambda: ("UTC", timezone.utc)
    )
    assert all("waiting" not in text for text in warm_task_brief._QUIET_TEXT.values())
    assert inputs.clock(noon.timestamp() - 3600) == "11:00"
    assert inputs.clock(noon.timestamp() - 26 * 3600) == "2026-10-30 10:00"


def test_each_epoch_helper_has_one_definition():
    assert warm_task_brief._parse_epoch is inputs.parse_epoch
    assert warm_task_brief._row_epoch is inputs.row_epoch


# ------------------------------------------------- the "Waiting on you" list


ASKED = "2026-10-18T18:40:00+00:00"
ASKED_S = 1792348800.0  # 2026-10-18T18:40:00Z
_OPTIONS_ASK = "Open a PR for the docs fix? [OPTIONS: Open the PR | Not yet]"


class _Cards:
    """The change-card store: its pending cards and the person's presses."""

    def __init__(self, cards):
        self.cards = cards

    async def warm(self):
        return None

    def pending(self, _slot_key):
        return [c for c in self.cards if c.get("status") == "pending"]

    def last_finished_at(self, _slot_key, statuses):
        done = [c.get("finished_at", 0.0) for c in self.cards if c.get("status") in statuses]
        return float(max(done, default=0.0))


class _WaitSlot:
    key = "member-dm:atlas"

    def __init__(self, rows, questions=None):
        self.rows = rows
        self._question_pending = questions or {}

    def sidebar_messages(self):
        return self.rows


class _WaitState:
    def __init__(self, slot):
        self.slot = slot
        self._pending_questions = {}

    def get_slot(self, key):
        return self.slot if key == self.slot.key else None


async def _waiting_of(monkeypatch, rows, *, cards=(), questions=None):
    from kiro_crew.dashboard import change_cards

    monkeypatch.setattr(change_cards, "card_store_for", lambda _s: _Cards(list(cards)))
    slot = _WaitSlot(rows, questions)
    state = _WaitState(slot)
    loaded = await warm_task_brief._warm_cards(state, slot.key)
    return warm_task_brief._waiting(state, slot.key, cards=loaded)


_SAID = {"role": "assistant", "content": _OPTIONS_ASK, "ts": ASKED}


@pytest.mark.asyncio
async def test_waiting_an_options_line_is_an_item(monkeypatch):
    (item,) = await _waiting_of(monkeypatch, [_SAID])
    assert item["kind"] == "options" and item["options"] == ["Open the PR", "Not yet"]


@pytest.mark.asyncio
async def test_waiting_a_question_card_is_an_item(monkeypatch):
    questions = {
        "q1": {"questions": [{"question": "Which tracker?", "options": ["SIM"]}], "ts": ASKED_S}
    }
    (item,) = await _waiting_of(monkeypatch, [], questions=questions)
    assert item["kind"] == "question" and item["options"] == ["SIM"]


@pytest.mark.asyncio
async def test_waiting_a_pending_change_card_is_an_item(monkeypatch):
    card = {"status": "pending", "title": "Set the milestones", "created_at": ASKED_S}
    (item,) = await _waiting_of(monkeypatch, [], cards=[card])
    assert item["kind"] == "card" and item["text"] == "Set the milestones"


@pytest.mark.asyncio
async def test_a_card_declined_during_the_threaded_read_is_not_listed(monkeypatch):
    # The owner declines the card while the snapshot is read off the loop.
    # The waiting list is read after that hop, so the declined card is gone.
    from kiro_crew.dashboard import change_cards

    card = {"status": "pending", "title": "Set the milestones", "created_at": ASKED_S}
    store = _Cards([card])
    monkeypatch.setattr(change_cards, "card_store_for", lambda _s: store)

    def read_then_decline(*_a, **kwargs):
        card["status"] = "cancelled"
        # Keep any list read before the hop, as the thread read would.
        return replace(_snapshot(), waiting=kwargs.get("waiting", ()))

    monkeypatch.setattr(warm_task_brief, "_snapshot_sync", read_then_decline)
    slot = _WaitSlot([])
    state = _WaitState(slot)
    state._slots = {}
    snap = await warm_task_brief._snapshot(state, slot)
    assert snap is not None and snap.waiting == ()


@pytest.mark.asyncio
async def test_waiting_a_pressed_change_card_retires_the_ask(monkeypatch):
    pressed = {"status": "cancelled", "finished_at": ASKED_S + 60}
    assert await _waiting_of(monkeypatch, [_SAID], cards=[pressed]) == ()


@pytest.mark.asyncio
async def test_waiting_only_a_press_after_the_ask_retires_it(monkeypatch):
    earlier = {"status": "applied", "finished_at": ASKED_S - 60}
    assert len(await _waiting_of(monkeypatch, [_SAID], cards=[earlier])) == 1
    later = {"status": "applied", "finished_at": ASKED_S + 60}
    assert await _waiting_of(monkeypatch, [_SAID], cards=[later]) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "waiting",
    [(), ({"kind": "card", "text": "Set the milestones", "options": [], "asked_at": ASKED},)],
)
async def test_the_quiet_line_shows_only_when_nothing_waits(monkeypatch, waiting):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        turns=(_turn(1, "user", "Fix it", 9000),),
        work_items=(),
        waiting=waiting,
        ledger_digest="d1",
        previous_digest="d1",
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    content = slot.rows[-1]["content"]
    assert content.startswith("Nothing new since") is (not waiting)
    if waiting:
        assert "- Set the milestones (change card)" in content


# ---------------------------------------------- the ledger digest (quiet line)


def _records():
    from kiro_crew.work_ledger_records import ConductorRecord, WorkItem

    conductor = ConductorRecord(goal="Ship it", round=1, goal_version=1)
    item = WorkItem(
        item_id="it_1",
        title="Fix",
        state="open",
        status="progress",
        summary="working",
        created_at="2026-10-31T10:00:00+00:00",
        last_report_at="2026-10-31T10:05:00+00:00",
    )
    return conductor, item


def _digest(conductor, items, session=None):
    return inputs.ledger_digest(conductor, items, session or {})


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda c, i: (c, [i, replace(i, item_id="it_2")]), id="create"),
        pytest.param(lambda c, i: (c, [replace(i, summary="rebased")]), id="report"),
        pytest.param(lambda c, i: (c, [replace(i, decision="retry on main")]), id="decide"),
        pytest.param(lambda c, i: (c, [replace(i, verdict="fail")]), id="verdict"),
        pytest.param(lambda c, i: (c, [replace(i, state="accepted")]), id="close"),
        pytest.param(lambda c, i: (replace(c, goal="Ship it twice"), [i]), id="goal"),
        pytest.param(lambda c, i: (replace(c, round=2), [i]), id="round"),
        pytest.param(lambda c, i: (replace(c, concurrency_cap=3), [i]), id="any_stored_field"),
    ],
)
def test_every_ledger_write_changes_the_digest(write):
    conductor, item = _records()
    assert _digest(conductor, [item]) != _digest(*write(conductor, item))


def test_an_unchanged_ledger_and_derived_flags_keep_the_digest():
    conductor, item = _records()
    same = _digest(conductor, [item])
    assert _digest(*_records()[:1], [_records()[1]]) == same
    flagged = {**item.to_dict(), "stale": True, "orphaned": True, "pending_overdue": True}
    assert _digest(conductor, [flagged]) == _digest(conductor, [item.to_dict()])


def test_a_session_ledger_event_changes_the_digest():
    conductor, item = _records()
    one = {"phase": "implementing", "events": [{"ts": "t1", "text": "a"}]}
    two = {**one, "events": one["events"] + [{"ts": "t2", "text": "b"}]}
    assert _digest(conductor, [item], one) != _digest(conductor, [item], two)


async def _quiet_run(monkeypatch, *, ledger_digest, previous_digest):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        turns=(_turn(1, "user", "Fix it", 9000),),
        work_items=(),
        ledger_digest=ledger_digest,
        previous_digest=previous_digest,
    )
    calls: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        calls.append(prompt)
        return '{"headline":"Here is where we are."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    return slot, calls


@pytest.mark.asyncio
async def test_an_unchanged_ledger_keeps_the_line_quiet_however_long_away(monkeypatch):
    # The owner was away for hours (last_user_at is hours back); nothing was written.
    slot, calls = await _quiet_run(monkeypatch, ledger_digest="same", previous_digest="same")
    assert calls == [] and slot.rows[-1]["content"].startswith("Nothing new since")
    # The claim stores the digest the next return compares against.
    assert slot.rows[0]["meta"]["ledger_digest"] == "same"


@pytest.mark.asyncio
async def test_a_changed_ledger_runs_the_model(monkeypatch):
    _, calls = await _quiet_run(monkeypatch, ledger_digest="new", previous_digest="old")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_earlier_brief_means_the_model_runs(monkeypatch):
    _, calls = await _quiet_run(monkeypatch, ledger_digest="d1", previous_digest="")
    assert len(calls) == 1


def _claim(digest):
    return {
        "role": "notice",
        "content": "",
        "meta": {"kind": "warm_task_brief_claim", "ledger_digest": digest},
    }


def _row(kind):
    return {"role": "assistant", "content": "x", "meta": {"kind": kind}}


def test_the_digest_is_read_from_the_newest_answered_claim():
    rows = [
        _claim("old"),
        _row("warm_task_brief_result"),
        _claim("new"),
        _row("warm_task_brief_result"),
    ]
    assert warm_task_brief._claim_digest(rows) == "new"
    assert warm_task_brief._claim_digest([]) == ""


@pytest.mark.parametrize("tail", [[_row("warm_task_brief_failure")], []], ids=["failed", "dropped"])
def test_a_failed_or_dropped_brief_leaves_no_digest_to_match(tail):
    rows = [_claim("old"), _row("warm_task_brief_result"), _claim("new"), *tail]
    assert warm_task_brief._claim_digest(rows) == ""


# ------------------------------------- state that changes during the model call


_CARD = {"kind": "card", "text": "Set the milestones", "options": [], "asked_at": ASKED}


async def _write_run(monkeypatch, settled_states):
    """Run one brief whose settled re-reads return *settled_states* in order."""
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(waiting=(_CARD,), ledger_digest="d0")
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Set the milestones."}'

    reads = [
        base,
        _claimed(base),
        *[replace(_claimed(base), **change) for change in settled_states],
    ]
    _arm_model(monkeypatch, slot, reads, run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    posted = [row for row in slot.rows if row["role"] == "assistant"]
    return prompts, posted


@pytest.mark.asyncio
async def test_an_unchanged_state_posts_after_one_call(monkeypatch):
    prompts, posted = await _write_run(monkeypatch, [{}])
    assert len(prompts) == 1 and len(posted) == 1


# ---------------------------------------------------------------- RC4, RC5: live reads and buttons


@pytest.fixture(autouse=True)
def _no_github(_floor_monkeypatch):
    """No unit test reaches GitHub; each read is recorded instead."""
    calls: list[list[str]] = []

    def probe(urls):
        calls.append(list(urls))
        return {}

    _floor_monkeypatch.setattr(inputs, "probe_github", probe)
    _floor_monkeypatch.setattr(inputs, "_live_cache", {})
    return calls


@pytest.mark.asyncio
async def test_rc4_live_read_is_bounded_cached_and_stamped(monkeypatch, _no_github):
    def probe(urls):
        _no_github.append(list(urls))
        return {
            url: {"state": "merged", "review": "APPROVED", "mergeable": "", "checks": None}
            for url in urls
            if url.endswith("/26100")
        }

    monkeypatch.setattr(inputs, "probe_github", probe)
    ledger = [inputs.PrRef("kirodotdev/KiroCrew", n) for n in (26100, 26101, 1, 2, 3, 4)]
    refs = inputs.pr_refs(["https://github.com/o/r/pull/7"], ledger)
    assert len(refs) == inputs.MAX_LIVE_PRS
    now = time.time()
    first = await inputs.read_live_prs(refs, now)
    assert first[0]["checked"] is True and first[0]["state"] == "merged"
    assert first[0]["source"] == "GitHub" and first[0]["read_at"] == inputs.clock(now)
    assert first[1] == {
        "pr": "#26101",
        "repo": "kirodotdev/KiroCrew",
        "url": "https://github.com/kirodotdev/KiroCrew/pull/26101",
        "checked": False,
        "not_checked_since": "never",
    }
    # Within 15 minutes the good read is reused; only failed ones are retried.
    await inputs.read_live_prs(refs[:1], now + 60)
    assert len(_no_github) == 1
    await inputs.read_live_prs(refs[:1], now + inputs.LIVE_PR_TTL_SECS)
    assert len(_no_github) == 2


@pytest.mark.asyncio
async def test_rc4_live_read_past_its_limit_marks_unread_prs_not_checked(monkeypatch, _no_github):
    async def never_done(fn, urls):
        # Stands in for the worker thread: the read starts at once and never
        # answers, so the limit always ends it. No thread, no pool.
        _no_github.append(list(urls))
        await asyncio.Event().wait()

    seam = SimpleNamespace(wait_for=asyncio.wait_for, to_thread=never_done)
    monkeypatch.setattr(inputs, "asyncio", seam)
    now = time.time()
    cached, unread = (inputs.PrRef("kirodotdev/KiroCrew", n) for n in (26100, 26101))
    inputs._live_cache[cached.url] = (now, {"state": "open", "review": "APPROVED"})
    entries = await inputs.read_live_prs([cached, unread], now, budget=0.01)
    # What was already read still goes out; the unread PR is never guessed.
    assert entries[0]["checked"] is True and entries[0]["state"] == "open"
    assert entries[1] == {
        "pr": "#26101",
        "repo": "kirodotdev/KiroCrew",
        "url": unread.url,
        "checked": False,
        "not_checked_since": "never",
    }
    assert _no_github == [[unread.url]]
    # A spent limit starts no read at all.
    again = await inputs.read_live_prs([unread], now, budget=0)
    assert again[0]["checked"] is False and len(_no_github) == 1


@pytest.mark.asyncio
async def test_rc4_only_ledger_prs_and_full_urls_are_read(monkeypatch, _no_github):
    state = _State()
    slot = _foreground_slot(state)
    items = (
        _item(1, state="open", status="progress", summary="#111 in review"),
        _item(2, state="open", status="question", summary="Merge #222?", pr=222),
    )
    base = _snap(
        work_items=items,
        waiting=(
            {
                "kind": "options",
                "text": "See #444 and https://github.com/kirodotdev/KiroCrew/pull/333",
                "options": ["Go"],
                "asked_at": "",
            },
        ),
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    read = [url.rsplit("/", 1)[1] for url in _no_github[0]]
    # The ask's ledger PR, then the other open item's ledger PR (M3: read before
    # the welcome shows its state); a URL in text and a bare #111 or #444 never are.
    assert read == ["222", "1"]


def test_rc4_the_providers_own_response_type_is_read_not_dropped():
    from kiro_crew.monitoring.github_pull_request import (
        GitHubPullRequestResponse,
        parse_github_pull_request_target,
    )
    from kiro_crew.monitoring.pull_request import PullRequestCheck

    response = GitHubPullRequestResponse(
        target=parse_github_pull_request_target("https://github.com/o/r/pull/7"),
        state="open",
        draft=False,
        head_revision="abc",
        mergeability="CLEAN",
        review_decision="APPROVED",
        checks=(PullRequestCheck("ci", "passed"), PullRequestCheck("lint", "failed")),
        checks_complete=True,
        unresolved_review_threads=0,
        review_threads_complete=True,
    )
    assert inputs.facts_of(response) == {
        "state": "open",
        "review": "APPROVED",
        "mergeable": "CLEAN",
        "checks": {"failed": 1, "pending": 0, "passed": 1, "unknown": 0},
    }
    assert inputs.facts_of(None) is None
    # A check read that did not finish is not an empty, green board.
    partial = replace(response, checks=(), checks_complete=False)
    assert inputs.facts_of(partial)["checks"] == inputs.CHECKS_NOT_READ


@pytest.mark.asyncio
async def test_rc4_a_bare_number_in_the_choices_is_never_read(monkeypatch, _no_github):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        work_items=(_item(1, state="open", status="progress", summary="working"),),
        waiting=(
            {
                "kind": "options",
                "text": "Proceed?",
                "options": ["Merge #26100", "Hold #26100"],
                "asked_at": "",
            },
        ),
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    # The repo would have to be guessed, so the bare number is never read.
    assert _no_github == [["https://github.com/kirodotdev/KiroCrew/pull/1"]]


@pytest.mark.asyncio
async def test_rc4_a_pr_the_ledger_holds_as_a_number_counts_as_named(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    item = _item(1, state="accepted", status="done", summary="merged", pr=222)
    item["closed_at"] = _iso(time.time() - 60)
    base = _snap(work_items=(item,))

    async def run_model(*args, **kwargs):
        return '{"headline":"#222 merged while you were away."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["meta"]["kind"] == "warm_task_brief_result"


def test_rc4_the_item_payload_keeps_the_repo_its_own_pr_is_read_in():
    item = SimpleNamespace(
        item_id="it_1",
        title="t",
        state="open",
        verdict="",
        decision="",
        worker_session_key="",
        status="question",
        reason="",
        summary="Merge #222?",
        artifacts={},
        last_report_at="",
        created_at="",
        closed_at="",
        decided_at="",
        acceptance={"kind": "pr_checks", "pr": 222, "repo": "kirodotdev/KiroCrew"},
        pr=None,
    )
    payload = warm_task_brief._work_item_payload(item)
    assert inputs.ledger_ref(payload) == inputs.PrRef("kirodotdev/KiroCrew", 222)
    snapshot = _snap(work_items=(payload,), waiting=())
    assert [ref.number for ref in warm_task_brief._live_refs(snapshot)] == [222]


def test_pr_urls_in_ask_text_never_crowd_out_an_items_own_pr():
    urls = " ".join(f"https://github.com/o/r/pull/{n}" for n in range(1, 7))
    ask = {"item_id": "it_1", "state": "open", "status": "question", "summary": urls}
    running = {"item_id": "it_2", "state": "open", "status": "progress",
               "acceptance": {"kind": "pr_checks", "repo": "o/r", "pr": 99}}  # fmt: skip
    waiting = ({"kind": "card", "text": urls, "options": []},)
    snapshot = _snap(work_items=(ask, running), waiting=waiting)
    # Only a ledger PR is read: the welcome shows PR state on its item's line only.
    assert warm_task_brief._live_refs(snapshot) == [inputs.PrRef("o/r", 99)]


@pytest.mark.asyncio
async def test_rc4_a_hex_colour_in_the_widget_is_not_an_invented_pr(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snapshot()

    async def run_model(*args, **kwargs):
        return json.dumps({"headline": "On track.", "widget": '<div style="color:#333">ok</div>'})

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["meta"]["kind"] == "warm_task_brief_result"


@pytest.mark.asyncio
async def test_rc4_an_unfinished_check_read_reaches_the_model_as_not_read(monkeypatch):
    from kiro_crew.monitoring.github_pull_request import (
        GitHubPullRequestResponse,
        parse_github_pull_request_target,
    )

    url = "https://github.com/kirodotdev/KiroCrew/pull/26100"
    partial = GitHubPullRequestResponse(
        target=parse_github_pull_request_target(url),
        state="open",
        draft=False,
        head_revision="abc",
        mergeability="CLEAN",
        review_decision="APPROVED",
        checks=(),
        checks_complete=False,
        unresolved_review_threads=0,
        review_threads_complete=True,
    )
    monkeypatch.setattr(
        inputs, "probe_github", lambda urls: {u: inputs.facts_of(partial) for u in urls}
    )
    entry = (await inputs.read_live_prs([inputs.PrRef("kirodotdev/KiroCrew", 26100)]))[0]
    assert entry["checked"] is True and entry["checks"] == inputs.CHECKS_NOT_READ


@pytest.mark.asyncio
async def test_rc4_a_pr_the_input_names_with_its_repo_does_not_fail_the_result(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    item = _item(1, state="open", status="question", summary="Merge kirodotdev/KiroCrew#26100?")
    base = _snap(work_items=(item,))

    async def run_model(*args, **kwargs):
        return '{"headline":"#26100 waits on you."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["meta"]["kind"] == "warm_task_brief_result"


@pytest.mark.asyncio
async def test_rc4_a_pr_after_a_line_break_in_the_transcript_counts_as_named(monkeypatch):
    # The prompt is JSON: "\n#222" there reads "n#222", which no boundary allows.
    state = _State()
    slot = _foreground_slot(state)
    turns = (
        _turn(1, "user", "Ship it", 7200),
        _turn(2, "assistant", "Update:\n#222 is still open.", 60),
    )
    base = _snap(turns=turns, last_user_at=time.time() - 7200)

    async def run_model(*args, **kwargs):
        return '{"headline":"#222: not checked since 10:00."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["meta"]["kind"] == "warm_task_brief_result"
