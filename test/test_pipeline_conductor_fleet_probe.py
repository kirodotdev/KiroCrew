"""Fleet probe -- the one call that answers a conductor's whole patrol cycle.

The probe is the only thing standing between a supervisor and a fleet it cannot
see: a quiet cycle is supposed to cost one script call, and every signal it
suppresses is a signal nobody reads. That makes its quiet answers as
load-bearing as its loud ones, and a quiet answer is exactly what a shallow
test cannot tell from a broken one.

So the suite is organised around the probe's own failure directions rather than
around its function list:

* a report must be recognised in its protocol form and nowhere else, decoration
  included, because a missed report is an escalation that never fires;
* a sticky report must outlive the heartbeats that follow it, because a sampling
  reader is otherwise structurally unable to see a state the protocol
  guarantees will be overwritten;
* tool rows must never classify, in either direction -- no tag from a quoted
  protocol word, no ERR from a quoted error phrase;
* an index must count only what the session produced, so a supervisor's own
  nudge cannot read as the nudged worker making progress;
* ownership must fail toward ``unknown``, never toward ``fleet``, since
  ``fleet`` is the class that stops a session;
* a derived path must stay inside the store it is derived from, and a config
  cannot widen it.

This file holds the transcript side: protocol tags, the own-row index, run scope, tail
entries, classification, the sticky report, the delivery counters and the handled set.
The rest of the suite sits beside it, one file per area:

* ``test_pipeline_conductor_fleet_probe_ownership.py`` -- paths, process ownership and age;
* ``test_pipeline_conductor_fleet_probe_host_lines.py`` -- host posture and the BANNED line;
* ``test_pipeline_conductor_fleet_probe_cycle.py`` -- one probe cycle, dispositions, config
  and ``main``;
* ``test_pipeline_conductor_fleet_probe_worker_count.py``,
  ``test_pipeline_conductor_fleet_probe_program_position.py`` and
  ``test_pipeline_conductor_fleet_probe_argv_row.py`` -- the argv runner shape.

Shared fixtures and builders are in ``pipeline_conductor_fleet_probe_helpers.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import sessions_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import KEY, err_res, row, transcript

# --------------------------------------------------------------------------
# A report is a protocol form, not a word that appears first
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("GREEN: PR is green", "GREEN"),
        ("**BLOCKED:** waiting on a ruling", "BLOCKED"),
        ("> WORKING: still building", "WORKING"),
        ("- PR: opened", "PR"),
        ("1. GREEN: done", "GREEN"),
        ("### STANDDOWN: covered elsewhere", "STANDDOWN"),
        ("`PROPOSAL:` split it", "PROPOSAL"),
        ("GREEN : spaced colon", "GREEN"),
    ],
)
def test_proto_tag_reads_a_report_through_decoration(mod, text, tag):
    assert mod._proto_tag(text) == tag


@pytest.mark.parametrize(
    "text",
    [
        "the worker said **BLOCKED:** yesterday",
        "GREENISH: not a tag",
        "no tag at all",
        "",
    ],
)
def test_proto_tag_refuses_prose_that_merely_mentions_a_report(mod, text):
    assert mod._proto_tag(text) is None


# --------------------------------------------------------------------------
# The index counts what the session produced, and nothing sent to it
# --------------------------------------------------------------------------


def test_own_rows_count_excludes_inbound_rows(mod):
    raw = "".join(
        f"{line}\n"
        for line in (
            row("assistant", "WORKING: one"),
            json.dumps({"role": "tool_call", "name": "shell"}),
            row("user", "a nudge from the conductor"),
            row("nudge", "another inbound row"),
            json.dumps({"role": "tool_result", "name": "shell"}),
            row("assistant", "WORKING: two"),
        )
    ).encode("utf-8")
    assert mod._count_own_rows(raw) == 4


def test_own_rows_count_includes_the_first_line(mod):
    assert mod._count_own_rows(row("assistant", "GREEN: x").encode("utf-8")) == 1


def test_own_rows_needle_matches_the_real_writer_spelling(mod):
    """The needle is a byte pattern, so it pins the writer's separators."""
    raw = json.dumps({"role": "assistant", "content": "GREEN: x"}).encode("utf-8")
    assert mod._count_own_rows(raw) == 1


# --------------------------------------------------------------------------
# Run scope: severity without echoing an argument
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "scope"),
    [
        (["python3", "-c", "print(1)"], "unknown"),
        (["pytest", "-k", "TestThing"], "paths"),
        (["pytest", "-m=slow"], "paths"),
        (["/usr/bin/pytest", "test/test_a.py"], "paths"),
        (["pytest", "test/test_a.py::TestB::test_c"], "paths"),
        (["pytest", "-n", "0"], "suite"),
        (["pytest", "--ignore", "src/x", "-q"], "suite"),
        (["pytest", "test"], "suite"),
        (["python", "-m", "pytest", "--pyargs", "kiro_crew.mod"], "suite"),
        (["vitest", "run"], "suite"),
        (["vitest", "run", "src/a.test.ts"], "paths"),
        (["pytest.exe", "C:\\repo\\test_a.py"], "paths"),
        (["pytest.exe"], "suite"),
    ],
)
def test_run_scope_ranks_without_quoting(mod, argv, scope):
    assert mod._run_scope(argv) == scope


# --------------------------------------------------------------------------
# Reading one transcript
# --------------------------------------------------------------------------


def test_text_of_reads_both_content_shapes(mod):
    assert mod._text_of({"content": "plain"}) == "plain"
    assert mod._text_of({"content": [{"text": "a"}, {"text": "b"}, "skip"]}) == "a b"
    assert mod._text_of({"text": "fallback"}) == "fallback"


def test_tail_entries_on_an_unreadable_path(mod, tmp_path):
    assert mod._tail_entries(tmp_path / "absent.jsonl", 1000) == ([], None)


def test_tail_entries_skips_malformed_lines_and_non_objects(mod, sessions):
    path = transcript(
        sessions,
        KEY,
        "{not json",
        "[1, 2]",
        row("assistant", "GREEN: x"),
    )
    entries, index = mod._tail_entries(path, 200_000)
    assert [entry["role"] for entry in entries] == ["assistant"]
    assert index == 0


def test_tail_entries_index_counts_the_whole_file_not_the_window(mod, sessions):
    rows = [row("assistant", f"WORKING: step {n} " + "x" * 200) for n in range(40)]
    path = transcript(sessions, KEY, *rows)
    entries, index = mod._tail_entries(path, 500)
    assert len(entries) < 40, "the window must be smaller than the file"
    assert index == 39, "the index is a file position, so it cannot saturate"


def test_tail_entries_reports_no_index_without_session_rows(mod, sessions):
    path = transcript(sessions, KEY, row("user", "only inbound"))
    entries, index = mod._tail_entries(path, 200_000)
    assert entries and index is None


def test_classify_ignores_tool_rows_in_both_directions(mod):
    entries = [
        json.loads(row("assistant", "plain prose")),
        json.loads(json.dumps({"role": "tool_result", "content": "GREEN: quoted in a card"})),
        json.loads(json.dumps({"role": "tool_result", "content": "Bedrock is throttling"})),
    ]
    assert mod._classify(entries, err_res(mod)) == ("-", "plain prose")


def test_classify_raises_err_from_an_error_row(mod):
    entries = [
        json.loads(row("assistant", "WORKING: fine")),
        json.loads(row("error", "dispatch failure")),
    ]
    tag, tail = mod._classify(entries, err_res(mod))
    assert tag == "ERR"
    assert tail == "dispatch failure"


def test_classify_raises_err_from_a_matching_pattern_on_the_last_row(mod):
    entries = [json.loads(row("assistant", "Bedrock is throttling this turn"))]
    assert mod._classify(entries, err_res(mod))[0] == "ERR"


def test_classify_keeps_a_sticky_report_under_later_heartbeats(mod):
    entries = [
        json.loads(row("assistant", "BLOCKED: a ruling is owed")),
        json.loads(row("assistant", "WORKING: still waiting")),
        json.loads(row("assistant", "WORKING: still waiting")),
    ]
    assert mod._classify(entries, err_res(mod)) == ("BLOCKED", "BLOCKED: a ruling is owed")


def test_classify_lets_a_payload_report_supersede_a_sticky_one(mod):
    entries = [
        json.loads(row("assistant", "BLOCKED: a ruling is owed")),
        json.loads(row("assistant", "PR: opened 42")),
    ]
    assert mod._classify(entries, err_res(mod)) == ("PR", "PR: opened 42")


def test_classify_returns_no_tag_and_the_last_assistant_text(mod):
    entries = [
        json.loads(row("assistant", "first")),
        json.loads(row("assistant", "   ")),
        json.loads(row("user", "inbound")),
    ]
    assert mod._classify(entries, err_res(mod)) == ("-", "first")


def test_classify_on_an_empty_window(mod):
    assert mod._classify([], err_res(mod)) == ("-", "")


# --------------------------------------------------------------------------
# The sticky report behind a suppressed error
# --------------------------------------------------------------------------


def test_sticky_pending_reaches_past_an_error_row(mod):
    entries = [
        json.loads(row("assistant", "BLOCKED: a ruling is owed")),
        json.loads(row("assistant", "WORKING: heartbeat")),
        json.loads(row("error", "dispatch failure")),
    ]
    assert mod._sticky_pending(entries) == ("BLOCKED", "BLOCKED: a ruling is owed")


def test_sticky_pending_refuses_a_superseded_state(mod):
    entries = [
        json.loads(row("assistant", "BLOCKED: a ruling is owed")),
        json.loads(row("assistant", "GREEN: moved on")),
    ]
    assert mod._sticky_pending(entries) is None


def test_sticky_pending_with_no_report_at_all(mod):
    assert mod._sticky_pending([json.loads(row("assistant", "WORKING: only heartbeats"))]) is None


def test_sticky_pending_walks_past_blank_rows(mod):
    entries = [
        json.loads(row("assistant", "BLOCKED: a ruling is owed")),
        json.loads(row("assistant", "   ")),
    ]
    assert mod._sticky_pending(entries) == ("BLOCKED", "BLOCKED: a ruling is owed")


# --------------------------------------------------------------------------
# Delivery counters
# --------------------------------------------------------------------------


def watchdog_res(mod):
    import re

    return [re.compile(pattern) for pattern in mod.DEFAULT_WATCHDOG_RES]


def test_tail_matches_counts_a_notice_no_report_has_answered(mod):
    entries = [
        json.loads(row("assistant", "WORKING: earlier")),
        json.loads(row("inject", "[Tool stall detected -- automatic recovery]")),
        json.loads(row("assistant", "prose, not a report")),
    ]
    assert mod._tail_matches(entries, watchdog_res(mod)) is True


def test_tail_matches_stops_at_a_report_that_got_through(mod):
    entries = [
        json.loads(row("inject", "[Tool stall detected -- automatic recovery]")),
        json.loads(row("assistant", "WORKING: a turn landed since")),
    ]
    assert mod._tail_matches(entries, watchdog_res(mod)) is False


def test_tail_matches_skips_tool_rows_and_empty_text(mod):
    entries = [
        json.loads(json.dumps({"role": "tool_result", "content": "error: tool stall"})),
        json.loads(row("assistant", "")),
    ]
    assert mod._tail_matches(entries, watchdog_res(mod)) is False


def test_tail_matches_without_a_match(mod):
    assert mod._tail_matches([json.loads(row("assistant", "calm"))], watchdog_res(mod)) is False


# --------------------------------------------------------------------------
# What the handled set remembers
# --------------------------------------------------------------------------


def test_recorded_proto_prefers_the_settled_record(mod):
    handled = {KEY: {"tag": "IDLE", "digest": "d", "settled": {"tag": "GREEN", "digest": "g"}}}
    assert mod._recorded_proto(handled, KEY) == "GREEN"


def test_recorded_proto_recovers_a_payload_from_a_legacy_entry(mod):
    assert mod._recorded_proto({KEY: {"tag": "STANDDOWN", "digest": "d"}}, KEY) == "STANDDOWN"


@pytest.mark.parametrize(
    "handled",
    [
        {},
        {KEY: "not a dict"},
        {KEY: {"tag": "WORKING", "digest": "d"}},
    ],
)
def test_recorded_proto_without_a_dispositioned_payload(mod, handled):
    assert mod._recorded_proto(handled, KEY) is None


def test_stalled_since_disposition_needs_an_index_and_an_aged_mark(mod):
    handled = {KEY: {"index": 7, "ts": time.time() - 3600}}
    assert mod._stalled_since_disposition(handled, KEY, 7, 900) is True


@pytest.mark.parametrize(
    ("handled", "index"),
    [
        ({KEY: {"index": 7, "ts": 0}}, None),
        ({}, 7),
        ({KEY: "not a dict"}, 7),
        ({KEY: {"index": 6, "ts": 0}}, 7),
        ({KEY: {"index": True, "ts": 0}}, 1),
        ({KEY: {"ts": 0}}, 7),
        ({KEY: {"index": 7}}, 7),
    ],
)
def test_stalled_since_disposition_stays_quiet_without_a_comparison(mod, handled, index):
    assert mod._stalled_since_disposition(handled, KEY, index, 900) is False


def test_stalled_since_disposition_stays_quiet_for_a_fresh_mark(mod):
    """A mark made now is not an aged mark, however long the shard took to get here.

    The mark and the window are read at the same moment, so the case states a
    fact about the function rather than about the gap between this module's
    import and this line: ``time.time() - marked`` is zero here, whatever the
    wall clock says.
    """
    handled = {KEY: {"index": 7, "ts": time.time()}}
    assert mod._stalled_since_disposition(handled, KEY, 7, 900) is False


def test_digest_is_short_and_stable(mod):
    first = mod._digest("GREEN: x")
    assert first == mod._digest("GREEN: x")
    assert len(first) == 12
    assert first != mod._digest("GREEN: y")


def test_load_state_tolerates_absence_and_corruption(mod, tmp_path):
    assert mod._load_state(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert mod._load_state(bad) == {}
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2]", encoding="utf-8")
    assert mod._load_state(listy) == {}
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"handled": {}}), encoding="utf-8")
    assert mod._load_state(good) == {"handled": {}}


def test_handled_of_tolerates_a_corrupted_map(mod):
    assert mod._handled_of({"handled": {KEY: {}}}) == {KEY: {}}
    assert mod._handled_of({"handled": "broken"}) == {}
    assert mod._handled_of({}) == {}


def test_suppressed_on_an_exact_match(mod):
    handled = {KEY: {"tag": "GREEN", "digest": "abc"}}
    assert mod._suppressed(handled, KEY, "GREEN", "abc", 900) is True


def test_suppressed_by_the_settled_record_after_the_entry_moved_on(mod):
    handled = {KEY: {"tag": "IDLE", "digest": "zzz", "settled": {"tag": "GREEN", "digest": "abc"}}}
    assert mod._suppressed(handled, KEY, "GREEN", "abc", 900) is True


@pytest.mark.parametrize(
    ("handled", "tag", "digest"),
    [
        ({}, "GREEN", "abc"),
        ({KEY: "not a dict"}, "GREEN", "abc"),
        ({KEY: {"tag": "GREEN", "digest": "abc"}}, "GREEN", "other"),
        ({KEY: {"tag": "GREEN", "digest": "abc"}}, "PR", "abc"),
    ],
)
def test_not_suppressed_when_the_payload_is_new(mod, handled, tag, digest):
    assert mod._suppressed(handled, KEY, tag, digest, 900) is False


def test_an_idle_mark_expires_after_another_idle_budget(mod):
    fresh = {KEY: {"tag": "IDLE", "digest": "abc", "ts": time.time()}}
    assert mod._suppressed(fresh, KEY, "IDLE", "abc", 900) is True
    stale = {KEY: {"tag": "IDLE", "digest": "abc", "ts": time.time() - 1800}}
    assert mod._suppressed(stale, KEY, "IDLE", "abc", 900) is False
    undated = {KEY: {"tag": "IDLE", "digest": "abc"}}
    assert mod._suppressed(undated, KEY, "IDLE", "abc", 900) is False


def test_atomic_write_leaves_no_temp_file_behind(mod, tmp_path):
    target = tmp_path / "state.json"
    mod._atomic_write(target, "payload")
    assert target.read_text(encoding="utf-8") == "payload"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_cleans_up_when_the_write_fails(mod, tmp_path, monkeypatch):
    target = tmp_path / "state.json"

    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(RuntimeError):
        mod._atomic_write(target, "payload")
    assert list(tmp_path.iterdir()) == []


def test_data_home_follows_the_environment(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "elsewhere"))
    assert mod.data_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("KIROCREW_HOME")
    assert mod.data_home() == Path.home() / ".kiro" / "crew"
