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
"""

from __future__ import annotations

import ast
import collections
import json
import os
import re
import time
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from conftest import make_dir_link

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
    / "scripts"
    / "fleet_probe.py"
)

KEY = "dashboard_chat-601-1788099254"


# Three platform facts meet this file, and each is handled by the mechanism the
# repository already has for it.
#
# ``os.sysconf`` and ``os.getloadavg`` are absent on Windows, and the probe's own
# source treats both as optional -- the age is uncomputable without a clock-tick
# rate, the load average sits behind a ``hasattr`` guard -- so the cases needing
# them patch them in with ``raising=False`` and run everywhere, and the answer a
# host without a clock source gets is asserted on every platform.
#
# A name meaning another DIRECTORY is what most of the ``/proc`` fixtures below
# need: a junction supplies it on Windows with no privilege at all, so they go
# through ``conftest.make_dir_link`` and keep their coverage on every host.
#
# A real link to a FILE -- the ``exe`` entries and the transcript that escapes
# its store -- needs ``SeCreateSymbolicLinkPrivilege``, which CI runners hold and
# an ordinary Windows shell does not. Those cases are inventoried by exact node
# id in ``test/requires-real-symlinks.txt``, which the root conftest skips only
# when its capability probe fails, so the inventory stays the one place that
# records what a host without the privilege loses.


# The name the script is loaded under decides whether it is measured at all. CI
# measures the backend with the package selector ``--cov=kiro_crew``, and coverage
# treats that as a module-name boundary: a file loaded under a bare top-level name
# falls outside it, so every line these cases execute is recorded against nothing
# and the script reads as untested however thoroughly it is exercised. Measured on
# this checkout with that selector: a ``fleet_probe`` load leaves the file absent
# from the coverage data entirely, a dotted load under the package records it.
# The dotted spelling mirrors where the script physically sits; its directory is
# not importable (a hyphen in the skill name, no ``__init__.py``), which is why
# the module name has to be supplied rather than derived.
LOAD_NAME = "kiro_crew.builtin_skills.pipeline_conductor.scripts.fleet_probe"


@pytest.fixture
def mod():
    return load_skill_script(LOAD_NAME, SCRIPT)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """The gateway session store the probe derives from the data home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    store = tmp_path / "crew" / "sessions"
    store.mkdir(parents=True)
    return store


@pytest.fixture
def empty_proc(tmp_path, monkeypatch):
    """A host with nothing running, so host posture never colours a probe test."""
    root = tmp_path / "proc-empty"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(root))
    return root


def row(role: str, text: str) -> str:
    """One transcript row as this package's writers spell it."""
    return json.dumps({"role": role, "content": text})


def transcript(store: Path, key: str, *rows: str, age_secs: int = 0) -> Path:
    path = store / f"{key}.jsonl"
    path.write_text("".join(f"{line}\n" for line in rows), encoding="utf-8")
    if age_secs:
        stamp = time.time() - age_secs
        os.utime(path, (stamp, stamp))
    return path


def fired_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("\U0001f514")]


def ok_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("OK "))


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


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def err_res(mod):
    import re

    return [re.compile(pattern) for pattern in mod.DEFAULT_ERR_RES]


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


# --------------------------------------------------------------------------
# Comparing paths, and who owns a process
# --------------------------------------------------------------------------


def test_norm_path_strips_the_extended_length_prefix(mod):
    assert mod._norm_path("\\\\?\\D:\\work") == os.path.normcase(os.path.normpath("D:\\work"))


def test_under_uses_a_separator_boundary(mod):
    root = os.path.normpath("/oss/wt-a")
    assert mod._under(root, root) is True
    assert mod._under(os.path.join(root, "src"), root) is True
    assert mod._under(os.path.normpath("/oss/wt-a-old"), root) is False


def test_program_path_takes_the_first_token(mod):
    assert mod._program_path("/usr/bin/python3 -m pytest") == "/usr/bin/python3"
    assert mod._program_path("") == ""


@pytest.mark.parametrize(
    ("program", "base"),
    [
        ("/usr/bin/bash", "bash"),
        ("C:\\Python\\Py.EXE", "py"),
        ("bash", "bash"),
    ],
)
def test_basename_folds_case_and_separators(mod, program, base):
    assert mod._basename(program) == base


def test_venv_root_identifies_the_checkout_that_owns_an_interpreter(mod, tmp_path):
    venv = tmp_path / "wt" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    assert mod._venv_root(str(venv / "bin" / "python")) == str(venv)
    assert mod._venv_root("/usr/bin/python3") is None
    assert mod._venv_root("python3") is None


def proc_pid(root: Path, pid: str, argv: list[str], *, starttime: int | None = None) -> Path:
    entry = root / pid
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    if starttime is not None:
        (entry / "stat").write_text(f"{pid} (py test) {stat_fields(starttime)}\n", encoding="utf-8")
    return entry


def stat_fields(starttime: int) -> str:
    """Fields 3 onward of a ``/proc/<pid>/stat`` line, with field 22 set."""
    fields = ["S"] + [str(n) for n in range(4, 25)]
    fields[19] = str(starttime)
    return " ".join(fields)


def test_trusted_program_base_reads_the_kernel_link(mod, tmp_path):
    entry = tmp_path / "101"
    entry.mkdir()
    (entry / "exe").symlink_to("/usr/bin/bash")
    assert mod._trusted_program_base(entry) == "bash"


def test_trusted_program_base_tolerates_a_deleted_binary(mod, tmp_path):
    entry = tmp_path / "102"
    entry.mkdir()
    (entry / "exe").symlink_to("/usr/bin/bash (deleted)")
    assert mod._trusted_program_base(entry) == "bash"


def test_trusted_program_base_refuses_an_untrusted_directory(mod, tmp_path):
    entry = tmp_path / "103"
    entry.mkdir()
    (entry / "exe").symlink_to("/home/someone/bin/bash")
    assert mod._trusted_program_base(entry) is None


def test_trusted_program_base_refuses_an_unreadable_link(mod, tmp_path):
    entry = tmp_path / "104"
    entry.mkdir()
    assert mod._trusted_program_base(entry) is None


@pytest.mark.parametrize(
    ("argv", "exe_base"),
    [
        (["bash", "-c", "pytest -q"], "bash"),
        (["bash", "-lc", "pytest -q"], "bash"),
        (["sh", "-euxc", "pytest -q"], "sh"),
        (["bash", "-o", "pipefail", "-c", "pytest -q"], "bash"),
        (["bash", "--rcfile", "/dev/null", "-c", "pytest -q"], "bash"),
        (["bash", "--posix", "-c", "pytest -q"], "bash"),
        (["bash", "-e", "-c", "pytest -q"], "bash"),
        (["busybox", "sh", "-c", "pytest -q"], "busybox"),
    ],
)
def test_shell_command_wrapper_is_recognised(mod, argv, exe_base):
    assert mod._is_shell_command_wrapper(argv, exe_base) is True


@pytest.mark.parametrize(
    ("argv", "exe_base"),
    [
        ([], "bash"),
        (["bash", "-c", "pytest"], None),
        (["bash", "script.sh"], "bash"),
        (["python3", "-c", "import pytest"], "python3"),
        (["/usr/bin/wget", "-c", "http://example.invalid/x"], "busybox"),
    ],
)
def test_a_real_tool_is_not_treated_as_a_wrapper(mod, argv, exe_base):
    assert mod._is_shell_command_wrapper(argv, exe_base) is False


def test_program_class_never_guesses_fleet(mod, tmp_path):
    wt = tmp_path / "wt"
    (wt / "bin").mkdir(parents=True)
    assert mod._program_class("/usr/bin/python3 -m pytest", []) == "unknown"
    assert mod._program_class("", [str(wt)]) == "unknown"
    assert mod._program_class(f"{wt}/bin/python -m pytest", [str(wt)]) == "fleet"
    assert mod._program_class("/usr/bin/python3 -m pytest", [str(wt)]) == "unknown"


def test_program_class_attributes_a_venv_interpreter_elsewhere(mod, tmp_path):
    other = tmp_path / "other" / ".venv"
    (other / "bin").mkdir(parents=True)
    (other / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    fleet = tmp_path / "wt"
    fleet.mkdir()
    assert mod._program_class(f"{other}/bin/python -m pytest", [str(fleet)]) == "foreign"


def test_owner_class_reads_the_working_directory(mod, tmp_path):
    fleet = tmp_path / "wt"
    (fleet / "src").mkdir(parents=True)
    entry = tmp_path / "201"
    entry.mkdir()
    make_dir_link(entry / "cwd", fleet / "src")
    assert mod._owner_class(entry, [str(fleet)]) == "fleet"
    assert mod._owner_class(entry, [str(tmp_path / "somewhere-else")]) == "foreign"
    assert mod._owner_class(entry, []) == "unknown"


def test_owner_class_absorbs_a_symlinked_fleet_root(mod, tmp_path):
    real = tmp_path / "real-wt"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link-wt"
    make_dir_link(link, real)
    entry = tmp_path / "202"
    entry.mkdir()
    make_dir_link(entry / "cwd", real / "src")
    assert mod._owner_class(entry, [str(link)]) == "fleet"


def test_owner_class_falls_back_to_the_program_when_the_cwd_is_unreadable(mod, tmp_path):
    fleet = tmp_path / "wt"
    (fleet / "bin").mkdir(parents=True)
    entry = tmp_path / "203"
    entry.mkdir()
    assert mod._owner_class(entry, [str(fleet)], f"{fleet}/bin/python -m pytest") == "fleet"
    assert mod._owner_class(entry, [str(fleet)], "/usr/bin/python3 -m pytest") == "unknown"


def test_a_root_that_cannot_be_resolved_reads_as_unknown_not_as_fleet(mod, tmp_path, monkeypatch):
    """A root that cannot be compared must widen nothing.

    The refusal is forced through the resolver rather than through a path the
    platform happens to reject. An embedded NUL raises on POSIX and resolves on
    Windows, so asserting on one measures the standard library instead of this
    branch -- and the branch is the safety-relevant half, since ``fleet`` is the
    class that stops a session.
    """
    entry = tmp_path / "204"
    entry.mkdir()
    make_dir_link(entry / "cwd", tmp_path)
    elsewhere = str(tmp_path / "some-other-root")

    def refuse(path):
        raise OSError("cannot resolve")

    monkeypatch.setattr(mod.os.path, "realpath", refuse)
    assert mod._owner_class(entry, [elsewhere]) == "unknown"
    assert mod._program_class("/usr/bin/python3 -m pytest", [elsewhere]) == "unknown"


# --------------------------------------------------------------------------
# Process age, bound to one incarnation of a pid
# --------------------------------------------------------------------------


def test_starttime_survives_a_comm_containing_parentheses(mod, tmp_path):
    entry = tmp_path / "301"
    entry.mkdir()
    (entry / "stat").write_text(f"301 ((sh )nasty)) {stat_fields(4242)}\n", encoding="utf-8")
    assert mod._proc_starttime_ticks(tmp_path, "301") == 4242


def test_starttime_is_none_when_unreadable_or_short(mod, tmp_path):
    assert mod._proc_starttime_ticks(tmp_path, "404") is None
    entry = tmp_path / "302"
    entry.mkdir()
    (entry / "stat").write_text("302 (py) S 1 2 3\n", encoding="utf-8")
    assert mod._proc_starttime_ticks(tmp_path, "302") is None


def test_age_is_measured_against_the_captured_incarnation(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "303", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "303", 50_000) == 500


def test_age_is_refused_when_the_pid_was_recycled(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "304", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "304", 49_999) is None
    assert mod._proc_age_secs(tmp_path, "304", None) is None
    assert mod._proc_age_secs(tmp_path, "999", 50_000) is None


def test_age_is_refused_without_a_usable_clock(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "305", ["pytest", "-q"], starttime=50_000)
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None, "no uptime file"
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")

    def no_sysconf(name):
        raise ValueError("unsupported")

    monkeypatch.setattr(os, "sysconf", no_sysconf, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None
    monkeypatch.setattr(os, "sysconf", lambda name: 0, raising=False)
    assert mod._proc_age_secs(tmp_path, "305", 50_000) is None


def test_age_is_refused_on_a_platform_with_no_clock_tick_source(mod, tmp_path, monkeypatch):
    """The answer a platform without ``os.sysconf`` gets, asserted on every platform.

    The field is never omitted there: an absent age would read as a new process,
    while an unavailable one is reported as unknown.
    """
    proc_pid(tmp_path, "307", ["pytest", "-q"], starttime=50_000)
    (tmp_path / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    monkeypatch.delattr(os, "sysconf", raising=False)
    assert mod._proc_age_secs(tmp_path, "307", 50_000) is None


def test_age_clamps_a_clock_that_reads_backwards(mod, tmp_path, monkeypatch):
    proc_pid(tmp_path, "306", ["pytest", "-q"], starttime=500_000)
    (tmp_path / "uptime").write_text("10.0 5.0\n", encoding="utf-8")
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    assert mod._proc_age_secs(tmp_path, "306", 500_000) == 0


# --------------------------------------------------------------------------
# Host posture: what counts as the fleet's problem
# --------------------------------------------------------------------------


def host_proc(tmp_path, monkeypatch, *, mem_kb: int | None = 8_388_608) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000.0 900.0\n", encoding="utf-8")
    if mem_kb is not None:
        (root / "meminfo").write_text(
            f"MemTotal:       16000000 kB\nMemAvailable:   {mem_kb} kB\n", encoding="utf-8"
        )
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(root))
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    return root


def test_host_lines_reports_a_fleet_owned_unbounded_run(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    (fleet / "src").mkdir(parents=True)
    entry = proc_pid(root, "101", ["pytest", "-q"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet / "src")
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == [
        f"BANNED pid=101 rule={mod.DEFAULT_BANNED_RES[0]} cwd=fleet age=500s scope=suite "
        f"cmd=pytest,-q"
    ]
    assert "banned 1 | foreign 0" in host


#: A worker's test step, as the one multi-line script a single ``cmdline`` carries:
#: a capped run, then a read of the log that run wrote. Both lines name ``pytest``
#: and only one of them is a command.
CAPPED_STEP = (
    "set -euo pipefail\n"
    'timeout 900 "$PY" -m pytest -n0 test/test_x.py -q > /wt/pytest.log 2>&1\n'
    'grep -E "^(FAILED|ERROR)| passed|failed" /wt/pytest.log | tail -2\n'
)


def test_a_pytest_filename_is_not_a_pytest_command(mod, tmp_path, monkeypatch):
    """A mention of the runner is not a run of it, in either of the two shapes.

    Both quiet rows here were reported as violations by a rule that looked for the
    word alone: ``.`` and ``-`` are non-word characters, so ``\\bpytest\\b`` holds
    inside ``pytest.log`` and ``pytest-cov``, and the reading of a log a capped run
    just wrote is the commonest command in a worker's test step. The cost of that
    is entirely in the signal -- a conductor gates intake on a zero banned count,
    so a false row withholds work while nothing is wrong, and it teaches whoever
    reads the probe to discount the counter.

    The packaging rows are the same defect wearing other punctuation, and they are
    why the boundary is stated positively: naming the characters that CONTINUE a
    token means naming ``.``, then ``-``, then ``:``, then ``=``, then ``@``, one
    false stop at a time. Requiring a whole shell token covers all of them at once.

    The loud rows are the reason this cannot be fixed by matching less: a bare run
    and a run capped on a DIFFERENT line of the same script must still be told
    apart, which is what the last row pins.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    quiet = {
        # The capped run itself, both spellings.
        "201": ["python", "-m", "pytest", "-n0", "test/test_x.py", "-q"],
        "202": ["pytest", "-n0", "test/test_x.py"],
        # A filename that merely carries the word.
        "203": ["grep", "-E", "^(FAILED|ERROR)| passed|failed", "/wt/pytest.log"],
        "204": ["tail", "-2", "/wt/pytest.log"],
        "205": ["pip", "install", "pytest-cov"],
        # The word as a DIRECTORY, where a path separator rather than a dot ends
        # the token: the command here is `grep`, and the runner names a folder.
        "210": ["grep", "-rn", "FAILED", "/wt/pytest/results.log"],
        "211": ["type", r"C:\wt\pytest\results.log"],
        # The word as the head of a PACKAGING token. Each of these continues the
        # token with a character a per-character exclusion list has to name one at a
        # time, and each would otherwise stop a worker that is installing or pulling
        # something rather than running a suite.
        "212": ["docker", "run", "pytest:latest"],
        "213": ["pip", "install", "pytest==7.4.0"],
        "214": ["conda", "install", "pytest=7.4"],
        "215": ["npm", "i", "pytest@1.2.3"],
        # The whole step: a capped run and a log read in one argument.
        "206": ["bash", "-c", CAPPED_STEP],
    }
    loud = {
        # A run whose worker count nobody chose.
        "207": ["pytest", "test/test_x.py"],
        # The same step with an UNCAPPED run beside it, in both orders. A cap
        # belongs to the command that carries it and can excuse no other, which is
        # what a lookahead widened to the whole script text would break: scanning
        # forward past the command's own end reaches the cap on the line BELOW,
        # and scanning backward would reach the one above.
        "208": ["bash", "-c", "pytest -q test/test_y.py\n" + CAPPED_STEP],
        "209": ["bash", "-c", CAPPED_STEP + "pytest -q test/test_y.py\n"],
    }
    for pid, argv in {**quiet, **loud}.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    reported = {line.split("pid=")[1].split()[0] for line in lines}
    assert reported == set(loud), lines
    assert f"banned {len(loud)} | foreign 0" in host


def test_the_banned_line_names_the_command_without_echoing_its_arguments(
    mod, tmp_path, monkeypatch
):
    """``cmd=`` has to make a match judgeable, and carry nothing that can be secret.

    A pid alone cannot separate a real uncapped run from a command that only names
    one, and by the time anybody opens ``ps`` the process is usually gone. The
    field answers that -- but a command line is where a credential and a checkout
    layout ride, so only what cannot hold either is printed: program and runner
    NAMES from a fixed vocabulary, recognised option names with the value dropped --
    the cap flag's digits included -- and a count for the rest.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        # A path-qualified interpreter, a secret as an option VALUE under a flag name
        # the printable list does not carry, a private path glued to a recognised
        # option with `=`, and a target.
        "301": [
            "/wt/private-checkout/.venv/bin/python",
            "-m",
            "pytest",
            "--token",
            "s3cr3t-value",
            "--cov=/wt/private-checkout/src",
            "test/test_x.py",
        ],
        # An environment assignment in front of the command: the one place a secret
        # sits in the LEADING token, where a program name would otherwise print.
        "302": ["GITHUB_TOKEN=ghp-not-a-real-secret", "pytest", "test/test_x.py"],
        # No flag keeps its value, the cap flag included, so `--maxfail=2` prints as
        # its name and `-n auto` prints as `-n` with the word counted like any other
        # bare token.
        "303": ["pytest", "--maxfail=2", "-n", "auto", "test/test_x.py"],
        # More flags than the field prints: the remainder is counted, never cut
        # silently.
        "304": ["pytest", *(f"-{letter}" for letter in "abcdefghij")],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=python,-m,pytest,--cov,+3" in line["301"]
    assert "cmd=pytest,+2" in line["302"]
    assert "cmd=pytest,--maxfail,-n,+2" in line["303"]
    assert "cmd=pytest,-a,-b,-c,-d,-e,-f,-g,+3" in line["304"]

    whole = "\n".join(lines)
    for secret in (
        "s3cr3t-value",
        "ghp-not-a-real-secret",
        "private-checkout",
        ".venv",
        "test/test_x.py",
        "auto",
    ):
        assert secret not in whole, secret


def test_an_assignment_value_holding_a_separator_is_still_withheld(mod, tmp_path, monkeypatch):
    """The ``=`` decides before the directory is dropped, or the strip leaks the tail.

    An inline ``KEY=value`` in front of a command is the one place a secret sits in
    the LEADING token, where a program name would otherwise print. Testing its
    shape AFTER dropping everything up to the last separator cannot see that,
    because base64 secrets and presigned URLs routinely contain ``/``: the tail of
    ``AWS_SECRET_ACCESS_KEY=…/dEf9gHi`` is ``dEf9gHi``, which is a perfectly good
    program name by shape. So the assignment is recognised on the whole token.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        # Slash-bearing, the shape a separator-stripping check gets wrong.
        "311": ["OPAQUE_ONE=q7t/x2v/zPmKdR", "pytest", "test/test_x.py"],
        # Backslash-bearing: the strip folds ``\`` to ``/`` first, so it is the
        # same hole spelled for the other platform.
        "312": [r"OPAQUE_TWO=abc\def\gHiJkL", "pytest", "test/test_x.py"],
        # A separator-bearing value on a token that is NOT first, so neither the
        # program branch nor the flag branch may take it.
        "313": ["pytest", "TMPDIR=/wt/private-checkout/tmp", "test/test_x.py"],
        # The case the ORDER alone decides: a value whose tail after the last
        # separator is itself a name the printable list carries, so dropping the
        # directory first hands the program branch a word it accepts.
        "314": ["SECRET_PATH=/wt/private-checkout/bin/pytest", "pytest", "test/test_x.py"],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,+2" in line["311"]
    assert "cmd=pytest,+2" in line["312"]
    assert "cmd=pytest,+2" in line["313"]
    assert "cmd=pytest,+2" in line["314"]
    # Exactly one runner name: a second would be the assignment's tail arriving at
    # the program branch because the directory was dropped before the ``=`` was read.
    assert line["314"].split("cmd=")[1].split()[0].count("pytest") == 1

    whole = "\n".join(lines)
    for fragment in (
        "q7t",
        "x2v",
        "zPmKdR",
        "gHiJkL",
        "private-checkout",
        "OPAQUE_ONE",
        "OPAQUE_TWO",
        "TMPDIR",
        "SECRET_PATH",
    ):
        assert fragment not in whole, fragment


def test_a_short_option_value_is_dropped_whether_glued_or_spaced(mod, tmp_path, monkeypatch):
    """A short option glues its value on, so length is all that separates the two.

    ``-k`` takes a selector, which the scope readout already treats as being as
    sensitive as any other argument. Spelled ``-k name`` the value is its own token
    and is withheld; spelled ``-kname`` there is no ``=`` to split at, so a shape
    that accepts ``-`` plus letters accepts the value along with the name. Only a
    bare two-character short flag is echoed whole.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        "321": ["pytest", "-kMyCustomerName", "test/test_x.py"],
        "322": ["pytest", "-k", "MyCustomerName", "test/test_x.py"],
        "323": ["pytest", "-k=MyCustomerName", "test/test_x.py"],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,-k,+1" in line["321"]
    assert "cmd=pytest,-k,+2" in line["322"]
    assert "cmd=pytest,-k,+1" in line["323"]
    assert "MyCustomerName" not in "\n".join(lines)


def test_no_option_value_is_printed_not_even_the_cap_flags(mod, tmp_path, monkeypatch):
    """No flag keeps its value, and the cap flag is not the exception it looks like.

    A caller-supplied rule can point this scan at any program, and that program's
    numeric option value can be a secret -- an account id, a token that happens to be
    digits. The cap flag reads like the one value worth printing, since it is the
    field the rule judged, but nothing separates ``-n4`` meaning four workers from
    ``-n<account id>``: both are a flag whose value is digits. So its digits go too.
    What that costs is the ``-n0``-versus-``-n auto`` readout, and only under a custom
    rule -- a default-rule line never carries a digits-valued cap, because a run with
    one is exactly the run the rule declines to report.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        "331": ["custom-runner", "-u1234567890", "--jobs=4"],
        "332": ["custom-runner", "-n0", "--numprocesses=4"],
        # Both cap spellings carrying a secret in place of a worker count.
        "333": ["custom-runner", "-n4055511234", "--numprocesses=4055511234"],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bcustom-runner\b"]}
    )
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=-u,--jobs,+1" in line["331"]
    assert "cmd=-n,--numprocesses,+1" in line["332"]
    assert "cmd=-n,--numprocesses,+1" in line["333"]

    whole = "\n".join(lines)
    assert "1234567890" not in whole
    assert "4055511234" not in whole
    # No digit of any option value survives anywhere in the printed field.
    for pid in cases:
        printed = line[pid].split("cmd=")[1].split()[0]
        # The custom program's own NAME is withheld too: a rule may point this scan at
        # any program, and "looks like a program name" is satisfied by an opaque
        # credential. What identifies the command is ``rule=``, which the operator
        # wrote. The only digits left are the withheld count after ``+``.
        assert "custom-runner" not in printed, printed
        assert not any(ch.isdigit() for ch in printed.split("+")[0]), printed


def test_no_module_level_name_in_the_probe_is_defined_twice() -> None:
    """A second definition binds and the first goes dead, carrying its docstring with it.

    Rebinding a constant to the same value changes no behaviour, so nothing in the
    normal evidence chain objects: the formatter, the linter and the type checker all
    read a plain assignment as legal, and every test still passes because the value is
    identical. What moves is which docstring the module ships -- the LAST block wins,
    so an edited explanation sitting in the first copy is the one that stops being the
    file's own account of the constant. This reads the file structurally rather than
    importing it, because import keeps only the surviving binding and cannot see that
    a second one existed.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    counts: collections.Counter[str] = collections.Counter()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    counts[target.id] += 1
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            counts[node.target.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] += 1

    assert counts, "parsed no module-level definitions, so this guard proves nothing"
    duplicated = {name: n for name, n in counts.items() if n > 1}
    assert not duplicated, duplicated


def test_every_printable_token_is_shorter_than_the_character_bound(mod):
    """The clip is a BACKSTOP, so what needs proving is that nothing reaches it.

    No option keeps its value, so every token this field can print is either a
    two-character flag or an entry of a fixed vocabulary -- the per-token clip has
    nothing to clip, and a runtime test feeding a long token cannot reach it. What
    matters instead is the property that makes it unreachable, which is a statement
    about the vocabularies themselves: check them directly, and the day somebody adds
    a longer entry this fails rather than silently emitting a clipped token nobody
    expected.
    """
    bound = mod._MAX_CMD_TOKEN_CHARS
    vocabularies = {
        "_RUNNER_BASES": mod._RUNNER_BASES,
        "_LAUNCHER_BASES": mod._LAUNCHER_BASES,
        "_SAFE_LONG_FLAG_NAMES": mod._SAFE_LONG_FLAG_NAMES,
    }
    for label, entries in vocabularies.items():
        assert entries, label
        for entry in entries:
            assert len(entry) <= bound, (label, entry, len(entry))

    # ``python3.12`` and the longest version this stem admits.
    assert mod._PYTHON_BASE_RE.match("python3.12")
    assert len("python3.99.exe") <= bound

    # The clip itself still works, checked on the helper rather than through a
    # process, since no process can produce a token this long any more.
    assert mod._SAFE_SHORT_FLAG_RE.match("-n")


def test_the_character_bound_clips_and_marks_an_over_long_token(mod, tmp_path, monkeypatch):
    """The backstop is dead code unless it is exercised, so exercise it directly.

    A vocabulary entry longer than the bound is the case this guards, and no argv can
    produce one today. Adding such an entry for the length of one call is what shows
    the clip fires and marks what it cut.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    long_flag = "--" + "z" * 4096
    monkeypatch.setattr(
        mod, "_SAFE_LONG_FLAG_NAMES", frozenset(mod._SAFE_LONG_FLAG_NAMES | {long_flag})
    )
    entry = proc_pid(root, "341", ["pytest", long_flag], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1, lines
    printed = lines[0].split("cmd=")[1].split()[0]
    assert printed == f"pytest,{long_flag[: mod._MAX_CMD_TOKEN_CHARS]}~"
    assert len(printed) < len(long_flag)


def test_a_launchers_own_option_cannot_supply_the_runners_cap(mod, tmp_path, monkeypatch):
    """Suppressing a genuinely uncapped run is the fail-OPEN direction, so it must not.

    Reading the cap from the tokens is what stops an argument's own bytes being read as
    shell syntax, but it introduces the opposite risk: ``-n`` is not pytest's alone.
    ``nice`` spells its priority that way and ``xvfb-run`` its display number, and both
    are launchers this scan recognises, so a scan starting at argv[0] finds a number
    attached to the wrong program and withholds a run nobody capped. A conductor gates
    intake on a zero banned count, so that row going missing is worse than a false one:
    the run keeps its unbounded worker pool and nothing says so.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    loud = {
        # The launcher's own numeric option, in each spelling, on an UNCAPPED run.
        "391": ["nice", "-n", "10", "pytest", "test/"],
        "392": ["nice", "-n19", "python", "-m", "pytest", "test/"],
        "393": ["xvfb-run", "-n", "99", "pytest", "test/"],
        # The launcher carries one AND the runner carries a non-numeric one.
        "394": ["nice", "-n", "5", "pytest", "-n", "auto", "test/"],
    }
    quiet = {
        # The RUNNER's own cap, behind the same launcher: genuinely capped.
        "395": ["nice", "-n", "10", "pytest", "-n0", "test/"],
        "396": ["nice", "-n19", "python", "-m", "pytest", "--numprocesses=4", "test/"],
    }
    for pid, argv in {**loud, **quiet}.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    reported = {ln.split("pid=")[1].split()[0] for ln in lines}

    assert set(loud) <= reported, sorted(set(loud) - reported)
    assert reported & set(quiet) == set(), sorted(reported & set(quiet))

    # Read the helper directly for the case the rule cannot deliver: an argv with no
    # standalone runner token. The rule needs ``pytest`` as a whole shell token, and
    # wherever that sits inside a script string the launcher's options sit there too,
    # invisible to a token walk -- so this guard cannot be reached through
    # ``_host_lines`` today. It is what keeps the answer conservative rather than
    # launcher-derived if some future argv shape does arrive at it.
    assert mod._argv_declares_a_worker_cap(["pytest", "-n0"]) is True
    assert mod._argv_declares_a_worker_cap(["nice", "-n", "10", "pytest"]) is False
    assert mod._argv_declares_a_worker_cap(["nice", "-n", "10"]) is False


def test_the_cap_scan_does_not_backtrack_over_bracketed_spans(mod):
    """Ambiguous alternatives turn an ordinary rerun argv into a stalled patrol.

    The scan's first branch must not also match the characters that OPEN its span
    branches, or every span has two parses -- whole, or character by character -- and k
    spans admit 2**k of them. The star sits inside a NEGATIVE lookahead, so the case
    forced to walk every parse is the one with no cap to find: exactly the ``BANNED``
    case. A rerun naming a few dozen failed parametrized node ids is an ordinary command
    line, nothing in this script bounds the cmdline length or the match time, and the
    probe stalls inside the ``/proc`` walk, so the conductor loses the whole cycle.

    Structural, not a stopwatch: a timing assertion on a shared runner is a flake, while
    the disjointness is the property that makes the blow-up impossible.
    """
    # The first branch is a negated character class; splitting the pattern on ``|``
    # would cut inside it, so the class is extracted and compiled instead.
    leading = re.match(r"\(\?:(\[\^[^\]]*\])", mod._CAP_SCAN)
    assert leading, mod._CAP_SCAN
    first_branch = re.compile(leading.group(1))
    for opener in ("[", "'", '"'):
        assert not first_branch.match(opener), (opener, leading.group(1))

    # And the cheap end-to-end check: a many-span uncapped argv still resolves.
    rule = re.compile(mod.DEFAULT_BANNED_RES[0])
    spans = " ".join(f"a.py::t[c{i}]" for i in range(40))
    assert rule.search(f"pytest -n auto {spans}") is not None
    assert rule.search(f"pytest -n0 {spans}") is None


def test_a_separator_inside_one_argument_does_not_hide_the_runs_own_cap(mod, tmp_path, monkeypatch):
    """``/proc`` gives NUL-separated arguments, so a metacharacter in one is not syntax.

    The arguments are joined before matching, which puts every byte of every argument
    into the text the cap search walks -- and a node id, a log format or a ``-k``
    expression all carry the bytes that end a shell command. A capped run whose own
    ``-n0`` sits behind one therefore reads as uncapped, and the response to a
    ``cwd=fleet`` line is to stop the worker and discard the turn it was in, so this
    direction of the mistake costs work rather than signal.

    Two things answer it, and the second is what makes the first sufficient. The scan
    crosses a bracketed or quoted span whole, which covers the spellings that mark
    themselves as data. And for a pid that IS the runner rather than a shell holding a
    script, the whole argv is ONE command, so the cap is re-asked of the TOKENS, where
    an argument's own bytes can never be read as syntax -- which is the only thing that
    covers an option value with no bracket or quote around it at all.

    The loud rows keep the other direction: an UNQUOTED separator in shell text really
    is one, and a bare run must not borrow a neighbour's cap.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    quiet = {
        # A pipe and a semicolon inside a node id, on a properly capped run.
        "381": ["pytest", "test/test_x.py::test_y[a|b]", "-n0"],
        "382": ["pytest", "test/test_x.py::test_y[a;b]", "-n0"],
        # The same inside a quoted argument, and with the long cap spelling.
        "383": ["pytest", "'test/test_x.py::test_y[a|b]'", "--numprocesses=4"],
        # Cap BEFORE the bracketed separator rather than after it.
        "384": ["pytest", "-n0", "test/test_x.py::test_y[a|b]", "--cov", "src"],
        # A separator in an argument with NO bracket and NO quote around it: an option
        # VALUE that happens to carry one. Nothing in the text marks it as data, which
        # is why the TOKENS rather than the joined line have to answer.
        "387": ["pytest", "--log-format=%(levelname)s|%(message)s", "-n0"],
        "388": ["pytest", "-k", "not slow&not flaky", "-n", "4"],
        "389": ["pytest", "--deselect", "f.py::t[a|b]", "--numprocesses", "2"],
    }
    loud = {
        # A real pipe: the command before it carries no cap, so the run is unbounded.
        "385": ["bash", "-c", "pytest test/test_x.py | tee out.log"],
        # A bracketed separator must not let a LATER command's cap reach back.
        "386": ["bash", "-c", "pytest test/test_x.py::test_y[a|b];pytest -n0 g.py"],
    }
    for pid, argv in {**quiet, **loud}.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    reported = {ln.split("pid=")[1].split()[0] for ln in lines}

    assert reported & set(quiet) == set(), sorted(reported & set(quiet))
    assert set(loud) <= reported, sorted(set(loud) - reported)


def test_a_recognised_name_prints_its_vocabulary_entry_not_the_token(mod, tmp_path, monkeypatch):
    """The branch licences a vocabulary ENTRY, so the entry is what it may print.

    Matching is case-insensitive, which leaves the token's casing as the one thing on
    this line that the command being read still chooses: a word already known to equal
    an entry of ``_RUNNER_BASES``, ``_LAUNCHER_BASES`` or the ``_PYTHON_BASE_RE`` family
    carries no other information. That is a small disclosure, but it is the only
    caller-chosen material the branch can reach, and printing the canonical form
    removes it for the cost of one expression -- cheaper than tracking each token's
    offset back to the regex match, which couples the redactor to the rule.

    Both fixtures carry a lowercase invocation because the RULE is case-sensitive; the
    vocabulary check in the redactor is not, and that difference is exactly why a
    mixed-case token can reach the printing branch at all.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        # A runner name in an OPERAND slot, in a casing the command chose.
        "371": ["pytest", "--password", "PyTeSt", "test/test_x.py"],
        # A recognised launcher and a Windows-cased runner, both path-qualified.
        "372": [
            "/wt/private-checkout/.venv/bin/PYTHON3.12",
            "-m",
            "pytest",
            "PyTest.EXE",
            "f.py",
        ],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,pytest,+2" in line["371"]
    assert "cmd=python3.12,-m,pytest,pytest.exe,+1" in line["372"]

    whole = "\n".join(lines)
    for chosen in ("PyTeSt", "PYTHON3.12", "PyTest.EXE", "--password"):
        assert chosen not in whole, chosen


def test_an_opaque_word_in_the_program_position_is_withheld(mod, tmp_path, monkeypatch):
    """A program NAME and an opaque credential are the same text, so a shape cannot sort them.

    The leading token is the one position where a bare word is printed rather than
    counted, and a secret carrying no ``/`` and no ``=`` satisfies every rule a shape
    can state about a program name: letters, digits, a leading alphanumeric. So the
    branch asks a fixed list instead. Reading the name from the executable's path
    would answer the same finding and blank the field on the ordinary case, which the
    last two pids here are: a fleet's runner lives in a venv, and its BASENAME is what
    this prints, so the directory it sits in does not decide whether it is readable.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    opaque = "q7TkV2xPmKdR9sLbN4zH"
    cases = {
        # Program-shaped by every test a shape can apply, and not a program.
        "351": [opaque, "pytest", "test/test_x.py"],
        # A recognised launcher, version and all, out of a private venv.
        "352": ["/wt/private-checkout/.venv/bin/python3.12", "-m", "pytest", "test/test_x.py"],
        # The runner itself out of a venv: the case a path-derived name would blank.
        "353": ["/wt/private-checkout/.venv/bin/pytest", "test/test_x.py"],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,+2" in line["351"]
    assert "cmd=python3.12,-m,pytest,+1" in line["352"]
    assert "cmd=pytest,+1" in line["353"]

    whole = "\n".join(lines)
    assert opaque not in whole
    assert "private-checkout" not in whole


def test_an_opaque_word_spelled_as_a_long_option_is_withheld(mod, tmp_path, monkeypatch):
    """``--`` and letters is a well-formed option name AND a well-formed credential.

    A long option's value is dropped at the ``=`` whatever it holds, which is why the
    NAME was the printable part -- but the name is as long as its author cares to make
    it, so a secret spelled with two leading dashes arrived under the same branch that
    prints ``--numprocesses``. The list of names this line has a reason to print is
    fixed for that reason; an unrecognised flag is counted like any other token.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    opaque = "q7TkV2xPmKdR9sLbN4zH"
    cases = {
        # The secret wearing an option's spelling, bare and with a value.
        "361": ["pytest", f"--{opaque}", "test/test_x.py"],
        "362": ["pytest", f"--{opaque}=1", "test/test_x.py"],
        # A recognised concurrency flag still prints, with its value dropped.
        "363": ["pytest", "--dist=loadscope", "test/test_x.py"],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,+2" in line["361"]
    assert "cmd=pytest,+2" in line["362"]
    assert "cmd=pytest,--dist,+1" in line["363"]

    whole = "\n".join(lines)
    assert opaque not in whole
    assert "loadscope" not in whole


def test_every_default_rule_prints_as_one_whitespace_separated_field(mod):
    """``rule=`` echoes the pattern verbatim onto a line read as fields.

    The reader splits a ``BANNED`` line on whitespace to find ``cwd=``, ``age=``,
    ``scope=`` and ``cmd=``, which is why ``cmd=`` joins its tokens with commas. A
    pattern carrying a literal space would split ``rule=`` into several fields and
    break the same reader, so whitespace inside a rule is spelled as an escape.
    """
    for pattern in mod.DEFAULT_BANNED_RES:
        assert len(pattern.split()) == 1, pattern


def test_host_lines_counts_someone_elses_run_without_printing_it(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    entry = proc_pid(root, "102", ["pytest", "-q"], starttime=50_000)
    make_dir_link(entry / "cwd", elsewhere)
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == []
    assert "banned 0 | foreign 1" in host


def test_host_lines_skips_a_shell_holding_a_command_string(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "103", ["bash", "-c", "cd x && pytest -q"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    (entry / "exe").symlink_to("/usr/bin/bash")
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == []
    assert "banned 0 | foreign 0" in host


def test_host_lines_reports_the_wrapper_under_a_custom_rule(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "104", ["bash", "-c", "cd x && pytest -q"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    (entry / "exe").symlink_to("/usr/bin/bash")
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bpytest\b"]}
    )
    assert len(lines) == 1
    assert "pid=104" in lines[0]


def test_host_lines_drops_the_age_when_the_pid_is_recycled_mid_scan(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "105", ["pytest", "-q"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    reads = iter([50_000, 60_000])
    monkeypatch.setattr(mod, "_proc_starttime_ticks", lambda *args: next(reads))
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1
    assert "cwd=unknown" in lines[0]
    assert "age=?s" in lines[0]


def test_host_lines_ignores_non_pid_entries_and_unmatched_commands(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    (root / "self").mkdir()
    proc_pid(root, "106", ["python3", "-m", "http.server"], starttime=50_000)
    lines, host = mod._host_lines({})
    assert lines == []
    assert "banned 0" in host


def test_host_lines_degrades_one_row_when_a_process_vanishes(mod, tmp_path, monkeypatch):
    """A pid that exits mid-scan costs its own row, never the cycle."""
    root = host_proc(tmp_path, monkeypatch)
    (root / "108").mkdir()
    entry = proc_pid(root, "109", ["pytest", "-q"], starttime=50_000)
    make_dir_link(entry / "cwd", tmp_path)
    lines, host = mod._host_lines({})
    assert [line.split()[1] for line in lines] == ["pid=109"]
    assert "banned 1" in host


def test_host_lines_without_a_proc_filesystem(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "absent"))
    lines, host = mod._host_lines({})
    assert lines == []
    assert "mem n/a" in host


def test_host_lines_reports_a_hot_host(mod, tmp_path, monkeypatch):
    host_proc(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.os, "getloadavg", lambda: (1000.0, 1.0, 1.0), raising=False)
    _lines, host = mod._host_lines({"load_alert_per_cpu": 1.5})
    assert "(hot)" in host
    assert "mem 8G" in host


def test_host_lines_when_the_load_average_is_unavailable(mod, tmp_path, monkeypatch):
    host_proc(tmp_path, monkeypatch, mem_kb=None)

    def no_load():
        raise OSError("unsupported")

    monkeypatch.setattr(mod.os, "getloadavg", no_load, raising=False)
    _lines, host = mod._host_lines({})
    assert "load/cpu n/a" in host
    assert "mem n/a" in host


# --------------------------------------------------------------------------
# A session key is a filename stem, never a path
# --------------------------------------------------------------------------


def test_sessions_dir_is_derived_from_the_data_home(mod, sessions):
    assert mod._sessions_dir() == sessions


def test_transcript_path_tries_the_stem_then_the_surface_prefix(mod, sessions):
    direct = transcript(sessions, "worker-a", row("assistant", "GREEN: x"))
    assert mod._transcript_path(sessions, "worker-a") == direct
    prefixed = transcript(sessions, "dashboard_worker-b", row("assistant", "GREEN: x"))
    assert mod._transcript_path(sessions, "worker-b") == prefixed
    colon = transcript(sessions, "chat_601", row("assistant", "GREEN: x"))
    assert mod._transcript_path(sessions, "chat:601") == colon


def test_transcript_path_is_none_for_a_missing_session(mod, sessions):
    assert mod._transcript_path(sessions, "worker-absent") is None


def test_transcript_path_refuses_a_link_out_of_the_store(mod, sessions, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(row("assistant", "GREEN: x") + "\n", encoding="utf-8")
    (sessions / "escapee.jsonl").symlink_to(outside)
    assert mod._transcript_path(sessions, "escapee") is None


# --------------------------------------------------------------------------
# One probe cycle
# --------------------------------------------------------------------------


def probe(mod, cfg, tmp_path, name="probe-config.json"):
    return mod.run_probe(cfg, tmp_path / f"{name}.state.json")


def write_state(tmp_path, handled, name="probe-config.json") -> Path:
    path = tmp_path / f"{name}.state.json"
    path.write_text(json.dumps({"handled": handled}), encoding="utf-8")
    return path


def test_probe_fires_gone_for_a_missing_transcript(mod, sessions, empty_proc, tmp_path, capsys):
    assert probe(mod, {"sessions": [KEY]}, tmp_path) == 0
    out = capsys.readouterr().out
    line = fired_lines(out)[0]
    assert "GONE" in line and "i=?" in line
    assert ok_line(out).startswith("OK 1 watched, 1 fired")


def test_probe_fires_a_payload_report_with_its_digest(mod, sessions, empty_proc, tmp_path, capsys):
    transcript(sessions, KEY, row("assistant", "GREEN: PR 42 is green"))
    assert probe(mod, {"sessions": [KEY]}, tmp_path) == 0
    out = capsys.readouterr().out
    expected = mod._digest("GREEN:GREEN: PR 42 is green")
    assert f"d={expected}" in fired_lines(out)[0]
    assert "GREEN" in fired_lines(out)[0]
    assert "i=0" in fired_lines(out)[0]


def test_probe_suppresses_a_payload_the_conductor_already_acted_on(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(sessions, KEY, row("assistant", "GREEN: PR 42 is green"))
    digest = mod._digest("GREEN:GREEN: PR 42 is green")
    write_state(tmp_path, {KEY: {"tag": "GREEN", "digest": digest, "ts": int(time.time())}})
    assert probe(mod, {"sessions": [KEY]}, tmp_path) == 0
    out = capsys.readouterr().out
    assert fired_lines(out) == []
    assert ok_line(out).startswith("OK 1 watched, 0 fired")


def test_probe_fires_idle_after_the_threshold(mod, sessions, empty_proc, tmp_path, capsys):
    transcript(sessions, KEY, row("assistant", "no tag here"), age_secs=2000)
    assert probe(mod, {"sessions": [KEY], "idle_alert_secs": 900}, tmp_path) == 0
    assert "IDLE" in fired_lines(capsys.readouterr().out)[0]


def test_probe_fires_terminal_when_a_finished_worker_keeps_talking(
    mod, sessions, empty_proc, tmp_path, capsys
):
    """The finished report is what the handled set remembers, not what the window holds.

    A terminal report scrolls out of the window while the session goes on
    writing unprefixed text, so the reading has to come from the recorded
    disposition -- which is the difference between closing a finished worker out
    and nudging it forever.
    """
    transcript(sessions, KEY, row("assistant", "some prose after the work ended"))
    write_state(
        tmp_path,
        {KEY: {"tag": "IDLE", "digest": "x", "settled": {"tag": "GREEN", "digest": "g"}}},
    )
    assert probe(mod, {"sessions": [KEY], "idle_alert_secs": 900}, tmp_path) == 0
    assert "TERMINAL" in fired_lines(capsys.readouterr().out)[0]


def test_probe_fires_noprogress_when_nothing_was_produced_since_the_mark(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(sessions, KEY, row("assistant", "prose, no report"))
    write_state(tmp_path, {KEY: {"tag": "WORKING", "index": 0, "ts": time.time() - 3600}})
    assert probe(mod, {"sessions": [KEY], "idle_alert_secs": 900}, tmp_path) == 0
    assert "NOPROGRESS" in fired_lines(capsys.readouterr().out)[0]


def test_probe_surfaces_a_sticky_report_behind_a_handled_error(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(
        sessions,
        KEY,
        row("assistant", "BLOCKED: a ruling is owed"),
        row("error", "dispatch failure"),
    )
    err_digest = mod._digest("ERR:dispatch failure")
    write_state(tmp_path, {KEY: {"tag": "ERR", "digest": err_digest, "ts": int(time.time())}})
    assert probe(mod, {"sessions": [KEY]}, tmp_path) == 0
    line = fired_lines(capsys.readouterr().out)[0]
    assert "BLOCKED" in line
    assert f"d={mod._digest('BLOCKED:BLOCKED: a ruling is owed')}" in line


def test_probe_converts_a_suppressed_payload_into_noprogress(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(sessions, KEY, row("assistant", "PR: opened 42"))
    digest = mod._digest("PR:PR: opened 42")
    write_state(
        tmp_path,
        {KEY: {"tag": "PR", "digest": digest, "index": 0, "ts": time.time() - 3600}},
    )
    assert probe(mod, {"sessions": [KEY], "idle_alert_secs": 900}, tmp_path) == 0
    assert "NOPROGRESS" in fired_lines(capsys.readouterr().out)[0]


def test_probe_counts_undelivered_sessions_even_when_nothing_fires(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(
        sessions,
        KEY,
        row("assistant", "WORKING: a while ago"),
        row("inject", "initialize timed out"),
        row("inject", "[Tool stall detected -- automatic recovery]"),
    )
    assert probe(mod, {"sessions": [KEY], "idle_alert_secs": 900}, tmp_path) == 0
    out = capsys.readouterr().out
    assert fired_lines(out) == []
    assert ok_line(out).endswith("deliver init-timeout 1, watchdog 1")


def test_probe_prints_host_lines_beside_the_summary(mod, sessions, tmp_path, monkeypatch, capsys):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "107", ["pytest", "-q"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    assert probe(mod, {"sessions": [], "fleet_worktrees": [str(fleet)]}, tmp_path) == 0
    out = capsys.readouterr().out
    assert any(line.startswith("BANNED pid=107 ") for line in out.splitlines())
    assert ok_line(out).startswith("OK 0 watched, 0 fired")


# --------------------------------------------------------------------------
# Recording a disposition
# --------------------------------------------------------------------------


def test_mark_refuses_a_key_that_is_not_a_stem(mod, sessions, tmp_path, capsys):
    assert mod.mark_handled({}, tmp_path / "s.json", "../etc/passwd", "GREEN", "abc") == 2
    assert "malformed key" in capsys.readouterr().err


def test_mark_refuses_a_digest_the_tail_has_moved_past(mod, sessions, tmp_path, capsys):
    transcript(sessions, KEY, row("assistant", "GREEN: newer payload"))
    state = tmp_path / "s.json"
    assert mod.mark_handled({}, state, KEY, "GREEN", "staledigest1") == 3
    assert "payload changed since the probe" in capsys.readouterr().err
    assert not state.exists()


def test_mark_records_the_tag_digest_index_and_settled_payload(mod, sessions, tmp_path, capsys):
    transcript(sessions, KEY, row("assistant", "GREEN: PR 42 is green"))
    state = tmp_path / "s.json"
    digest = mod._digest("GREEN:GREEN: PR 42 is green")
    assert mod.mark_handled({}, state, KEY, "GREEN", digest) == 0
    assert capsys.readouterr().out.strip() == f"handled {KEY} GREEN"
    entry = json.loads(state.read_text(encoding="utf-8"))["handled"][KEY]
    assert entry["tag"] == "GREEN"
    assert entry["digest"] == digest
    assert entry["index"] == 0
    assert entry["settled"] == {"tag": "GREEN", "digest": digest}
    assert isinstance(entry["ts"], int)


def test_a_later_non_payload_mark_carries_the_settled_report_forward(mod, sessions, tmp_path):
    transcript(sessions, KEY, row("assistant", "PR: opened 42"), age_secs=2000)
    state = tmp_path / "s.json"
    pr_digest = mod._digest("PR:PR: opened 42")
    assert mod.mark_handled({}, state, KEY, "PR", pr_digest) == 0
    idle_digest = mod._digest("IDLE:PR: opened 42")
    assert mod.mark_handled({}, state, KEY, "IDLE", idle_digest) == 0
    entry = json.loads(state.read_text(encoding="utf-8"))["handled"][KEY]
    assert entry["tag"] == "IDLE"
    assert entry["settled"] == {"tag": "PR", "digest": pr_digest}
    third = mod._digest("IDLE:PR: opened 42")
    assert mod.mark_handled({}, state, KEY, "IDLE", third) == 0
    again = json.loads(state.read_text(encoding="utf-8"))["handled"][KEY]
    assert again["settled"] == {"tag": "PR", "digest": pr_digest}


def test_mark_accepts_the_gone_payload_for_a_vanished_session(mod, sessions, tmp_path):
    state = tmp_path / "s.json"
    digest = mod._digest("GONE:transcript missing")
    assert mod.mark_handled({}, state, KEY, "GONE", digest) == 0
    entry = json.loads(state.read_text(encoding="utf-8"))["handled"][KEY]
    assert entry["tag"] == "GONE"
    assert "index" not in entry


def test_mark_keys_a_sticky_tag_on_the_report_the_probe_surfaced(mod, sessions, tmp_path):
    transcript(
        sessions,
        KEY,
        row("assistant", "BLOCKED: a ruling is owed"),
        row("error", "dispatch failure"),
    )
    state = tmp_path / "s.json"
    digest = mod._digest("BLOCKED:BLOCKED: a ruling is owed")
    assert mod.mark_handled({}, state, KEY, "BLOCKED", digest) == 0
    entry = json.loads(state.read_text(encoding="utf-8"))["handled"][KEY]
    assert entry["settled"] == {"tag": "BLOCKED", "digest": digest}


# --------------------------------------------------------------------------
# Typed misconfiguration is a message, never a crash
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "fragment"),
    [
        ({"sessions": "one-key"}, "sessions must be a list of strings"),
        ({"sessions": [1]}, "sessions must be a list of strings"),
        ({"err_res": {"a": 1}}, "err_res must be a list of strings"),
        ({"sessions": ["../escape"]}, "is not a plain key"),
        ({"fleet_worktrees": ["rel/path"]}, "must be an absolute path"),
        ({"fleet_worktrees": ["/wt\0extra"]}, "contains a NUL byte"),
        ({"fleet_worktrees": ["/"]}, "resolves to a filesystem root"),
        ({"idle_alert_secs": True}, "must be a finite non-negative number"),
        ({"idle_alert_secs": -1}, "must be a finite non-negative number"),
        ({"tail_bytes": float("nan")}, "must be a finite non-negative number"),
        ({"load_alert_per_cpu": float("inf")}, "must be a finite non-negative number"),
        ({"err_res": ["([unclosed"]}, "bad regex"),
    ],
)
def test_config_error_names_the_offending_key(mod, sessions, cfg, fragment):
    problem = mod._config_error(cfg)
    assert problem is not None and fragment in problem


def test_config_error_refuses_a_root_that_contains_the_session_store(mod, sessions):
    problem = mod._config_error({"fleet_worktrees": [str(sessions.parent)]})
    assert problem is not None and "contains the session store" in problem


def test_config_error_accepts_a_well_formed_config(mod, sessions, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert (
        mod._config_error(
            {
                "sessions": [KEY, "chat:601"],
                "idle_alert_secs": 900,
                "tail_bytes": 200_000,
                "load_alert_per_cpu": 1.5,
                "err_res": [r"\bboom\b"],
                "fleet_worktrees": [str(wt)],
            }
        )
        is None
    )


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def config_file(tmp_path, payload) -> Path:
    path = tmp_path / "probe-config.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_main_reports_malformed_config_rather_than_crashing(mod, sessions, tmp_path, capsys):
    missing = tmp_path / "absent.json"
    assert mod.main(["--config", str(missing)]) == 2
    assert "malformed config" in capsys.readouterr().err
    not_json = config_file(tmp_path, "{not json")
    assert mod.main(["--config", str(not_json)]) == 2
    not_object = config_file(tmp_path, "[1, 2]")
    assert mod.main(["--config", str(not_object)]) == 2
    assert "config must be a JSON object" in capsys.readouterr().err


def test_main_reports_a_typed_config_problem(mod, sessions, tmp_path, capsys):
    path = config_file(tmp_path, {"sessions": ["../escape"]})
    assert mod.main(["--config", str(path)]) == 2
    assert "is not a plain key" in capsys.readouterr().err


def test_main_runs_a_probe_and_derives_its_own_state_path(
    mod, sessions, empty_proc, tmp_path, capsys
):
    transcript(sessions, KEY, row("assistant", "GREEN: PR 42 is green"))
    path = config_file(tmp_path, {"sessions": [KEY]})
    assert mod.main(["--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "GREEN" in fired_lines(out)[0]
    digest = mod._digest("GREEN:GREEN: PR 42 is green")
    assert mod.main(["--config", str(path), "--mark-handled", KEY, "GREEN", digest]) == 0
    assert (tmp_path / "probe-config.json.state.json").exists()
    capsys.readouterr()
    assert mod.main(["--config", str(path)]) == 0
    assert fired_lines(capsys.readouterr().out) == []


def test_main_returns_the_refusal_code_for_a_stale_mark(mod, sessions, empty_proc, tmp_path):
    transcript(sessions, KEY, row("assistant", "GREEN: PR 42 is green"))
    path = config_file(tmp_path, {"sessions": [KEY]})
    assert mod.main(["--config", str(path), "--mark-handled", KEY, "GREEN", "staledigest1"]) == 3


def test_main_requires_a_config(mod):
    with pytest.raises(SystemExit) as excinfo:
        mod.main([])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------
# Every mark this module supplies is read at the moment it is compared
# --------------------------------------------------------------------------


def test_no_parametrize_argument_reads_the_clock() -> None:
    """A mark in this file is always weighed against an elapsed-time window.

    Each ``ts`` here reaches a function that subtracts it from ``time.time()``
    and compares the difference to an idle window -- ``_stalled_since_disposition``,
    ``_suppressed``, and the state ``main`` reloads. A mark therefore means
    nothing on its own; it means something only relative to the instant the
    assertion runs.

    A ``@pytest.mark.parametrize`` argument is evaluated once, while the module
    is imported for collection. A clock read there freezes the mark at collection
    time and then asserts it against a window measured at run time, so the gap
    between those two moments decides the verdict: green on a fast shard, red on
    a shard that queues longer than the window, and nothing in the diff under
    test to explain either. A clock read belongs in the test body, which runs at
    the same moment as the comparison.

    Read as a syntax tree rather than as text. The property is the absence of a
    call inside a decorator argument, which running the module cannot
    demonstrate, and a text scan would match the names in this docstring.
    """
    import ast

    reads = {"time", "time_ns", "monotonic", "monotonic_ns", "now", "utcnow", "today"}
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not (isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize"):
                continue
            for arg in [*dec.args, *(kw.value for kw in dec.keywords)]:
                for inner in ast.walk(arg):
                    if not isinstance(inner, ast.Call):
                        continue
                    if not isinstance(inner.func, ast.Attribute):
                        continue
                    assert inner.func.attr not in reads, (
                        f"line {inner.lineno}: {ast.unparse(inner)} is evaluated at "
                        f"collection time in the parametrize list of {node.name}"
                    )


# Every way this repo's runner is spelled on a command line, as an argv PREFIX. The
# pin below is enumerated over this table crossed with the cap spellings, not over a
# list of command lines: the property under test is "any invocation form, bounded or
# not", so a form added here is covered in both directions by construction and a fix
# that only repairs the spelling someone happened to write down cannot pass.
RUNNER_FORMS = {
    "bare": ["pytest"],
    "module": ["python3", "-m", "pytest"],
    "versioned-interpreter-module": ["python3.12", "-m", "pytest"],
    "venv-abspath": ["/wt/.venv/bin/pytest"],
    "py.test": ["py.test"],
    "pytest.exe": ["pytest.exe"],
    "alias": ["pytest-3"],
    "alias-minor": ["pytest-3.12"],
    "alias-abspath": ["/usr/bin/pytest-3"],
    "alias-py.test": ["py.test-3"],
    "alias-shebang": ["python3", "{sys}/bin/pytest-3"],
    "alias-shebang-venv": ["python3", "{venv}/bin/pytest-3"],
    "alias-shebang-behind-value-flag": ["python3", "-W", "ignore", "{sys}/bin/pytest-3"],
    "alias-behind-launcher": ["timeout", "900", "pytest-3"],
    "alias-behind-launcher-with-own-flag": ["nice", "-n", "10", "pytest-3"],
}

# Every spelling of a numeric worker cap. `-n auto` is deliberately absent: the rule's
# documented sense is that a count nobody chose is the reportable one, and `auto` is
# bounded by the rootdir hook rather than by the caller.
CAP_SPELLINGS = {
    "glued-zero": ["-n0"],
    "glued-four": ["-n4"],
    "split": ["-n", "0"],
    "equals": ["-n=0"],
    "long-equals": ["--numprocesses=0"],
    "long-split": ["--numprocesses", "2"],
}

# Tokens that appear AFTER the cap and name the runner without being an invocation of
# it. Each one defeated the cap lookahead, which only ever looked forward from the
# token it matched: the bound sits earlier in the line, so from the second occurrence
# it is invisible and a bounded run was reported.
TRAILING_RUNNER_SHAPED_ARGS = {
    "junitxml": ["--junitxml=build/pytest.xml"],
    "log-file": ["--log-file", "/var/tmp/pytest-run.log"],
    "basetemp": ["--basetemp", "/var/tmp/pytest-of-ci"],
    "rootdir": ["--rootdir", "/wt/pytest-sandbox"],
}


def banned_pids(mod, root: Path, fleet: Path) -> set[str]:
    """The pids ``_host_lines`` emitted a ``BANNED`` line for."""
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    return {line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")}


def fleet_pid(
    root: Path, fleet: Path, pid: str, argv: list[str], *, unspoofed: bool = True
) -> None:
    """A fleet-owned pid whose ``cmdline`` carries the real procfs byte shape.

    ``proc_pid`` writes ``"\\0".join(argv) + "\\0"`` -- arguments NUL-separated and one
    NUL terminator, which is what the kernel produces. The scan is driven through that
    byte path on purpose: a pre-joined string handed straight to the rules cannot show
    that the split, the dropped terminator and the re-join preserved the argument
    boundaries the argv-side checks read.

    The ``cwd`` link goes through ``make_dir_link`` like every other directory link in
    this file: a name meaning another DIRECTORY is a junction on Windows and needs no
    privilege, where a bare ``symlink_to`` would raise on an unelevated Windows shell and
    ERROR every case built on this fixture instead of running it.

    ``unspoofed`` supplies the ``exe`` link as well, because a real fleet-owned process
    has one: it runs as the fleet's own uid, so the kernel's binary always reads, and the
    ``argv[0]`` rule requires that binary to CONFIRM the name the process claims. An
    unspoofed process is one whose binary's name IS what ``argv[0]`` says, so the link is
    built from ``argv[0]``'s own last component -- the ordinary case, and therefore the
    default. Pass ``unspoofed=False`` for a test that arranges the link itself or needs it
    absent.

    That link is a DIRECTORY junction rather than a symlink to a file, and the difference
    is free: the probe reads the link's TARGET as a string and takes its last component,
    never stating it, so a junction carries exactly the same fact while needing no
    Windows privilege and adding nothing to the real-symlink inventory.
    """
    entry = proc_pid(root, pid, argv, starttime=500)
    make_dir_link(entry / "cwd", fleet)
    if unspoofed and argv:
        name = argv[0].replace("\\", "/").rpartition("/")[2] or "program"
        binary = fleet.parent / "kernel" / name
        binary.mkdir(parents=True, exist_ok=True)
        make_dir_link(entry / "exe", binary)


def install_prefixes(tmp_path: Path) -> dict[str, str]:
    """Real prefixes for the entry-point tests, keyed for ``str.format`` substitution.

    A console script is installed into the same directory as the interpreter it was
    installed for, so these tests have to BUILD that rather than name a path and rely on
    the host having one. Naming ``/usr/local/bin`` would pass or fail with whatever the
    runner happens to have installed there, which is the worst kind of test: green on one
    host and red on another for reasons the change never touched.

    The four installed prefixes are deliberately unlike each other -- a system root, a
    virtualenv inside the fleet checkout, a version-manager prefix -- because the rule
    must recognise all of them without naming any. Each one's ``bin`` gets the three
    interpreter spellings a real installation carries.

    Three counterexamples come with them, and the third is the one that matters most.
    ``checkout`` is the case GPT's finding named: a repository that merely HOLDS a ``bin``
    directory, with no interpreter installed beside its contents. Every prefix's ``sbin``
    is created EMPTY, because a python console script is never installed there -- it
    separates "a directory whose name looks like an installation's" from "a directory an
    interpreter was installed into". And ``user`` models a ``pip install --user``, which
    writes the console script into ``~/.local/bin`` and NO interpreter with it: writing
    one there would fabricate a file a real ``--user`` install never creates, and a test
    passing on a fabricated file certifies a behaviour that cannot occur. So the
    interpreter is deliberately absent and ``user`` belongs among the declining cases --
    that miss is stated in ``_is_installed_entry_point`` rather than claimed away.

    Only plain files and directories are created, so nothing here joins the real-symlink
    privilege inventory.
    """
    made: dict[str, str] = {}
    for key, relative in (
        ("sys", "usr"),
        ("local", "usr/local"),
        ("venv", "wt/.venv"),
        ("shim", "opt/pythons/3.12.13"),
    ):
        prefix = tmp_path / relative
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        (prefix / "sbin").mkdir(parents=True, exist_ok=True)
        (prefix / "tools").mkdir(parents=True, exist_ok=True)
        for interpreter in ("python", "python3", "python3.12"):
            (prefix / "bin" / interpreter).write_text("#!/bin/sh\n", encoding="utf-8")
        made[key] = str(prefix)
    (tmp_path / "checkout" / "bin").mkdir(parents=True, exist_ok=True)
    made["checkout"] = str(tmp_path / "checkout")
    # A --user install: the console script, and pointedly no interpreter beside it.
    (tmp_path / "home" / "u" / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    made["user"] = str(tmp_path / "home" / "u" / ".local")
    return made


def with_prefixes(tokens: list[str], prefixes: dict[str, str]) -> list[str]:
    """Substitute ``install_prefixes`` keys into an argv enumeration's tokens.

    An enumeration cannot hold the paths directly, because they only exist once a test
    has built them under its own ``tmp_path``. A token carrying no placeholder passes
    through unchanged, so a case that is deliberately relative or deliberately Windows
    stays exactly as written.
    """
    return [token.format(**prefixes) for token in tokens]


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
@pytest.mark.parametrize("cap", sorted(CAP_SPELLINGS))
def test_every_runner_form_stays_quiet_when_it_declares_a_cap(
    mod, tmp_path, monkeypatch, form, cap
):
    """A run that CHOSE its worker count is never reported, however it is spelled.

    This is the direction that destroys work: the documented answer to a fleet-owned
    ``BANNED`` line is to stop that worker and discard the turn it was in, so a false
    row here costs real work rather than signal.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    form_argv = with_prefixes(RUNNER_FORMS[form], prefixes)
    argv = [*form_argv, *CAP_SPELLINGS[cap], "test/test_x.py"]
    fleet_pid(root, fleet, "401", argv)
    assert banned_pids(mod, root, fleet) == set(), f"{form} + {cap} was reported while capped"


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
def test_every_runner_form_is_reported_when_it_declares_no_cap(mod, tmp_path, monkeypatch, form):
    """A run whose worker count nobody chose is reported, however it is spelled.

    The other direction, and the one the probe exists for. A form missing here is an
    unbounded run the conductor's banned counter cannot see, so intake keeps admitting
    work while the host is being consumed.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    fleet_pid(root, fleet, "402", [*with_prefixes(RUNNER_FORMS[form], prefixes), "test/"])
    assert banned_pids(mod, root, fleet) == {"402"}, f"{form} went unreported while uncapped"


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
@pytest.mark.parametrize("trailing", sorted(TRAILING_RUNNER_SHAPED_ARGS))
def test_a_capped_run_stays_quiet_when_a_later_argument_names_the_runner(
    mod, tmp_path, monkeypatch, form, trailing
):
    """A cap is still a cap when a LATER argument spells the runner's name.

    ``--junitxml=build/pytest.xml`` and ``--log-file /var/tmp/pytest-run.log`` are
    ordinary arguments of a bounded run. A forward-only cap lookahead re-tries at that
    second occurrence, where the bound is behind it and cannot be seen, and reports the
    run -- measured as two deterministic false rows on a live fleet.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        *with_prefixes(RUNNER_FORMS[form], prefixes),
        "-n0",
        "test/test_x.py",
        *TRAILING_RUNNER_SHAPED_ARGS[trailing],
    ]
    fleet_pid(root, fleet, "403", argv)
    assert banned_pids(mod, root, fleet) == set(), (
        f"{form} was reported while capped because a later argument named the runner "
        f"({trailing})"
    )


# The alias in a position that is NOT the program: a directory component, a package
# name, a log filename, an argument to some other command. These are what a joined-line
# rule cannot separate from an invocation, and the whole reason the alias is detected on
# the argv side instead -- so they are the control for that choice, not a side note.
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["ls", "/var/tmp/pytest-of-ci/pytest-3"], id="tmpdir-as-final-token"),
        pytest.param(["pip", "install", "pytest-3"], id="package-name"),
        pytest.param(["pip", "install", "pytest-3.12"], id="versioned-package-name"),
        pytest.param(["cat", "pytest-3.log"], id="log-filename"),
        pytest.param(["cat", "py.test.log"], id="py.test-log-filename"),
        pytest.param(["ls", "/var/tmp/pytest-of-ci/py.test"], id="py.test-as-path"),
        pytest.param(
            ["grep", "-rn", "FAILED", "/var/tmp/pytest-of-ci/pytest-3/results.log"],
            id="grep-target",
        ),
        pytest.param(["tail", "-2", "/var/tmp/pytest-of-ci/pytest-3/x.log"], id="tail-target"),
        pytest.param(["rm", "-rf", "/var/tmp/pytest-of-ci/pytest-3"], id="cleanup-target"),
    ],
)
def test_an_alias_that_is_not_the_program_is_never_reported(mod, tmp_path, monkeypatch, argv):
    """The alias as data, under a command that is not a test run.

    Each one is disqualified by its own FIRST token rather than by a pattern that has to
    guess: the command already had a program before the alias appeared, so the alias is
    an argument. Reporting these is what adding the alias to the joined-line rule would
    have cost.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "404", argv)
    assert banned_pids(mod, root, fleet) == set()


def test_the_argv_row_names_the_argv_path_rather_than_a_rule_that_did_not_fire(
    mod, tmp_path, monkeypatch
):
    """``rule=`` on an alias row names the argv path, not the pytest pattern.

    The pattern genuinely did not match -- the alias is invisible to it by design --
    so printing it would send a reader to a lookahead that is working correctly.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "405", ["pytest-3", "test/"])
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1
    assert f"rule={mod.ARGV_RUNNER_RULE_LABEL}" in lines[0]
    assert mod.ARGV_RUNNER_RULE_LABEL not in mod.DEFAULT_BANNED_RES
    # The label has to survive being read as one whitespace-separated field on a line
    # the conductor parses, so it carries no space and nothing that reopens a field.
    assert not any(ch in mod.ARGV_RUNNER_RULE_LABEL for ch in " \t\"'`")


def test_an_alias_run_reports_its_scope_instead_of_declining(mod, tmp_path, monkeypatch):
    """``scope=`` answers for an alias run too.

    Scope keys on the runner's own token standing alone in argv. While the alias was
    absent from that set, every alias row printed ``scope=unknown`` -- the readout that
    says the probe could not tell a whole-suite run from a one-file one, on exactly the
    rows where it matters.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "406", ["pytest-3", "test/test_x.py"])
    fleet_pid(root, fleet, "407", ["pytest-3", "--cov", "src/kiro_crew"])
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    scopes = {
        line.split()[1].split("=", 1)[1]: line.split("scope=", 1)[1].split()[0] for line in lines
    }
    assert scopes == {"406": "paths", "407": "suite"}


@pytest.mark.parametrize(
    ("base", "is_alias"),
    [
        ("pytest-3", True),
        ("pytest-3.12", True),
        ("pytest-3.12.1", True),
        ("py.test-3", True),
        ("pytest-3.exe", True),
        ("pytest", False),
        ("pytest-cov", False),
        ("pytest-3.log", False),
        ("pytest-of-ci", False),
        ("pytest3", False),
        ("pytest-", False),
        ("mypytest-3", False),
        ("pytest-3-extra", False),
    ],
)
def test_the_alias_pattern_admits_a_version_and_nothing_else(mod, base, is_alias):
    """The alias shape is a version suffix, not any suffix.

    ``pytest-cov`` is a plugin, ``pytest-3.log`` is a file and ``pytest-of-ci`` is a
    tmpdir. All three are alias-SHAPED under a loose pattern, and the argv-position gate
    would not save a token that reached it at index 0.
    """
    assert bool(mod._ALIAS_RUNNER_BASE_RE.match(base)) is is_alias


# Every spelling of the interpreter, so the module-position rule is pinned as a property
# of the python family rather than of one token.
PYTHON_SPELLINGS = {
    "python": ["python"],
    "python3": ["python3"],
    "python3.12": ["python3.12"],
    "python-abspath": ["/wt/.venv/bin/python3"],
    "python-with-own-flag": ["python3", "-u"],
}

# What an interpreter is given when the thing it runs is a SCRIPT. In each one the alias
# is an argument to that script, so it names no program.
#
# The tails are split deliberately. A script whose name carries a program suffix is also
# caught by the suffix rule, so those cases alone cannot show the interpreter rule doing
# any work -- a mutation removing it stays green. The suffixless tails are the ones that
# isolate it: nothing but "an interpreter's first non-option operand is the script"
# separates them from an invocation.
INTERPRETER_SCRIPT_TAILS = {
    "script-then-alias": ["worker.py", "pytest-3"],
    "script-then-alias-path": ["cleanup.py", "/var/tmp/pytest-of-ci/pytest-3"],
    "script-then-py.test": ["worker.py", "py.test"],
    "script-then-alias-among-args": ["runner.py", "--target", "pytest-3", "test/"],
    "dashless-script-then-alias": ["tools/sweep.py", "pytest-3.12"],
    "suffixless-script-then-alias": ["worker", "pytest-3"],
    "suffixless-script-then-py.test": ["harness", "py.test"],
    "suffixless-script-then-alias-path": ["sweep", "/var/tmp/pytest-of-ci/pytest-3"],
    "inline-code-then-alias": ["-c", "import sys; print(sys.argv)", "pytest-3"],
    "subcommand-shaped-operand-then-alias": ["run", "pytest-3"],
    # These two isolate the POSITION guard from the entry-point requirement. Every tail
    # above hands the script a name that is not an installed path, so the entry-point
    # test alone would decline it and the position guard is never reached. Here the
    # argument IS an installed entry-point path, so only "the candidate must BE python's
    # execution target" can decline it -- an ordinary way to tell a wrapper which runner
    # to use.
    "script-then-entry-point-path": ["worker.py", "{sys}/bin/pytest-3"],
    "script-then-venv-entry-point": ["harness.py", "{venv}/bin/pytest-3"],
}

# What an interpreter is given when the SCRIPT IT RUNS IS the runner. This is the
# position the ordinary packaged alias actually occupies: `/usr/bin/pytest-3` carries a
# python shebang, so the kernel's argv for it is `python3 /usr/bin/pytest-3 …` and the
# alias never reaches argv[0] at all. Admitting the alias only as the argument of `-m`
# therefore declines the ordinary run of the very spelling this file detects.
#
# The flag tails are here for the same reason the suffixless tails are in the set above:
# they isolate one rule. `python3 -W ignore prog` has a non-option token that is NOT the
# script, so a reading that simply took the first dashless token would answer `ignore`
# and decline the real invocation one position later.
INTERPRETER_ENTRY_POINT_SCRIPTS = {
    "system-prefix": ["{sys}/bin/pytest-3"],
    "local-prefix": ["{local}/bin/pytest-3"],
    "venv-prefix": ["{venv}/bin/pytest-3"],
    "venv-minor-version": ["{venv}/bin/pytest-3.12"],
    "venv-dotted-spelling": ["{venv}/bin/py.test"],
    "version-manager-prefix": ["{shim}/bin/py.test-3"],
    "behind-flag-bundle": ["-Es", "{sys}/bin/pytest-3"],
    "behind-split-value-flag": ["-W", "ignore", "{sys}/bin/pytest-3"],
    "behind-attached-value-flag": ["-Wignore", "{sys}/bin/pytest-3"],
    "behind-long-option": ["--check-hash-based-pycs", "always", "{sys}/bin/pytest-3"],
}

# A script position whose path is an ordinary repository file rather than an installed
# console script. The name alone cannot tell these from the real thing -- they are drawn
# from the same namespace -- so the INSTALLATION is the evidence: a console script is
# written into the same directory as the interpreter it was installed for.
#
# ``checkout-bin-dir`` is why the directory's NAME cannot carry this on its own. Any
# repository can hold a ``bin``, so a rule satisfied by the name reports
# ``python3 <worktree>/bin/pytest-3`` -- a file the checkout happens to carry -- and stops
# a healthy worker over it. ``sbin-holds-no-interpreter`` and
# ``install-prefix-but-not-bin`` sit inside a REAL prefix and still decline, which is what
# separates a directory an interpreter was installed into from one that merely sits near
# it. The three relative paths cannot be resolved at all from here, because the probe's
# working directory is not the scanned process's.
NON_ENTRY_POINT_SCRIPTS = {
    "repo-tools-dir": "tools/pytest-3",
    "repo-scripts-dir": "scripts/py.test",
    "repo-tools-py.test-3": "tools/py.test-3",
    "bare-name-in-cwd": "pytest-3",
    "dot-slash-in-cwd": "./pytest-3",
    "nested-repo-path": "src/vendor/pytest-3",
    "pytest-temp-root": "/var/tmp/pytest-of-ci/pytest-3",
    "absolute-repo-path": "/wt/tools/pytest-3.12",
    "windows-venv-scripts": "C:\\wt\\.venv\\Scripts\\pytest-3",
    "checkout-bin-dir": "{checkout}/bin/pytest-3",
    "checkout-bin-dotted": "{checkout}/bin/py.test",
    "checkout-bin-minor-version": "{checkout}/bin/pytest-3.12",
    "sbin-holds-no-interpreter": "{sys}/sbin/pytest-3",
    "install-prefix-but-not-bin": "{sys}/tools/pytest-3",
    "user-install-has-no-interpreter": "{user}/bin/py.test",
    "user-install-versioned-alias": "{user}/bin/pytest-3",
    "relative-bin-dir": "bin/pytest-3",
    "relative-dot-bin-dir": "./bin/py.test",
    "relative-sbin-dir": "sbin/pytest-3",
}


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize("script", sorted(NON_ENTRY_POINT_SCRIPTS))
def test_a_script_position_that_is_not_an_installed_entry_point_is_not_a_runner(
    mod, tmp_path, monkeypatch, python, script
):
    """``python3 tools/pytest-3`` runs a file the checkout holds, not the test runner.

    The script position has no ``/proc`` fact to lean on -- the kernel's binary there is
    the interpreter -- so the path is the only evidence, and a bare or repository-relative
    name is a file rather than an installation. Accepting one stops a healthy worker and
    discards its turn, which is the direction that destroys work.

    The Windows ``Scripts`` case is here rather than among the entry points on purpose:
    ``_basename`` lowercases, so it would arrive indistinguishable from an ordinary
    ``scripts/`` directory, and a ``/proc`` cmdline comes from a Linux kernel anyway.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    script_path = NON_ENTRY_POINT_SCRIPTS[script].format(**prefixes)
    argv = [*PYTHON_SPELLINGS[python], script_path, "test/"]
    fleet_pid(root, fleet, "433", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{python} + {script} was reported, but that path is a repo file"


@pytest.mark.parametrize("prefix", ["sys", "local", "venv", "shim"])
def test_an_installed_entry_point_directory_still_reports(mod, tmp_path, monkeypatch, prefix):
    """The other direction: requiring an installation must not cost the packaged run.

    The packaged alias run is the whole point of the argv path, so requiring an
    installation has to leave every ordinary installation reporting -- and these five
    prefixes are deliberately unalike, because the rule reads what an installation IS
    rather than where it sits. A system root, a virtualenv and a version-manager prefix
    all answer yes without the rule naming any of them, which is what keeps a host whose
    interpreter is a global shim from being a special case. A ``--user`` install is NOT
    among them: it installs no interpreter beside its scripts, so it is a stated miss and
    sits among the declining cases instead.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    script = f"{prefixes[prefix]}/bin/pytest-3"
    fleet_pid(root, fleet, "434", ["python3", script, "test/"])
    assert banned_pids(mod, root, fleet) == {
        "434"
    }, f"{prefix} entry point went unreported while uncapped"


def test_a_relative_script_path_is_not_resolved_against_the_probes_own_directory(
    mod, tmp_path, monkeypatch
):
    """A relative path belongs to the scanned process's directory, not to the probe's.

    ``/proc/<pid>/cmdline`` records the arguments as written, so ``python3 bin/pytest-3``
    says nothing about which ``bin`` was meant -- and the one directory the probe must not
    answer with is its own. Here the probe's working directory really does hold
    ``bin/python3``, so a rule that resolved the relative path from where it happens to be
    running would find an interpreter beside a file it has never seen and stop a healthy
    worker. Refusing the relative path outright is what makes that impossible, and the
    cost is a missed signal on a spelling no packaged run uses.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    monkeypatch.chdir(prefixes["sys"])
    fleet_pid(root, fleet, "437", ["python3", "bin/pytest-3", "test/"])
    assert banned_pids(mod, root, fleet) == set(), (
        "a relative script path was resolved against the probe's own working directory, "
        "which is not the scanned process's"
    )


def test_an_interpreter_absent_from_the_scripts_directory_is_not_an_installation(
    mod, tmp_path, monkeypatch
):
    """The interpreter that must be present is the one ``argv[0]`` names, not any python.

    A prefix carrying ``python3.12`` did not install a console script for ``python3.13``,
    so a run naming that interpreter is not evidence of an installation here. The pin
    matters because the cheap mistake is to accept the directory once ANY interpreter is
    found in it, which would readmit a checkout that vendors an unrelated one.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    script = f"{prefixes['sys']}/bin/pytest-3"
    fleet_pid(root, fleet, "435", ["python3.13", script, "test/"])
    assert banned_pids(mod, root, fleet) == set(), (
        "python3.13 was accepted against a prefix that only installed python3.12, so the "
        "check is not asking for the interpreter argv[0] names"
    )
    fleet_pid(root, fleet, "436", ["python3.12", script, "test/"])
    assert banned_pids(mod, root, fleet) == {
        "436"
    }, "the control failed: python3.12 IS installed there and must still report"


# An assignment or option whose VALUE ends in a runner-shaped path component, standing in
# front of the real runner. `_token_base` reduces a token to its last path component, so
# each value below reduces to a runner name -- and every pytest temp directory is named
# after the runner, which makes the shape ordinary rather than contrived. Answering with
# that position hands the cap reader a span that begins before the LAUNCHER, so the
# launcher's own `-n` is read as the run's cap and a genuinely uncapped run is skipped.
RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER = {
    "tmpdir-alias": "TMPDIR=/tmp/pytest-of-ci/pytest-3",
    "tmpdir-plain": "TMPDIR=/tmp/pytest-of-ci/pytest",
    "basetemp-dotted": "BASETEMP=/var/tmp/pytest-of-x/py.test",
    "tmpdir-minor-version": "TMPDIR=/tmp/pytest-of-ci/pytest-3.12",
    "lowercase-name": "pytest_tmp=/tmp/pytest-of-ci/py.test-3",
}

# A launcher's own worker-ish option, which is NOT the run's cap. `nice -n 10` sets a
# scheduling priority and `timeout 900` a deadline; neither says anything about pytest
# workers, and reading either as the cap is how the run goes unreported.
LAUNCHER_OWN_CAP_SHAPED_OPTIONS = {
    "nice": ["nice", "-n", "10"],
    "nice-glued": ["nice", "-n10"],
    "timeout": ["timeout", "900"],
}


@pytest.mark.parametrize("launcher", sorted(LAUNCHER_OWN_CAP_SHAPED_OPTIONS))
@pytest.mark.parametrize("value", sorted(RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER))
def test_a_runner_shaped_assignment_value_is_not_the_runners_own_position(
    mod, tmp_path, monkeypatch, value, launcher
):
    """An UNCAPPED run is still reported when a token in front of it looks like the runner.

    This is the fail-open direction of the position lookup, and the expensive thing here is
    not a false row but a lost one: the run really is unbounded, and the conductor's banned
    counter never sees it, so intake keeps admitting work while the host is consumed.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        "env",
        RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER[value],
        *LAUNCHER_OWN_CAP_SHAPED_OPTIONS[launcher],
        "pytest",
        "test/",
    ]
    fleet_pid(root, fleet, "438", argv)
    assert banned_pids(mod, root, fleet) == {"438"}, (
        f"{value} + {launcher} went unreported: the position lookup answered with the "
        "assignment, so the launcher's own option was read as the run's cap"
    )


@pytest.mark.parametrize("launcher", sorted(LAUNCHER_OWN_CAP_SHAPED_OPTIONS))
@pytest.mark.parametrize("value", sorted(RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER))
def test_the_runners_own_cap_still_silences_a_run_behind_such_a_value(
    mod, tmp_path, monkeypatch, value, launcher
):
    """The other direction: moving the position forward must not cost the cap.

    Skipping the assignment makes the cap be read from the RUNNER's own arguments, which is
    where it belongs -- so a run that chose its worker count stays silent, and this is the
    direction whose failure would stop a healthy worker.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        "env",
        RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER[value],
        *LAUNCHER_OWN_CAP_SHAPED_OPTIONS[launcher],
        "pytest",
        "-n0",
        "test/",
    ]
    fleet_pid(root, fleet, "439", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{value} + {launcher} was reported despite the runner declaring its own cap"


# A transparent launcher's OWN operand, standing where the candidate itself sits. Each
# value's last path component is alias-shaped for the same reason every pytest temp path's
# is, and each command runs something that is not pytest at all. With the candidate at
# index 1 the "what stands in front" slice is empty and vacuously true, so the candidate
# has to be vetted as well as its predecessors.
LAUNCHER_OWN_OPERAND_AS_CANDIDATE = {
    "env-tmpdir-make": ["env", "TMPDIR=/var/tmp/pytest-of-ci/pytest-3", "make", "test"],
    "env-basetemp-npm": ["env", "BASETEMP=/tmp/pytest-of-x/pytest-3.12", "npm", "test"],
    "env-two-assignments": ["env", "CI=1", "TMPDIR=/tmp/pytest-of-x/pytest-3", "make", "test"],
    "env-assignment-then-node": ["env", "TMPDIR=/tmp/pytest-of-x/py.test-3", "node", "run.js"],
    "env-assignment-alone": ["env", "TMPDIR=/tmp/pytest-of-x/pytest-3"],
    "timeout-assignment-shaped": ["timeout", "900", "TMPDIR=/tmp/pytest-of-x/pytest-3", "make"],
}


@pytest.mark.parametrize("shape", sorted(LAUNCHER_OWN_OPERAND_AS_CANDIDATE))
def test_a_launchers_own_operand_is_never_the_runners_program_position(
    mod, tmp_path, monkeypatch, shape
):
    """An assignment is the launcher's grammar, so it cannot be the command's subject.

    The empty-slice case is the one that matters: when the candidate stands at index 1
    there is nothing in front of it to vet, and a check written only about predecessors
    passes vacuously. Every command here runs ``make``, ``npm`` or ``node`` and holds no
    pytest invocation, so a row against one stops a healthy worker and discards its turn.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "436", LAUNCHER_OWN_OPERAND_AS_CANDIDATE[shape])
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{shape} was reported, but the alias-shaped token is the launcher's own operand"


#: An alias-shaped assignment standing in FRONT of a genuine run. Declining the
#: assignment must not end the search, or the run two tokens later is hidden.
ASSIGNMENT_BEFORE_A_REAL_RUNNER = ["env", "TMPDIR=/var/tmp/pytest-of-ci/pytest-3"]


def test_an_assignment_in_front_does_not_hide_a_real_runner_behind_it(mod, tmp_path, monkeypatch):
    """Declining one candidate must not stop the search: a later token can be the runner.

    This argv holds BOTH -- an alias-shaped assignment that is the launcher's own grammar,
    and a genuine uncapped run two tokens later. Answering only the FIRST candidate would
    decide the pid on the assignment and never look at the runner, turning a fix for a
    false positive into a missed detection.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    argv = [*ASSIGNMENT_BEFORE_A_REAL_RUNNER, "pytest-3", "test/"]
    fleet_pid(root, fleet, "437", argv)
    assert banned_pids(mod, root, fleet) == {
        "437"
    }, "the runner behind the assignment went unreported"


@pytest.mark.parametrize("cap", sorted(CAP_SPELLINGS))
def test_a_runner_behind_an_assignment_still_stays_quiet_when_capped(
    mod, tmp_path, monkeypatch, cap
):
    """The other direction: searching every candidate must not cost the cap.

    The cap is read from the runner's own arguments, so it is asserted here through the
    same cross product of spellings as every other invocation form.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    argv = [*ASSIGNMENT_BEFORE_A_REAL_RUNNER, "pytest-3", *CAP_SPELLINGS[cap], "test/"]
    fleet_pid(root, fleet, "438", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"the runner behind the assignment was reported despite {cap}"


# A kernel binary that REFUTES the name the process gave itself. `argv[0]` is chosen by
# the process, so `exec -a pytest-3 sleep 600` presents a sleeping shell as a runner, and
# this path would otherwise stop a healthy worker on a self-issued name. Each value is
# what `/proc/<pid>/exe` really points at.
SPOOFED_ARGV0_KERNEL_PROGRAMS = {
    "sleep": "/usr/bin/sleep",
    "bash": "/usr/bin/bash",
    "cat": "/usr/bin/cat",
    "outside-a-trusted-dir": "/home/someone/bin/sleep",
    "deleted-binary": "/usr/bin/sleep (deleted)",
    # An interpreter belongs here rather than among the exemptions. A shebang script's
    # kernel binary IS the interpreter, but the kernel puts the interpreter at argv[0]
    # and the runner one position later, so a packaged run never reaches this check.
    # What reaches it with an interpreter behind it is `exec -a pytest-3 python3 -c …`.
    "interpreter": "/usr/bin/python3",
}

# A launcher's operand that is not part of the launcher's OWN grammar is the subject of
# the command, whatever shape it takes: a subcommand, a bare word, a script name, a path.
# The runner spelling behind it is that subject's argument.
LAUNCHER_SUBJECT_OPERANDS = {
    "npm-subcommand": (["npm"], ["run", "build"]),
    "npm-single-subcommand": (["npm"], ["test"]),
    "poetry-run": (["poetry"], ["run"]),
    "yarn-subcommand": (["yarn"], ["workspace", "api"]),
    "tox-bare-env": (["tox"], ["envlist"]),
    "make-target": (["make"], ["clean"]),
    "coverage-script": (["coverage", "run"], ["worker.py"]),
    "node-script": (["node"], ["runner.js"]),
    "bash-script": (["bash"], ["teardown.sh"]),
    "hatch-script-path": (["hatch"], ["scripts/sweep"]),
    "timeout-then-subject": (["timeout", "900"], ["make"]),
}

# A launcher that interposes a subject of its own never puts the runner in the program
# position, not even as its IMMEDIATE operand. `make pytest-3.12` is the case that makes
# this matter: a per-interpreter test matrix spells its targets that way, each target
# wraps a run that caps its own workers, and a fleet-owned row against one stops a
# healthy worker with no automatic recovery.
NON_TRANSPARENT_IMMEDIATE_OPERAND = {
    "make": ["make"],
    "make-minor-version": ["make"],
    "npm": ["npm"],
    "npx": ["npx"],
    "yarn": ["yarn"],
    "poetry": ["poetry"],
    "tox": ["tox"],
    "nox": ["nox"],
    "hatch": ["hatch"],
    "coverage": ["coverage"],
    "node": ["node"],
    "bash": ["bash"],
    "sh": ["sh"],
    "py": ["py"],
    "uv": ["uv"],
}

# What a launcher consumes as part of its own grammar. A runner standing after only these
# is still the program, so every one of them must keep qualifying.
LAUNCHER_OWN_GRAMMAR = {
    "nothing": ([], []),
    "numeric-operand": (["timeout"], ["900"]),
    "flag-and-numeric": (["nice"], ["-n", "10"]),
    "bare-flag": (["xvfb-run"], ["-a"]),
    "env-assignment": (["env"], ["CI=1"]),
    "two-assignments": (["env"], ["CI=1", "TERM=dumb"]),
    "flag-then-numeric-then-flag": (["timeout"], ["-k", "5", "900"]),
}


@pytest.mark.parametrize("launcher", sorted(NON_TRANSPARENT_IMMEDIATE_OPERAND))
@pytest.mark.parametrize("runner", ["pytest-3", "pytest-3.12", "py.test-3"])
def test_a_non_transparent_launchers_immediate_operand_is_not_the_program(
    mod, tmp_path, monkeypatch, launcher, runner
):
    """`make pytest-3.12` names a TARGET, and that target runs a capped suite.

    A launcher that interposes a subject of its own does not put the runner in the
    program position at any distance, the immediate operand included. This is the case
    with the highest cost in the file: a per-interpreter matrix target is ordinary, the
    run behind it caps its own workers, and the row stops a healthy worker with no
    automatic recovery beyond switching the whole detection off.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "460", [*NON_TRANSPARENT_IMMEDIATE_OPERAND[launcher], runner])
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{launcher} {runner} was reported, but the operand is that launcher's subject"


def test_the_transparent_launchers_are_a_subset_of_the_recognised_ones(mod):
    """The two launcher sets cannot drift apart.

    A transparent launcher this file does not otherwise recognise would be admitted by
    one rule and unknown to the rest, so the subset relation is asserted rather than
    assumed.
    """
    assert mod._TRANSPARENT_LAUNCHER_BASES <= mod._LAUNCHER_BASES
    # Each one adjusts the environment and execs what follows, which is why its own
    # grammar is exactly options, numbers and assignments.
    assert mod._TRANSPARENT_LAUNCHER_BASES == {"env", "nice", "timeout", "xvfb-run"}


@pytest.mark.parametrize("shape", sorted(LAUNCHER_SUBJECT_OPERANDS))
def test_a_launcher_operand_that_is_the_subject_ends_the_program_position(
    mod, tmp_path, monkeypatch, shape
):
    """A bare word after a launcher is what the launcher runs, so the alias is its argument.

    Most of ``_LAUNCHER_BASES`` takes a subcommand or a script this way -- `npm run`,
    `poetry run`, `tox`, `make`, `node`, `bash`. Reporting any of these stops a worker
    over a string that names a runner without being one, and an attributable false
    positive costs a discarded turn rather than only noise.
    """
    launcher, operands = LAUNCHER_SUBJECT_OPERANDS[shape]
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "422", [*launcher, *operands, "pytest-3"])
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{shape} was reported, but the launcher's operand is the program"


@pytest.mark.parametrize("shape", sorted(LAUNCHER_OWN_GRAMMAR))
@pytest.mark.parametrize("runner", ["pytest-3", "py.test", "pytest.exe"])
def test_a_runner_after_only_the_launchers_own_grammar_is_still_the_program(
    mod, tmp_path, monkeypatch, shape, runner
):
    """The other direction: an option, a number and an assignment are not subjects.

    `timeout 900 pytest-3` is a real uncapped run. Declining these would hide exactly the
    invocations the probe exists to see, so the allow-list has to admit each one.
    """
    launcher, operands = LAUNCHER_OWN_GRAMMAR[shape]
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "423", [*launcher, *operands, runner, "test/"])
    assert banned_pids(mod, root, fleet) == {
        "423"
    }, f"{shape} + {runner} went unreported while uncapped"


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize("tail", sorted(INTERPRETER_SCRIPT_TAILS))
def test_an_interpreter_running_a_script_never_reports_an_alias_in_its_arguments(
    mod, tmp_path, monkeypatch, python, tail
):
    """An interpreter's first non-option operand is the script, and it ends the walk.

    ``python3 worker.py pytest-3`` runs ``worker.py``; the alias is a string that script
    was handed. Nothing unusual is needed to produce this shape -- no custom config, no
    timing -- so admitting it draws a stop against a worker doing compliant work, which
    is the direction that destroys a turn.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [*PYTHON_SPELLINGS[python], *with_prefixes(INTERPRETER_SCRIPT_TAILS[tail], prefixes)]
    fleet_pid(root, fleet, "420", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{python} + {tail} was reported, but the program is the script, not the alias"


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize(
    "runner", ["pytest-3", "pytest-3.12", "py.test-3", "py.test", "pytest.exe"]
)
def test_an_argv_only_spelling_in_the_module_position_is_not_a_runner(
    mod, tmp_path, monkeypatch, python, runner
):
    """No pytest MODULE carries a version suffix, so ``-m pytest-3`` is not a pytest run.

    The packaged ``pytest-3`` is a console SCRIPT; the module has always been plain
    ``pytest``, which the joined-line rule matches on its own. What ``python -m pytest-3``
    actually runs is a checkout-local ``pytest-3.py``, found by name because an import
    finder resolves names rather than identifiers -- and reporting it stops a healthy
    worker and discards its turn.

    What declining costs is ``python -m py.test``, a spelling modern pytest does not
    provide, so the trade is a missed signal against a stopped worker.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "421", [*PYTHON_SPELLINGS[python], "-m", runner, "test/"])
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{python} -m {runner} was reported, but no pytest module is spelled that way"


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
def test_the_plain_module_spelling_is_still_reported_by_its_own_rule(
    mod, tmp_path, monkeypatch, python
):
    """Declining the module position must not touch ``python -m pytest``.

    That spelling is matched by the joined-line rule, not by the argv path, so narrowing
    the argv path leaves it exactly where it was -- the direction that proves the
    subtraction above is a subtraction and not a hole.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "422", [*PYTHON_SPELLINGS[python], "-m", "pytest", "test/"])
    assert banned_pids(mod, root, fleet) == {"422"}


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize("script", sorted(INTERPRETER_ENTRY_POINT_SCRIPTS))
def test_an_interpreter_reports_the_runner_standing_as_its_script_operand(
    mod, tmp_path, monkeypatch, python, script
):
    """The interpreter's SCRIPT is the program, so a runner standing there qualifies.

    This is the shape the alias really arrives in. A packaged ``pytest-3`` is a python
    script with a shebang, so running it makes the kernel's argv ``python3
    /usr/bin/pytest-3 …``: the alias is the interpreter's script operand and appears at
    ``argv[0]`` never. A rule admitting the alias only behind ``-m`` declines the
    ordinary packaged run of the one spelling this detection exists for, and nothing
    later re-asks -- a declined pid emits no line at all.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    tail = with_prefixes(INTERPRETER_ENTRY_POINT_SCRIPTS[script], prefixes)
    argv = [*PYTHON_SPELLINGS[python], *tail, "test/"]
    fleet_pid(root, fleet, "426", argv)
    assert banned_pids(mod, root, fleet) == {
        "426"
    }, f"{python} + {script} went unreported while uncapped"


@pytest.mark.parametrize("cap", sorted(CAP_SPELLINGS))
@pytest.mark.parametrize("script", sorted(INTERPRETER_ENTRY_POINT_SCRIPTS))
def test_a_runner_as_the_script_operand_stays_quiet_when_it_declares_a_cap(
    mod, tmp_path, monkeypatch, script, cap
):
    """Admitting the script position must not cost the cap, in either spelling.

    The expensive direction of this whole file: a run that CHOSE its worker count is
    healthy, and reporting it stops a worker and discards its in-flight turn. The cap
    is re-asked of the runner's own arguments, so it is read here through the same
    function that reads it for every other form.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        "python3",
        *with_prefixes(INTERPRETER_ENTRY_POINT_SCRIPTS[script], prefixes),
        *CAP_SPELLINGS[cap],
        "test/",
    ]
    fleet_pid(root, fleet, "427", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{script} + {cap} was reported despite declaring a cap"


@pytest.mark.parametrize("program", sorted(SPOOFED_ARGV0_KERNEL_PROGRAMS))
def test_a_kernel_binary_that_is_not_a_runner_refutes_a_spoofed_argv0(
    mod, tmp_path, monkeypatch, program
):
    """``argv[0]`` is the process's own claim, and the kernel's binary overrules it.

    ``exec -a pytest-3 sleep 600`` introduces a sleeping process as a test runner. At
    ``argv[0]`` there is no earlier token to read, so this is the only position where the
    claim stands unchecked -- and acting on it stops a healthy worker and discards its
    turn.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "428", ["pytest-3", "test/"], unspoofed=False)
    (root / "428" / "exe").symlink_to(SPOOFED_ARGV0_KERNEL_PROGRAMS[program])
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"a pid whose kernel binary is {program} was reported as a runner"


def test_an_unreadable_kernel_link_declines_argv0(mod, tmp_path, monkeypatch):
    """At ``argv[0]`` the kernel's binary must CONFIRM, so an unreadable link declines.

    This position is the one that ACCUSES on nothing but the name the process chose for
    itself, and a process can make its own ``exe`` unreadable by going non-dumpable -- so
    treating that silence as permission to report puts the stop back under the control of
    whatever is being stopped.

    The reading that an unreadable link is harmless does not survive measurement. It
    rested on the pid already being sorted ``foreign``/``unknown``, but ``_owner_class``
    falls back to ``_program_class``, which reads the SAME process-chosen argv: an
    ``argv[0]`` naming a path under a fleet worktree is classified ``fleet`` with no link
    and no readable ``cwd`` at all. One token then produces a fleet-owned row, and the
    documented response to one is to stop that worker and discard its in-flight turn,
    which nothing restores. What declining costs instead is a missed signal on a process
    whose binary this uid cannot read.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "429", ["pytest-3", "test/"], unspoofed=False)
    assert not (root / "429" / "exe").exists()
    assert banned_pids(mod, root, fleet) == set(), (
        "an alias-shaped argv[0] was reported with no kernel binary to confirm it, so one "
        "process-chosen token can stop a healthy worker"
    )

    # The same shape the adjudication named: ownership derived from that same token.
    spoofed = tmp_path / "wt" / "bin" / "pytest-3"
    fleet_pid(root, fleet, "432", [str(spoofed), "test/"], unspoofed=False)
    assert banned_pids(mod, root, fleet) == set(), (
        "an argv[0] naming a path under the fleet worktree was reported with no kernel "
        "binary, so argv alone supplied both the accusation and the ownership"
    )


def test_a_kernel_binary_agreeing_with_argv0_confirms_it(mod, tmp_path, monkeypatch):
    """The other direction: a genuine runner binary outside a trusted directory still counts.

    ``_trusted_program_base`` refuses a basename outside ``_TRUSTED_PROGRAM_DIRS``
    because a SHELL's name there is a claim about a file anybody could have placed. A
    runner's own ``bin`` inside a venv is the ordinary home of one, so this question is
    asked of the link without that gate.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "430", ["pytest-3", "test/"], unspoofed=False)
    (root / "430" / "exe").symlink_to("/home/u/wt/.venv/bin/pytest-3")
    assert banned_pids(mod, root, fleet) == {"430"}


def test_an_interpreter_kernel_binary_does_not_refute_a_shebang_runner(mod, tmp_path, monkeypatch):
    """A shebang run is decided one position later, so the refutation never sees it.

    This is why an interpreter is not exempted from the refutation: the kernel puts the
    interpreter at ``argv[0]`` and the runner after it, so the packaged run is answered
    by the script-operand rule and reaches the ``argv[0]`` check at all. An exemption
    there would only ever admit ``exec -a pytest-3 python3 -c …``, which is the spoof.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    prefixes = install_prefixes(tmp_path)
    script = f"{prefixes['sys']}/bin/pytest-3"
    fleet_pid(root, fleet, "431", ["python3", script, "test/"], unspoofed=False)
    (root / "431" / "exe").symlink_to(f"{prefixes['sys']}/bin/python3")
    assert banned_pids(mod, root, fleet) == {"431"}


@pytest.mark.parametrize(
    "argv, expected",
    [
        pytest.param(["python3", "prog.py"], (1, "script"), id="script"),
        pytest.param(["python3", "-W", "ignore", "prog.py"], (3, "script"), id="split-value-flag"),
        pytest.param(["python3", "-Wignore", "prog.py"], (2, "script"), id="attached-value-flag"),
        pytest.param(["python3", "-Es", "prog.py"], (2, "script"), id="flag-bundle"),
        pytest.param(["python3", "-X", "utf8", "prog.py"], (3, "script"), id="X-split"),
        pytest.param(["python3", "-m", "pytest"], (2, "module"), id="module-selector"),
        pytest.param(["python3", "-O", "-m", "pytest"], (3, "module"), id="flag-then-module"),
        pytest.param(["python3", "-Om", "pytest"], (2, "module"), id="bundle-trailing-module"),
        pytest.param(["python3", "-m"], None, id="module-selector-with-no-module"),
        pytest.param(["python3", "-mpytest"], None, id="attached-module-declines"),
        pytest.param(
            ["python3", "worker.py", "-m", "pytest"],
            (1, "script"),
            id="post-script-m-is-an-argument",
        ),
        pytest.param(
            ["python3", "-c", "print(1)", "-m", "x"], None, id="post-command-m-is-an-argument"
        ),
        pytest.param(["python3", "-", "-m", "x"], None, id="post-stdin-m-is-an-argument"),
        pytest.param(["python3", "-c", "print(1)"], None, id="command-runs-no-script"),
        pytest.param(["python3", "-"], None, id="stdin-script"),
        pytest.param(["python3"], None, id="no-operand-at-all"),
        pytest.param(["python3", "-Z", "prog.py"], None, id="unknown-option-declines"),
    ],
)
def test_the_execution_target_is_located_by_pythons_own_grammar(mod, argv, expected):
    """Python runs exactly ONE thing, and its grammar says which token that is.

    The first selector to appear wins -- ``-m``, ``-c``, ``-``, or the script operand --
    and every token after it belongs to the thing being run. A single index is what makes
    ``python3 worker.py -m pytest`` answer 1 rather than 3: the script came first, so the
    ``-m`` is an argument that script was handed, and a script may spell its own options
    however it likes. An option this grammar cannot classify answers None, which
    declines, because a missed line costs a signal and a false one costs a turn.

    The KIND travels with the index because only one of the two can be checked for being
    an installed entry point: a module name is resolved by the import machinery, while a
    script is a path that proves only what sits there.
    """
    assert mod._python_execution_target(argv) == expected


# An ``-m`` that is NOT python's own selector, because a selector already appeared. Each
# tail hands the interpreter a program and then a string that merely looks like a module
# request, which is an ordinary way for a script to take its own options.
POST_SELECTOR_MODULE_TAILS = {
    "script-then-m": ["worker.py", "-m", "pytest-3"],
    "script-then-m-capped": ["worker.py", "-m", "pytest-3", "-n0"],
    "script-then-m-among-args": ["cleanup.py", "--mode", "-m", "pytest-3"],
    "suffixless-script-then-m": ["worker", "-m", "pytest-3"],
    "subcommand-shaped-then-m": ["run", "-m", "pytest-3"],
    "command-then-m": ["-c", "import sys", "-m", "pytest-3"],
    "stdin-then-m": ["-", "-m", "pytest-3"],
}


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize("tail", sorted(POST_SELECTOR_MODULE_TAILS))
def test_an_m_after_pythons_selector_is_an_argument_not_a_module_request(
    mod, tmp_path, monkeypatch, python, tail
):
    """``python3 worker.py -m pytest-3`` runs ``worker.py``; the ``-m`` is its argument.

    Testing the token in FRONT of the candidate cannot see this: an ``-m`` sits directly
    before the alias in every one of these, and every one is a healthy process. Only
    python's own first selector settles it, which is why the grammar walk returns one
    index and the rule compares against it.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    argv = [*PYTHON_SPELLINGS[python], *POST_SELECTOR_MODULE_TAILS[tail]]
    fleet_pid(root, fleet, "432", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{python} + {tail} was reported, but python's program is not the alias"


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(["-O"], id="optimise"),
        pytest.param(["-B"], id="no-bytecode"),
        pytest.param(["-O", "-B"], id="two-flags"),
    ],
)
def test_an_interpreter_reports_the_alias_when_only_flags_stand_in_front(
    mod, tmp_path, monkeypatch, python, flags
):
    """``python3 -O pytest-3`` runs the file ``pytest-3``, and that file IS the program.

    Only flags intervene here, so nothing about the launcher's own grammar separates
    this from an invocation -- which makes it the case that isolates the script-operand
    rule from every other reason a token can qualify.

    The opposite reading, that such a file may be any data an operator named that way,
    does not survive the mechanism. For the pid to exist long enough to be scanned the
    operand has to be something python can actually execute: a file of unparsable data
    or a directory without ``__main__.py`` -- a pytest temp root such as
    ``/var/tmp/pytest-of-ci/pytest-3`` is exactly that -- exits before any walk sees it.
    An alias in a script position that runs is a runner, and it is the position the
    ordinary packaged spelling always occupies.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    prefixes = install_prefixes(tmp_path)
    argv = [*PYTHON_SPELLINGS[python], *flags, f"{prefixes['sys']}/bin/pytest-3", "test/"]
    fleet_pid(root, fleet, "424", argv)
    assert banned_pids(mod, root, fleet) == {
        "424"
    }, f"{python} + {flags} went unreported, but the alias stands as the script"


def test_the_named_opt_out_switches_the_argv_shape_off(mod, tmp_path, monkeypatch):
    """An operator CAN disable the argv shape, by saying so about the argv shape.

    Without an escape hatch this detection is an absolute an operator cannot decline,
    which is a real complaint. With one it is policy -- and the key names the thing it
    disables, so nobody removes it while meaning to change something else.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "450", ["pytest-3", "test/"])
    off, _host = mod._host_lines({"fleet_worktrees": [str(fleet)], "argv_runner_detection": False})
    assert [line for line in off if line.startswith("BANNED pid=")] == []
    # The control: the same pid with the key absent IS reported, so the silence is the
    # opt-out and not a broken fixture.
    assert banned_pids(mod, root, fleet) == {"450"}


def test_the_opt_out_and_the_rule_list_are_independent(mod, tmp_path, monkeypatch):
    """Neither setting reaches the other, in either direction.

    This is the whole point of the separation. Replacing ``banned_process_res`` must not
    disable the argv shape -- a protection removed as a side effect of an unrelated edit
    is removed by someone who never decided to remove it. And disabling the argv shape
    must not disable the operator's own rules.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "451", ["pytest-3", "test/"])
    fleet_pid(root, fleet, "452", ["npm", "audit", "--json"])
    custom = [r"\bnpm\b\s+audit"]

    def reported(cfg):
        lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)], **cfg})
        return {
            line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
        }

    # A replaced rule list leaves the argv shape ON.
    assert reported({"banned_process_res": custom}) == {"451", "452"}
    # The opt-out leaves the operator's own rule in force.
    assert reported({"banned_process_res": custom, "argv_runner_detection": False}) == {"452"}
    # The opt-out alone leaves the built-in rules in force; 452 is not an npm-audit
    # match under the defaults, so only the argv row disappears.
    assert reported({"argv_runner_detection": False}) == set()
    assert reported({}) == {"451"}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("false", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param([], id="empty-list"),
        pytest.param({}, id="empty-dict"),
    ],
)
def test_a_non_boolean_opt_out_is_malformed_config(mod, value):
    """A near-miss value is refused at load, never guessed at.

    ``"false"`` is not False, so a permissive read would leave the shape ON for an
    operator who believes they switched it off. A falsey read would switch a protection
    off for one who wrote nothing of the kind. Both directions are silent, so the value
    has to be a real boolean.
    """
    problem = mod._config_error({"argv_runner_detection": value})
    assert problem == "argv_runner_detection must be true or false"


@pytest.mark.parametrize("value", [True, False, None])
def test_a_boolean_or_absent_opt_out_is_valid_config(mod, value):
    """Both booleans and omission are accepted, so the check does not reject real use."""
    cfg = {} if value is None else {"argv_runner_detection": value}
    assert mod._config_error(cfg) is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="explicit-null"),
        pytest.param(0, id="zero"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
    ],
)
def test_a_value_that_is_not_false_never_disables_the_shape(mod, tmp_path, monkeypatch, value):
    """Only ``false`` disables it. Anything merely falsey leaves the protection ON.

    An explicit ``null`` is the live case: config validation treats it as "not set", so
    it reaches here, and a permissive truthiness read would switch the shape off for an
    operator who wrote nothing of the kind. The other values cannot pass validation, so
    for them this is the fail-safe direction held one layer deeper -- a caller reaching
    ``_host_lines`` without validating still keeps the protection.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "453", ["pytest-3", "test/"])
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "argv_runner_detection": value}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
    }
    assert reported == {"453"}, f"{value!r} disabled the shape without saying false"


def test_only_transparent_launchers_can_ever_carry_a_detection(mod):
    """The detector is BOUNDED: every recognised launcher is enumerated, and the set that
    can carry a detection is exactly the transparent one.

    This is the asymmetry expressed at the position check. The two error directions cost
    wildly different things -- a miss costs a signal, a false row costs a stopped worker
    and its discarded turn -- so the check answers from a closed list of shapes and
    anything unlisted is NOT DETECTED. A launcher added to ``_LAUNCHER_BASES`` therefore
    produces a miss, never a stop, until someone decides it execs what follows and adds it
    to ``_TRANSPARENT_LAUNCHER_BASES`` deliberately.

    Enumerated from source rather than from a written list, so the assertion cannot drift
    behind the launcher set it is about.
    """
    tails = (
        ["pytest-3", "test/"],
        ["run", "pytest-3", "test/"],
        ["-n", "1", "pytest-3", "test/"],
        ["900", "pytest-3", "test/"],
        ["CI=1", "pytest-3", "test/"],
        ["build", "release", "pytest-3", "test/"],
    )
    carriers = set()
    for base in sorted(mod._LAUNCHER_BASES):
        for tail in tails:
            argv = [base, *tail]
            indices = mod._argv_only_runner_indices(argv)
            if indices and mod._argv_is_uncapped_argv_only_runner(argv):
                carriers.add(base)
    assert carriers == set(mod._TRANSPARENT_LAUNCHER_BASES), (
        "a launcher outside the transparent set can carry a detection: "
        f"{sorted(carriers - set(mod._TRANSPARENT_LAUNCHER_BASES))}"
    )
    # The control: the enumeration really does reach a detection, so equality above is not
    # two empty sets agreeing.
    assert carriers, "no launcher carried a detection, the enumeration is broken"


def test_the_cap_reader_accepts_every_spelling_its_flags_have(mod):
    """The reader's COVERAGE, enumerated from ``_CAP_FLAGS`` rather than from the reader.

    The implication pin below asks "whatever the reader accepts is never reported", which
    is the asymmetry itself -- but it reads its own subject, so a reader that stops
    recognising a spelling simply drops out of it and the pin stays green while a capped
    run starts being reported. This asserts the other half: each flag's spellings are
    generated from the flag list, and the reader must accept every one of them.

    Glued digits are asserted only for a SHORT flag. The file documents why:
    ``--numprocessesN`` is not a spelling that option has, so a longer token starting with
    it is a different option and must not be read as a cap.
    """
    assert mod._CAP_FLAGS, "no cap flags to enumerate"
    for flag in sorted(mod._CAP_FLAGS):
        short = len(flag) == 2 and flag.startswith("-") and not flag.startswith("--")
        required = [[f"{flag}=0"], [flag, "0"], [f"{flag}=4"], [flag, "4"]]
        if short:
            required += [[f"{flag}0"], [f"{flag}4"]]
        for cap in required:
            assert mod._argv_declares_a_worker_cap(
                ["pytest-3", *cap, "test/"]
            ), f"the reader stopped recognising {' '.join(cap)!r} as a cap"
        if not short:
            glued = [f"{flag}0"]
            assert not mod._argv_declares_a_worker_cap(
                ["pytest-3", *glued, "test/"]
            ), f"{glued[0]!r} is a different option, not a cap spelling"


def test_a_cap_the_reader_accepts_is_never_reported(mod, tmp_path, monkeypatch):
    """THE ASYMMETRY, as an implication: cap reader says yes, so the scan stays silent.

    A false negative costs a signal. A false positive is a fleet-owned row, which is
    ``session_stop`` and a worker's discarded in-flight turn with no automatic recovery.
    So the one thing that must never happen is a row against a run that declared its
    worker count.

    The cap positions are enumerated FROM SOURCE -- every flag in ``_CAP_FLAGS``, every
    spelling around it -- and which of them count is decided by
    ``_argv_declares_a_worker_cap`` itself rather than by a list written here. Whatever
    that reader accepts, every detectable shape carrying it must produce no line.
    """
    candidates: list[list[str]] = []
    for flag in sorted(mod._CAP_FLAGS):
        candidates.append([f"{flag}0"])
        candidates.append([f"{flag}4"])
        candidates.append([f"{flag}=0"])
        candidates.append([f"{flag}=4"])
        candidates.append([flag, "0"])
        candidates.append([flag, "4"])
    accepted = [
        cap for cap in candidates if mod._argv_declares_a_worker_cap(["pytest-3", *cap, "test/"])
    ]
    assert accepted, "the cap reader accepted no spelling, so this pin proves nothing"

    prefixes = install_prefixes(tmp_path)
    shapes: dict[str, list[str]] = {
        name: with_prefixes(tokens, prefixes) for name, tokens in RUNNER_FORMS.items()
    }
    for name, (launcher, operands) in LAUNCHER_OWN_GRAMMAR.items():
        shapes[f"grammar-{name}"] = [*launcher, *operands, "pytest-3"]

    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    pid = 700
    for shape, prefix in sorted(shapes.items()):
        for cap in accepted:
            pid += 1
            fleet_pid(root, fleet, str(pid), [*prefix, *cap, "test/test_x.py"])
    assert banned_pids(mod, root, fleet) == set(), (
        f"a run declaring a cap was reported; {len(shapes)} shapes x {len(accepted)} "
        "accepted cap spellings must all stay silent"
    )


def test_every_directory_link_in_this_file_goes_through_make_dir_link():
    """A ``cwd`` link is a DIRECTORY name, so it must be a junction, not a symlink.

    This file's header states the split: a name meaning another directory is supplied by a
    junction on Windows with no privilege, while a link to a FILE needs
    ``SeCreateSymbolicLinkPrivilege`` and is inventoried by exact node id. A bare
    ``symlink_to`` on a ``cwd`` entry therefore does not skip on an unelevated Windows
    shell -- it raises, and every case built on that fixture ERRORS instead of running.

    Asserted over the file's own source because the rule is about which call is written,
    which no runtime behaviour on a POSIX host can reveal.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Built from parts so this scan's own condition is not a match for itself: written
    # whole, the line below would be the first offender it reported.
    directory_entry = '"' + "cwd" + '"'
    symlink_call = ".symlink" + "_to("
    offenders = [
        (number, line.strip())
        for number, line in enumerate(source.splitlines(), start=1)
        if directory_entry in line and symlink_call in line
    ]
    assert offenders == [], f"cwd links must use make_dir_link: {offenders}"
    # The control: the file really does contain directory links, so an empty offender
    # list means they are all correct rather than that the scan matched nothing.
    assert source.count("make_dir_link(entry / " + directory_entry) >= 10


def test_every_argv_only_spelling_is_also_a_recognised_runner_base(mod):
    """A spelling the argv path admits must also be one the CAP check can see.

    `_runner_token_index` keys on `_is_runner_base`, and `_argv_declares_a_worker_cap`
    declines to answer when no runner token stands alone. So a member of
    `_ARGV_ONLY_RUNNER_BASES` that `_is_runner_base` does not recognise is reported
    while its own `-n0` is invisible -- a CAPPED run drawing a stop, the most expensive
    direction this file has. The invariant is asserted over the whole set rather than
    one token, so adding a spelling cannot reopen it.
    """
    for base in sorted(mod._ARGV_ONLY_RUNNER_BASES):
        assert mod._is_runner_base(base), f"{base} is admitted but its cap cannot be read"


@pytest.mark.parametrize("runner", ["py.test", "py.test.exe", "pytest.exe"])
@pytest.mark.parametrize("cap", sorted(CAP_SPELLINGS))
def test_a_capped_argv_only_spelling_is_never_reported(mod, tmp_path, monkeypatch, runner, cap):
    """Every argv-only spelling honours a cap, through the same check as plain ``pytest``.

    This is the direction that destroys work: the run chose its worker count, and a row
    against it stops a worker doing exactly what the standing directive asks.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "441", [runner, *CAP_SPELLINGS[cap], "test/test_x.py"])
    assert banned_pids(mod, root, fleet) == set(), f"{runner} + {cap} was reported while capped"


def test_a_custom_rule_list_does_not_switch_off_the_argv_shape(mod, tmp_path, monkeypatch):
    """Rule ORIGIN carries built-in authority, not the absence of custom config.

    ``test_pipeline_conductor_agent.py`` already writes this down for the wrapper
    exemption: the gate is written against the rule that MATCHED rather than against
    ``cfg["banned_process_res"]`` being set at all. Standing the argv shape down whenever
    an operator supplies a list would let a config EDIT switch a built-in protection off,
    which is a worse property than one extra line on a replaced policy -- and the line it
    emits is factually true of the process, an uncapped alias run in a fleet worktree.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "430", ["pytest-3", "test/"])
    custom, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bnpm\b\s+audit"]}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in custom if line.startswith("BANNED pid=")
    }
    assert reported == {"430"}, "a custom rule list silenced the argv shape"
    # The same pid under the defaults, so the assertion above is about authority rather
    # than about the fixture happening to report everything.
    assert banned_pids(mod, root, fleet) == {"430"}


def test_a_custom_rule_list_still_reports_its_own_shape(mod, tmp_path, monkeypatch):
    """The operator's own rules keep working alongside the built-in shape.

    The two authorities are additive in effect even though the config REPLACES the
    pattern list: a custom rule reports what it names, and the argv shape reports what
    this file detects on its own authority.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "431", ["npm", "audit", "--json"])
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bnpm\b\s+audit"]}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
    }
    assert reported == {"431"}


@pytest.mark.parametrize(
    ("argv", "program"),
    [
        pytest.param(["pytest-3", "test/"], "pytest-<version>", id="alias"),
        pytest.param(["pytest-3.12", "test/"], "pytest-<version>", id="alias-minor"),
        pytest.param(["/usr/bin/pytest-3", "test/"], "pytest-<version>", id="alias-abspath"),
        pytest.param(["py.test-3", "test/"], "py.test-<version>", id="alias-py.test"),
        pytest.param(["pytest-3.exe", "test/"], "pytest-<version>", id="alias-exe"),
        pytest.param(["py.test", "test/"], "py.test", id="py.test"),
        pytest.param(["pytest.exe", "test/"], "pytest.exe", id="pytest.exe"),
    ],
)
def test_an_argv_row_prints_the_program_it_fired_on(mod, tmp_path, monkeypatch, argv, program):
    """``cmd=`` names the runner on an argv row, because ``rule=`` cannot.

    A joined-line row can be judged from its rule text. An argv row names a shape rather
    than a pattern, so the program name is the only field that separates a real uncapped
    run from a command that merely spells one -- and it is the field the owning skill
    says to read before stopping anyone.

    A versioned alias prints as a fixed LABEL rather than as itself, because the pattern
    admitting it is a shape and a shape cannot bound what follows its stem. The label says
    which stem fired, which is what the field is for, and carries none of the digits.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "440", argv)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1
    cmd = lines[0].split("cmd=", 1)[1].split()[0]
    assert cmd.split(",")[0] == program, f"cmd= withheld the program name: {cmd}"


@pytest.mark.parametrize(
    "secret",
    [
        "pytest-123456",
        "pytest-9",
        "pytest-0.0.0.0.1",
        "py.test-99999999",
        "pytest-123456.exe",
        "pytest-" + "1" * 40,
    ],
)
def test_no_digits_a_command_carried_ever_reach_the_printed_command(mod, secret):
    """A token the alias SHAPE admits is printed as a label, so its text cannot escape.

    The pattern admitting a versioned alias is anchored at both ends but open in the
    middle: it accepts ``pytest-`` followed by any digits, so a secret of that spelling
    satisfies it exactly as ``pytest-3.12`` does. The row lands in the conductor's model
    context, so echoing the token would carry the secret there.

    Each case is asserted to BE an alias by the pattern first, so a future narrowing of
    the pattern makes this test stop proving nothing rather than silently pass.
    """
    assert mod._ALIAS_RUNNER_BASE_RE.match(
        secret
    ), f"{secret} is not alias-shaped, so it proves nothing"
    cmd = f"pytest -q --token {secret} test/x.py"
    out = mod._redacted_command(cmd, (0, len(cmd)))
    digits = secret.partition("-")[2]
    assert digits not in out, f"the token's own text reached cmd=: {out}"
    assert mod._alias_program_label(secret) in out.split(","), f"no label stood in for it: {out}"


def test_every_alias_the_pattern_admits_has_a_label(mod):
    """A stem the pattern admits with no label would fall back to echoing the token.

    The label lookup and the pattern are two lists that must agree, so this asserts the
    agreement structurally rather than by naming today's two stems.
    """
    for stem, _label in mod._ALIAS_PROGRAM_LABELS:
        assert mod._ALIAS_RUNNER_BASE_RE.match(f"{stem}3"), f"{stem} is labelled but not admitted"
    for spelling in ("pytest-3", "py.test-3", "pytest-3.12.4", "py.test-12.0"):
        assert (
            mod._alias_program_label(spelling) is not None
        ), f"{spelling} is admitted but not labelled"
    # The lookup is gated on the PATTERN, not on the stem alone. Today's caller only
    # reaches it for a token the pattern already admitted, so this cannot be observed
    # through `cmd=`; it is asserted directly so a future caller admitting a wider set
    # cannot obtain a runner label for a word that is not a versioned alias.
    for not_an_alias in ("pytest-abc", "pytest-", "py.test-v3", "pytest-3x", "pytest"):
        assert mod._alias_program_label(not_an_alias) is None, f"{not_an_alias} got a runner label"


def test_vitest_is_left_to_its_own_rule_rather_than_a_pytest_cap_check(mod):
    """``vitest.cmd`` is a runner base and is deliberately not an argv-only shape.

    Its rule spells an uncapped run as ``vitest run`` with nothing following, not as a
    missing ``-n``. Admitting it here would route it through the pytest cap grammar,
    where a bounded vitest run carries no ``-n`` and would be reported as unbounded.
    """
    assert "vitest.cmd" in mod._RUNNER_BASES
    assert "vitest.cmd" not in mod._ARGV_ONLY_RUNNER_BASES
    assert not mod._ALIAS_RUNNER_BASE_RE.match("vitest.cmd")
    # The two spellings whose dot defeats the rule's token boundary ARE admitted, which
    # is the asymmetry this assertion fixes in place: the reason is the cap grammar, not
    # the spelling.
    assert {"py.test", "pytest.exe"} <= mod._ARGV_ONLY_RUNNER_BASES
