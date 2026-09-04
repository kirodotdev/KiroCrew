"""Contract tests for release.yml's stable publication gate.

The gate exists because a tag name is the only thing that selects the release
channel and nothing about the run is attended: on v0.1.3 the stable CLI wheel
was public 3m41s after the tag push, the CDN key is immutable, and
``cancel-in-progress: false`` means a corrected tag queues rather than
superseding. ``v0.2.0-rc.2`` mistyped as ``v0.2.0`` is therefore an
unrecoverable stable release.

These tests EXECUTE the workflow's actual shell step against throwaway git
repositories, with a ``gh`` shim on PATH standing in for the API, so a rewritten
check, a regex that stops treating the version literally, a query failure that
starts passing silently, or a publish lane that quietly stops depending on the
gate cannot slip through.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the gate step runs under bash on ubuntu-latest",
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
GATE_JOB = "stable-gate"
STEP_NAME = "Verify stable publication preconditions"

#: Every job that makes bytes public, or publishes the GitHub Release that
#: points at them. Each one MUST depend on the gate; this list is the ratchet.
#: Linux publishes one job per arch, and BOTH must be gated -- an ungated arch
#: would put stable bytes on the CDN without the preconditions check.
PUBLISH_JOBS = (
    "publish-cli",
    "publish-linux-appimage-x64",
    "publish-linux-appimage-arm64",
    "publish-linux-deb-x64",
    "publish-linux-deb-arm64",
    "publish-linux-rpm-x64",
    "publish-linux-rpm-arm64",
    "publish-docker",
    "sign-and-notarize",
    "github-release",
)

#: What the gh shim prints when it is standing in for a matching release run.
#: The real `gh api --jq` emits one head_branch per matching run.
MATCHING_RUN = "v1.2.3-rc.1"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _needs(spec: dict) -> list[str]:
    """A job's ``needs``, normalized -- the key accepts a bare string too."""
    needs = spec.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _gate_step() -> dict:
    steps = _workflow()["jobs"][GATE_JOB]["steps"]
    step = next((item for item in steps if item.get("name") == STEP_NAME), None)
    assert step is not None, f"release workflow step {STEP_NAME!r} not found"
    return step


def _gate_script() -> str:
    script = _gate_step()["run"]
    # The step reads its inputs through env, never through `${{ }}` spliced
    # into the shell. Keep it that way: a version string interpolated into a
    # shell body is a command-injection shape, and it also breaks this harness.
    assert "${{" not in script, "gate script must take inputs via env, not inline expressions"
    return script


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "gate test",
            "GIT_AUTHOR_EMAIL": "gate@example.invalid",
            "GIT_COMMITTER_NAME": "gate test",
            "GIT_COMMITTER_EMAIL": "gate@example.invalid",
        },
    )


def _make_repo(
    tmp_path: Path,
    *,
    changelog_headings: Iterable[str] = (),
    stable_tag: str | None = None,
    declared_version: str | None = "1.2.3",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    body = "# Changelog\n\n" + "".join(
        f"{heading}\n\nnotes for {heading}\n\n" for heading in changelog_headings
    )
    (repo / "CHANGELOG.md").write_text(body, encoding="utf-8")
    tracked = ["CHANGELOG.md"]

    # The gate also compares the tag against all three version declarations, so
    # a fixture repo needs them or every case would fail on the missing files
    # rather than on the behaviour under test. ``declared_version=None`` omits
    # them deliberately, for the case that pins the missing-file failure.
    if declared_version is not None:
        (repo / "src" / "kiro_crew").mkdir(parents=True)
        (repo / "src" / "kiro_crew" / "__init__.py").write_text(
            f'__version__ = "{declared_version}"\n', encoding="utf-8"
        )
        (repo / "pyproject.toml").write_text(
            f'[project]\nname = "kirocrew"\nversion = "{declared_version}"\n', encoding="utf-8"
        )
        (repo / "website" / "electron").mkdir(parents=True)
        (repo / "website" / "electron" / "package.json").write_text(
            '{\n  "name": "kirocrew-desktop",\n' f'  "version": "{declared_version}",\n' "}\n",
            encoding="utf-8",
        )
        tracked += [
            "src/kiro_crew/__init__.py",
            "pyproject.toml",
            "website/electron/package.json",
        ]

    _git(repo, "add", *tracked)
    _git(repo, "commit", "-q", "-m", "first")
    if stable_tag:
        _git(repo, "tag", stable_tag)
    return repo


def _run(
    repo: Path,
    *,
    channel: str,
    version: str,
    override: str = "",
    gh: str = "match",
    promote_mode: str = "false",
) -> subprocess.CompletedProcess[str]:
    """Execute the real gate step with a ``gh`` shim on PATH.

    ``gh`` selects the stand-in behaviour: ``match`` (a successful release run
    exists), ``none`` (queried fine, nothing matched) or ``fail`` (the API call
    itself failed). The shim mimics the real contract that matters here --
    non-zero exit for an API failure, zero-with-no-output for no match.
    """
    bin_dir = repo / "shim-bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$GH_SHIM_MODE" = "fail" ]; then\n'
        '  echo "HTTP 503: upstream connect error" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ -n "$GH_SHIM_OUT" ]; then printf \'%s\\n\' "$GH_SHIM_OUT"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    return subprocess.run(
        ["bash", "-c", _gate_script()],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(repo),
            "CHANNEL": channel,
            "VERSION": version,
            "PROMOTE_MODE": promote_mode,
            "OVERRIDE": override,
            "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
            "GH_TOKEN": "shim",
            "GH_SHIM_MODE": "fail" if gh == "fail" else "ok",
            "GH_SHIM_OUT": MATCHING_RUN if gh == "match" else "",
        },
    )


# ── wiring: the gate cannot be bypassed by adding a publish lane ──────────


@pytest.mark.parametrize("job", PUBLISH_JOBS)
def test_every_publish_job_depends_on_the_gate(job: str) -> None:
    needs = _workflow()["jobs"][job]["needs"]
    assert GATE_JOB in needs, f"{job} publishes without waiting for {GATE_JOB}"


def test_gate_job_has_no_job_level_condition() -> None:
    """A skipped dependency skips its dependents.

    An ``if:`` on the gate would make insider and nightly publish jobs skip
    entirely instead of passing through, so the channel test must stay inside
    the step body.
    """
    assert "if" not in _workflow()["jobs"][GATE_JOB]


def _skippable_jobs(jobs: dict) -> set[str]:
    """Every job that can report ``skipped``, transitively.

    A job with an ``if:`` can skip on its own. A job without one skips whenever
    anything it needs skipped, so the property propagates down the graph -- and
    it propagates THROUGH an ``always()``-guarded job that itself succeeded,
    which is the part that is not obvious (actions/runner#2205).
    """
    skippable = {name for name, spec in jobs.items() if "if" in spec}
    while True:
        grown = {
            name
            for name, spec in jobs.items()
            if name not in skippable and any(n in skippable for n in _needs(spec))
        }
        if not grown:
            return skippable
        skippable |= grown


def test_no_job_downstream_of_a_skippable_one_relies_on_the_default_condition() -> None:
    """A job with no ``if:`` does not run in a graph that skips anything.

    With no ``if:`` the condition is ``success()``, which is false when ANY job
    in the needs closure was skipped. Every release graph skips something by
    design -- ``resolve-promotion`` on insider, the build jobs on stable -- so a
    lane that leans on the default never executes. It also never goes red: a
    skip is not a failure, so the run still concludes ``success``. That is how
    five consecutive releases, v0.3.0 among them, published bytes to a channel
    with no GitHub Release page and nobody noticed.

    ``stable-gate`` and ``build-windows`` are not exempted by name: they need
    only ``version``, which has no dependencies and therefore cannot skip.
    """
    jobs = _workflow()["jobs"]
    skippable = _skippable_jobs(jobs)
    offenders = {
        name: str(spec.get("if", ""))
        for name, spec in jobs.items()
        if any(n in skippable for n in _needs(spec)) and "needs." not in str(spec.get("if", ""))
    }
    assert not offenders, (
        "these jobs depend on a job that can be skipped, but their condition "
        "does not name their dependencies' results, so a skip upstream silently "
        f"skips them: {offenders}"
    )


def test_gate_checks_out_full_history() -> None:
    steps = _workflow()["jobs"][GATE_JOB]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0


def test_gate_job_can_read_workflow_runs() -> None:
    """The promotion check is an `actions: read` API call; least privilege only."""
    permissions = _workflow()["jobs"][GATE_JOB]["permissions"]
    assert permissions["actions"] == "read"
    assert permissions["contents"] == "read"
    assert "write" not in str(permissions.values())


# ── the promotion query's shape (the shim cannot exercise jq itself) ──────


def test_promotion_query_requires_a_successful_push_run() -> None:
    script = _gate_script()
    assert "status=success" in script, "a cancelled or failed prerelease run must not count"
    assert "event=push" in script


def test_promotion_query_filters_on_both_the_commit_and_the_tag() -> None:
    """Dropping either selector would accept the wrong run."""
    script = _gate_script()
    assert "select(.head_sha == env.STABLE_COMMIT)" in script
    assert 'startswith("v" + env.VERSION + "-")' in script


def test_promotion_query_reads_its_inputs_from_env() -> None:
    """No shell value is spliced into the jq filter text."""
    script = _gate_script()
    start = script.index("--jq '")
    program = script[start + len("--jq '"):]
    program = program[: program.index("'")]
    assert "$" not in program, f"jq program splices a shell value: {program!r}"
    assert "env.VERSION" in program


# ── behaviour: non-stable channels pass straight through ──────────────────


@pytest.mark.parametrize("channel", ["insider", "nightly"])
def test_non_stable_channels_pass_with_no_evidence_at_all(tmp_path: Path, channel: str) -> None:
    repo = _make_repo(tmp_path)  # no changelog section, no tag
    result = _run(repo, channel=channel, version="1.2.3", gh="none")
    assert result.returncode == 0, result.stderr
    assert "does not apply" in result.stdout


# ── behaviour: a well-formed stable promotion ─────────────────────────────


def test_documented_and_promoted_release_passes(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3")
    assert result.returncode == 0, result.stdout + result.stderr
    assert MATCHING_RUN in result.stdout
    assert "all preconditions satisfied" in result.stdout


# ── behaviour: each precondition fails on its own ─────────────────────────


def test_a_release_branch_still_declaring_the_rc_spelling_blocks(tmp_path: Path) -> None:
    """The tag being bare is not evidence that the branch is.

    A rebuild re-stamps the artifact, so a stale declaration would ship correct
    bytes while leaving every source install and later RC on the old spelling.
    The 0.4.0 promotion was nearly tagged in exactly this state because only the
    tag name was checked.
    """
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
        declared_version="1.2.3-rc.9",
    )
    result = _run(repo, channel="stable", version="1.2.3")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "declares '1.2.3-rc.9', not the bare '1.2.3'" in result.stdout
    assert "Land the drop-RC-suffix PR" in result.stdout


def test_a_missing_version_file_fails_closed_rather_than_aborting(tmp_path: Path) -> None:
    """An absent declaration must be a verdict, not a crash.

    Left to ``set -e``, sed's own error would abort the script mid-way and skip
    both the failure summary and the override path -- the same trap the tag
    lookup is guarded against.
    """
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
        declared_version=None,
    )
    result = _run(repo, channel="stable", version="1.2.3")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is missing from the tagged tree" in result.stdout
    # Still reached the summary rather than dying inside the loop.
    assert "unmet precondition(s)" in result.stdout


def test_promoting_bytes_warns_that_the_shipped_version_keeps_the_rc_stamp(
    tmp_path: Path,
) -> None:
    """Byte reuse is allowed, but the cost has to be visible in the run log."""
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3", promote_mode="true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will ship an RC-stamped version" in result.stdout

    rebuilt = _run(repo, channel="stable", version="1.2.3", promote_mode="false")
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    assert "carry the bare 1.2.3" in rebuilt.stdout


def test_missing_changelog_section_blocks(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.2] — 2026-08-01"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3")
    assert result.returncode == 1
    assert "no '## [1.2.3]' section" in result.stdout


def test_no_successful_release_run_blocks(tmp_path: Path) -> None:
    """The defect GPT 5.6 found: a leftover tag from a cancelled run."""
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3", gh="none")
    assert result.returncode == 1
    assert "no SUCCESSFUL release run" in result.stdout
    assert "cancelled or failed does not count" in result.stdout


def test_a_failed_query_fails_closed_and_says_so(tmp_path: Path) -> None:
    """A check that could not run is not a check that passed."""
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3", gh="fail")
    assert result.returncode == 1
    assert "could not list successful release runs" in result.stdout
    assert "503" in result.stdout, "the underlying error must be surfaced, not swallowed"
    # Distinguishable from the "checked, nothing matched" verdict.
    assert "no SUCCESSFUL release run" not in result.stdout


def test_absent_stable_tag_fails_closed_with_a_readable_error(tmp_path: Path) -> None:
    """The tag is the trigger in CI, but it can be deleted before a re-run.

    Found by replaying the gate over real repository history: an unguarded
    ``git rev-list`` aborted the script under ``set -e`` with git's own
    "ambiguous argument" fatal and exit 128, which skipped both the failure
    summary and the override path.
    """
    repo = _make_repo(tmp_path, changelog_headings=["## [1.2.3] — 2026-08-07"])
    result = _run(repo, channel="stable", version="1.2.3")
    assert result.returncode == 1, f"expected a clean failure, got {result.returncode}"
    assert "tag v1.2.3 is not present" in result.stdout
    assert "ambiguous argument" not in result.stderr
    assert "blocked by 1 unmet precondition" in result.stdout


def test_both_failures_are_reported_together(tmp_path: Path) -> None:
    """One run should name everything wrong, not fail on the first check."""
    repo = _make_repo(tmp_path, stable_tag="v1.2.3")
    result = _run(repo, channel="stable", version="1.2.3", gh="none")
    assert result.returncode == 1
    assert "no '## [1.2.3]' section" in result.stdout
    assert "no SUCCESSFUL release run" in result.stdout
    assert "blocked by 2 unmet precondition" in result.stdout


# ── behaviour: the version string is matched literally ────────────────────


def test_a_longer_version_heading_does_not_satisfy_a_shorter_release(tmp_path: Path) -> None:
    """`## [1.2.31]` must not pass for 1.2.3 -- the anchor is a literal prefix."""
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.31] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    assert _run(repo, channel="stable", version="1.2.3").returncode == 1


def test_dots_in_the_version_are_not_regex_wildcards(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1x2x3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    assert _run(repo, channel="stable", version="1.2.3").returncode == 1


def test_a_heading_that_is_not_at_the_line_start_does_not_count(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, stable_tag="v1.2.3")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\nsee also ## [1.2.3] elsewhere\n", encoding="utf-8"
    )
    assert _run(repo, channel="stable", version="1.2.3").returncode == 1


# ── behaviour: the override is deliberate and version-scoped ──────────────


def test_override_matching_the_version_releases_anyway(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, stable_tag="v1.2.3")
    result = _run(repo, channel="stable", version="1.2.3", override="1.2.3", gh="none")
    assert result.returncode == 0, result.stdout
    assert "::warning::" in result.stdout
    assert "overridden for 1.2.3" in result.stdout


def test_override_rescues_an_api_outage_too(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    assert _run(repo, channel="stable", version="1.2.3", override="1.2.3", gh="fail").returncode == 0


def test_override_for_a_different_version_does_not_apply(tmp_path: Path) -> None:
    """A stale override left in repo settings must not clear a later release."""
    repo = _make_repo(tmp_path, stable_tag="v1.2.3")
    assert _run(repo, channel="stable", version="1.2.3", override="1.2.2", gh="none").returncode == 1


def test_override_does_not_fire_when_nothing_is_wrong(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        changelog_headings=["## [1.2.3] — 2026-08-07"],
        stable_tag="v1.2.3",
    )
    result = _run(repo, channel="stable", version="1.2.3", override="1.2.3")
    assert result.returncode == 0
    assert "::warning::" not in result.stdout
