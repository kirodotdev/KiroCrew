"""The warm welcome redesign, one test group per root cause of the analysis.

RC1 the payload was sliced at 40,000 characters and lost the transcript; RC2
assistant turns were cut to 400+400 characters and the pending ask with them;
RC3 the reply language followed a guessed "original request"; RC6 a
conductor's own crewmate thread got the crewmate prompt; RC8 a separate opener
could be orphaned; RC4 facts had no source or read time and RC5 the asks had
no options, links or buttons. RC7 (the whole-board count) is pinned in
``test_warm_task_brief_shape.py``.
"""

from __future__ import annotations

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
from kiro_crew.history import HUMAN_TURN_META_KEY
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


def test_rc1_a_huge_board_never_cuts_the_payload_or_loses_the_transcript():
    away = time.time() - 3600
    items = tuple(_item(n) for n in range(170)) + (
        _item(500, state="open", status="question", title="Pick a layout"),
    )
    turns = (
        _turn(1, "user", "Ship wave 2", 7200),
        _turn(2, "assistant", "Done; the newest line is here.", 60),
    )
    snapshot = _snap(work_items=items, turns=turns, last_user_at=away)
    prompt = warm_task_brief._build_prompt(snapshot, anchor=away)
    payload = _payload_of(prompt)  # whole, valid JSON: nothing was sliced
    assert payload["transcript_since_owner"][-1]["text"] == "Done; the newest line is here."
    # Terminal items that did not change while the owner was away stay out.
    assert [entry["title"] for entry in payload["open_items"]] == ["Pick a layout"]
    order = list(payload)
    assert order.index("pending_ask") < order.index("transcript_since_owner")
    assert order.index("changes_since_away") < order.index("open_items")


def test_rc1_each_section_keeps_whole_entries_within_its_budget():
    entries = [{"text": "x" * 900} for _ in range(20)]
    kept = inputs.fit(entries, 5_000)
    assert 0 < len(kept) < 20 and all(entry == entries[0] for entry in kept)
    items = tuple(_item(n, state="open", status="progress") for n in range(100))
    open_items = inputs.open_item_entries(items, None, warm_task_brief.work_item_is_terminal)
    size = len(json.dumps(open_items, ensure_ascii=False, separators=(",", ":")))
    assert 0 < len(open_items) < 100 and size <= inputs.OPEN_ITEMS_CHARS + 2 * len(open_items)


def test_rc1_questions_and_changes_rank_first_and_changed_closes_are_kept():
    away = time.time() - 3600
    items = (
        _item(1, state="open", status="progress"),
        _item(2, state="accepted", status="done", closed_at=_iso(time.time() - 60)),
        _item(3, state="open", status="blocked", reason="needs_human"),
        _item(4, state="accepted"),
    )
    entries = inputs.open_item_entries(items, away, warm_task_brief.work_item_is_terminal)
    assert [entry["title"] for entry in entries] == ["Item 3", "Item 2", "Item 1"]
    assert entries[1]["changed_since_away"] is True
    changes = inputs.change_entries(items, away)
    assert changes == [
        {"item": "Item 2", "state": "accepted", "status": "done", "source": "work ledger"}
        | {"at": inputs.clock(inputs.parse_epoch(items[1]["closed_at"]))}
    ]


# ---------------------------------------------------------------- RC2


def test_rc2_the_pending_ask_is_read_whole_with_its_options():
    rows = [
        {"role": "user", "content": "Status?", "ts": _iso(time.time() - 7200)},
        {"role": "assistant", "content": _ASK, "ts": _iso(time.time() - 7000)},
        # The hidden claim row is a notice, not a reply.
        {"role": "notice", "content": "", "meta": {"kind": "warm_task_brief_claim"}},
    ]
    ask = inputs.pending_ask(rows)
    assert ask is not None
    assert ask.options == ("Merge #26100 now", "Hold #26100 until Monday")
    assert "#26100 is green; I recommend merging it." in ask.text
    assert "[OPTIONS" not in ask.text


@pytest.mark.parametrize(
    "answer",
    [
        {"role": "user", "content": "Hold it"},
        {"role": "assistant", "content": "Merged.", "meta": {}},
        {"role": "user", "content": "x", "meta": {HUMAN_TURN_META_KEY: True}},
        # Any newer reply retires the ask, a wake or an earlier brief included:
        # an older [OPTIONS:] turn is never brought back.
        {"role": "user", "content": "[work-ledger wake] item done"},
        {"role": "assistant", "content": "Catch-up", "meta": {"kind": "warm_task_brief_result"}},
    ],
)
def test_rc2_an_answered_ask_is_not_pending(answer):
    rows = [{"role": "assistant", "content": _ASK}, answer]
    assert inputs.pending_ask(rows) is None


def test_rc2_the_transcript_slice_starts_at_the_owners_last_message_whole():
    long_reply = "head " + "middle " * 300 + "tail [OPTIONS: A | B]"
    turns = (
        _turn(1, "user", "The 10-10 goal", 9000),
        _turn(2, "assistant", "Old reply", 8000),
        _turn(3, "user", "Now do wave 2", 7200),
        _turn(4, "user", "[work-ledger wake] x", 3000, injected=True),
        _turn(5, "assistant", long_reply, 60),
        _turn(6, "assistant", "\u200b", 30),
    )
    entries = inputs.turns_since_owner(turns)
    assert [entry["role"] for entry in entries] == ["user", "automation", "assistant"]
    assert entries[0]["text"] == "Now do wave 2"
    assert entries[-1]["text"] == long_reply


def test_rc2_over_budget_the_oldest_turns_drop_and_the_owner_message_stays():
    turns = (_turn(1, "user", "Go", 9000),) + tuple(
        _turn(n, "assistant", f"{n} " + "x" * 3000, 9000 - n) for n in range(2, 12)
    )
    entries = inputs.turns_since_owner(turns)
    assert entries[0]["text"] == "Go"
    assert entries[-1]["text"].startswith("11 ")
    assert len(entries) < len(turns)


# ---------------------------------------------------------------- RC3


@pytest.mark.asyncio
async def test_rc3_fixed_lines_follow_the_owners_recent_messages(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    turns = (
        _turn(1, "user", "Fix the failing test", 9000),
        _turn(2, "user", "现在进展如何？", 7200),
        _turn(3, "assistant", "还在跑。", 60),
    )
    base = _snap(turns=turns)

    async def fail(*args, **kwargs):
        raise RuntimeError("provider failed")

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], fail)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["content"] == warm_task_brief._FAILURE_TEXT["zh"]
    prompt = warm_task_brief._build_prompt(base)
    assert _payload_of(prompt)["owner_recent_messages"][-1] == "现在进展如何？"
    assert 'language of "owner_recent_messages"' in prompt


# ---------------------------------------------------------------- RC6


@pytest.mark.asyncio
async def test_rc6_a_crewmate_thread_that_owns_a_ledger_gets_the_conductor_prompt(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    assert slot.mode == "member"
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Here is where we are."}'

    base = _snapshot()  # carries a conductor goal
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert prompts[0].startswith("You are the conductor in this chat.")

    plain = _snap(conductor=None, work_items=())
    prompts.clear()
    _arm_model(monkeypatch, slot, [plain, _claimed(plain), _claimed(plain)], run_model)
    slot.rows.clear()
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert prompts[0].startswith("You are the crewmate in this chat.")


# ---------------------------------------------------------------- quiet, RC8


@pytest.mark.asyncio
async def test_a_quiet_return_posts_one_line_and_spends_nothing(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(turns=(_turn(1, "user", "Fix it", 9000),), work_items=())

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


@pytest.mark.asyncio
async def test_a_plain_chat_is_never_briefed_even_with_pending_choices(monkeypatch):
    state, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    slot.mode = "chat"
    log.rows.append({"role": "assistant", "content": _ASK, "ts": _iso(time.time() - 60)})
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
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
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Rebased onto main."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert len(prompts) == 1
    changes = _payload_of(prompts[0])["changes_since_away"]
    assert changes[0]["event"] == "Rebased onto main"
    assert changes[0]["source"] == "session ledger"


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
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Pick a layout."}'

    _arm_model(monkeypatch, slot, [quiet, asked, asked], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert len(prompts) == 1
    assert _payload_of(prompts[0])["open_items"][0]["title"] == "Pick a layout"
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
    refs = inputs.pr_refs(
        ["#26100 then #26101 and https://github.com/o/r/pull/7, #1 #2 #3 #4"],
        "kirodotdev/KiroCrew",
    )
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


def test_rc4_a_bare_number_is_read_only_against_one_unambiguous_repo():
    one = [_item(1)]
    two = [_item(1), _item(2, acceptance={"repo": "other/repo"})]
    assert inputs.default_repo(one, []) == "kirodotdev/KiroCrew"
    assert inputs.default_repo(two, []) is None
    assert inputs.pr_refs(["#5"], None) == []


@pytest.mark.asyncio
async def test_rc4_only_prs_named_in_open_asks_are_read(monkeypatch, _no_github):
    state = _State()
    slot = _foreground_slot(state)
    items = (
        _item(1, state="open", status="progress", summary="#111 in review"),
        _item(2, state="open", status="question", summary="Merge #222?", pr=222),
    )
    base = _snap(work_items=items, pending=inputs.PendingAsk("See #333", ("Go",), None))
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    read = [url.rsplit("/", 1)[1] for url in _no_github[0]]
    assert read == ["333", "222"]


@pytest.mark.asyncio
async def test_rc4_a_pr_number_the_input_never_named_fails_the_result(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(pending=inputs.PendingAsk("Merge #26100?", ("Merge it", "Hold"), None))

    async def run_model(*args, **kwargs):
        return '{"headline":"Merge #18901 now.","body":"#26100 is fine."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    row = slot.rows[-1]
    assert row["meta"]["kind"] == "warm_task_brief_failure"
    assert "18901" not in row["content"]
    # The fallback still offers the real choices.
    assert row["content"].endswith("[OPTIONS: Merge it | Hold]")


@pytest.mark.asyncio
async def test_rc5_the_gateway_emits_the_pending_choices_as_buttons(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    items = (_item(9, state="open", status="question", title="Pick | a [layout]"),)
    base = _snap(
        work_items=items,
        pending=inputs.PendingAsk("Merge #26100?", ("Merge #26100 now", "Hold #26100"), None),
    )
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        # A model-written marker must never become a button.
        return json.dumps(
            {"headline": "#26100 waits on you.", "body": "Pick one. [OPTIONS: Delete main]"}
        )

    monkeypatch.setattr(
        inputs,
        "probe_github",
        lambda urls: {
            u: {"state": "open", "review": "", "mergeable": "", "checks": None} for u in urls
        },
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    content = slot.rows[-1]["content"]
    assert content.endswith(
        "[OPTIONS: Merge #26100 now | Hold #26100 | Tell me what Pick a layout needs from me]"
    )
    assert content.count("[OPTIONS:") == 1 and "Delete main" in content
    assert _payload_of(prompts[0])["pending_ask"]["options"] == ["Merge #26100 now", "Hold #26100"]
    links = slot.rows[-1]["meta"]["brief"]["links"]
    assert links == [
        {"label": "#26100", "url": "https://github.com/kirodotdev/KiroCrew/pull/26100"}
    ]
    assert "#26100 https://github.com/kirodotdev/KiroCrew/pull/26100" in content


def test_rc5_buttons_are_capped_and_cannot_break_the_marker():
    ask = inputs.PendingAsk("x", tuple(f"Choice {n} | ]" for n in range(9)), None)
    labels = inputs.buttons(ask, [], "en")
    assert len(labels) == inputs.MAX_BUTTONS
    assert all("|" not in label and "]" not in label for label in labels)


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
async def test_rc4_a_pr_named_only_in_the_pending_choices_is_read(monkeypatch, _no_github):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        work_items=(_item(1, state="open", status="progress", summary="working"),),
        pending=inputs.PendingAsk("Proceed?", ("Merge #26100", "Hold #26100"), None),
    )
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert _no_github == [["https://github.com/kirodotdev/KiroCrew/pull/26100"]]


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


@pytest.mark.asyncio
async def test_rc5_a_lone_choice_with_a_comma_stays_one_button(monkeypatch):
    from kiro_crew.dashboard.state import _parse_options

    state = _State()
    slot = _foreground_slot(state)
    base = _snap(work_items=(_item(3, state="open", status="question", title="Fix login, signup"),))
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    content = slot.rows[-1]["content"]
    assert _parse_options(content) == ["Tell me what Fix login, signup needs from me"]
    assert content.endswith("signup needs from me |]")


@pytest.mark.asyncio
async def test_rc5_links_cover_only_prs_that_were_read_and_named(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(
        work_items=(_item(9, state="open", status="progress"),),
        pending=inputs.PendingAsk("Merge #1 or #2? See #3.", ("Merge #1", "Merge #2"), None),
    )
    monkeypatch.setattr(
        inputs,
        "probe_github",
        lambda urls: {
            u: {"state": "open", "review": "", "mergeable": "", "checks": None}
            for u in urls
            if not u.endswith("/2")
        },
    )

    async def run_model(*args, **kwargs):
        return '{"headline":"Pick #1 or #2."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    links = slot.rows[-1]["meta"]["brief"]["links"]
    # #2 was not read; #3 was read but neither the brief nor a choice names it.
    assert [link["label"] for link in links] == ["#1"]


def test_rc4_the_item_payload_keeps_the_repo_a_bare_number_resolves_against():
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
    assert inputs.default_repo([payload], []) == "kirodotdev/KiroCrew"
    snapshot = _snap(work_items=(payload,), pending=None)
    assert [ref.number for ref in warm_task_brief._live_refs(snapshot)] == [222]


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
    assert inputs.public_live([entry])[0]["checks"] == inputs.CHECKS_NOT_READ
