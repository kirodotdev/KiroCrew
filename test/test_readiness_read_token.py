"""PR Readiness and the readiness sweep can read from a token pool of their own.

Every workflow here shares one hourly REST pool through ``GITHUB_TOKEN``. When
the AI review lanes spend it, PR Readiness cannot read the lanes it aggregates
and the sweep defers its recomputes. With the repository secret
``PR_READINESS_READ_TOKEN`` set, both workflows make their READS with that
token, on its own pool. With it unset, refused or nearly spent, they read with
``GITHUB_TOKEN`` exactly as before.

Writes never use it. A status, label or comment written with any token but
``GITHUB_TOKEN`` starts workflow runs of its own, which would add to the load
this exists to shed.

These tests run the workflows' own step scripts against a stub ``gh`` that
records which token each call carried. No network, no real token.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pr_readiness_sweep_helpers import SWEEP_STEP

from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
READINESS = WORKFLOWS / "pr-readiness.yml"
SWEEP = WORKFLOWS / "pr-readiness-sweep.yml"
SELECT = "Select the token pool for reads"
SECRET_REF = "${{ secrets.PR_READINESS_READ_TOKEN }}"
DEFAULT_REF = "${{ secrets.GITHUB_TOKEN }}"

# Placeholders, not credentials: the stub only compares and records them.
READ_TOKEN = "stub-readiness-read-token"
DEFAULT_TOKEN = "stub-default-token"

needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the stubs are POSIX scripts found on PATH by their shebang, under a POSIX bash",
)

# Records `<token> <args>` for every call. `api rate_limit --jq FILTER` answers
# what the selection step's filter selects from a pool of $POOL ("remaining
# limit"), or fails when $POOL is "refused".
GH_STUB = r"""#!/usr/bin/env bash
echo "${GH_TOKEN:-none} $*" >> "$CALLS"
if [ "${1:-} ${2:-}" = "api rate_limit" ]; then
  [ "${POOL:-refused}" != refused ] || { echo "gh: Bad credentials (HTTP 401)" >&2; exit 1; }
  read -r remaining limit <<<"$POOL"
  case "$4" in
    *'"\(.remaining)/\(.limit)"'*) echo "$remaining/$limit" ;;
    *) echo "gh stub: unhandled rate_limit filter: $4" >&2; exit 90 ;;
  esac
  exit 0
fi
echo '{}'
"""

# The evaluator steps run `jq` on the gate's answer. A host without one gets
# this stand-in for exactly the filters those steps use, on stdin, as `jq -r`.
JQ_STAND_IN = r"""import json
import sys

flag, program = sys.argv[1:]
assert flag == "-r", flag
document = json.load(sys.stdin)
if program in (".ok", ".error"):
    value = document.get(program[1:])
    values = ["null" if value is None else json.dumps(value) if isinstance(value, bool) else value]
elif program in (".violations[]", ".unpublished[]", ".blocking_dropped[]"):
    values = document[program[1:-2]]
else:
    sys.exit("jq stand-in: unsupported filter: " + program)
for value in values:
    print(value)
"""


def _supply_jq(stubs: Path) -> None:
    """Put the stand-in on PATH as `jq` where the host has none."""
    if shutil.which("jq") is not None:
        return
    program = stubs / "jq_stand_in.py"
    program.write_text(JQ_STAND_IN, encoding="utf-8")
    jq = stubs / "jq"
    jq.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" -I "{program}" "$@"\n', "utf-8")
    jq.chmod(0o755)


def _steps(workflow: Path) -> list[dict]:
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    (job,) = document["jobs"].values()
    return job["steps"]


def _named(workflow: Path, name: str) -> dict:
    (found,) = [s for s in _steps(workflow) if s.get("name") == name]
    return found


def _helper_install() -> str:
    (found,) = [
        s["run"]
        for s in _steps(READINESS)
        if 'cat > "$RUNNER_TEMP/gh-retry.sh"' in s.get("run", "")
    ]
    return found


class Runner:
    def __init__(self, root: Path) -> None:
        self.temp = root / "runner_temp"
        self.calls_file = root / "calls.log"
        stubs = root / "bin"
        for directory in (self.temp, stubs):
            directory.mkdir()
        self.calls_file.write_text("", encoding="utf-8")
        gh = stubs / "gh"
        gh.write_text(GH_STUB, encoding="utf-8")
        gh.chmod(0o755)
        _supply_jq(stubs)
        self.env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.temp),
            "CALLS": str(self.calls_file),
            "GH_TOKEN": DEFAULT_TOKEN,
        }
        self.env.pop("READINESS_READ_TOKEN", None)

    def bash(self, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv, workflow-authored script
            ["bash", "-e", "-c", script],
            env={**self.env, **extra},
            capture_output=True,
            **UTF8_TEXT,
        )

    def select(self, pool: str | None = None, token: str | None = None) -> str:
        """Run the selection step and return everything it printed."""
        step = _named(READINESS, SELECT)
        extra = {"READ_POOL_FLOOR": step["env"]["READ_POOL_FLOOR"]}
        if token is not None:
            extra["READINESS_READ_TOKEN"] = token
        if pool is not None:
            extra["POOL"] = pool
        done = self.bash(step["run"], **extra)
        assert done.returncode == 0, done.stderr
        return done.stdout + done.stderr

    @property
    def selected(self) -> str:
        return (self.temp / "readiness-read-pool").read_text(encoding="utf-8").strip()

    @property
    def calls(self) -> list[tuple[str, str]]:
        lines = self.calls_file.read_text(encoding="utf-8").splitlines()
        return [(line.split(" ", 1)[0], line.split(" ", 1)[1]) for line in lines]

    def reads_and_a_write(self, **extra: str) -> list[tuple[str, str]]:
        """One read through `gh_retry` and one direct write, as a step makes them."""
        self.calls_file.write_text("", encoding="utf-8")
        installed = self.bash(_helper_install())
        assert installed.returncode == 0, installed.stderr
        done = self.bash(
            'source "$RUNNER_TEMP/gh-retry.sh"\n'
            'gh_retry gh api "repos/o/r/pulls/1" >/dev/null\n'
            'gh api --method POST "repos/o/r/statuses/abc" >/dev/null\n',
            **extra,
        )
        assert done.returncode == 0, done.stderr
        return self.calls


@pytest.fixture()
def runner(tmp_path: Path) -> Runner:
    return Runner(tmp_path)


# --- selection ----------------------------------------------------------------


@needs_bash
def test_without_the_secret_reads_stay_on_the_default_token(runner: Runner) -> None:
    printed = runner.select()
    assert runner.selected == "shared"
    assert "token pool -- shared (GITHUB_TOKEN)" in printed
    assert runner.calls == [], "nothing is asked of GitHub when there is no second token"
    assert runner.reads_and_a_write() == [
        (DEFAULT_TOKEN, "api repos/o/r/pulls/1"),
        (DEFAULT_TOKEN, "api --method POST repos/o/r/statuses/abc"),
    ]


@needs_bash
def test_with_the_secret_reads_use_it_and_writes_do_not(runner: Runner) -> None:
    printed = runner.select(pool="4200 5000", token=READ_TOKEN)
    assert runner.selected == "readiness"
    assert "token pool -- readiness (PR_READINESS_READ_TOKEN), 4200/5000 remaining" in printed
    assert runner.reads_and_a_write(READINESS_READ_TOKEN=READ_TOKEN) == [
        (READ_TOKEN, "api repos/o/r/pulls/1"),
        (DEFAULT_TOKEN, "api --method POST repos/o/r/statuses/abc"),
    ]


@needs_bash
@pytest.mark.parametrize(
    ("pool", "reading"),
    [("refused", "no reading"), ("49 5000", "49/5000"), ("0 5000", "0/5000")],
)
def test_a_refused_or_nearly_spent_token_falls_back_to_the_default(
    runner: Runner, pool: str, reading: str
) -> None:
    """An expired token or a drained pool must not stop the required check."""
    printed = runner.select(pool=pool, token=READ_TOKEN)
    assert runner.selected == "shared"
    assert "::warning::" in printed and reading in printed
    assert runner.reads_and_a_write(READINESS_READ_TOKEN=READ_TOKEN) == [
        (DEFAULT_TOKEN, "api repos/o/r/pulls/1"),
        (DEFAULT_TOKEN, "api --method POST repos/o/r/statuses/abc"),
    ]


@needs_bash
@pytest.mark.parametrize("pool", ["4200 5000", "49 5000", "refused"])
def test_the_selection_never_prints_the_token(runner: Runner, pool: str) -> None:
    assert READ_TOKEN not in runner.select(pool=pool, token=READ_TOKEN)


@needs_bash
def test_a_read_can_opt_out_of_the_second_token(runner: Runner) -> None:
    """The collaborator-permission read answers by what the asking token may
    see, so it always asks with the default token."""
    runner.select(pool="4200 5000", token=READ_TOKEN)
    runner.calls_file.write_text("", encoding="utf-8")
    assert runner.bash(_helper_install()).returncode == 0
    done = runner.bash(
        'source "$RUNNER_TEMP/gh-retry.sh"\n'
        'READINESS_READ_TOKEN="" gh_retry gh api "repos/o/r/collaborators/x/permission"\n',
        READINESS_READ_TOKEN=READ_TOKEN,
    )
    assert done.returncode == 0, done.stderr
    assert [token for token, _ in runner.calls] == [DEFAULT_TOKEN]


# --- wiring -------------------------------------------------------------------


def test_both_workflows_select_the_pool_with_one_identical_step() -> None:
    readiness, sweep = _named(READINESS, SELECT), _named(SWEEP, SELECT)
    assert readiness == sweep
    assert readiness["env"]["READINESS_READ_TOKEN"] == SECRET_REF
    assert int(readiness["env"]["READ_POOL_FLOOR"]) > 0


def _function(text: str, name: str) -> str:
    """A shell function's body, stripped of indentation."""
    match = re.search(rf"^( *){name}\(\) \{{\n(.*?)^\1\}}$", text, re.S | re.M)
    assert match, f"{name}() not found"
    return "\n".join(line.strip() for line in match.group(2).splitlines())


def test_both_workflows_route_reads_through_the_same_function() -> None:
    readiness = _function(READINESS.read_text(encoding="utf-8"), "with_read_token")
    sweep = _function(SWEEP.read_text(encoding="utf-8"), "with_read_token")
    assert readiness == sweep
    assert 'GH_TOKEN="$READINESS_READ_TOKEN" "$@"' in readiness


@pytest.mark.parametrize("workflow", [READINESS, SWEEP], ids=lambda p: p.name)
def test_the_secret_reaches_a_step_only_through_its_env(workflow: Path) -> None:
    """Never interpolated into a script, where it would be in the log."""
    text = workflow.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "secrets.PR_READINESS_READ_TOKEN" in line and not line.lstrip().startswith("#"):
            assert line.strip() == f"READINESS_READ_TOKEN: {SECRET_REF}", line
    for step in _steps(workflow):
        for line in step.get("run", "").splitlines():
            if not re.search(r"\$\{?READINESS_READ_TOKEN", line):
                continue
            assert not re.search(r"\b(echo|printf|cat)\b", line), line


@pytest.mark.parametrize("workflow", [READINESS, SWEEP], ids=lambda p: p.name)
def test_every_step_that_reads_through_the_function_is_given_both_tokens(workflow: Path) -> None:
    users = [
        s
        for s in _steps(workflow)
        if 'source "$RUNNER_TEMP/gh-retry.sh"' in s.get("run", "") or s.get("name") == SWEEP_STEP
    ]
    assert users
    for step in users:
        assert step["env"]["GH_TOKEN"] == DEFAULT_REF, step.get("name")
        assert step["env"]["READINESS_READ_TOKEN"] == SECRET_REF, step.get("name")


_WRITE = re.compile(
    r"--method\s+(POST|PUT|PATCH|DELETE)|-X\s*(POST|PUT|PATCH|DELETE)"
    r"|\bgh\s+(label\s+create|pr\s+comment|workflow\s+run|run\s+rerun)|\s--input\b|\s-[fF]\s"
)


def _commands(script: str, marker: str) -> list[str]:
    """Each shell command that contains ``marker``, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        line
        for line in joined.splitlines()
        if marker in line and "()" not in line and not line.lstrip().startswith("#")
    ]


def test_no_write_goes_through_the_read_token_in_pr_readiness() -> None:
    commands = [c for s in _steps(READINESS) for c in _commands(s.get("run", ""), "gh_retry gh ")]
    assert commands
    for command in commands:
        assert not _WRITE.search(command), command


def test_the_sweep_reads_through_the_token_and_writes_without_it() -> None:
    script = _named(SWEEP, SWEEP_STEP)["run"]
    wrapped = _commands(script, "with_read_token ")
    assert wrapped
    for command in wrapped:
        assert not _WRITE.search(command), command
    assert any("readiness_sweep_scan.py" in c for c in wrapped)
    for marker in ("gh workflow run", "republish_withheld_verdict.py", "gh api rate_limit"):
        commands = _commands(script, marker)
        assert commands, marker
        for command in commands:
            assert "with_read_token" not in command, command


EVALUATORS = ("Evaluate disposition records", "Evaluate superseded verdicts")

# Stands in for pr_status.py: asks for a collaborator's permission, as the real
# evaluator does for every disposition author, and answers like a gate.
GATE_STUB = """import subprocess
call = ["gh", "api", "repos/o/r/collaborators/someone/permission"]
subprocess.run(call, check=False, capture_output=True)
print('{"ok": true, "violations": [], "unpublished": [], "blocking_dropped": []}')
"""


def test_the_evaluators_are_never_given_the_read_token() -> None:
    """They decide whether a disposition's author is a repository writer from a
    collaborator-permission read, and a token refused that read makes every
    author read as a non-writer: the records are dropped and the gate is clean."""
    for name in EVALUATORS:
        step = _named(READINESS, name)
        assert step["env"]["GH_TOKEN"] == DEFAULT_REF
        assert "READINESS_READ_TOKEN" not in step["env"]
        assert "READINESS_READ_TOKEN" not in step["run"]
    for step in _steps(READINESS):
        assert "export GH_TOKEN" not in step.get("run", ""), step.get("name")


@needs_bash
@pytest.mark.parametrize("name", EVALUATORS)
def test_an_evaluator_asks_for_permissions_with_the_default_token(
    runner: Runner, tmp_path: Path, name: str
) -> None:
    """With the readiness pool selected and its token in the environment by
    mistake, the evaluator still asks with the default token."""
    runner.select(pool="4200 5000", token=READ_TOKEN)
    assert runner.selected == "readiness"
    runner.calls_file.write_text("", encoding="utf-8")
    gate = tmp_path / "gate.py"
    gate.write_text(GATE_STUB, encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    done = runner.bash(
        _named(READINESS, name)["run"],
        GATE=str(gate),
        REPO="o/r",
        PR="1",
        SHA="a" * 40,
        GITHUB_OUTPUT=str(output),
        READINESS_READ_TOKEN=READ_TOKEN,
    )
    assert done.returncode == 0, done.stderr
    assert runner.calls == [(DEFAULT_TOKEN, "api repos/o/r/collaborators/someone/permission")]
    assert "ok=true" in output.read_text(encoding="utf-8")


def test_the_collaborator_permission_read_keeps_the_default_token() -> None:
    script = _named(READINESS, "Evaluate current revision")["run"]
    (command,) = _commands(script, "/collaborators/")
    assert 'READINESS_READ_TOKEN="" gh_retry gh api' in command


def test_pr_readiness_logs_the_pool_it_read_from() -> None:
    step = _named(READINESS, "Log the remaining REST budget")
    assert step["if"] == "always()"
    assert "pr-readiness: core rate limit -- " in step["run"]
    assert "pr-readiness: readiness pool rate limit -- " in step["run"]


@needs_bash
@pytest.mark.parametrize(
    ("program", "expected"),
    [
        (".ok", "true\n"),
        (".error", "null\n"),
        (".violations[]", "one\ntwo\n"),
        (".unpublished[]", ""),
        (".blocking_dropped[]", "lane\n"),
    ],
)
def test_the_jq_stand_in_answers_the_evaluator_filters(
    tmp_path: Path, program: str, expected: str
) -> None:
    """Run directly, so a host that has a real `jq` still checks the stand-in."""
    script = tmp_path / "jq_stand_in.py"
    script.write_text(JQ_STAND_IN, encoding="utf-8")
    document = '{"ok": true, "violations": ["one", "two"], "unpublished": [], "blocking_dropped": ["lane"]}'
    done = subprocess.run(  # noqa: S603 - fixed argv, test-local script
        [sys.executable, "-I", str(script), "-r", program],
        input=document,
        capture_output=True,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == expected
