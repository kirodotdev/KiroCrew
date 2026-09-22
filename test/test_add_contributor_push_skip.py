"""The rolling-contributors push guard must compare the README, not the root tree.

``.github/workflows/add-contributor.yml`` rebuilds ``chore/add-contributors`` from
the current base on every daily run (``git checkout -B``), then force-pushes it. A
force-push dismisses the open rolling PR's stale-review approvals and re-runs CI,
so on a repository whose merge gate is a required human approval an unconditional
daily push can starve auto-merge indefinitely. The step therefore guards the push:
when this run's README is byte-identical to the one already on the remote branch
there is nothing to publish, and the push is skipped.

The subject of that comparison has to be the README blob, because the README is the
only file the job authors. The rebuild inherits every OTHER tracked file from the
base, so the commit's root tree moves whenever anything at all lands on the base --
which in an active repository is every day. A root-tree comparison therefore reports
"changed" for a run whose README is identical, and the skip cannot fire.

These tests lift the guard's own shell verbatim out of the workflow and run it
against a real git repository and a real remote, once per combination of
(README changed?) x (base moved?), plus the first-ever run where the remote branch
does not exist yet. ``readme-identical-base-moved`` is the case a root-tree
comparison gets wrong.

The remote branch is populated by fetching FROM the work tree INTO the bare repo,
never by pushing: the guard only needs the ref to exist, and a fetch keeps the test
free of a push whose destination a reader cannot determine.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "add-contributor.yml"

STEP_NAME = "Open or update the contributors PR"
BRANCH = "chore/add-contributors"

#: Printed when the guard's decision says the workflow would push.
PUSH_MARKER = "GUARD_WOULD_PUSH"

#: Printed after the guard to prove its control flow reaches PR handling.
PR_ENSURE_MARKER = "PR_ENSURE_REACHED"

#: The guard records its push decision before fetching the rolling branch.
_GUARD_START = re.compile(r"^push_needed=true$", re.MULTILINE)


def _bash() -> str | None:
    """A Bash that accepts native paths from this Python process.

    On Windows ``shutil.which("bash")`` commonly resolves to the WSL launcher in
    System32, which starts a Linux process without translating the Windows argv
    paths. Git for Windows ships a native-path-aware Bash; prefer it.
    """
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                candidate = Path(root) / "Git" / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _proc_log(result: "subprocess.CompletedProcess[str]") -> str:
    """A failure message that cannot itself raise when a stream is ``None``."""
    return f"rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _step_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")) or {}
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == STEP_NAME and step.get("run"):
                return str(step["run"])
    raise AssertionError(
        f"{WORKFLOW.name} has no step named {STEP_NAME!r} with a `run:` script, so "
        "every assertion below would measure an empty string. Update STEP_NAME to "
        "the step that guards and performs the rolling-branch push."
    )


def _guard_block(script: str) -> str:
    """The push decision shell, verbatim, up to the ``fi`` closing it."""
    match = _GUARD_START.search(script)
    if match is None:
        raise AssertionError(
            f"{WORKFLOW.name} / {STEP_NAME!r} does not open its push guard with "
            "`push_needed=true`, so the guard could not be extracted and the "
            "behavioural tests would run nothing. Re-anchor _GUARD_START."
        )
    lines = script[match.start() :].splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "fi":
            return "\n".join(lines[: index + 1])
    raise AssertionError(
        f"{WORKFLOW.name} / {STEP_NAME!r} has no `fi` closing the push guard, so its "
        "extent is undecidable. The guard must stay a single `if ... ; then ... fi`."
    )


def _git_env(home: Path, tmp: Path) -> dict[str, str]:
    """A git environment that cannot read or write the developer's own state.

    ``HOME`` and ``PATH`` travel as a pair: with a substituted ``HOME`` a bare tool
    name resolved through a version-manager shim finds no tool state and can hang,
    so git's own directory goes first on ``PATH``. Identity is supplied through the
    environment because the host's global config is deliberately unreachable.
    """
    git = shutil.which("git") or "git"
    path = os.pathsep.join([str(Path(git).resolve().parent), os.environ.get("PATH", "")])
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Contributor Bot Test",
        "GIT_AUTHOR_EMAIL": "bot@example.invalid",
        "GIT_COMMITTER_NAME": "Contributor Bot Test",
        "GIT_COMMITTER_EMAIL": "bot@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
        "TMPDIR": str(tmp),
        "TMP": str(tmp),
        "TEMP": str(tmp),
    }
    for name in ("SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed in {repo}\n{_proc_log(result)}"
    return (result.stdout or "").strip()


def _scenario(
    tmp_path: Path,
    *,
    remote_branch_exists: bool,
    readme_changes: bool,
    base_moves: bool,
) -> tuple[Path, dict[str, str]]:
    """Build a checkout whose HEAD is this run's freshly rebuilt rolling branch.

    The remote rolling branch, when it exists, always carries ``REMOTE_README``.
    ``readme_changes`` decides whether this run's contributor set produced the same
    bytes or different ones; ``base_moves`` decides whether an unrelated commit
    landed on the base between the two runs.
    """
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    for directory in (home, scratch):
        directory.mkdir()
    env = _git_env(home, scratch)

    remote_readme = "entryA\nentryB\n"
    head_readme = "entryA\nentryB\nentryC\n" if readme_changes else remote_readme

    origin.mkdir()
    _git(origin, "init", "--quiet", "--bare", env=env)
    work.mkdir()
    _git(work, "init", "--quiet", "--initial-branch=main", env=env)
    _git(work, "remote", "add", "origin", str(origin), env=env)

    (work / "README.md").write_text("entryA\n", encoding="utf-8")
    (work / "src.py").write_text("v1\n", encoding="utf-8")
    _git(work, "add", "-A", env=env)
    _git(work, "commit", "--quiet", "-m", "base", env=env)

    if remote_branch_exists:
        _git(work, "checkout", "--quiet", "-B", BRANCH, env=env)
        (work / "README.md").write_text(remote_readme, encoding="utf-8")
        _git(work, "add", "README.md", env=env)
        _git(work, "commit", "--quiet", "-m", "docs: add new contributors to README", env=env)
        _git(
            origin,
            "fetch",
            "--quiet",
            str(work),
            f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
            env=env,
        )
        _git(work, "checkout", "--quiet", "main", env=env)

    if base_moves:
        (work / "src.py").write_text("v2\n", encoding="utf-8")
        _git(work, "add", "-A", env=env)
        _git(work, "commit", "--quiet", "-m", "an unrelated commit on the base", env=env)

    # The run under test: rebuild the rolling branch from the current base, write
    # this run's README, commit -- exactly what the step does before the guard.
    _git(work, "checkout", "--quiet", "-B", BRANCH, "main", env=env)
    (work / "README.md").write_text(head_readme, encoding="utf-8")
    _git(work, "add", "README.md", env=env)
    _git(work, "commit", "--quiet", "-m", "docs: add new contributors to README", env=env)
    return work, env


def _run_guard(work: Path, env: dict[str, str], bash: str) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f"BRANCH={BRANCH}\n"
        f"{_guard_block(_step_script())}\n"
        f'if [ "$push_needed" = true ]; then echo {PUSH_MARKER}; fi\n'
        f"echo {PR_ENSURE_MARKER}\n"
    )
    return subprocess.run(
        [bash, "-c", script],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_the_guard_block_is_actually_extracted():
    """A silent extraction miss would let every behavioural assertion pass empty."""
    guard = _guard_block(_step_script())
    assert guard.splitlines()[0].strip() == "push_needed=true", guard
    assert guard.splitlines()[-1].strip() == "fi", guard
    assert "exit 0" not in guard, (
        "the extracted guard exits the step, so matching content cannot reach PR "
        f"handling:\n{guard}"
    )
    assert "push_needed=false" in guard, guard
    assert "remote_commit" in guard, guard


def test_the_force_push_is_gated_on_the_guard_decision():
    """The decision is worthless unless the push itself honours it.

    ``_guard_block`` stops at the decision, so the behavioural cases below read
    the flag rather than the push. A step that recorded ``push_needed=false`` and
    then pushed anyway would satisfy all of them while force-pushing every day.
    This reads the WHOLE step and pins that wiring.
    """
    script = _step_script()
    lines = script.splitlines()

    pushes = [line for line in lines if re.search(r"\bgit push\b", line)]
    assert len(pushes) == 1, (
        f"{WORKFLOW.name} / {STEP_NAME!r} holds {len(pushes)} `git push` lines. This "
        f"test pins exactly one, so a second could otherwise slip past ungated:\n{pushes}"
    )

    gate = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("if ")
            and "push_needed" in line
            and line.rstrip().endswith("then")
        ),
        None,
    )
    assert gate is not None, (
        f"{WORKFLOW.name} / {STEP_NAME!r} does not gate its force-push on "
        "`push_needed`, so deciding to skip cannot actually stop the push."
    )
    closing = next(
        (index for index in range(gate + 1, len(lines)) if lines[index].strip() == "fi"),
        None,
    )
    assert (
        closing is not None
    ), f"{WORKFLOW.name} / {STEP_NAME!r} opens a `push_needed` gate that no `fi` closes."
    gated = "\n".join(lines[gate + 1 : closing])
    assert re.search(r"\bgit push\b", gated), (
        f"{WORKFLOW.name} / {STEP_NAME!r} gates something other than the force-push on "
        f"`push_needed`; the push itself runs unconditionally:\n{gated}"
    )


def test_unchanged_readme_reaches_pr_ensure(tmp_path: Path):
    """Matching content skips the push decision and continues to PR handling."""
    bash = _bash()
    if bash is None:
        pytest.skip("no native-path Bash available to run the workflow's own shell")

    work, env = _scenario(
        tmp_path,
        remote_branch_exists=True,
        readme_changes=False,
        base_moves=True,
    )
    result = _run_guard(work, env, bash)
    assert result.returncode == 0, f"the guard itself errored\n{_proc_log(result)}"
    assert PUSH_MARKER not in (result.stdout or ""), _proc_log(result)
    assert PR_ENSURE_MARKER in (
        result.stdout or ""
    ), "matching content stopped the step before PR handling\n" + _proc_log(result)


def test_the_skip_compares_the_readme_blob_not_the_root_tree():
    """Name the defect class statically, so a revert is red without running git.

    ``HEAD^{tree}`` is the commit's ROOT tree and covers every tracked file, so it
    cannot stand in for "did the README change".
    """
    guard = _guard_block(_step_script())
    assert "README.md" in guard, (
        f"{WORKFLOW.name} / {STEP_NAME!r} guards the force-push without naming "
        "README.md in the comparison. The rolling branch is rebuilt from the base "
        "each run, so only the README blob answers whether this run has anything to "
        f"publish:\n{guard}"
    )
    assert not re.search(r"\^\{tree\}", guard), (
        f"{WORKFLOW.name} / {STEP_NAME!r} compares a root tree (`^{{tree}}`). The "
        "rebuild inherits every other tracked file from the base, so that tree "
        "differs whenever anything lands on the base and the skip can never fire in "
        f"an active repository:\n{guard}"
    )


@pytest.mark.parametrize(
    "remote_branch_exists,readme_changes,base_moves,should_skip",
    [
        pytest.param(False, False, False, False, id="first-ever-run-no-remote-branch"),
        pytest.param(True, False, False, True, id="readme-identical-base-still"),
        pytest.param(True, False, True, True, id="readme-identical-base-moved"),
        pytest.param(True, True, False, False, id="readme-changed-base-still"),
        pytest.param(True, True, True, False, id="readme-changed-base-moved"),
    ],
)
def test_push_skip_fires_exactly_when_the_readme_is_unchanged(
    tmp_path: Path,
    remote_branch_exists: bool,
    readme_changes: bool,
    base_moves: bool,
    should_skip: bool,
):
    bash = _bash()
    if bash is None:
        pytest.skip("no native-path Bash available to run the workflow's own shell")

    work, env = _scenario(
        tmp_path,
        remote_branch_exists=remote_branch_exists,
        readme_changes=readme_changes,
        base_moves=base_moves,
    )
    result = _run_guard(work, env, bash)
    assert result.returncode == 0, f"the guard itself errored\n{_proc_log(result)}"

    skipped = PUSH_MARKER not in (result.stdout or "")
    if should_skip:
        assert skipped, (
            "the guard let the force-push through for a run whose README is "
            "byte-identical to the remote branch's. Every such push dismisses the "
            "open rolling PR's approvals and re-runs CI, which starves auto-merge "
            "behind a required human review.\n" + _proc_log(result)
        )
    else:
        assert not skipped, (
            "the guard skipped the push for a run that has something to publish, so "
            "the README update never reaches the rolling branch.\n" + _proc_log(result)
        )
