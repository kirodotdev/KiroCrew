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

import json
import os
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
        f"BANNED pid=101 rule={mod.DEFAULT_BANNED_RES[0]} cwd=fleet age=500s scope=suite"
    ]
    assert "banned 1 | foreign 0" in host


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
