"""Fleet probe (``fleet_probe.py``): host posture, what counts as the fleet's problem.

Covers ``_host_lines``: which runs it reports, how ``cmd=`` withholds arguments, the cap
scan, and the load and memory readouts. Shared helpers are in
``pipeline_conductor_fleet_probe_helpers.py``.
"""

from __future__ import annotations

import ast
import collections
import re
from pathlib import Path

from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import (
    SCRIPT,
    SUITE_FILES,
    UNBUDGETED,
    host_proc,
    proc_pid,
)

from conftest import make_dir_link


def test_host_lines_reports_a_fleet_owned_unbounded_run(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    (fleet / "src").mkdir(parents=True)
    entry = proc_pid(root, "101", ["pytest", "-q", *UNBUDGETED], starttime=50_000)
    make_dir_link(entry / "cwd", fleet / "src")
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == [
        f"BANNED pid=101 rule={mod.DEFAULT_BANNED_RES[0]} cwd=fleet age=500s scope=suite "
        f"cmd=pytest,-q,-n"
    ]
    assert "banned 1 | foreign 0" in host


#: A worker's test step, as the one multi-line script a single ``cmdline`` carries:
#: a single-process run, then a read of the log that run wrote. Both lines name
#: ``pytest`` and only one of them is a command.
CAPPED_STEP = (
    "set -euo pipefail\n"
    'timeout 900 "$PY" -m pytest -n0 test/test_x.py -q > /wt/pytest.log 2>&1\n'
    'grep -E "^(FAILED|ERROR)| passed|failed" /wt/pytest.log | tail -2\n'
)


def test_a_pytest_filename_is_not_a_pytest_command(mod, tmp_path, monkeypatch):
    """A mention of the runner is not a run of it; only a whole shell token counts.

    ``\\bpytest\\b`` matches inside ``pytest.log`` and ``pytest-cov``, and a conductor
    gates intake on a zero banned count, so a false row withholds work. The loud rows
    pin that runs on different lines of one script are still told apart.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    quiet = {
        # The single-process run itself, both spellings, and the budgeted forms: the
        # gate runner's own ``-n auto`` and a bare pytest whose ``addopts`` supply it.
        "201": ["python", "-m", "pytest", "-n0", "test/test_x.py", "-q"],
        "202": ["pytest", "-n0", "test/test_x.py"],
        "216": ["python", "-m", "pytest", "-n", "auto", "--dist", "loadgroup", "test/"],
        "217": ["pytest", "test/test_x.py"],
        # A filename that merely carries the word.
        "203": ["grep", "-E", "^(FAILED|ERROR)| passed|failed", "/wt/pytest.log"],
        "204": ["tail", "-2", "/wt/pytest.log"],
        "205": ["pip", "install", "pytest-cov"],
        # The word as a DIRECTORY, where a path separator rather than a dot ends
        # the token: the command here is `grep`, and the runner names a folder.
        "210": ["grep", "-rn", "FAILED", "/wt/pytest/results.log"],
        "211": ["type", r"C:\wt\pytest\results.log"],
        # The word as the head of a packaging token, continued by `:`, `=` or `@`.
        "212": ["docker", "run", "pytest:latest"],
        "213": ["pip", "install", "pytest==7.4.0"],
        "214": ["conda", "install", "pytest=7.4"],
        "215": ["npm", "i", "pytest@1.2.3"],
        # The whole step: a single-process run and a log read in one argument.
        "206": ["bash", "-c", CAPPED_STEP],
        # A NUMBER on a later line belongs to that line's command. A lookahead widened
        # to the whole script text would read ``grep -n 5`` as the run's own count.
        "218": ["bash", "-c", CAPPED_STEP + "grep -n 5 /wt/pytest.log\n"],
    }
    loud = {
        # A run whose worker count bypasses the budget.
        "207": ["pytest", *UNBUDGETED, "test/test_x.py"],
        # The same step with an UNBUDGETED run beside it, in both orders. A count
        # belongs only to the command that carries it, not to a line above or below.
        "208": ["bash", "-c", "pytest -n 4 -q test/test_y.py\n" + CAPPED_STEP],
        "209": ["bash", "-c", CAPPED_STEP + "pytest -n 4 -q test/test_y.py\n"],
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
    """``cmd=`` makes a match judgeable and carries nothing that can be secret.

    Only names from a fixed vocabulary and recognised option names print, with every
    value dropped (worker-count digits included); the rest is counted.
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
            *UNBUDGETED,
        ],
        # An environment assignment in front of the command: the one place a secret
        # sits in the LEADING token, where a program name would otherwise print.
        "302": ["GITHUB_TOKEN=ghp-not-a-real-secret", "pytest", "test/test_x.py", *UNBUDGETED],
        # No flag keeps its value, the worker-count flag included, so `--maxfail=2`
        # prints as its name and `-n 97` prints as `-n` with the digits counted like
        # any other bare token.
        "303": ["pytest", "--maxfail=2", "-n", "97", "test/test_x.py"],
        # More flags than the field prints: the remainder is counted, never cut
        # silently.
        "304": ["pytest", *(f"-{letter}" for letter in "abcdefghij"), *UNBUDGETED],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=python,-m,pytest,--cov,-n,+3" in line["301"]
    assert "cmd=pytest,-n,+2" in line["302"]
    assert "cmd=pytest,--maxfail,-n,+2" in line["303"]
    assert "cmd=pytest,-a,-b,-c,-d,-e,-f,-g,+4" in line["304"]

    whole = "\n".join(lines)
    for secret in (
        "s3cr3t-value",
        "ghp-not-a-real-secret",
        "private-checkout",
        ".venv",
        "test/test_x.py",
        "97",
    ):
        assert secret not in whole, secret


def test_an_assignment_value_holding_a_separator_is_still_withheld(mod, tmp_path, monkeypatch):
    """The ``=`` is read on the whole token before the directory is dropped.

    Secrets often contain ``/``: the tail of ``KEY=…/dEf9gHi`` is ``dEf9gHi``, which
    looks like a program name and would print if the directory were stripped first.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        # Slash-bearing, the shape a separator-stripping check gets wrong.
        "311": ["OPAQUE_ONE=q7t/x2v/zPmKdR", "pytest", "test/test_x.py", *UNBUDGETED],
        # Backslash-bearing: the strip folds ``\`` to ``/`` first, so it is the
        # same hole spelled for the other platform.
        "312": [r"OPAQUE_TWO=abc\def\gHiJkL", "pytest", "test/test_x.py", *UNBUDGETED],
        # A separator-bearing value on a token that is NOT first, so neither the
        # program branch nor the flag branch may take it.
        "313": ["pytest", "TMPDIR=/wt/private-checkout/tmp", "test/test_x.py", *UNBUDGETED],
        # The case the ORDER alone decides: a value whose tail after the last
        # separator is itself a name the printable list carries, so dropping the
        # directory first hands the program branch a word it accepts.
        "314": [
            "SECRET_PATH=/wt/private-checkout/bin/pytest",
            "pytest",
            "test/test_x.py",
            *UNBUDGETED,
        ],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,-n,+2" in line["311"]
    assert "cmd=pytest,-n,+2" in line["312"]
    assert "cmd=pytest,-n,+2" in line["313"]
    assert "cmd=pytest,-n,+2" in line["314"]
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
    """A short option glues its value on, so only a bare two-character flag prints.

    ``-kname`` has no ``=`` to split at; the selector is as sensitive as any argument.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        "321": ["pytest", "-kMyCustomerName", "test/test_x.py", *UNBUDGETED],
        "322": ["pytest", "-k", "MyCustomerName", "test/test_x.py", *UNBUDGETED],
        "323": ["pytest", "-k=MyCustomerName", "test/test_x.py", *UNBUDGETED],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,-k,-n,+1" in line["321"]
    assert "cmd=pytest,-k,-n,+2" in line["322"]
    assert "cmd=pytest,-k,-n,+1" in line["323"]
    assert "MyCustomerName" not in "\n".join(lines)


def test_no_option_value_is_printed_not_even_the_cap_flags(mod, tmp_path, monkeypatch):
    """No flag keeps its value, the cap flag included.

    A custom rule can point the scan at any program, and nothing separates ``-n4``
    (four workers) from ``-n<account id>``, so the digits go too.
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

    No linter objects to rebinding a constant to the same value. The file is parsed,
    not imported, because import keeps only the surviving binding.
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
    """The clip is a backstop, so this proves no vocabulary entry reaches it.

    Every printable token is a two-character flag or a vocabulary entry, so no argv
    can reach the clip; checking the vocabularies catches a longer entry being added.
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
    entry = proc_pid(root, "341", ["pytest", long_flag, *UNBUDGETED], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1, lines
    printed = lines[0].split("cmd=")[1].split()[0]
    assert printed == f"pytest,{long_flag[: mod._MAX_CMD_TOKEN_CHARS]}~,-n"
    assert len(printed) < len(long_flag)


def test_a_launchers_own_option_cannot_supply_the_runners_count(mod, tmp_path, monkeypatch):
    """A launcher's number is not the run's worker count, in either direction.

    ``nice -n`` and ``xvfb-run -n`` also spell a number with ``-n``, so the scan starts
    after the runner's own token; a count the runner carries is still read.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    quiet = {
        # The launcher's own numeric option, in each spelling, on a BUDGETED run.
        "391": ["nice", "-n", "10", "pytest", "test/"],
        "392": ["nice", "-n19", "python", "-m", "pytest", "test/"],
        "393": ["xvfb-run", "-n", "99", "pytest", "test/"],
        # The launcher carries one AND the runner carries a budgeted one.
        "394": ["nice", "-n", "5", "pytest", "-n", "auto", "test/"],
        # The launcher carries one AND the runner runs single-process.
        "395": ["nice", "-n", "10", "pytest", "-n0", "test/"],
    }
    loud = {
        # The RUNNER's own count, behind the same launcher: genuinely unbudgeted.
        "396": ["nice", "-n19", "python", "-m", "pytest", "--numprocesses=4", "test/"],
        "397": ["xvfb-run", "-n", "99", "pytest", "-n", "8", "test/"],
        # And behind a launcher whose own number is priority 1 -- a value that would
        # read as single-process if the scan started at argv[0].
        "398": ["nice", "-n", "1", "pytest", "-n", "4", "test/"],
    }
    for pid, argv in {**loud, **quiet}.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    reported = {ln.split("pid=")[1].split()[0] for ln in lines}

    assert set(loud) <= reported, sorted(set(loud) - reported)
    assert reported & set(quiet) == set(), sorted(reported & set(quiet))

    # Called directly: an argv with no standalone runner token cannot reach this guard
    # through ``_host_lines``. It keeps the answer conservative (not budgeted).
    assert mod._argv_worker_pool_is_budgeted(["pytest", "-n0"]) is True
    assert mod._argv_worker_pool_is_budgeted(["nice", "-n", "10", "pytest"]) is True
    assert mod._argv_worker_pool_is_budgeted(["nice", "-n", "10", "pytest", "-n", "4"]) is False
    assert mod._argv_worker_pool_is_budgeted(["nice", "-n", "10"]) is False


def test_the_cap_scan_does_not_backtrack_over_bracketed_spans(mod):
    """The scan's first branch must not match a span opener, or k spans have 2**k parses.

    A budgeted ``-n auto`` rerun naming dozens of node ids would stall the probe. This
    checks the disjointness, not a stopwatch, because timing on a shared runner flakes.
    """
    # The first branch is a negated character class; splitting the pattern on ``|``
    # would cut inside it, so the class is extracted and compiled instead.
    leading = re.match(r"\(\?:(\[\^[^\]]*\])", mod._CAP_SCAN)
    assert leading, mod._CAP_SCAN
    first_branch = re.compile(leading.group(1))
    for opener in ("[", "'", '"'):
        assert not first_branch.match(opener), (opener, leading.group(1))

    # And the cheap end-to-end check: a many-span argv still resolves, whether the walk
    # ends with no count to find (the budgeted forms, which stay quiet) or has to cross
    # every span to reach the count at the very end (reported).
    rule = re.compile(mod.DEFAULT_BANNED_RES[0])
    spans = " ".join(f"a.py::t[c{i}]" for i in range(40))
    assert rule.search(f"pytest -n auto {spans}") is None
    assert rule.search(f"pytest -n0 {spans}") is None
    assert rule.search(f"pytest {spans}") is None
    assert rule.search(f"pytest {spans} -n 4") is not None


def test_a_separator_inside_one_argument_does_not_hide_the_runs_own_count(
    mod, tmp_path, monkeypatch
):
    """``/proc`` gives NUL-separated arguments, so a metacharacter in one is not syntax.

    The scan crosses a bracketed or quoted span whole; an unquoted separator in shell
    text still ends the command.

    Known miss: an unquoted separator in an option value (``--log-format=%(a)s|%(b)s``)
    before the run's own count. The rows under ``missed`` pin that residual.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    quiet = {
        # A pipe and a semicolon inside a node id, on a single-process run.
        "381": ["pytest", "test/test_x.py::test_y[a|b]", "-n0"],
        "382": ["pytest", "test/test_x.py::test_y[a;b]", "-n0"],
        # The same inside a quoted argument, on a budgeted run.
        "383": ["pytest", "'test/test_x.py::test_y[a|b]'", "--numprocesses=auto"],
        # Single-process BEFORE the bracketed separator rather than after it.
        "384": ["pytest", "-n0", "test/test_x.py::test_y[a|b]", "--cov", "src"],
        # A separator in an option value, on a budgeted run: quiet for the right reason
        # (the run IS budgeted), whichever way the scan reads the ``|``.
        "387": ["pytest", "--log-format=%(levelname)s|%(message)s", "-n", "auto"],
        # A real pipe: the number after it belongs to the NEXT command.
        "385": ["bash", "-c", "pytest test/test_x.py | grep -n 4 out.log"],
        # A bracketed separator must not let a LATER command's count reach back.
        "386": ["bash", "-c", "pytest test/test_x.py::test_y[a|b];grep -n 4 g.log"],
    }
    loud = {
        # The run's own count behind a bracketed separator, both flag spellings.
        "390": ["pytest", "test/test_x.py::test_y[a|b]", "-n", "8"],
        "389": ["pytest", "--deselect", "f.py::t[a|b]", "--numprocesses", "2"],
        # And behind a quoted one.
        "391": ["pytest", "'test/test_x.py::test_y[a|b]'", "--numprocesses=4"],
        # A real pipe with the count BEFORE it: the first command is unbudgeted.
        "392": ["bash", "-c", "pytest -n 4 test/test_x.py | tee out.log"],
    }
    missed = {
        # The disclosed residual: an unquoted separator in an option value, then the
        # count. Asserted as quiet so a change in this behaviour is noticed, not so the
        # behaviour is endorsed.
        "388": ["pytest", "-k", "not slow&not flaky", "-n", "4"],
        "393": ["pytest", "--log-format=%(levelname)s|%(message)s", "-n", "8"],
    }
    for pid, argv in {**quiet, **loud, **missed}.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    reported = {ln.split("pid=")[1].split()[0] for ln in lines}

    assert reported & set(quiet) == set(), sorted(reported & set(quiet))
    assert set(loud) <= reported, sorted(set(loud) - reported)
    assert reported & set(missed) == set(), sorted(reported & set(missed))


def test_a_recognised_name_prints_its_vocabulary_entry_not_the_token(mod, tmp_path, monkeypatch):
    """A recognised name prints as its vocabulary entry, not the token's own casing.

    Vocabulary matching is case-insensitive, so the casing is the only caller-chosen
    text left. Each fixture also carries a lowercase invocation because the RULE is
    case-sensitive.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = {
        # A runner name in an OPERAND slot, in a casing the command chose.
        "371": ["pytest", "--password", "PyTeSt", "test/test_x.py", *UNBUDGETED],
        # A recognised launcher and a Windows-cased runner, both path-qualified.
        "372": [
            "/wt/private-checkout/.venv/bin/PYTHON3.12",
            "-m",
            "pytest",
            "PyTest.EXE",
            "f.py",
            *UNBUDGETED,
        ],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,pytest,-n,+2" in line["371"]
    assert "cmd=python3.12,-m,pytest,pytest.exe,-n,+1" in line["372"]

    whole = "\n".join(lines)
    for chosen in ("PyTeSt", "PYTHON3.12", "PyTest.EXE", "--password"):
        assert chosen not in whole, chosen


def test_an_opaque_word_in_the_program_position_is_withheld(mod, tmp_path, monkeypatch):
    """A program NAME and an opaque credential are the same text, so a shape cannot sort them.

    The leading token prints only if its basename is on a fixed list. The venv pids pin
    that the directory a runner sits in does not blank the field.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    opaque = "q7TkV2xPmKdR9sLbN4zH"
    cases = {
        # Program-shaped by every test a shape can apply, and not a program.
        "351": [opaque, "pytest", "test/test_x.py", *UNBUDGETED],
        # A recognised launcher, version and all, out of a private venv.
        "352": [
            "/wt/private-checkout/.venv/bin/python3.12",
            "-m",
            "pytest",
            "test/test_x.py",
            *UNBUDGETED,
        ],
        # The runner itself out of a venv: the case a path-derived name would blank.
        "353": ["/wt/private-checkout/.venv/bin/pytest", "test/test_x.py", *UNBUDGETED],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,-n,+2" in line["351"]
    assert "cmd=python3.12,-m,pytest,-n,+1" in line["352"]
    assert "cmd=pytest,-n,+1" in line["353"]

    whole = "\n".join(lines)
    assert opaque not in whole
    assert "private-checkout" not in whole


def test_an_opaque_word_spelled_as_a_long_option_is_withheld(mod, tmp_path, monkeypatch):
    """``--`` and letters is a well-formed option name AND a well-formed credential.

    So only a fixed list of long option names prints; an unrecognised flag is counted.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    opaque = "q7TkV2xPmKdR9sLbN4zH"
    cases = {
        # The secret wearing an option's spelling, bare and with a value.
        "361": ["pytest", f"--{opaque}", "test/test_x.py", *UNBUDGETED],
        "362": ["pytest", f"--{opaque}=1", "test/test_x.py", *UNBUDGETED],
        # A recognised concurrency flag still prints, with its value dropped.
        "363": ["pytest", "--dist=loadscope", "test/test_x.py", *UNBUDGETED],
    }
    for pid, argv in cases.items():
        entry = proc_pid(root, pid, argv, starttime=50_000)
        make_dir_link(entry / "cwd", fleet)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    line = {ln.split("pid=")[1].split()[0]: ln for ln in lines}
    assert set(line) == set(cases), lines

    assert "cmd=pytest,-n,+2" in line["361"]
    assert "cmd=pytest,-n,+2" in line["362"]
    assert "cmd=pytest,--dist,-n,+1" in line["363"]

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
    entry = proc_pid(root, "102", ["pytest", "-q", *UNBUDGETED], starttime=50_000)
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


# --- the vitest rule: an invocation, never a mention ------------------------
#
# Pinned as conditions over a generated space, because any command whose text ends
# in the phrase is a false stop. Cases are stored NUL-separated as the kernel stores
# them, since the joined string alone cannot tell a mention from a run.

#: Programs that carry the phrase as DATA. None of them is a test runner, and each
#: is a thing someone maintaining these rules actually runs.
_PHRASE_CARRIERS = (
    ["grep", "-rn"],
    ["rg", "--files-with-matches"],
    ["cat"],
    ["echo"],
)


#: Ways ONE argument can hold the phrase: alone, behind another command's words,
#: and as the tail of a longer word. All three are one argv entry, which is what
#: separates them from an invocation and what the joined text erases.
_PHRASE_FORMS = (
    "vitest run",
    "docker run vitest run",
    "my-vitest run",
    "./vitest run",
)


#: How a real vitest reaches a cmdline, including the qualified shim path.
_VITEST_PROGRAMS = ("vitest", "node_modules/.bin/vitest", "/wt/node_modules/.bin/vitest")


#: What stands in front of the program when something launches it.
_VITEST_LAUNCHERS = ([], ["npx"], ["pnpm"], ["yarn"], ["npx", "--yes"])


def vitest_case(root, fleet, pid: str, argv: list[str]):
    """One /proc entry for *argv*, owned by the fleet so its row would be printed."""
    entry = proc_pid(root, pid, argv, starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    return entry


def test_a_command_carrying_the_phrase_as_text_is_never_a_banned_run(mod, tmp_path, monkeypatch):
    """The condition: the phrase inside ONE argument names no run, however carried."""
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = [carrier + [form] for carrier in _PHRASE_CARRIERS for form in _PHRASE_FORMS]
    for offset, argv in enumerate(cases):
        # Not vacuous: the rule's PATTERN still selects every one of these, so a
        # quiet answer can only come from the argv decision. Without this guard a
        # case that merely stopped matching would pass while proving nothing.
        assert re.search(mod._VITEST_BANNED_RE, " ".join(argv)), argv
        vitest_case(root, fleet, str(200 + offset), argv)
    lines, host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == []
    assert "banned 0 | foreign 0" in host


def test_a_vitest_run_that_names_a_target_is_never_a_banned_run(mod, tmp_path, monkeypatch):
    """The condition: a run with a file argument is scoped, whatever spells it."""
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    targets = (["test/a.test.ts"], ["packages/ui/test/b.spec.ts"], ["test/a.test.ts", "-t", "x"])
    cases = [
        launcher + [program, "run"] + target
        for launcher in _VITEST_LAUNCHERS
        for program in _VITEST_PROGRAMS
        for target in targets
    ]
    for offset, argv in enumerate(cases):
        vitest_case(root, fleet, str(300 + offset), argv)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert lines == []


def test_a_bare_whole_suite_vitest_run_stays_banned(mod, tmp_path, monkeypatch):
    """The loud half of the same condition: no file argument is a whole-suite run."""
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    cases = [
        launcher + [program, "run"]
        for launcher in _VITEST_LAUNCHERS
        for program in _VITEST_PROGRAMS
    ]
    for offset, argv in enumerate(cases):
        vitest_case(root, fleet, str(400 + offset), argv)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == len(cases)
    assert all(mod._VITEST_BANNED_RE in line for line in lines)


def test_the_argv_decision_only_ever_narrows_the_pattern(mod):
    """Nothing this rule reports was quiet under the pattern alone.

    The argv decision is a proper SUBSET of the pattern's, so it can only remove stops.
    """
    space = [
        launcher + [program, "run"] + tail
        for launcher in _VITEST_LAUNCHERS
        for program in _VITEST_PROGRAMS + ("my-vitest", "vitest.cmd", "notvitest")
        for tail in ([], ["test/a.test.ts"], ["--reporter=dot"])
    ] + [carrier + [form] for carrier in _PHRASE_CARRIERS for form in _PHRASE_FORMS]
    narrower = 0
    for argv in space:
        if mod._invokes_bare_vitest_run(argv):
            assert re.search(mod._VITEST_BANNED_RE, " ".join(argv)), argv
        elif re.search(mod._VITEST_BANNED_RE, " ".join(argv)):
            narrower += 1
    # The subset is PROPER, so the assertion above is not passing on an empty set.
    assert narrower > 0


def test_the_argv_decision_states_the_whole_shape_it_requires(mod):
    """The predicate holds its own contract, not one borrowed from the pattern.

    The last two cases cannot arrive through ``_host_lines`` (the pattern's anchor
    filters them), but retuning the pattern would expose them.
    """
    # The program, its subcommand, nothing else -- however the program is qualified.
    assert mod._invokes_bare_vitest_run(["npx", "vitest", "run"])
    assert mod._invokes_bare_vitest_run(["/wt/node_modules/.bin/vitest", "run"])
    # The name has to be the program, not a word ending in it, and not a phrase
    # carried inside one argument.
    assert not mod._invokes_bare_vitest_run(["my-vitest", "run"])
    assert not mod._invokes_bare_vitest_run(["vitest run"])
    assert not mod._invokes_bare_vitest_run(["run"])
    # ``run`` has to be the LAST token, and adjacent to the program: a command that
    # merely carries the word somewhere names no bare run.
    assert not mod._invokes_bare_vitest_run(["vitest", "run", "test/a.test.ts"])
    assert not mod._invokes_bare_vitest_run(["npm", "run", "test:unit", "--", "vitest", "-w"])


def test_a_custom_rule_keeps_its_joined_text_decision(mod, tmp_path, monkeypatch):
    """An operator's own pattern keeps the joined-text decision; only the built-in narrows.

    A rule is identified by its pattern TEXT, so a verbatim copy of the built-in
    pattern is narrowed with it.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "500", ["grep", "-rn", "vitest run"], starttime=50_000)
    make_dir_link(entry / "cwd", fleet)
    operator_rule = r"vitest\s+run\s*$"
    assert operator_rule not in mod.DEFAULT_BANNED_RES
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [operator_rule]}
    )
    assert len(lines) == 1
    assert "pid=500" in lines[0]


def test_host_lines_drops_the_age_when_the_pid_is_recycled_mid_scan(mod, tmp_path, monkeypatch):
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    entry = proc_pid(root, "105", ["pytest", "-q", *UNBUDGETED], starttime=50_000)
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
    entry = proc_pid(root, "109", ["pytest", "-q", *UNBUDGETED], starttime=50_000)
    make_dir_link(entry / "cwd", tmp_path)
    lines, host = mod._host_lines({})
    assert [line.split()[1] for line in lines] == ["pid=109"]
    assert "banned 1" in host


def test_host_lines_without_a_proc_filesystem(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "absent"))
    lines, host = mod._host_lines({})
    assert lines == []
    assert "mem n/a" in host
    # The scan never ran, so its counts are unknown: a 0 would read as a scan
    # that ran and found nothing.
    assert host.endswith("| banned n/a | foreign n/a"), host
    assert "banned 0" not in host and "foreign 0" not in host


def test_host_lines_counts_zero_when_an_empty_proc_was_scanned(mod, tmp_path, monkeypatch):
    """The other side of the unknown: a proc root that exists and holds no
    process is a real measurement, and prints 0."""
    root = tmp_path / "empty-proc"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(root))
    lines, host = mod._host_lines({})
    assert lines == []
    assert host.endswith("| banned 0 | foreign 0"), host


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


def test_every_directory_link_in_this_file_goes_through_make_dir_link():
    """A ``cwd`` link is a DIRECTORY name, so it must be a junction, not a symlink.

    On an unelevated Windows shell a bare ``symlink_to`` raises, so every case built on
    it errors. Checked in the source because a POSIX run cannot reveal it.
    """
    sources = {path.name: path.read_text(encoding="utf-8") for path in SUITE_FILES}
    # Built from parts so this scan's own condition is not a match for itself: written
    # whole, the line below would be the first offender it reported.
    directory_entry = '"' + "cwd" + '"'
    symlink_call = ".symlink" + "_to("
    offenders = [
        (name, number, line.strip())
        for name, source in sources.items()
        for number, line in enumerate(source.splitlines(), start=1)
        if directory_entry in line and symlink_call in line
    ]
    assert offenders == [], f"cwd links must use make_dir_link: {offenders}"
    # The control: the suite really does contain directory links, so an empty offender
    # list means they are all correct rather than that the scan matched nothing.
    linked = sum(
        text.count("make_dir_link(entry / " + directory_entry) for text in sources.values()
    )
    assert linked >= 10
    # The suite controls: this file is among the files read, and no file fell out of the glob.
    assert Path(__file__).resolve() in SUITE_FILES
    assert len(SUITE_FILES) >= 8
