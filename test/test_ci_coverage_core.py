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


def _measuring_step(job: dict) -> dict:
    """The lane's one step that runs pytest with a ``--cov`` selector."""
    (step,) = [s for s in job["steps"] if "--cov=" in str(s.get("run", ""))]
    return step


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


def _exported(event: str) -> dict[str, str]:
    """The variables the ``exports`` line for ``event`` sets, parsed back."""
    line = coverage_core.exports(event)
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


def _refusal(tmp_path: Path, event: str, environ: dict[str, str], text: str | None) -> str:
    log = tmp_path / "pytest.log"
    if text is not None:
        log.write_text(text, encoding="utf-8")
    return coverage_core.refusal(event, environ, log)


@pytest.mark.parametrize("event", TRIGGERS)
def test_a_clean_log_passes_with_exactly_what_the_event_exports(event, tmp_path):
    assert _refusal(tmp_path, event, _exported(event), CLEAN) == ""
    assert _refusal(tmp_path, event, {**_exported(event), "PATH": "/bin"}, CLEAN) == ""


@pytest.mark.parametrize("event", TRIGGERS)
@pytest.mark.parametrize("text", [None, "", " \n\t\n"])
def test_a_missing_or_empty_log_is_refused_for_every_event(event, text, tmp_path):
    reason = _refusal(tmp_path, event, _exported(event), text)
    assert "cannot be read" in reason if text is None else "is empty" in reason


@pytest.mark.parametrize("event", TRIGGERS)
def test_the_fallback_warning_is_refused_exactly_when_the_event_switches(event, tmp_path):
    reason = _refusal(tmp_path, event, _exported(event), FELL_BACK)
    assert (coverage_core.FALLBACK_MARK in reason) is coverage_core.switches(event)
    assert bool(reason) is coverage_core.switches(event)


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
def test_an_environment_the_event_did_not_export_is_refused(event, environ, tmp_path):
    reason = _refusal(tmp_path, event, environ, CLEAN)
    assert (reason == "") is (environ == _exported(event)), reason


@pytest.mark.parametrize("event", TRIGGERS)
def test_the_command_line_prints_the_line_and_exits_by_the_verdict(
    event, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    for name in SWITCHED:
        monkeypatch.delenv(name, raising=False)
    assert coverage_core.main(["exports"]) == 0
    assert capsys.readouterr().out.strip() == coverage_core.exports(event)
    log = tmp_path / "pytest.log"
    assert coverage_core.main(["check", str(log)]) == 1
    assert "::error::coverage core check:" in capsys.readouterr().out
    log.write_text(CLEAN, encoding="utf-8")
    for name, value in _exported(event).items():
        monkeypatch.setenv(name, value)
    assert coverage_core.main(["check", str(log)]) == 0


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
    for job_id, job in LANES.items():
        commands = _commands(_measuring_step(job)["run"])
        (capture,) = [line for line in commands if f"{SCRIPT} exports" in line]
        variable = re.fullmatch(rf'(\w+)="\$\(python {re.escape(SCRIPT)} exports\)"', capture)
        assert variable, f"{job_id}: a failed exports call must stop the step: {capture}"
        evaluated = commands.index(f'eval "${variable.group(1)}"')
        measured = _measured_pytest(commands)
        assert measured, job_id
        assert commands.index(capture) < evaluated < min(map(commands.index, measured)), job_id
        calls.add(capture)
    assert len(calls) == 1, calls


def test_every_lane_tees_its_measured_pytest_and_checks_that_log():
    for job_id, job in LANES.items():
        step = _measuring_step(job)
        commands = _commands(step["run"])
        for line in _measured_pytest(commands):
            tee = re.search(r' 2>&1 \| tee "(\$\w+)"$', line)
            assert tee, f"{job_id}: measured pytest output is not kept: {line[-80:]}"
            check = f'python {SCRIPT} check "{tee.group(1)}"'
            after = commands[commands.index(line) + 1 :]
            assert check in after, f"{job_id}: no check of {tee.group(1)} after the run"
        assert not re.search(r"tee\b[^\n]*(\|\||;)", step["run"]), "nothing may mask the pipeline"


def test_every_lane_shell_stops_on_a_failed_pipeline():
    action = (ROOT / ".github/actions/run-as-runner/action.yml").read_text(encoding="utf-8")
    wrapper_lines = [line for line in action.splitlines() if "bash --noprofile" in line]
    assert wrapper_lines and all("-eo pipefail" in line for line in wrapper_lines)
    for job_id, job in LANES.items():
        step = _measuring_step(job)
        shell = step.get("shell") or job.get("defaults", {}).get("run", {}).get("shell")
        # An explicit `bash` is run by the runner as `bash --noprofile --norc -eo pipefail`.
        assert shell in ("bash", "/usr/local/bin/ci-shell {0}"), (job_id, shell)


# ── Each lane's own step body, under bash ──────────────────────────────────

STAND_IN = """
python() {
  case "$1" in
    scripts/ci/coverage_core.py) shift; "$REAL_PYTHON" "$REPO_ROOT/scripts/ci/coverage_core.py" "$@" ;;
    -) cat > /dev/null ;;
    *)
      echo "${KC_COV_BRANCH-<unset>} ${COVERAGE_CORE-<unset>}" >> "$RUNNER_TEMP/seen-by-pytest"
      printf '%s' "$PYTEST_OUT"
      return "$PYTEST_RC"
      ;;
  esac
}
"""
UNSET = "<unset> <unset>"


def _run_lane(job: dict, tmp_path: Path, event: str, out: str, status: int = 0):
    bash = shutil.which("bash")
    assert bash, "the lane steps are bash scripts"
    body = EXPRESSION.sub("1", str(_measuring_step(job)["run"]))
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "RUNNER_OS": "Linux",
        "GITHUB_EVENT_NAME": event,
        "REAL_PYTHON": sys.executable,
        "REPO_ROOT": str(ROOT),
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
        timeout=60,
    )
    seen = (tmp_path / "seen-by-pytest").read_text(encoding="utf-8").splitlines()
    return result, seen


lane_bodies = pytest.mark.skipif(
    sys.platform != "linux", reason="the lane steps read /proc and run on Linux runners"
)


@lane_bodies
@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_lane_measures_with_what_its_event_exports_and_passes(lane, event, tmp_path):
    result, seen = _run_lane(LANES[lane], tmp_path, event, CLEAN)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = " ".join(_exported(event).values()) if coverage_core.switches(event) else UNSET
    assert seen == [expected], "pytest ran with another environment, or an empty variable"
    assert CLEAN in result.stdout, "the tee must still show pytest's output in the job log"


@lane_bodies
@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_lane_fails_on_the_fallback_warning_exactly_when_it_switched(lane, event, tmp_path):
    result, _ = _run_lane(LANES[lane], tmp_path, event, FELL_BACK)
    assert (result.returncode != 0) is coverage_core.switches(event), result.stdout
    assert ("::error::coverage core check:" in result.stdout) is coverage_core.switches(event)


@lane_bodies
@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("event", TRIGGERS)
def test_a_lane_fails_when_pytest_printed_nothing(lane, event, tmp_path):
    result, _ = _run_lane(LANES[lane], tmp_path, event, "")
    assert result.returncode != 0 and "is empty" in result.stdout, result.stdout + result.stderr


@lane_bodies
@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("status", [1, 3])
def test_a_lane_ends_with_pytest_exit_status_through_the_tee(lane, status, tmp_path):
    event = sorted(coverage_core.SWITCH_EVENTS)[0]
    result, _ = _run_lane(LANES[lane], tmp_path, event, CLEAN, status)
    assert result.returncode == status, result.stdout + result.stderr
    assert "coverage core check" not in result.stdout, "the step stops at the failed pytest"
