"""Fleet probe (``fleet_probe.py``): which argv token is the runner's program position.

Covers versioned aliases, installed entry points, launchers, python's script and module
positions, and ``argv[0]`` refuted by the kernel binary. Shared helpers are in
``pipeline_conductor_fleet_probe_helpers.py``.
"""

from __future__ import annotations

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import (
    BUDGETED_SPELLINGS,
    LAUNCHER_OWN_GRAMMAR,
    UNBUDGETED,
    banned_pids,
    fleet_pid,
    host_proc,
    install_prefixes,
    require_resolvable_prefixes,
    with_prefixes,
)


# The alias in a position that is NOT the program. A joined-line rule cannot tell these
# from an invocation, which is why the alias is detected on the argv side.
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

    The first token is already a program, so the alias is an argument.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "404", argv)
    assert banned_pids(mod, root, fleet) == set()


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


# What an interpreter is given when the thing it runs is a SCRIPT; the alias is the
# script's argument. The suffixless tails isolate the interpreter rule, since a `.py`
# name is also caught by the suffix rule.
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
    # These two isolate the POSITION guard: the argument IS an installed entry-point path,
    # so only "the candidate must be python's execution target" can decline it.
    "script-then-entry-point-path": ["worker.py", "{sys}/bin/pytest-3"],
    "script-then-venv-entry-point": ["harness.py", "{venv}/bin/pytest-3"],
}


# What an interpreter is given when the SCRIPT IT RUNS IS the runner. `/usr/bin/pytest-3`
# has a python shebang, so the kernel's argv is `python3 /usr/bin/pytest-3 …`.
# The flag tails pin that `-W ignore` is skipped: the first dashless token is not always
# the script.
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


# A script position whose path is a repository file, not an installed console script.
# The evidence is an interpreter in the same directory: ``checkout-bin-dir`` shows a
# directory named ``bin`` is not enough. Relative paths cannot be resolved, because the
# probe's working directory is not the scanned process's.
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

    The path is the only evidence, and a false report stops a healthy worker. The
    Windows ``Scripts`` case declines because ``_basename`` lowercases it into ``scripts/``.
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

    The prefixes are unalike on purpose; the rule names none of them. A ``--user``
    install has no interpreter beside its scripts, so it is a stated miss.
    """
    prefixes = install_prefixes(tmp_path)
    require_resolvable_prefixes(prefixes)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    script = f"{prefixes[prefix]}/bin/pytest-3"
    fleet_pid(root, fleet, "434", ["python3", script, *UNBUDGETED, "test/"])
    assert banned_pids(mod, root, fleet) == {
        "434"
    }, f"{prefix} entry point went unreported while unbudgeted"


def test_a_relative_script_path_is_not_resolved_against_the_probes_own_directory(
    mod, tmp_path, monkeypatch
):
    """A relative path belongs to the scanned process's directory, not to the probe's.

    The probe's working directory here holds ``bin/python3``, so resolving the path from
    it would report a healthy worker. Relative paths are refused outright.
    """
    prefixes = install_prefixes(tmp_path)
    require_resolvable_prefixes(prefixes)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    monkeypatch.chdir(prefixes["sys"])
    fleet_pid(root, fleet, "437", ["python3", "bin/pytest-3", "test/"])
    assert banned_pids(mod, root, fleet) == set(), (
        "a relative script path was resolved against the probe's own working directory, "
        "which is not the scanned process's"
    )


def test_an_installed_entry_point_needs_a_posix_absolute_path_on_any_host(mod, tmp_path):
    """A path the rule cannot resolve declines, and a drive letter is one of those.

    Only a Linux kernel writes ``/proc/<pid>/cmdline``. The expectation follows whether
    the path is POSIX-absolute, not the platform; the Windows spellings run on every host.
    """
    prefix = tmp_path / "usr"
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    installed = f"{str(prefix).replace(chr(92), '/')}/bin/pytest-3"
    resolvable = installed.startswith("/")
    assert mod._is_installed_entry_point(installed, "python3") is resolvable, (
        "a real interpreter sits beside this script, so the answer must follow only "
        f"whether the path is POSIX-absolute (it is {resolvable})"
    )
    for spelling in (
        "C:/wt/.venv/Scripts/pytest-3",
        "D:\\wt\\.venv\\Scripts\\pytest-3.exe",
    ):
        assert mod._is_installed_entry_point(spelling, "python3") is False, (
            f"{spelling!r} was accepted, but a drive-lettered path cannot come from the "
            "/proc cmdline this rule reads and cannot be resolved from the probe"
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
    require_resolvable_prefixes(prefixes)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    script = f"{prefixes['sys']}/bin/pytest-3"
    fleet_pid(root, fleet, "435", ["python3.13", script, *UNBUDGETED, "test/"])
    assert banned_pids(mod, root, fleet) == set(), (
        "python3.13 was accepted against a prefix that only installed python3.12, so the "
        "check is not asking for the interpreter argv[0] names"
    )
    fleet_pid(root, fleet, "436", ["python3.12", script, *UNBUDGETED, "test/"])
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
    """A BUDGETED run stays quiet when a token in front of it looks like the runner.

    Answering the position lookup with the assignment starts the count scan from there,
    so the launcher's own ``-n 10`` is read as the run's worker count and a healthy run
    draws a fleet-owned row -- a stopped worker and its discarded turn. The run here
    carries no ``-n`` of its own, so any count found is the launcher's.
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
    assert banned_pids(mod, root, fleet) == set(), (
        f"{value} + {launcher} was reported: the position lookup answered with the "
        "assignment, so the launcher's own option was read as the run's worker count"
    )


@pytest.mark.parametrize("launcher", sorted(LAUNCHER_OWN_CAP_SHAPED_OPTIONS))
@pytest.mark.parametrize("value", sorted(RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER))
def test_the_runners_own_count_is_still_read_behind_such_a_value(
    mod, tmp_path, monkeypatch, value, launcher
):
    """The other direction: moving the position forward must not lose the run's count.

    Skipping the assignment makes the count be read from the RUNNER's own arguments,
    which is where it belongs -- so a run that fixed its worker count is still reported
    behind the assignment and the launcher.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        "env",
        RUNNER_SHAPED_VALUES_BEFORE_THE_RUNNER[value],
        *LAUNCHER_OWN_CAP_SHAPED_OPTIONS[launcher],
        "pytest",
        *UNBUDGETED,
        "test/",
    ]
    fleet_pid(root, fleet, "439", argv)
    assert banned_pids(mod, root, fleet) == {
        "439"
    }, f"{value} + {launcher} went unreported despite the runner fixing its own count"


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
    and a genuine unbudgeted run two tokens later. Answering only the FIRST candidate
    would decide the pid on the assignment and never look at the runner, turning a fix
    for a false positive into a missed detection.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    argv = [*ASSIGNMENT_BEFORE_A_REAL_RUNNER, "pytest-3", *UNBUDGETED, "test/"]
    fleet_pid(root, fleet, "437", argv)
    assert banned_pids(mod, root, fleet) == {
        "437"
    }, "the runner behind the assignment went unreported"


@pytest.mark.parametrize("count", sorted(BUDGETED_SPELLINGS))
def test_a_runner_behind_an_assignment_still_stays_quiet_when_budgeted(
    mod, tmp_path, monkeypatch, count
):
    """The other direction: searching every candidate must not cost the budget reading.

    The count is read from the runner's own arguments, so it is asserted here through the
    same cross product of spellings as every other invocation form.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    argv = [*ASSIGNMENT_BEFORE_A_REAL_RUNNER, "pytest-3", *BUDGETED_SPELLINGS[count], "test/"]
    fleet_pid(root, fleet, "438", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"the runner behind the assignment was reported despite {count}"


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


@pytest.mark.parametrize("launcher", sorted(NON_TRANSPARENT_IMMEDIATE_OPERAND))
@pytest.mark.parametrize("runner", ["pytest-3", "pytest-3.12", "py.test-3"])
def test_a_non_transparent_launchers_immediate_operand_is_not_the_program(
    mod, tmp_path, monkeypatch, launcher, runner
):
    """`make pytest-3.12` names a TARGET, and that target runs a capped suite.

    A subject-taking launcher's immediate operand is never the program. Per-interpreter
    matrix targets are ordinary, and a false row stops a healthy worker.
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

    `timeout 900 pytest-3 -n4` is a real unbudgeted run. Declining these would hide
    exactly the invocations the probe exists to see, so the allow-list has to admit each
    one.
    """
    launcher, operands = LAUNCHER_OWN_GRAMMAR[shape]
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "423", [*launcher, *operands, runner, *UNBUDGETED, "test/"])
    assert banned_pids(mod, root, fleet) == {
        "423"
    }, f"{shape} + {runner} went unreported while unbudgeted"


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

    It runs a checkout-local ``pytest-3.py``. Declining costs only ``python -m py.test``,
    which modern pytest does not provide.
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
    fleet_pid(root, fleet, "422", [*PYTHON_SPELLINGS[python], "-m", "pytest", *UNBUDGETED, "test/"])
    assert banned_pids(mod, root, fleet) == {"422"}


@pytest.mark.parametrize("python", sorted(PYTHON_SPELLINGS))
@pytest.mark.parametrize("script", sorted(INTERPRETER_ENTRY_POINT_SCRIPTS))
def test_an_interpreter_reports_the_runner_standing_as_its_script_operand(
    mod, tmp_path, monkeypatch, python, script
):
    """The interpreter's SCRIPT is the program, so a runner standing there qualifies.

    A packaged ``pytest-3`` has a shebang, so the kernel's argv is ``python3
    /usr/bin/pytest-3 …`` and the alias never reaches ``argv[0]``.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    tail = with_prefixes(INTERPRETER_ENTRY_POINT_SCRIPTS[script], prefixes)
    argv = [*PYTHON_SPELLINGS[python], *tail, *UNBUDGETED, "test/"]
    fleet_pid(root, fleet, "426", argv)
    assert banned_pids(mod, root, fleet) == {
        "426"
    }, f"{python} + {script} went unreported while unbudgeted"


@pytest.mark.parametrize("count", sorted(BUDGETED_SPELLINGS))
@pytest.mark.parametrize("script", sorted(INTERPRETER_ENTRY_POINT_SCRIPTS))
def test_a_runner_as_the_script_operand_stays_quiet_when_budgeted(
    mod, tmp_path, monkeypatch, script, count
):
    """Admitting the script position must not cost the budget reading, in any spelling.

    The expensive direction of this whole file: a budgeted or single-process run is
    healthy, and reporting it stops a worker and discards its in-flight turn. The count
    is asked of the runner's own arguments, so it is read here through the same function
    that reads it for every other form.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        "python3",
        *with_prefixes(INTERPRETER_ENTRY_POINT_SCRIPTS[script], prefixes),
        *BUDGETED_SPELLINGS[count],
        "test/",
    ]
    fleet_pid(root, fleet, "427", argv)
    assert (
        banned_pids(mod, root, fleet) == set()
    ), f"{script} + {count} was reported despite being budgeted"


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

    A process can hide its own ``exe`` by going non-dumpable. Ownership is no safeguard:
    ``_owner_class`` falls back to ``_program_class``, which reads the same argv, so an
    ``argv[0]`` under a fleet worktree is classed ``fleet`` with no link at all.
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

    # Ownership derived from that same token.
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
    fleet_pid(root, fleet, "430", ["pytest-3", *UNBUDGETED, "test/"], unspoofed=False)
    (root / "430" / "exe").symlink_to("/home/u/wt/.venv/bin/pytest-3")
    assert banned_pids(mod, root, fleet) == {"430"}


def test_an_interpreter_kernel_binary_does_not_refute_a_shebang_runner(mod, tmp_path, monkeypatch):
    """A shebang run is decided one position later, so the refutation never sees it.

    So an interpreter is not exempted: an exemption at ``argv[0]`` would only admit
    ``exec -a pytest-3 python3 -c …``, which is the spoof.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    prefixes = install_prefixes(tmp_path)
    require_resolvable_prefixes(prefixes)
    script = f"{prefixes['sys']}/bin/pytest-3"
    fleet_pid(root, fleet, "431", ["python3", script, *UNBUDGETED, "test/"], unspoofed=False)
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

    The first selector (``-m``, ``-c``, ``-`` or a script) wins, so ``python3 worker.py
    -m pytest`` answers 1. An unknown option answers None, which declines. The KIND is
    returned because only a script path can be checked as an installed entry point.
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

    Only flags intervene, so this isolates the script-operand rule. A pid that lives long
    enough to scan is running something python can execute; a data file or a pytest temp
    root exits first.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    prefixes = install_prefixes(tmp_path)
    require_resolvable_prefixes(prefixes)
    argv = [
        *PYTHON_SPELLINGS[python],
        *flags,
        f"{prefixes['sys']}/bin/pytest-3",
        *UNBUDGETED,
        "test/",
    ]
    fleet_pid(root, fleet, "424", argv)
    assert banned_pids(mod, root, fleet) == {
        "424"
    }, f"{python} + {flags} went unreported, but the alias stands as the script"


def test_only_transparent_launchers_can_ever_carry_a_detection(mod):
    """Only a launcher in ``_TRANSPARENT_LAUNCHER_BASES`` can carry a detection.

    A miss costs a signal and a false row a stopped worker, so a new entry in
    ``_LAUNCHER_BASES`` yields a miss until it is added to the transparent set.
    """
    tails = (
        ["pytest-3", *UNBUDGETED, "test/"],
        ["run", "pytest-3", *UNBUDGETED, "test/"],
        ["-n", "1", "pytest-3", *UNBUDGETED, "test/"],
        ["900", "pytest-3", *UNBUDGETED, "test/"],
        ["CI=1", "pytest-3", *UNBUDGETED, "test/"],
        ["build", "release", "pytest-3", *UNBUDGETED, "test/"],
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
