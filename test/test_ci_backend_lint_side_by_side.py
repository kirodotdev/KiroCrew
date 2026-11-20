"""``backend-lint`` runs flake8 and mypy in one step, side by side, and both block.

The wiring tests read the parsed workflow; the behaviour tests execute the
step's own script with stub ``flake8`` and ``mypy`` on PATH.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    _bash,
    _child_env,
    _proc_log,
    _script_file,
    _step_script,
    _workflow,
    _write_stub,
)

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

STEP = "Lint and type check (flake8 and mypy, side by side)"
RESTORE = "Restore the mypy cache (pull requests)"
SAVE = "Save the mypy cache (main)"
TOOLS = ("flake8", "mypy")
#: The command line each tool is started with.
COMMANDS = {
    "flake8": "flake8 src/kiro_crew test conftest.py xdist_budget.py",
    "mypy": "mypy src/kiro_crew/",
}

#: A stub tool. It records that it started, then holds until the OTHER tool has
#: started too, so a step that ran the two in series would never get past the
#: first one: that one exits 97 after its bounded wait and the test fails on it.
#: Its behaviour after that comes from ``<dir>/<tool>.rc`` and ``<dir>/<tool>.mode``.
_STUB = """\
me="$(basename "$0")"
case "$me" in flake8) other=mypy ;; *) other=flake8 ;; esac
printf '%s\\n' "$*" > "$STUB_DIR/$me.argv"
echo "$$" > "$STUB_DIR/$me.pid"
tries=0
while [ ! -e "$STUB_DIR/$other.pid" ]; do
  tries=$((tries + 1))
  if [ "$tries" -gt 600 ]; then echo "$me: $other never started beside it"; exit 97; fi
  sleep 0.05
done
echo "$me says: line on stdout"
echo "$me says: line on stderr" >&2
case "$(cat "$STUB_DIR/$me.mode")" in
  killed) kill -KILL "$$" ;;
  hang) echo "$me says: about to hang"; exec sleep 600 ;;
  after-other)
    # Finish only once the other tool has exited, whatever its status.
    while kill -0 "$(cat "$STUB_DIR/$other.pid")" 2>/dev/null; do sleep 0.05; done ;;
esac
exit "$(cat "$STUB_DIR/$me.rc")"
"""


def _lint_job() -> dict:
    return yaml.safe_load(_workflow("ci.yml"))["jobs"]["backend-lint"]


def _steps() -> list[dict]:
    return _lint_job()["steps"]


def _index(name: str) -> int:
    matches = [i for i, step in enumerate(_steps()) if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step, found {len(matches)}"
    return matches[0]


def _run_lines() -> list[str]:
    return [line.strip() for line in _steps()[_index(STEP)]["run"].splitlines()]


class TestWiring:
    @pytest.mark.parametrize("tool", TOOLS)
    def test_each_tool_is_started_in_the_background_and_its_pid_kept(self, tool: str) -> None:
        lines = _run_lines()
        start = f"timed {COMMANDS[tool]} &"
        assert start in lines, f"{tool} is not started as {start!r}"
        assert lines[lines.index(start) + 1] == f"{tool}_pid=$!"

    def test_the_wrapper_returns_the_status_of_the_tool_it_ran(self) -> None:
        # `timed` is what each PID above is: it must run the tool it was handed,
        # into that tool's own log, and exit with that tool's status.
        lines = _run_lines()
        body = lines[lines.index("timed() {") + 1 : lines.index("}")]
        assert body == [
            "local tool=$1 status=0",
            '"$@" > "$logs/$tool.log" 2>&1 &',
            "local child=$!",
            "trap 'kill \"$child\" 2>/dev/null' TERM",
            'wait "$child" || status=$?',
            'echo "$SECONDS" > "$logs/$tool.seconds"',
            'exit "$status"',
        ]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_each_pid_is_waited_on_and_its_status_kept(self, tool: str) -> None:
        lines = _run_lines()
        wait = f'wait "${tool}_pid" || {tool}_status=$?'
        assert wait in lines
        assert lines[lines.index(wait) - 1] == f"{tool}_status=0"

    def test_both_tools_start_before_either_is_waited_on(self) -> None:
        lines = _run_lines()
        last_start = max(i for i, line in enumerate(lines) if line.startswith("timed "))
        first_wait = min(i for i, line in enumerate(lines) if re.match(r'wait "\$\w+_pid"', line))
        assert last_start < first_wait

    def test_every_wait_names_a_pid(self) -> None:
        # A bare `wait` returns 0 whatever the children did.
        waits = [line for line in _run_lines() if re.match(r"wait\b", line)]
        assert waits
        for line in waits:
            assert re.match(r'wait "\$\w+" \|\| \w*status=\$\?$', line), line

    def test_logs_live_under_runner_temp(self) -> None:
        assert 'logs="$RUNNER_TEMP/backend-lint"' in _run_lines()

    def test_the_step_cannot_pass_on_a_failure(self) -> None:
        step = _steps()[_index(STEP)]
        assert "continue-on-error" not in step
        assert "if" not in step
        # No `shell:` here or on the job: the script runs under GitHub's default
        # `bash -e {0}`, which is what the behaviour tests below run it with.
        assert "shell" not in step
        assert "defaults" not in _lint_job()
        assert "|| true" not in step["run"]

    @pytest.mark.parametrize("tool", TOOLS)
    def test_no_other_step_in_the_job_runs_the_tool(self, tool: str) -> None:
        others = [s for s in _steps() if s.get("name") != STEP]
        assert not [s["name"] for s in others if re.search(rf"\b{tool}\b", s.get("run", ""))]

    def test_the_mypy_cache_is_restored_before_and_saved_after(self) -> None:
        assert _index(RESTORE) < _index(STEP) < _index(SAVE)
        # Nothing between the tools and the save: the save's implicit success()
        # then reads this step's verdict, so a red flake8 or mypy saves nothing.
        assert _index(SAVE) == _index(STEP) + 1

    def test_the_mypy_cache_steps_keep_their_conditions_and_keys(self) -> None:
        restore, save = _steps()[_index(RESTORE)], _steps()[_index(SAVE)]
        assert restore["if"] == "github.event_name != 'push'"
        assert save["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
        assert restore["uses"].startswith("actions/cache/restore@")
        assert save["uses"].startswith("actions/cache/save@")
        key = (
            "mypy-${{ runner.os }}-${{ matrix.python-version }}-"
            "${{ hashFiles('pyproject.toml', 'setup.cfg') }}-"
        )
        assert restore["with"]["path"] == save["with"]["path"] == ".mypy_cache"
        assert restore["with"]["key"] == save["with"]["key"] == key + "${{ github.sha }}"
        assert restore["with"]["restore-keys"].strip() == key
        for step in (restore, save):
            assert "continue-on-error" not in step


def _prepare(tmp_path: Path, **tools: tuple[str, int]) -> tuple[str, Path, dict[str, str]]:
    """Stub both tools as ``tool=(mode, exit status)`` and return what runs the step."""
    bash = _bash()
    # The job runs on Linux runners only, and the script and these stubs lean on
    # what a Linux host has: its bash, a fractional `sleep`, signal numbering.
    if sys.platform != "linux":
        pytest.skip("executes a workflow step that only ever runs on Linux")
    assert bash is not None, "bash is not on PATH"
    bin_dir, stub_dir = tmp_path / "bin", tmp_path / "stub"
    for directory in (bin_dir, stub_dir, tmp_path / "runner-temp"):
        directory.mkdir()
    for tool in TOOLS:
        mode, status = tools[tool]
        _write_stub(bin_dir / tool, _STUB)
        (stub_dir / f"{tool}.mode").write_text(mode, encoding="utf-8")
        (stub_dir / f"{tool}.rc").write_text(str(status), encoding="utf-8")
    env = _child_env(
        {
            "PATH": os.pathsep.join([str(bin_dir), "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path / "runner-temp"),
            "STUB_DIR": str(stub_dir),
            "LC_ALL": "C",
        }
    )
    script = _script_file(tmp_path, _step_script(_workflow("ci.yml"), STEP))
    return bash, Path(script), env


def _run(tmp_path: Path, **tools: tuple[str, int]) -> "subprocess.CompletedProcess[str]":
    bash, script, env = _prepare(tmp_path, **tools)
    return subprocess.run(
        [bash, "-e", str(script)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _group(stdout: str, tool: str, title: str = r"finished \d+s into the step") -> str:
    """The lines printed between ``::group::<tool> (<title>)`` and its ``::endgroup::``."""
    opened = re.findall(rf"^::group::{tool} \({title}\)\n", stdout, re.MULTILINE)
    assert len(opened) == 1, stdout
    body = stdout.split(opened[0], 1)[1]
    assert "::endgroup::\n" in body, stdout
    return body.split("::endgroup::\n", 1)[0]


def _errors(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("::error")]


def _assert_both_reported(result: "subprocess.CompletedProcess[str]") -> None:
    out = result.stdout
    assert out.index("::group::flake8 ") < out.index("::group::mypy "), _proc_log(result)
    for tool, other in (("flake8", "mypy"), ("mypy", "flake8")):
        body = _group(out, tool)
        # stdout and stderr both land in the tool's own group, and only there.
        assert f"{tool} says: line on stdout\n" in body, _proc_log(result)
        assert f"{tool} says: line on stderr\n" in body, _proc_log(result)
        assert f"{other} says" not in body, _proc_log(result)


class TestBehaviour:
    def test_the_extracted_script_is_the_step_the_workflow_runs(self) -> None:
        extracted = _step_script(_workflow("ci.yml"), STEP)
        assert extracted.strip() == _steps()[_index(STEP)]["run"].strip()

    def test_both_pass(self, tmp_path: Path) -> None:
        result = _run(tmp_path, flake8=("exit", 0), mypy=("exit", 0))
        assert result.returncode == 0, _proc_log(result)
        _assert_both_reported(result)
        assert _errors(result.stdout) == []
        stub = tmp_path / "stub"
        for tool in TOOLS:
            argv = (stub / f"{tool}.argv").read_text(encoding="utf-8").strip()
            assert f"{tool} {argv}" == COMMANDS[tool]

    @pytest.mark.parametrize(
        ("flake8", "mypy", "named"),
        [
            pytest.param(1, 0, ["Lint (flake8)::flake8 exited 1"], id="flake8-fails"),
            pytest.param(0, 1, ["Type check (mypy)::mypy exited 1"], id="mypy-fails"),
            pytest.param(
                1,
                2,
                ["Lint (flake8)::flake8 exited 1", "Type check (mypy)::mypy exited 2"],
                id="both-fail",
            ),
        ],
    )
    def test_a_failing_tool_fails_the_step_and_is_named(
        self, tmp_path: Path, flake8: int, mypy: int, named: list[str]
    ) -> None:
        result = _run(tmp_path, flake8=("exit", flake8), mypy=("exit", mypy))
        assert result.returncode == 1, _proc_log(result)
        _assert_both_reported(result)
        errors = _errors(result.stdout)
        assert len(errors) == len(named), _proc_log(result)
        for error, expected in zip(errors, named):
            assert error.startswith(f"::error title={expected};"), _proc_log(result)

    @pytest.mark.parametrize(
        ("first", "second"), [("flake8", "mypy"), ("mypy", "flake8")], ids=["flake8", "mypy"]
    )
    def test_an_early_failure_does_not_hide_the_tool_still_running(
        self, tmp_path: Path, first: str, second: str
    ) -> None:
        # `first` fails while `second` is still running; `second` then fails
        # with its own status, and both verdicts are reported.
        result = _run(tmp_path, **{first: ("exit", 3), second: ("after-other", 4)})
        assert result.returncode == 1, _proc_log(result)
        _assert_both_reported(result)
        joined = "\n".join(_errors(result.stdout))
        assert f"{first} exited 3;" in joined and f"{second} exited 4;" in joined, joined

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_killed_tool_fails_the_step_and_is_named(self, tmp_path: Path, tool: str) -> None:
        other = "mypy" if tool == "flake8" else "flake8"
        result = _run(tmp_path, **{tool: ("killed", 0), other: ("after-other", 0)})
        assert result.returncode == 1, _proc_log(result)
        _assert_both_reported(result)
        errors = _errors(result.stdout)
        assert len(errors) == 1, _proc_log(result)
        assert f"{tool} exited {128 + signal.SIGKILL};" in errors[0], _proc_log(result)

    @pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
    def test_an_interrupted_step_prints_what_each_tool_wrote_and_stops_them(
        self, tmp_path: Path, interrupt: signal.Signals
    ) -> None:
        bash, script, env = _prepare(tmp_path, flake8=("exit", 0), mypy=("hang", 0))
        stub = tmp_path / "stub"
        with subprocess.Popen(
            [bash, "-e", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=tmp_path,
            env=env,
        ) as proc:
            try:
                # The step has recorded that flake8 finished (the last thing it
                # does for a tool), and mypy is parked mid-run.
                finished = tmp_path / "runner-temp" / "backend-lint" / "flake8.seconds"
                wait_until(
                    lambda: finished.is_file()
                    and finished.read_text(encoding="utf-8").endswith("\n"),
                    timeout=FALLBACK_TIMEOUT_SECS,
                )
                mypy_log = finished.with_name("mypy.log")
                wait_until(
                    lambda: "about to hang\n" in mypy_log.read_text(encoding="utf-8"),
                    timeout=FALLBACK_TIMEOUT_SECS,
                )
                proc.send_signal(interrupt)
                stdout, stderr = proc.communicate(timeout=FALLBACK_TIMEOUT_SECS)
            finally:
                if proc.poll() is None:
                    proc.kill()
        log = f"rc={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        assert proc.returncode == 1, log
        assert "flake8 says: line on stdout\n" in _group(stdout, "flake8"), log
        assert "mypy says: about to hang\n" in _group(stdout, "mypy", "did not finish"), log
        errors = _errors(stdout)
        assert len(errors) == 1 and "interrupted before flake8 and mypy" in errors[0], log
        hung = int((stub / "mypy.pid").read_text(encoding="utf-8"))

        def _gone() -> bool:
            try:
                os.kill(hung, 0)
            except ProcessLookupError:
                return True
            return False

        wait_until(_gone, timeout=FALLBACK_TIMEOUT_SECS, describe=lambda: f"pid {hung} alive")
