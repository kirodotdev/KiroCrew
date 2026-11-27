"""The lanes that feed Coverage Combine choose one coverage core, and prove it held.

``scripts/ci/coverage_core.py`` decides, from the run's event, whether a lane
measures with coverage.py's ``sysmon`` core and without branch arcs. Three things
are pinned here.

* The decision and its reading by coverage.py. A switching event exports both
  variables with values coverage.py accepts from the repository's own
  ``setup.cfg``; any other event exports nothing, and ``setup.cfg`` then records
  branch arcs. An empty ``KC_COV_BRANCH`` is refused by coverage.py itself.
* Fail closed. The check refuses a missing or empty log, an environment that does
  not carry what the event asks for, and coverage.py's own fallback warning.
* The kill switch. The repository variable set to ``off`` makes every event
  export nothing, so a pull-request run measures as a push does; unset, or any
  other value, changes nothing. Every measuring step hands the variable to the
  script.
* The workflow. Every job whose data Coverage Combine downloads makes the same
  ``exports`` call, pipes its measured pytest through ``tee`` under ``pipefail``
  and runs the check on that log; no ``env:`` block sets either variable. Each
  lane's own step body then runs under bash, with a shell function standing in
  for pytest, to show the exit status and the environment the wiring produces.

The lanes are read from the workflow: a job is one when it uploads an artifact
that Coverage Combine downloads.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

import coverage
import pytest
import yaml
from coverage.exceptions import ConfigError, CoverageWarning

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts.ci import coverage_core

ROOT = Path(__file__).resolve().parents[1]
SETUP_CFG = ROOT / "setup.cfg"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
# PyYAML reads the workflow's bare `on` key as the boolean True.
TRIGGERS = sorted(WORKFLOW.get("on") or WORKFLOW[True])
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
SCRIPT = "scripts/ci/coverage_core.py"


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"actions/{action}@")


def _combine_downloads() -> list[str]:
    """The artifact names and patterns Coverage Combine downloads."""
    return [
        str(step["with"].get("pattern") or step["with"]["name"])
        for step in JOBS["coverage-combine"]["steps"]
        if _uses(step, "download-artifact")
    ]


def _lanes() -> dict[str, dict]:
    """Every job that uploads an artifact Coverage Combine downloads."""
    wanted = _combine_downloads()
    lanes = {}
    for job_id, job in JOBS.items():
        for step in job["steps"]:
            if not _uses(step, "upload-artifact"):
                continue
            name = EXPRESSION.sub("1", str(step["with"]["name"]))
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in wanted):
                lanes[job_id] = job
    return lanes


LANES = _lanes()


def _core(step: dict) -> str:
    """The core a measuring step asks ``exports`` for: ``sysmon`` unless it names another."""
    named = re.search(rf"{re.escape(SCRIPT)} exports --core (\w+)", str(step["run"]))
    return named.group(1) if named else "sysmon"


def _coverage_steps(job: dict) -> list[dict]:
    """Every step of the lane whose commands carry a ``--cov`` selector."""
    return [s for s in job["steps"] if "--cov=" in str(s.get("run", ""))]


def _delegates(step: dict) -> bool:
    """Whether the step's measured pytest is started by another script, not by its shell."""
    return not _measured_pytest(_commands(str(step["run"])))


def _measuring_steps(job: dict, core: str | None = None) -> list[dict]:
    """The lane's steps whose own shell runs the measured pytest, for one core or all."""
    steps = [s for s in _coverage_steps(job) if not _delegates(s)]
    return [s for s in steps if core is None or _core(s) == core]


def _measuring_step(job: dict) -> dict:
    """The lane's one step that measures with the ``sysmon`` switch."""
    (step,) = _measuring_steps(job, "sysmon")
    return step


def _steps() -> dict[str, dict]:
    """Every measuring step of every lane, named ``<job>:<core>``."""
    found: dict[str, dict] = {}
    for job_id, job in LANES.items():
        for step in _measuring_steps(job):
            name = f"{job_id}:{_core(step)}"
            assert name not in found, f"two {name} steps: name them apart before testing them"
            found[name] = step
    return found


def _flag(core: str) -> str:
    return "" if core == "sysmon" else f" --core {core}"


def _marker_expression(line: str) -> str:
    """The ``-m`` expression of a pytest command line, or ``""``."""
    quoted = re.search(r'-m "([^"]+)"', line)
    bare = re.search(r"-m (\w+)", line)
    return quoted.group(1) if quoted else bare.group(1) if bare else ""


def _commands(run: str) -> list[str]:
    """The step's commands, with backslash continuations joined."""
    return [line.strip() for line in run.replace("\\\n", " ").splitlines()]


def _measured_pytest(commands: list[str]) -> list[str]:
    """The pytest command lines that can carry a ``--cov`` selector."""
    return [
        line
        for line in commands
        if re.search(r"\bpython -m pytest\b", line) and ("--cov=" in line or "COVERAGE[@]" in line)
    ]


STEPS = _steps()


def _env_blocks(node: object):
    """Every ``env:`` mapping anywhere in the workflow document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                yield value
            yield from _env_blocks(value)
    elif isinstance(node, list):
        for item in node:
            yield from _env_blocks(item)


def _option(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], name: str):
    """What coverage.py reads for ``name`` from the repository's ``setup.cfg``."""
    for variable in coverage_core.SWITCH_ENV:
        monkeypatch.delenv(variable, raising=False)
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)
    return coverage.Coverage(config_file=str(SETUP_CFG), data_file=None).get_option(name)


def _exported(event: str, core: str = "sysmon", switch: str = "") -> dict[str, str]:
    """The variables the ``exports`` line for ``event`` and ``core`` sets, parsed back."""
    line = coverage_core.exports(event, core, switch)
    if not line:
        return {}
    command, *assignments = line.split()
    assert command == "export"
    return dict(assignment.split("=", 1) for assignment in assignments)


# ── The decision, and how coverage.py reads it ─────────────────────────────


def test_the_workflow_has_events_on_both_sides_of_the_switch():
    switching = [event for event in TRIGGERS if coverage_core.switches(event)]
    assert switching and len(switching) < len(TRIGGERS), TRIGGERS
    assert not coverage_core.switches("push"), "a push to main records branch arcs"
    assert coverage_core.SWITCH_EVENTS <= set(TRIGGERS), "an event the workflow never runs on"


@pytest.mark.parametrize("event", [*TRIGGERS, "", "schedule", "workflow_dispatch"])
def test_an_event_exports_both_variables_or_neither(event, monkeypatch):
    exported = _exported(event)
    assert exported == (dict(coverage_core.SWITCH_ENV) if coverage_core.switches(event) else {})
    assert "" not in exported.values(), "an empty value stops coverage.py"
    branch = _option(monkeypatch, exported, "run:branch")
    assert branch is not coverage_core.switches(event)
    core = _option(monkeypatch, exported, "run:core")
    assert core == ("sysmon" if coverage_core.switches(event) else None)


def test_setup_cfg_records_branch_arcs_when_the_variable_is_unset(monkeypatch):
    assert _option(monkeypatch, {}, "run:branch") is True


@pytest.mark.parametrize("event", TRIGGERS)
def test_the_c_tracer_step_records_what_the_lane_records_and_names_no_core(event, monkeypatch):
    """Lines on a switching event and arcs otherwise, so its data appends to the lane's."""
    exported = _exported(event, "ctrace")
    assert set(exported) <= set(_exported(event)) and "" not in exported.values()
    assert _option(monkeypatch, exported, "run:core") is None
    for name in ("run:branch",):
        assert _option(monkeypatch, exported, name) == _option(monkeypatch, _exported(event), name)


@pytest.mark.parametrize("value", ["", " ", "sysmon", "0 1"])
def test_coverage_refuses_a_value_that_is_no_boolean(value, monkeypatch):
    with pytest.raises(ConfigError, match="Not a boolean"):
        _option(monkeypatch, {"KC_COV_BRANCH": value}, "run:branch")


@pytest.mark.skipif(
    sys.version_info[:2] not in ((3, 12), (3, 13)),
    reason="the sysmon core records branch arcs from CPython 3.14 on",
)
@pytest.mark.parametrize("exported", [True, False])
def test_coverage_names_the_fallback_exactly_when_branch_arcs_are_asked_for(exported, monkeypatch):
    """coverage.py's own core choice: sysmon with lines only, a marked warning with arcs."""
    from coverage.core import Core

    for variable in coverage_core.SWITCH_ENV:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("COVERAGE_CORE", coverage_core.SWITCH_ENV["COVERAGE_CORE"])
    if exported:
        monkeypatch.setenv("KC_COV_BRANCH", coverage_core.SWITCH_ENV["KC_COV_BRANCH"])
    cov = coverage.Coverage(config_file=str(SETUP_CFG), data_file=None)
    warned: list[str] = []

    def warn(message: str, slug: str | None = None, once: bool = False) -> None:
        warned.append(f"{message} ({slug})")

    core = Core(warn=warn, config=cov.config, dynamic_contexts=False, metacov=False)
    if exported:
        assert core.tracer_class.__name__ == "SysMonitor" and not warned
    else:
        assert core.tracer_class.__name__ != "SysMonitor"
        assert [coverage_core.FALLBACK_MARK in message for message in warned] == [True]
    # The mark is the slug in the parentheses coverage.py's warning ends with.
    with pytest.warns(CoverageWarning) as caught:
        cov._warn("probe", slug=coverage_core.FALLBACK_MARK.strip("()"))
    assert str(caught[0].message).endswith(coverage_core.FALLBACK_MARK)


# ── The check fails closed ─────────────────────────────────────────────────

CLEAN = "12 passed in 3.4s\n"
FELL_BACK = f"CoverageWarning: another core measured {coverage_core.FALLBACK_MARK}\n{CLEAN}"
SWITCHED = dict(coverage_core.SWITCH_ENV)


CORES = sorted(coverage_core.CORE_ENV)


def _refusal(
    tmp_path: Path, event: str, environ: dict[str, str], text: str | None, core: str = "sysmon"
) -> str:
    log = tmp_path / "pytest.log"
    if text is not None:
        log.write_text(text, encoding="utf-8")
    return coverage_core.refusal(event, environ, log, core)


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_clean_log_passes_with_exactly_what_the_event_exports(event, core, tmp_path):
    exported = _exported(event, core)
    assert _refusal(tmp_path, event, exported, CLEAN, core) == ""
    assert _refusal(tmp_path, event, {**exported, "PATH": "/bin"}, CLEAN, core) == ""


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
@pytest.mark.parametrize("text", [None, "", " \n\t\n"])
def test_a_missing_or_empty_log_is_refused_for_every_event(event, core, text, tmp_path):
    reason = _refusal(tmp_path, event, _exported(event, core), text, core)
    assert "cannot be read" in reason if text is None else "is empty" in reason


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
def test_the_fallback_warning_is_refused_exactly_when_sysmon_was_switched_on(event, core, tmp_path):
    reason = _refusal(tmp_path, event, _exported(event, core), FELL_BACK, core)
    refused = coverage_core.switches(event) and core == "sysmon"
    assert (coverage_core.FALLBACK_MARK in reason) is refused
    assert bool(reason) is refused


@pytest.mark.parametrize("event", TRIGGERS)
@pytest.mark.parametrize(
    "environ",
    [
        {},
        SWITCHED,
        {name: "" for name in SWITCHED},
        *({name: value} for name, value in SWITCHED.items()),
        *({**SWITCHED, name: "true"} for name in SWITCHED),
    ],
)
@pytest.mark.parametrize("core", CORES)
def test_an_environment_the_event_did_not_export_is_refused(event, core, environ, tmp_path):
    reason = _refusal(tmp_path, event, environ, CLEAN, core)
    assert (reason == "") is (environ == _exported(event, core)), reason


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
def test_the_command_line_prints_the_line_and_exits_by_the_verdict(
    event, core, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    for name in SWITCHED:
        monkeypatch.delenv(name, raising=False)
    flag = _flag(core).split()
    assert coverage_core.main(["exports", *flag]) == 0
    assert capsys.readouterr().out.strip() == coverage_core.exports(event, core)
    log = tmp_path / "pytest.log"
    assert coverage_core.main(["check", *flag, str(log)]) == 1
    assert "::error::coverage core check:" in capsys.readouterr().out
    log.write_text(CLEAN, encoding="utf-8")
    for name, value in _exported(event, core).items():
        monkeypatch.setenv(name, value)
    assert coverage_core.main(["check", *flag, str(log)]) == 0


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = test apps\n", encoding="utf-8")
    for name, text in {"test/__init__.py": "", "apps/__init__.py": "", **files}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_marked_files_are_the_test_files_that_name_the_marker(tmp_path):
    mark = f"@pytest.mark.{coverage_core.MARKER}\ndef test_x(): pass\n"
    root = _tree(
        tmp_path,
        {
            "test/test_deep.py": mark,
            "apps/one/tests/test_deep.py": mark,
            "test/test_plain.py": "def test_y(): pass\n",
            "test/notes.txt": mark,
            "elsewhere/test_deep.py": mark,
        },
    )
    assert coverage_core.marked_files(root) == ["apps/one/tests/test_deep.py", "test/test_deep.py"]
    assert coverage_core.marked_files(_tree(tmp_path / "bare", {})) == []


# ── The kill switch ────────────────────────────────────────────────────────

SWITCH = coverage_core.SWITCH_VARIABLE
OFF = [coverage_core.SWITCH_OFF, coverage_core.SWITCH_OFF.upper(), f" {coverage_core.SWITCH_OFF}\n"]
NOT_OFF = ["", "on", "true", "0", "disabled", coverage_core.SWITCH_OFF + "f", "no"]


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
def test_only_off_turns_the_switch_off_and_then_every_event_measures_as_a_push(
    event, core, tmp_path
):
    as_push = _exported("push", core)
    assert as_push == {}
    log = tmp_path / "pytest.log"
    log.write_text(FELL_BACK, encoding="utf-8")
    for value in OFF:
        assert not coverage_core.switches(event, value)
        assert _exported(event, core, value) == as_push
        assert coverage_core.refusal(event, {}, log, core, value) == ""
        assert coverage_core.refusal(event, SWITCHED, log, core, value) != ""
    for value in NOT_OFF:
        assert coverage_core.switches(event, value) is coverage_core.switches(event)
        assert _exported(event, core, value) == _exported(event, core)


@pytest.mark.parametrize("core", CORES)
@pytest.mark.parametrize("event", TRIGGERS)
def test_the_command_line_reads_the_switch_from_its_environment(
    event, core, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv(SWITCH, OFF[0])
    for name in SWITCHED:
        monkeypatch.delenv(name, raising=False)
    flag = _flag(core).split()
    assert coverage_core.main(["exports", *flag]) == 0
    assert capsys.readouterr().out == ""
    log = tmp_path / "pytest.log"
    log.write_text(CLEAN, encoding="utf-8")
    assert coverage_core.main(["check", *flag, str(log)]) == 0
    capsys.readouterr()
    monkeypatch.delenv(SWITCH)
    assert coverage_core.main(["exports", *flag]) == 0
    assert capsys.readouterr().out.strip() == coverage_core.exports(event, core)


def test_every_step_that_measures_hands_the_repository_variable_to_the_script():
    steps = [step for job in LANES.values() for step in _coverage_steps(job)]
    for step in steps:
        assert step.get("env", {}).get(SWITCH) == f"${{{{ vars.{SWITCH} }}}}", step["name"]
    # One variable, read only where a lane measures.
    for path in sorted(WORKFLOW_PATH.parent.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert text.count(f"vars.{SWITCH}") == (len(steps) if path == WORKFLOW_PATH else 0)


@pytest.mark.parametrize("switch", ["", OFF[0]])
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_step_that_delegates_its_pytest_gives_it_the_c_tracer_variables(
    event, switch, monkeypatch, tmp_path
):
    """Its data file is folded into the shard's, so it records what the shard records."""
    from scripts import ci_main_red_skip

    delegating = [s for job in LANES.values() for s in _coverage_steps(job) if _delegates(s)]
    assert delegating, "no lane delegates a measured run"
    for step in delegating:
        assert "scripts/ci_main_red_skip.py measure" in step["run"], step["name"]
    seen: list[dict[str, str]] = []

    def run(argv, env, check):
        seen.append(env)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ci_main_red_skip.subprocess, "run", run)
    plan = '{"issue": 900, "nodeids": ["test/test_a.py::test_b"]}'
    env = {
        ci_main_red_skip.ENV_NAME: plan,
        "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
        "GITHUB_EVENT_NAME": event,
        SWITCH: switch,
        # A core left in the environment by mistake must not reach the pass.
        "COVERAGE_CORE": coverage_core.SWITCH_ENV["COVERAGE_CORE"],
    }
    assert ci_main_red_skip.cmd_measure(env, ["-q"]) == 0
    (child,) = seen
    expected = _exported(event, "ctrace", switch)
    assert {name: child[name] for name in SWITCHED if name in child} == expected


# ── The workflow ───────────────────────────────────────────────────────────


def test_every_combine_download_has_exactly_one_uploading_lane():
    assert len(LANES) >= 2, "Coverage Combine merges more than one lane"
    for pattern in _combine_downloads():
        uploaders = [
            job_id
            for job_id, job in JOBS.items()
            for step in job["steps"]
            if _uses(step, "upload-artifact")
            and fnmatch.fnmatchcase(EXPRESSION.sub("1", str(step["with"]["name"])), pattern)
        ]
        assert len(uploaders) == 1 and uploaders[0] in LANES, (pattern, uploaders)


def test_no_env_block_sets_a_switch_variable():
    """An ``env:`` expression can evaluate to '', which coverage.py refuses."""
    for path in sorted(WORKFLOW_PATH.parent.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for block in _env_blocks(document):
            assert not set(block) & set(coverage_core.SWITCH_ENV), path.name


def test_every_lane_makes_the_same_exports_call_before_it_measures():
    calls = set()
    for name, step in STEPS.items():
        core = _core(step)
        commands = _commands(step["run"])
        (capture,) = [line for line in commands if f"{SCRIPT} exports" in line]
        call = rf'(\w+)="\$\(python {re.escape(SCRIPT)} exports{re.escape(_flag(core))}\)"'
        variable = re.fullmatch(call, capture)
        assert variable, f"{name}: a failed exports call must stop the step: {capture}"
        evaluated = commands.index(f'eval "${variable.group(1)}"')
        measured = _measured_pytest(commands)
        assert measured, name
        assert commands.index(capture) < evaluated < min(map(commands.index, measured)), name
        if core == "sysmon":
            calls.add(capture)
    assert len(calls) == 1, calls
    for job_id, job in LANES.items():
        assert len(_measuring_steps(job, "sysmon")) == 1, job_id


def test_every_lane_tees_its_measured_pytest_and_checks_that_log():
    for name, step in STEPS.items():
        commands = _commands(step["run"])
        for line in _measured_pytest(commands):
            tee = re.search(r' 2>&1 \| tee "(\$\w+)"$', line)
            assert tee, f"{name}: measured pytest output is not kept: {line[-80:]}"
            check = f'python {SCRIPT} check{_flag(_core(step))} "{tee.group(1)}"'
            after = commands[commands.index(line) + 1 :]
            assert check in after, f"{name}: no check of {tee.group(1)} after the run"
        assert not re.search(r"tee\b[^\n]*(\|\||;)", step["run"]), "nothing may mask the pipeline"


def test_every_lane_shell_stops_on_a_failed_pipeline():
    action = (ROOT / ".github/actions/run-as-runner/action.yml").read_text(encoding="utf-8")
    wrapper_lines = [line for line in action.splitlines() if "bash --noprofile" in line]
    assert wrapper_lines and all("-eo pipefail" in line for line in wrapper_lines)
    for name, step in STEPS.items():
        job = LANES[name.split(":")[0]]
        shell = step.get("shell") or job.get("defaults", {}).get("run", {}).get("shell")
        # An explicit `bash` is run by the runner as `bash --noprofile --norc -eo pipefail`.
        assert shell in ("bash", "/usr/local/bin/ci-shell {0}"), (name, shell)


# ── Tests at the recursion limit never run under sysmon, and always run ────

MARKER = coverage_core.MARKER
#: A lane's deselect: text added to its ``-m`` expression only when a variable is set.
CONDITIONAL = re.compile(r"\$\{(\w+):\+ and \$(\w+)\}$")


def _named_test_files(line: str) -> set[str]:
    """The test files a pytest command line names, by path or by node id."""
    words = (word.strip("\"'") for word in line.split())
    return {word.split("::")[0] for word in words if word.split("::")[0].endswith(".py")}


def _deselect(line: str) -> tuple[str, str] | None:
    """``(variable, the expression without it)`` when the line's ``-m`` ends in a deselect."""
    expression = _marker_expression(line)
    found = CONDITIONAL.search(expression)
    if not found or found.group(1) != found.group(2):
        return None
    return found.group(1), expression[: found.start()]


def _selects_the_marker(step: dict) -> bool:
    lines = _measured_pytest(_commands(step["run"]))
    return any(_marker_expression(line).startswith(MARKER) for line in lines)


@pytest.mark.parametrize("switch", ["", *OFF])
@pytest.mark.parametrize("event", [*TRIGGERS, "", "schedule"])
def test_a_lane_deselects_the_marker_exactly_when_it_measures_with_sysmon(
    event, switch, monkeypatch, capsys
):
    on = coverage_core.switches(event, switch)
    assert coverage_core.lane_marker(event, switch) == (f"not {MARKER}" if on else "")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv(SWITCH, switch)
    assert coverage_core.main(["lane-marker"]) == 0
    assert capsys.readouterr().out.strip() == coverage_core.lane_marker(event, switch)
    assert coverage_core.main(["deselected-files"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed == (coverage_core.marked_files() if on else [])


def test_a_sysmon_step_deselects_the_marker_or_names_files_that_do_not_carry_it():
    marked = set(coverage_core.marked_files())
    assert marked, "the tree has a test at the recursion limit"
    for job_id, job in LANES.items():
        commands = _commands(_measuring_step(job)["run"])
        for line in _measured_pytest(commands):
            deselect = _deselect(line)
            if deselect:
                # The variable holds what the script prints for this run, and nothing else.
                capture = f'{deselect[0]}="$(python {SCRIPT} lane-marker)"'
                assert capture in commands[: commands.index(line)], job_id
                continue
            named = _named_test_files(line)
            assert named, f"{job_id}: selects by marker alone and does not deselect {MARKER}"
            assert not named & marked, f"{job_id}: runs {sorted(named & marked)} under sysmon"


def test_every_deselected_test_runs_in_a_c_tracer_step_before_the_data_is_uploaded():
    deselecting = 0
    for job_id, job in LANES.items():
        steps = job["steps"]
        lane_step = _measuring_step(job)
        commands = _commands(lane_step["run"])
        for line in _measured_pytest(commands):
            deselect = _deselect(line)
            if not deselect:
                continue
            deselecting += 1
            kept = deselect[1]
            assert MARKER not in kept, f"{job_id}: {kept!r} names the marker unconditionally"
            # With nothing deselected the lane selects what its unmeasured arms select.
            others = {
                _marker_expression(c)
                for c in commands
                if re.search(r"\bpytest\b", c) and "--no-cov" in c and _marker_expression(c)
            }
            assert others == {kept}, (job_id, others)
            (second,) = [s for s in _measuring_steps(job, "ctrace") if _selects_the_marker(s)]
            second_commands = _commands(second["run"])
            (measured,) = _measured_pytest(second_commands)
            # Together the two expressions select what `kept` alone selects: nothing is lost.
            assert _marker_expression(measured) == f"{MARKER} and {kept}", job_id
            assert "--cov-append" in measured.split(), "the lane's own data must survive"
            assert set(re.findall(r"--cov=\S+", measured)) == set(re.findall(r"--cov=\S+", line))
            listed = [c for c in second_commands if f"python {SCRIPT} deselected-files" in c]
            assert listed and second_commands.index(listed[0]) < second_commands.index(measured)
            assert not _named_test_files(measured), "the files come from the marker, not a list"
            (upload,) = [
                s for s in steps if _uses(s, "upload-artifact") and "coverage" in s["with"]["name"]
            ]
            assert steps.index(lane_step) < steps.index(second) < steps.index(upload), job_id
            # It runs in the scope the lane measures in, on one leg of the lane's matrix.
            scope = re.search(r"steps\.scope\.outputs\.\w+", str(upload["if"])).group(0)
            assert f"{scope} == 'false'" in second["if"], second["if"]
            (leg,) = re.findall(r"matrix\.(\w+) == (\d+)", second["if"])
            assert int(leg[1]) in job["strategy"]["matrix"][leg[0]], leg
            assert not second.get("continue-on-error")
    assert deselecting, "no lane deselects the marker, so no test at the limit is protected"


# ── Each lane's own step body, under bash ──────────────────────────────────

STAND_IN = """
python() {
  case "$1" in
    scripts/ci/coverage_core.py) shift; "$REAL_PYTHON" "$REPO_ROOT/scripts/ci/coverage_core.py" "$@" ;;
    -) cat > /dev/null ;;
    *)
      echo "${KC_COV_BRANCH-<unset>} ${COVERAGE_CORE-<unset>}" >> "$RUNNER_TEMP/seen-by-pytest"
      echo "$*" >> "$RUNNER_TEMP/args-of-pytest"
      printf '%s' "$PYTEST_OUT"
      return "$PYTEST_RC"
      ;;
  esac
}
"""


def _seen(event: str, core: str) -> str:
    """What the stand-in pytest records for the two variables, in ``SWITCH_ENV`` order."""
    exported = _exported(event, core)
    return " ".join(exported.get(name, "<unset>") for name in coverage_core.SWITCH_ENV)


def _run_lane(
    step: dict,
    tmp_path: Path,
    event: str,
    out: str,
    status: int = 0,
    repo: Path = ROOT,
    switch: str = "",
):
    bash = shutil.which("bash")
    assert bash, "the lane steps are bash scripts"
    body = EXPRESSION.sub("1", str(step["run"]))
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "RUNNER_OS": "Linux",
        "GITHUB_EVENT_NAME": event,
        SWITCH: switch,
        "REAL_PYTHON": sys.executable,
        "REPO_ROOT": str(repo),
        "PYTEST_OUT": out,
        "PYTEST_RC": str(status),
        # A full-suite run: the scope every lane measures in.
        "REDUCED": "false",
        "LEAF": "false",
        "ONLY_FRONTEND": "false",
        "LEAF_TESTS": "false",
        "SHARD_COUNT": "8",
    }
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", STAND_IN + body],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    record = tmp_path / "seen-by-pytest"
    seen = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return result, seen


def _arguments(tmp_path: Path) -> list[str]:
    """The stand-in pytest's command lines, one per call."""
    record = tmp_path / "args-of-pytest"
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def _runs(name: str, event: str, switch: str = "") -> bool:
    """Whether the step starts pytest at all: the marker's step only when it was deselected."""
    return not _selects_the_marker(STEPS[name]) or coverage_core.switches(event, switch)


def _lane_expression(name: str, deselected: bool) -> str:
    """The ``-m`` words the lane's measured pytest gets, with or without the deselect."""
    (line,) = [m for m in _measured_pytest(_commands(STEPS[name]["run"])) if _deselect(m)]
    kept = _deselect(line)[1]
    return f"-m {kept} and not {MARKER} " if deselected else f"-m {kept} "


lane_bodies = pytest.mark.skipif(
    sys.platform != "linux", reason="the lane steps read /proc and run on Linux runners"
)


@lane_bodies
@pytest.mark.parametrize("name", sorted(STEPS))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_step_measures_with_what_its_event_exports_and_passes(name, event, tmp_path):
    result, seen = _run_lane(STEPS[name], tmp_path, event, CLEAN)
    assert result.returncode == 0, result.stdout + result.stderr
    if not _runs(name, event):
        assert seen == [], "nothing was deselected on this run, so the step has no test to run"
        return
    expected = _seen(event, _core(STEPS[name]))
    assert seen == [expected], "pytest ran with another environment, or an empty variable"
    assert CLEAN in result.stdout, "the tee must still show pytest's output in the job log"


@lane_bodies
@pytest.mark.parametrize("switch", ["", OFF[0]])
@pytest.mark.parametrize("event", TRIGGERS)
def test_the_lane_that_deselects_does_so_only_under_sysmon(event, switch, tmp_path):
    """Off, or on a push, its pytest gets the expression it has without this change."""
    (name,) = [
        n
        for n, step in STEPS.items()
        if any(_deselect(m) for m in _measured_pytest(_commands(step["run"])))
    ]
    result, _ = _run_lane(STEPS[name], tmp_path, event, CLEAN, switch=switch)
    assert result.returncode == 0, result.stdout + result.stderr
    (arguments,) = _arguments(tmp_path)
    on = coverage_core.switches(event, switch)
    assert _lane_expression(name, on) in arguments, arguments
    assert (MARKER in arguments) is on


@lane_bodies
@pytest.mark.parametrize("name", sorted(STEPS))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_step_with_the_switch_off_measures_as_a_push_does(name, event, tmp_path):
    result, seen = _run_lane(STEPS[name], tmp_path, event, FELL_BACK, switch=OFF[0])
    assert result.returncode == 0, result.stdout + result.stderr
    if not _runs(name, event, OFF[0]):
        assert seen == [], "with the switch off nothing is deselected and nothing runs here"
        return
    assert seen == [_seen("push", _core(STEPS[name]))] == ["<unset> <unset>"]


@lane_bodies
@pytest.mark.parametrize("name", sorted(STEPS))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_step_fails_on_the_fallback_warning_exactly_when_it_switched_to_sysmon(
    name, event, tmp_path
):
    result, _ = _run_lane(STEPS[name], tmp_path, event, FELL_BACK)
    refused = coverage_core.switches(event) and _core(STEPS[name]) == "sysmon"
    assert (result.returncode != 0) is refused, result.stdout
    assert ("::error::coverage core check:" in result.stdout) is refused


@lane_bodies
@pytest.mark.parametrize("name", sorted(STEPS))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_step_fails_when_pytest_printed_nothing(name, event, tmp_path):
    result, _ = _run_lane(STEPS[name], tmp_path, event, "")
    if not _runs(name, event):
        assert result.returncode == 0, result.stdout + result.stderr
        return
    assert result.returncode != 0 and "is empty" in result.stdout, result.stdout + result.stderr


@lane_bodies
@pytest.mark.parametrize("name", sorted(STEPS))
@pytest.mark.parametrize("status", [1, 3, 5])
def test_a_step_ends_with_pytest_exit_status_through_the_tee(name, status, tmp_path):
    """5 is pytest's "no test selected": a marker that selects nothing is a red step."""
    event = sorted(coverage_core.SWITCH_EVENTS)[0]
    result, _ = _run_lane(STEPS[name], tmp_path, event, CLEAN, status)
    assert result.returncode == status, result.stdout + result.stderr
    assert "coverage core check" not in result.stdout, "the step stops at the failed pytest"


@lane_bodies
@pytest.mark.parametrize("marked", [0, 2])
def test_the_c_tracer_step_hands_pytest_exactly_the_files_that_name_the_marker(marked, tmp_path):
    files = {
        f"test/test_deep_{index}.py": f"@pytest.mark.{MARKER}\ndef test_x(): pass\n"
        for index in range(marked)
    }
    repo = _tree(tmp_path / "repo", {**files, "test/test_plain.py": "def test_y(): pass\n"})
    script = repo / SCRIPT
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / SCRIPT, script)
    runs = tmp_path / "run"
    runs.mkdir()
    (step,) = [s for name, s in STEPS.items() if name.endswith(":ctrace")]
    event = sorted(coverage_core.SWITCH_EVENTS)[0]
    result, seen = _run_lane(step, runs, event, CLEAN, repo=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    if not marked:
        assert seen == [], "with no marked test there is nothing to run, and nothing deselected"
        return
    (arguments,) = (runs / "args-of-pytest").read_text(encoding="utf-8").splitlines()
    assert [word for word in arguments.split() if word.endswith(".py")] == sorted(files)
