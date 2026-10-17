"""The AI review lanes see the same diff from a shallow clone as from a full one.

The ten review lanes (Opus, GPT, Design, UX, First Principles, each with a fork
twin) check out shallow, ask GitHub's compare API for the merge-base, fetch
that one commit at depth 1, and diff the two-dot `<merge-base>..<tip>`. A
two-dot diff compares two trees, so it needs no history.

The invariant is that every lane hands its reviewer the SAME diff and the same
changed-file lists a three-dot `base...tip` range gives on a full clone. These
cases prove it by running each lane's REAL
merge-base step (and the fork lanes' real fetch step) against a local origin,
in a clone as shallow as the runner's, and comparing every diff spelling the
lanes use with the three-dot range on a full clone. The `gh` the steps call is
a stub answering the compare API from `git merge-base` on the origin, which is
what GitHub computes.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

SAME_REPO_LANES = (
    "claude-review.yml",
    "codex-review.yml",
    "design-review.yml",
    "ux-review.yml",
    "first-principles-review.yml",
)
FORK_LANES = (
    "fork-opus-review.yml",
    "fork-gpt-review.yml",
    "fork-design-review.yml",
    "fork-ux-review.yml",
    "fork-first-principles-review.yml",
)
#: The step outputs each same-repo lane needs, and which three-dot range each
#: one replaces: `pr` = base.sha...head.sha, `tree` = base.sha...<checkout>,
#: `patch_base` = <merge ref first parent>...<checkout>.
LANE_OUTPUTS = {
    "claude-review.yml": {"pr"},
    "codex-review.yml": {"tree"},
    "design-review.yml": {"pr", "patch_base"},
    "ux-review.yml": {"tree", "patch_base"},
    "first-principles-review.yml": {"tree"},
}
#: Every diff spelling the lanes and pr-committed-evidence.sh use.
DIFF_FORMS = (
    ("--no-color",),
    ("--name-only",),
    ("--name-only", "--diff-filter=AM"),
    ("--numstat", "--", ".", ":(exclude)src/kiro_crew/_vendor"),
    (
        "-z",
        "-M",
        "--name-status",
        "--diff-filter=d",
        "--",
        "temp-screenshots",
        ".github/screenshots",
    ),
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="runs the lanes' bash steps against real git; Linux/macOS runners cover it",
)


def _step(lane: str, name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == name:
                return step
    raise AssertionError(f"{lane}: no step named {name!r}")


def _git_env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


class _Origin:
    """A local origin holding a base branch, PR heads and GitHub-style merge refs."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.path = tmp / "origin"
        self.env = _git_env(tmp)
        self.git("init", "-q", "-b", "main", str(self.path), cwd=tmp)
        self.git("config", "uploadpack.allowAnySHA1InWant", "true")
        self.git("config", "commit.gpgsign", "false")
        self._n = 0

    def git(self, *args: str, cwd: Path | None = None) -> str:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd or self.path,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return out.stdout.strip()

    def commit(self, files: dict[str, str | None]) -> str:
        for rel, body in files.items():
            target = self.path / rel
            if body is None:
                self.git("rm", "-q", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            self.git("add", rel)
        self._n += 1
        self.git("commit", "-q", "-m", f"c{self._n}")
        return self.git("rev-parse", "HEAD")

    def merge_ref(self, pr: int, base_tip: str, head: str) -> str:
        """`refs/pull/<pr>/merge`: parents (base tip, head), as GitHub mints it."""
        self.git("checkout", "-q", "--detach", base_tip)
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {head} into {base_tip}", head)
        merge = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/pull/{pr}/merge", merge)
        self.git("update-ref", f"refs/pull/{pr}/head", head)
        return merge


def _scenarios(tmp: Path) -> tuple[_Origin, list[dict]]:
    o = _Origin(tmp)
    o.commit({"README.md": "r\n", "src/a.py": "a = 1\n", "src/kiro_crew/_vendor/v.py": "v\n"})
    fork_point = o.commit({"src/b.py": "b = 1\n", ".github/screenshots/old.png": "png-old\n"})
    out: list[dict] = []

    # 1. Normal PR: one commit on a base that has not moved.
    o.git("checkout", "-q", "-b", "normal", fork_point)
    normal = o.commit({"src/a.py": "a = 2\n", "website/x.ts": "x\n"})
    out.append({"name": "normal", "base": fork_point, "head": normal, "base_tip": fork_point})

    # 2. Multi-commit PR, including a delete, a rename and committed media.
    o.git("checkout", "-q", "-b", "multi", fork_point)
    o.commit({"src/c.py": "c = 1\n"})
    o.commit({"src/b.py": None, "temp-screenshots/shot.png": "png-new\n"})
    o.git("mv", ".github/screenshots/old.png", ".github/screenshots/renamed.png")
    multi = o.commit({"src/kiro_crew/_vendor/v.py": "v2\n"})
    out.append({"name": "multi-commit", "base": fork_point, "head": multi, "base_tip": fork_point})

    # 3. Base moved after branching: main is ahead of the merge-base. base.sha
    #    is the moved tip, and separately a stale base.sha (the fork point,
    #    which the event can still carry) under a merge ref minted on the tip.
    o.git("checkout", "-q", "main")
    o.git("reset", "-q", "--hard", fork_point)
    o.commit({"src/main_only.py": "m = 1\n"})
    moved = o.commit({"src/a.py": "a = 1  # main\n", "docs/d.md": "d\n"})
    o.git("checkout", "-q", "-b", "behind", fork_point)
    behind = o.commit({"website/y.ts": "y\n"})
    out.append({"name": "base-moved", "base": moved, "head": behind, "base_tip": moved})
    out.append(
        {"name": "base-moved-stale-base-sha", "base": fork_point, "head": behind, "base_tip": moved}
    )

    # 4. A PR that merged main into itself mid-review.
    o.git("checkout", "-q", "-b", "merged-main", fork_point)
    o.commit({"src/e.py": "e = 1\n"})
    o.git("merge", "-q", "--no-ff", "-m", "merge main", moved)
    merged_main = o.commit({"src/e.py": "e = 2\n"})
    out.append({"name": "merged-main-in", "base": moved, "head": merged_main, "base_tip": moved})

    # 5. Fork PR: the head is reachable only through refs/pull/<n>/head, never
    #    a branch of the origin.
    o.git("checkout", "-q", "--detach", moved)
    fork_head = o.commit({"src/fork.py": "f = 1\n", "temp-screenshots/f.png": "png-f\n"})
    out.append({"name": "fork", "base": moved, "head": fork_head, "base_tip": moved, "fork": True})

    # 6. Empty diff, and a checkout that is not a merge commit (the lanes'
    #    fallback): the head is the base itself.
    out.append({"name": "empty", "base": moved, "head": moved, "base_tip": moved, "no_merge": True})

    for pr, case in enumerate(out, start=1):
        case["pr"] = pr
        if case.get("no_merge"):
            o.git("update-ref", f"refs/pull/{pr}/head", case["head"])
            case["merge"] = case["head"]
            continue
        case["merge"] = o.merge_ref(pr, case["base_tip"], case["head"])
        if case.get("fork"):
            # Only the pull refs keep the fork head reachable.
            o.git("checkout", "-q", "main")
    o.git("checkout", "-q", "main")
    for branch in ("normal", "multi", "behind", "merged-main"):
        o.git("branch", "-q", "-D", branch)
    return o, out


def _gh_stub(tmp: Path, origin: Path) -> Path:
    """`gh api repos/<r>/compare/<a>...<b>?per_page=1 --jq .merge_base_commit.sha`."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'echo "$*" >> "$GH_CALLS"\n'
        '[ "$1" = api ] || exit 2\n'
        'spec="${2#repos/*/compare/}"; spec="${spec%%\\?*}"\n'
        'a="${spec%%...*}"; b="${spec##*...}"\n'
        f'git -C "{origin}" merge-base "$a" "$b"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _clone(o: _Origin, name: str, sha: str, depth: int) -> Path:
    """What actions/checkout leaves: `sha` checked out at `depth`, no credentials."""
    work = o.tmp / name
    o.git("init", "-q", str(work), cwd=o.tmp)
    o.git("remote", "add", "origin", f"file://{o.path}", cwd=work)
    o.git("fetch", "-q", "--no-tags", f"--depth={depth}", "origin", sha, cwd=work)
    o.git("checkout", "-q", "--detach", sha, cwd=work)
    assert o.git("rev-parse", "--is-shallow-repository", cwd=work) == "true"
    return work


def _run_step(o: _Origin, lane: str, name: str, work: Path, env: dict[str, str]) -> dict[str, str]:
    script = str(_step(lane, name)["run"])
    output = o.tmp / f"out-{lane}-{work.name}"
    output.write_text("", encoding="utf-8")
    bin_dir = o.tmp / "bin"
    run_env = {
        **o.env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "dummy-token",
        "GH_CALLS": str(o.tmp / "gh-calls"),
        "REPO": "example/repo",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_WORKSPACE": str(ROOT),
        **env,
    }
    res = subprocess.run(
        ["bash", "-c", script],
        cwd=work,
        env=run_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode == 0, f"{lane}:{name}\n{res.stdout}\n{res.stderr}"
    pairs = [
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    ]
    return dict(pairs)


def _diff(o: _Origin, repo: Path, rng: str, form: tuple[str, ...]) -> str:
    if "--" in form:
        i = form.index("--")
        return o.git("diff", *form[:i], rng, *form[i:], cwd=repo)
    return o.git("diff", *form, rng, cwd=repo)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[_Origin, list[dict], Path]:
    tmp = tmp_path_factory.mktemp("shallow")
    o, cases = _scenarios(tmp)
    _gh_stub(tmp, o.path)
    full = tmp / "full"
    o.git("clone", "-q", "--no-local", str(o.path), str(full), cwd=tmp)
    o.git("fetch", "-q", "origin", "+refs/pull/*:refs/pull/*", cwd=full)
    return o, cases, full


@pytest.mark.parametrize("lane", SAME_REPO_LANES)
def test_same_repo_lane_sees_the_full_clone_diff(world, lane: str) -> None:
    o, cases, full = world
    step = "Fetch the merge-base commits (shallow history)"
    if lane in ("claude-review.yml", "codex-review.yml", "first-principles-review.yml"):
        step = "Fetch the merge-base commit (shallow history)"
    checkout = _step_checkout(lane)
    assert checkout["fetch-depth"] == 2, lane
    for case in cases:
        work = _clone(o, f"{lane}-{case['name']}", case["merge"], depth=2)
        got = _run_step(o, lane, step, work, {"BASE_SHA": case["base"], "HEAD_SHA": case["head"]})
        assert set(got) == LANE_OUTPUTS[lane], (lane, got)
        # Each output replaces one three-dot range the lane spelled before.
        old_ranges = {
            "pr": f"{case['base']}...{case['head']}",
            "tree": f"{case['base']}...{case['merge']}",
            "patch_base": (
                f"{case['base_tip']}...{case['merge']}"
                if not case.get("no_merge")
                else f"{case['base']}...{case['merge']}"
            ),
        }
        tips = {"pr": case["head"], "tree": case["merge"], "patch_base": case["merge"]}
        for key, merge_base in got.items():
            new_range = f"{merge_base}..{tips[key]}"
            for form in DIFF_FORMS:
                assert _diff(o, work, new_range, form) == _diff(o, full, old_ranges[key], form), (
                    lane,
                    case["name"],
                    key,
                    form,
                )


@pytest.mark.parametrize("lane", FORK_LANES)
def test_fork_lane_sees_the_full_clone_diff(world, lane: str) -> None:
    o, cases, full = world
    checkout = _step_checkout(lane)
    assert checkout.get("fetch-depth") == 1, lane
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    step = next(
        s["name"]
        for job in doc["jobs"].values()
        for s in job.get("steps") or []
        if str(s.get("name", "")).startswith("Fetch authentic diff")
    )
    for case in cases:
        if case["base"] == case["head"]:
            continue  # an empty fork diff fails closed before and after alike
        work = _clone(o, f"{lane}-{case['name']}", case["base"], depth=1)
        patch = o.tmp / f"{lane}-{case['name']}.patch"
        got = _run_step(
            o,
            lane,
            step,
            work,
            {
                "PR": str(case["pr"]),
                "BASE_SHA": case["base"],
                "HEAD_SHA": case["head"],
                "PATCH": str(patch),
            },
        )
        old = f"{case['base']}...{case['head']}"
        assert patch.read_text(encoding="utf-8").rstrip("\n") == _diff(
            o, full, old, ("--no-color",)
        ), (
            lane,
            case["name"],
        )
        new_range = f"{got['merge_base']}..{case['head']}"
        for form in DIFF_FORMS:
            assert _diff(o, work, new_range, form) == _diff(o, full, old, form), (
                lane,
                case["name"],
                form,
            )


def _step_checkout(lane: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if "actions/checkout" in str(step.get("uses") or ""):
                return step.get("with") or {}
    raise AssertionError(f"{lane}: no checkout")


@pytest.mark.parametrize("lane", SAME_REPO_LANES + FORK_LANES)
def test_no_lane_keeps_a_three_dot_range_or_full_history(lane: str) -> None:
    """A three-dot range in a shallow clone has no merge-base and fails."""
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            assert (step.get("with") or {}).get("fetch-depth") != 0, lane
            for line in str(step.get("run") or "").splitlines():
                code = line.split("#", 1)[0]
                if "git diff" in code or "git log" in code:
                    assert "..." not in code, (lane, line)


def test_the_gpt_reviewer_is_told_the_two_dot_range(world) -> None:
    """codex-review's agent runs the prompt's `git diff` itself, in the shallow clone."""
    prompt = (ROOT / ".github" / "review-prompts" / "gpt-repo-context.md").read_text(
        encoding="utf-8"
    )
    assert "git diff __MERGE_BASE__..HEAD" in prompt
    assert "__BASE_SHA__...HEAD" not in prompt
    script = str(_step("codex-review.yml", "Write review prompt")["run"])
    sed = next(line.strip() for line in script.splitlines() if line.strip().startswith("sed -i "))
    # The step runs on ubuntu runners with GNU sed. BSD sed (macOS) reads the
    # word after a bare `-i` as a backup suffix, so the test passes the suffix
    # attached (`-i.bak`), a form both seds accept, and keeps the step's script.
    sed = "sed -i.bak " + sed[len("sed -i ") :]
    o, _, _ = world
    for text in ("git diff __MERGE_BASE__..HEAD", "git diff __BASE_SHA__...HEAD"):
        f = o.tmp / "codex-prompt.md"
        f.write_text(text + "\n", encoding="utf-8")
        res = subprocess.run(
            ["bash", "-c", sed.replace("/tmp/codex-prompt.md", str(f))],
            env={**os.environ, "MERGE_BASE": "m" * 40, "BASE_SHA": "b" * 40, "HEAD": "h" * 40},
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert f.read_text(encoding="utf-8") == f"git diff {'m' * 40}..HEAD\n", (text, res.stderr)
        f.with_name(f.name + ".bak").unlink()


_COMMITTED_SOURCE = '. "$GITHUB_WORKSPACE/.github/scripts/pr-committed-evidence.sh"'
_EVIDENCE_STEP = {
    "fork-design-review.yml": "Collect rendered evidence",
    "fork-ux-review.yml": "Collect review evidence (description attachments and committed media)",
}


def _compat_block(lane: str) -> str:
    """The lines of the fork evidence step that swap in this workflow's script."""
    assert _COMMITTED_SOURCE in "\n".join(
        str(_step(lane, _EVIDENCE_STEP[lane])["run"]).splitlines()
    )
    lines = str(_step(lane, _EVIDENCE_STEP[lane])["run"]).splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith('committed="$GITHUB_WORKSPACE')
    )
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi") + 1
    assert start < end, lane
    return "set -uo pipefail\n" + "\n".join(lines[start:end]) + "\n"


@pytest.mark.parametrize("lane", sorted(_EVIDENCE_STEP))
@pytest.mark.parametrize("reachable", [True, False])
def test_a_fork_base_with_the_old_evidence_script_gets_this_workflows_copy(
    world, lane: str, reachable: bool
) -> None:
    """An in-flight fork PR's base ships the three-dot script; it must not run."""
    o, cases, _ = world
    new_script = (ROOT / ".github" / "scripts" / "pr-committed-evidence.sh").read_text(
        encoding="utf-8"
    )
    assert "MERGE_BASE" in new_script
    rel = ".github/scripts/pr-committed-evidence.sh"
    o.git("checkout", "-q", "main")
    workflow_sha = "f" * 40
    if reachable:
        (o.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (o.path / rel).write_text(new_script, encoding="utf-8")
        o.git("add", rel)
        o.git("commit", "-q", "--allow-empty", "-m", f"workflow {lane}")
        workflow_sha = o.git("rev-parse", "HEAD")
    work = _clone(o, f"compat-{lane}-{reachable}", cases[0]["base"], depth=1)
    old = work / rel
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('git diff "$BASE_SHA...$HEAD_SHA"\n', encoding="utf-8")
    output = o.tmp / f"compat-out-{lane}-{reachable}"
    output.write_text("", encoding="utf-8")
    res = subprocess.run(
        ["bash", "-c", _compat_block(lane)],
        cwd=work,
        env={
            **o.env,
            "GITHUB_WORKSPACE": str(work),
            "GITHUB_OUTPUT": str(output),
            "GH_TOKEN": "dummy-token",
            "WORKFLOW_SHA": workflow_sha,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if reachable:
        assert res.returncode == 0, res.stderr
        assert old.read_text(encoding="utf-8") == new_script
    else:
        assert res.returncode == 1, res.stdout
        assert "unfetched=true" in output.read_text(encoding="utf-8")
        assert old.read_text(encoding="utf-8") == 'git diff "$BASE_SHA...$HEAD_SHA"\n'
    # A base whose copy already reads MERGE_BASE is left alone.
    res = subprocess.run(
        ["bash", "-c", _compat_block(lane)],
        cwd=work,
        env={
            **o.env,
            "GITHUB_WORKSPACE": str(work),
            "GITHUB_OUTPUT": str(output),
            "WORKFLOW_SHA": "0" * 40,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if reachable:
        assert res.returncode == 0, res.stderr


def test_a_failed_merge_base_read_fails_closed_with_a_visible_error(tmp_path: Path) -> None:
    """The callers read `$(merge_base ...)`: the error must reach stderr, not the variable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(sleep.stat().st_mode | stat.S_IXUSR)
    script = (
        "set -euo pipefail\n"
        '. "$GITHUB_WORKSPACE/.github/scripts/review-merge-base.sh"\n'
        'mb="$(merge_base ' + "a" * 40 + " " + "b" * 40 + ')"\n'
        'echo "reached:$mb"\n'
    )
    res = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **_git_env(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(ROOT),
            "GH_TOKEN": "dummy-token",
            "REPO": "example/repo",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode != 0
    assert "reached:" not in res.stdout
    assert "::error::could not read the merge-base" in res.stderr
