"""Round-16 review findings.

1. Both writers validated the destination, then resolved the parent again and pinned
   whatever that newer resolution named. An ancestor swapped in between moved the write
   into the resolution nobody had checked -- including the governance trust root, which
   is the one place the guard exists to keep a benchmark out of.
2. The nightly rebuilt the baseline branch from `main` and force-pushed it, discarding
   any commit a maintainer had added to the open PR. `--force-with-lease` alone does not
   fix that: the lease only proves the tip being overwritten is the one we fetched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import kiro_crew.eval.bench.safepath as sp
from kiro_crew.eval.bench.safepath import UnsafePathError

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "memory-benchmark.yml"
)


def _swap_after_first_check(
    monkeypatch: pytest.MonkeyPatch, link: Path, new_target: Path
) -> None:
    """Let the FIRST guard call pass, swapping *link* while it runs.

    Models the time-of-check: the destination was acceptable when it was judged, and
    the ancestor became something else immediately afterwards. Every later call goes to
    the real guard, so what the test proves is that a later call happens at all -- and
    that it looks at where the parent points NOW.
    """
    real_guard = sp.guard_write_path
    seen = {"n": 0}

    def guard_then_swap(path: str | Path, *, what: str) -> Path:
        seen["n"] += 1
        if seen["n"] == 1:
            link.unlink()
            link.symlink_to(new_target)
            return Path(path)
        return real_guard(path, what=what)

    monkeypatch.setattr(sp, "guard_write_path", guard_then_swap)


@pytest.fixture
def trust_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "crew"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


def _out_link_or_skip(tmp_path: Path) -> tuple[Path, Path]:
    benign = tmp_path / "benign"
    benign.mkdir()
    out = tmp_path / "reports"
    try:
        out.symlink_to(benign)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")
    return out, benign


@pytest.mark.skipif(
    not sp._supports_pinned_walk(), reason="the pinned walk is what re-validates"
)
def test_an_ancestor_swapped_after_the_check_cannot_reach_the_trust_root(
    tmp_path: Path, trust_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The atomic writer must refuse rather than publish into the swapped parent.

    The destination name is `security_policy.json` because that is what the guard
    actually protects: `is_sensitive_path` flags the governance LEAVES, not the whole
    data home, so a test aimed at the directory itself would pass for the wrong reason.
    """
    out, _ = _out_link_or_skip(tmp_path)
    _swap_after_first_check(monkeypatch, out, trust_root)

    with pytest.raises(UnsafePathError, match="protected location"):
        sp.write_text_atomic_nofollow(
            out / "security_policy.json", "{}", what="JSON report"
        )

    assert not (trust_root / "security_policy.json").exists(), (
        "the write landed on the governance policy file"
    )


@pytest.mark.skipif(
    not sp._supports_pinned_walk(), reason="the pinned walk is what re-validates"
)
def test_the_creating_writer_re_validates_the_swapped_parent_too(
    tmp_path: Path, trust_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole existed at both call sites, so both go through one helper."""
    out, _ = _out_link_or_skip(tmp_path)
    _swap_after_first_check(monkeypatch, out, trust_root)

    with pytest.raises(UnsafePathError, match="protected location"):
        sp.open_write_nofollow(out / "security_policy.json", what="corpus file")

    assert not (trust_root / "security_policy.json").exists()


@pytest.mark.skipif(
    not sp._supports_pinned_walk(), reason="the pinned walk is what re-validates"
)
def test_an_unswapped_parent_still_writes(tmp_path: Path, trust_root: Path) -> None:
    """The re-validation must not refuse the ordinary symlinked --out-dir.

    `/tmp` is itself a link on macOS, so refusing every symlinked ancestor would break
    the normal case rather than harden it.
    """
    out, benign = _out_link_or_skip(tmp_path)

    sp.write_text_atomic_nofollow(out / "report.json", "{}", what="JSON report")

    assert (benign / "report.json").read_text(encoding="utf-8") == "{}"


def _accept_step_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if "accept the new baseline" in str(step.get("name", "")):
                return str(step["run"])
    raise AssertionError("the accept step is gone")


def _effective_lines(script: str) -> list[str]:
    """Comment lines are excluded: three sweeps have matched their own documentation."""
    return [
        ln.strip()
        for ln in script.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_the_nightly_never_force_pushes_the_baseline_branch() -> None:
    """A bare `--force` on a branch with an open PR discards review work."""
    lines = _effective_lines(_accept_step_script())
    pushes = [ln for ln in lines if ln.startswith("git push")]
    assert pushes, "the accept step no longer pushes"
    for push in pushes:
        assert "--force-with-lease" in push, push
        assert "--force " not in push.replace("--force-with-lease", ""), push


def test_the_nightly_builds_on_the_open_branch_when_one_exists() -> None:
    """The lease is not enough on its own — the commit has to sit on top.

    Fetching the branch and committing on its tip is what preserves a maintainer's
    commit; the lease only prevents two overlapping nightlies from racing.
    """
    lines = _effective_lines(_accept_step_script())
    joined = "\n".join(lines)
    assert "git ls-remote" in joined, "the branch's existence is never checked"
    assert "git fetch --depth=1 origin" in joined, "the open branch is never fetched"
    assert any(
        ln.startswith("git checkout -B") and "FETCH_HEAD" in ln for ln in lines
    ), "the branch is not built from the fetched tip"


def test_the_baseline_diff_is_taken_after_the_branch_switch() -> None:
    """Diffing before the switch compares against main and adds an empty commit."""
    lines = _effective_lines(_accept_step_script())
    switch = next(i for i, ln in enumerate(lines) if ln.startswith("git checkout"))
    diff = next(i for i, ln in enumerate(lines) if "git diff --cached" in ln)
    assert switch < diff, "the staged diff is still taken against main"


def test_the_environment_agrees_the_pinned_walk_is_available() -> None:
    """Guards the skips above: silently skipping every case would prove nothing."""
    assert sp._supports_pinned_walk() is (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
    )
