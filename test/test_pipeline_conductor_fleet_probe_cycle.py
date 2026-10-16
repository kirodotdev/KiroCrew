"""Fleet probe (``fleet_probe.py``): one patrol cycle and its entry point.

Session keys as filename stems, a ``run_probe`` cycle and what it fires or suppresses,
recording a disposition with ``mark_handled``, typed misconfiguration, ``main``, and the
guard that no parametrize list reads the clock.

Shared fixtures and helpers are in ``pipeline_conductor_fleet_probe_helpers.py``;
``test_pipeline_conductor_fleet_probe.py`` heads the suite and states how it is organised.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import sessions_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import (
    KEY,
    SUITE_FILES,
    UNBUDGETED,
    host_proc,
    proc_pid,
    row,
    transcript,
)

from conftest import make_dir_link


@pytest.fixture
def empty_proc(tmp_path, monkeypatch):
    """A host with nothing running, so host posture never colours a probe test."""
    root = tmp_path / "proc-empty"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(root))
    return root


def fired_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("\U0001f514")]


def ok_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("OK "))


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
    # flake-ok: wall-clock mark far inside the idle window, pre-existing shape, moved verbatim; tracked for a de-flake pass
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
    # flake-ok: wall-clock mark an hour old, well past the 900 s idle window, pre-existing shape, moved verbatim; tracked for a de-flake pass
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
    # flake-ok: wall-clock mark far inside the idle window, pre-existing shape, moved verbatim; tracked for a de-flake pass
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
        # flake-ok: wall-clock mark an hour old, well past the 900 s idle window, pre-existing shape, moved verbatim; tracked for a de-flake pass
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
    entry = proc_pid(root, "107", ["pytest", "-q", *UNBUDGETED], starttime=50_000)
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
# Every mark this suite supplies is read at the moment it is compared
# --------------------------------------------------------------------------


def test_no_parametrize_argument_reads_the_clock() -> None:
    """A mark in this suite is always weighed against an elapsed-time window.

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
    nodes = [
        (path, node)
        for path in SUITE_FILES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    ]
    examined = 0
    for path, node in nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            if not (isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize"):
                continue
            examined += 1
            for arg in [*dec.args, *(kw.value for kw in dec.keywords)]:
                for inner in ast.walk(arg):
                    if not isinstance(inner, ast.Call):
                        continue
                    if not isinstance(inner.func, ast.Attribute):
                        continue
                    assert inner.func.attr not in reads, (
                        f"{path.name} line {inner.lineno}: {ast.unparse(inner)} is evaluated at "
                        f"collection time in the parametrize list of {node.name}"
                    )
    # The controls: this file is among the files read, no file of the suite fell out of the
    # glob, and the parametrize lists really were examined -- so a pass means they are clean,
    # not that the scan read nothing.
    assert Path(__file__).resolve() in SUITE_FILES
    assert len(SUITE_FILES) >= 8
    assert examined >= 40
