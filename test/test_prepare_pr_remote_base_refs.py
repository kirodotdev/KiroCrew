"""Every prepare-pr script reads its base as origin's copy, however it is spelled.

git resolves a short ``origin/<base>`` to a local branch, tag or other ref of that
name before ``refs/remotes/origin/<base>``, and only warns that the name is
ambiguous, and it reads a bare ``<base>`` as the local branch. The scans fail on a
script or a profile command that builds the short form, so a new site cannot land
beside the full ``refs/remotes/origin/<base>`` that ``push_guard.py`` reads. A line
that needs the short name itself (preflight asks git what it resolves to) carries
``# short-origin-ok: <reason>``. The spelling rows run each script that takes a base
with ``main``, ``origin/main`` and ``refs/remotes/origin/main`` in a clone where the
local ``main`` and a local ``origin/main`` both disagree with origin's.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from git_maintenance_helpers import git_maintenance_off_entries
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS
from kiro_crew.testing.wait import default_timeout

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPTS = SKILL / "scripts"
# The repository script the skill runs by name, held to the same rule.
LOCAL_GATE = REPO / "scripts" / "local-gate.py"
SHORT_PREFIX = "origin/"
ALLOWED = re.compile(r"#\s*short-origin-ok:\s*\S.{9,}")
# A whole short ref such as ``origin/main``: a name right after the prefix and no
# whitespace, so a message that mentions the base in a sentence is not read as one.
WHOLE_SHORT_REF = re.compile(r"^origin/[\w.-][^\s{}%]*$")
# The same in a shell command; a full ``refs/remotes/origin/...`` has a slash before it.
SHELL_SHORT_REF = re.compile(r"(?<![\w./-])origin/[\w.-]")
# The three ways a caller names origin's main.
SPELLINGS = ("main", "origin/main", "refs/remotes/origin/main")
ORIGIN_MAIN = "refs/remotes/origin/main"


def _starts_a_short_ref(text: str) -> bool:
    return text.startswith(SHORT_PREFIX) and not any(ch.isspace() for ch in text)


def short_ref_builds(source: str) -> list[int]:
    """The lines of ``source`` that build a short ``origin/<name>`` ref."""
    lines = source.splitlines()
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            head = node.left
            builds = isinstance(head, ast.Constant) and isinstance(head.value, str)
            builds = builds and _starts_a_short_ref(head.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            head = node.func.value
            builds = node.func.attr == "format" and isinstance(head, ast.Constant)
            builds = builds and isinstance(head.value, str) and _starts_a_short_ref(head.value)
        elif isinstance(node, ast.JoinedStr):
            head = node
            text = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
            builds = bool(node.values) and isinstance(node.values[0], ast.Constant)
            builds = builds and _starts_a_short_ref(text)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            head = node
            builds = bool(WHOLE_SHORT_REF.match(node.value))
        else:
            continue
        if builds and not ALLOWED.search(lines[head.lineno - 1]):
            found.add(head.lineno)
    return sorted(found)


def test_no_prepare_pr_script_builds_a_short_origin_ref() -> None:
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert len(scripts) > 10, "the scan found too few scripts to mean anything"

    offenders = [
        "{}:{}".format(path.name, line)
        for path in [*scripts, LOCAL_GATE]
        for line in short_ref_builds(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, (
        "these lines build a short origin/<name> ref, which a local branch, tag or "
        "other ref of that name shadows; spell refs/remotes/origin/<name>, or mark a "
        "line that needs the short name with '# short-origin-ok: <reason>': " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "line",
    [
        'ref = "origin/" + base',
        'ref = "origin/%s" % base',
        'ref = "origin/{}".format(base)',
        'ref = f"origin/{base}"',
        'ref = "origin/main"',
        'run(["git", "merge-base", "HEAD", "origin/main"])',
        'ref = "origin/" + base  # short-origin-ok: too short',
    ],
)
def test_the_scan_finds_each_way_a_script_builds_one(line: str) -> None:
    assert short_ref_builds(line) == [1]


@pytest.mark.parametrize(
    "line",
    [
        'ref = "refs/remotes/origin/" + base',
        'ref = "refs/remotes/origin/{}".format(base)',
        'print("STALE: origin/{} gained a commit".format(base))',
        'print("origin/{}:     {}".format(base, sha))',
        'name = base[len("origin/") :] if base.startswith("origin/") else base',
        'prefixes = ("refs/remotes/origin/", "origin/")',
        'note = "push_guard: squash onto origin/" + base',
        'ref = "origin/" + base  # short-origin-ok: the probe asks what this name resolves to',
    ],
)
def test_the_scan_leaves_messages_full_refs_and_marked_lines_alone(line: str) -> None:
    assert short_ref_builds(line) == []


def test_no_profile_command_names_the_base_by_its_short_name() -> None:
    profiles = sorted((SKILL / "profiles").glob("*.json"))
    assert profiles, "no bundled profile found"

    offenders = []
    for path in profiles:
        data = json.loads(path.read_text(encoding="utf-8"))
        commands = [*data.get("setup", []), *data.get("gates", [])]
        assert commands, "{} lists no setup or gate commands".format(path.name)
        offenders += [
            "{}: {}".format(path.name, command)
            for command in commands
            if SHELL_SHORT_REF.search(command)
        ]

    assert not offenders, (
        "these profile commands read the base as a short origin/<name>, which a local "
        "ref of that name shadows; spell refs/remotes/origin/<name>: " + "; ".join(offenders)
    )


@pytest.mark.parametrize(
    ("command", "short"),
    [
        ('BASE="$(git merge-base HEAD origin/main)" && X="$BASE" true', True),
        ("git diff --quiet origin/main -- website/src", True),
        ('BASE="$(git merge-base HEAD refs/remotes/origin/main)" && X="$BASE" true', False),
        ("python3 scripts/check_brand_name.py --test", False),
    ],
)
def test_the_profile_scan_tells_a_short_base_from_a_full_one(command: str, short: bool) -> None:
    assert bool(SHELL_SHORT_REF.search(command)) is short


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    return done.stdout.strip()


def _commit(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", "add " + path)


@pytest.fixture
def shadowed_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone in which only ``refs/remotes/origin/main`` is origin's main.

    HEAD adds ``src/feature.py`` to origin's main. The local ``main`` is a commit
    older, so a script that reads it counts origin's own last change, ``b.txt``,
    as this branch's. The local branch ``origin/main``, which git reads for
    ``origin/main`` before the remote ref, is HEAD itself, so a script that reads
    it sees no change at all. The scripts run git with the ambient environment, so
    the fixture's git settings are set there.
    """
    for name in _GIT_LOCATION_VARS:
        monkeypatch.delenv(name, raising=False)
    settings = {
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    # Pointing GIT_CONFIG_GLOBAL away drops the suite's global file, so the
    # maintenance entries go on the environment too.
    settings.update(git_maintenance_off_entries({**os.environ, **settings}))
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.txt", "the old base\n")
    _commit(repo, "b.txt", "origin's last change\n")
    _git(repo, "update-ref", ORIGIN_MAIN, "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", ORIGIN_MAIN)
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "src/feature.py", "VALUE = 1\n")
    _git(repo, "branch", "-f", "main", "HEAD~2")
    _git(repo, "branch", "origin/main", "HEAD")
    return repo


def _script(path: Path) -> ModuleType:
    return load_skill_script("remote_base_refs_" + path.stem + "_under_test", path)


def _git_status_in(repo: Path):
    """A ``git_status`` for ``_base_ref.base_revision`` that runs git in ``repo``."""

    def git_status(args: list[str]) -> int:
        done = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, timeout=default_timeout()
        )
        return done.returncode

    return git_status


def test_the_clone_shadows_origins_main_both_ways(shadowed_clone: Path) -> None:
    """The control: git itself reads the two short spellings as the stale local
    branch and the namesake, so a script that hands them to git as given fails
    every row below."""
    remote = _git(shadowed_clone, "rev-parse", ORIGIN_MAIN)

    assert _git(shadowed_clone, "rev-parse", "main") == _git(shadowed_clone, "rev-parse", "HEAD~2")
    assert _git(shadowed_clone, "rev-parse", "origin/main") == _git(
        shadowed_clone, "rev-parse", "HEAD"
    )
    assert remote == _git(shadowed_clone, "rev-parse", "HEAD~1")


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_each_spelling_of_origins_main_names_one_branch(spelling: str) -> None:
    assert _script(SCRIPTS / "_base_ref.py").base_branch(spelling) == "main"


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_each_spelling_of_origins_main_is_its_remote_ref(
    shadowed_clone: Path, spelling: str
) -> None:
    base_ref = _script(SCRIPTS / "_base_ref.py")

    assert base_ref.base_revision(spelling, _git_status_in(shadowed_clone)) == ORIGIN_MAIN


@pytest.mark.parametrize(
    ("base", "revision"),
    [
        ("HEAD", "HEAD"),
        ("HEAD~1", "HEAD~1"),
        ("main~1", "main~1"),
        ("main^", "main^"),
        ("refs/heads/main", "refs/heads/main"),
        ("upstream/main", "upstream/main"),
        ("feature", "feature"),
        ("", ""),
        ("origin/main~1", "refs/remotes/origin/main~1"),
        ("origin/feature", "refs/remotes/origin/feature"),
    ],
    ids=[
        "HEAD",
        "HEAD-revision",
        "branch-revision",
        "caret-revision",
        "local-branch-in-full",
        "other-remote",
        "branch-origin-lacks",
        "empty",
        "origin-revision",
        "origin-branch-not-fetched",
    ],
)
def test_a_base_that_is_no_branch_of_origins_is_used_as_given(
    shadowed_clone: Path, base: str, revision: str
) -> None:
    """A bare name is mapped only to a branch origin holds. A name spelled with
    origin's prefix is origin's copy even when this clone lacks it, so git fails on
    it rather than reading a local ref of that name."""
    _git(shadowed_clone, "update-ref", "refs/remotes/upstream/main", ORIGIN_MAIN)
    base_ref = _script(SCRIPTS / "_base_ref.py")

    assert base_ref.base_revision(base, _git_status_in(shadowed_clone)) == revision


def test_a_commit_is_used_as_given(shadowed_clone: Path) -> None:
    commit = _git(shadowed_clone, "rev-parse", "HEAD~1")
    base_ref = _script(SCRIPTS / "_base_ref.py")

    assert base_ref.base_revision(commit, _git_status_in(shadowed_clone)) == commit


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_prove_diffs_against_origins_main(shadowed_clone: Path, spelling: str) -> None:
    prove = _script(SCRIPTS / "prove.py")

    assert prove.resolve_base(shadowed_clone, spelling) == ORIGIN_MAIN


class _ResolveProfile:
    """A ``resolve_profile`` stand-in for ``local_review.assemble``: no remote
    default branch on record unless one is given, and a profile with one
    contract-backed reviewer that declares ``base_branch``."""

    def __init__(self, default=None, base_branch=None):
        self.default = default
        self.base_branch = base_branch
        self.profile_refs = []

    def default_base_ref(self, worktree):
        return self.default

    def resolve(self, worktree, base_ref=None):
        self.profile_refs.append(base_ref)
        return {
            "reviewers": [{"name": "gpt", "contract": "x.yml"}],
            "base_branch": self.base_branch,
        }


def _assemble(monkeypatch, tmp_path, resolve_profile, base_ref=None, worktree=None):
    """Run ``local_review.assemble`` up to its merge-base read, which answers "no
    merge base"; returns the git commands it ran other than the base probe, which
    runs for real in ``worktree``."""
    review = _script(SCRIPTS / "local_review.py")
    real_run = review.run
    ran = []

    def no_merge_base(args, cwd=None):
        if args[1:2] == ["show-ref"]:
            return real_run(args, cwd=cwd)
        ran.append(args)
        return 1, "", "no merge base"

    monkeypatch.setattr(review, "_load_sibling", lambda *args, **kwargs: resolve_profile)
    monkeypatch.setattr(review, "run", no_merge_base)
    with pytest.raises(EnvironmentError, match="cannot resolve a merge base"):
        review.assemble(
            str(worktree or tmp_path), base_ref, str(tmp_path / "out"), str(tmp_path / "stage")
        )
    return ran


def test_local_review_reads_a_declared_base_from_the_remote_ref(monkeypatch, tmp_path) -> None:
    """The profile's ``base_branch`` becomes ``refs/remotes/origin/<base>``. A short
    ``origin/release`` would resolve a local branch of that name first."""
    resolve_profile = _ResolveProfile(default=ORIGIN_MAIN, base_branch="release")

    ran = _assemble(monkeypatch, tmp_path, resolve_profile)

    assert ran == [["git", "merge-base", "HEAD", "refs/remotes/origin/release"]]


def test_local_review_without_a_remote_default_reads_the_profile_from_the_remote_ref(
    monkeypatch, tmp_path
) -> None:
    """With no remote default branch on record, the profile is read from
    ``refs/remotes/origin/main``, never from a short ``origin/main`` a local branch
    of that name would answer, which would let the branch under review pick its
    own reviewers."""
    resolve_profile = _ResolveProfile()

    ran = _assemble(monkeypatch, tmp_path, resolve_profile)

    assert resolve_profile.profile_refs == [ORIGIN_MAIN]
    assert ran == [["git", "merge-base", "HEAD", ORIGIN_MAIN]]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_local_review_briefs_against_origins_main(
    monkeypatch, tmp_path, shadowed_clone: Path, spelling: str
) -> None:
    """The profile and the diff both come from origin's main, so neither the local
    ``main`` nor the local ``origin/main`` can pick the reviewers or the diff."""
    resolve_profile = _ResolveProfile()

    ran = _assemble(monkeypatch, tmp_path, resolve_profile, spelling, shadowed_clone)

    assert resolve_profile.profile_refs == [ORIGIN_MAIN]
    assert ran == [["git", "merge-base", "HEAD", ORIGIN_MAIN]]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_resolve_profile_reads_the_profile_from_origins_main(
    monkeypatch, shadowed_clone: Path, spelling: str, capsys
) -> None:
    profile = _script(SCRIPTS / "resolve_profile.py")
    read_from = []
    monkeypatch.setattr(profile, "resolve", lambda root, base_ref=None: read_from.append(base_ref))

    assert profile.main(["resolve_profile.py", str(shadowed_clone), spelling]) == 0
    assert read_from == [ORIGIN_MAIN]


def _local_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("remote_base_refs_local_gate", LOCAL_GATE)
    assert spec and spec.loader, "could not build an import spec for the gate"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("spelling", [*SPELLINGS, None], ids=[*SPELLINGS, "default"])
def test_local_gate_runs_the_change_against_origins_main(
    monkeypatch, shadowed_clone: Path, spelling, capsys
) -> None:
    """The gate selects this branch's one changed file and runs a gate for it. Read
    as the local ``origin/main`` it would see no change, run nothing and exit 0;
    read as the local ``main`` it would select ``b.txt`` too."""
    gate = _local_gate()
    monkeypatch.setattr(gate, "_REPO_ROOT", shadowed_clone)
    monkeypatch.setattr(gate.run_scoped_tests, "REPO_ROOT", shadowed_clone)
    selected = []

    def one_passing_gate(plan, surface, paths, surface_changed):
        if surface_changed:
            selected.append(sorted(paths))
            plan.add(surface, [sys.executable, "-c", "pass"], shadowed_clone)
        return []

    monkeypatch.setattr(gate, "_add_related", one_passing_gate)

    code = gate.main(["--no-reuse", *(["--base", spelling] if spelling else [])])

    assert (code, selected) == (0, [["src/feature.py"]])
    assert "local-gate: running [backend]" in capsys.readouterr().err


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_diff_signals_lists_the_change_against_origins_main(
    monkeypatch, shadowed_clone: Path, spelling: str, capsys
) -> None:
    signals = _script(SCRIPTS / "diff_signals.py")
    monkeypatch.chdir(shadowed_clone)

    code = signals.main(["diff_signals.py", spelling])
    out = capsys.readouterr().out

    assert code == 0
    assert "=== Change vs origin/main ===" in out
    assert out.split("=== Files (name-status) ===")[1].split("===")[0].split() == [
        "A",
        "src/feature.py",
    ]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_regen_shared_compares_against_origins_main(
    monkeypatch, shadowed_clone: Path, spelling: str
) -> None:
    regen = _script(SCRIPTS / "regen_shared.py")
    git_lines = regen._git_lines
    merge_bases = []

    def recorded(root, *args):
        if args[:1] == ("merge-base",):
            merge_bases.append(args[1])
        return git_lines(root, *args)

    monkeypatch.setattr(regen, "REGEN", [])
    monkeypatch.setattr(regen, "_git_lines", recorded)
    monkeypatch.chdir(shadowed_clone)

    assert regen.main(["regen_shared.py", spelling]) == 0
    assert merge_bases == [ORIGIN_MAIN]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_green_age_measures_origins_main(monkeypatch, spelling: str, capsys) -> None:
    age = _script(SCRIPTS / "green_age.py")
    measured = []

    def fresh(base="main", pr=""):
        measured.append(base)
        return {"ok": True, "stale": False, "overlap": [], "commits": 0}

    monkeypatch.setattr(age, "summarize", fresh)

    assert age.main(["--base", spelling]) == age.EXIT_FRESH
    assert measured == ["main"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_pre_push_gate_takes_its_merge_base_with_origins_main(
    monkeypatch, shadowed_clone: Path, spelling: str
) -> None:
    gate = _script(SCRIPTS / "pre_push_gate.py")
    merge_bases = []
    monkeypatch.setattr(
        gate, "_gate_run", lambda args, git_dir, top: merge_bases.append(gate._base_sha(args.base))
    )
    monkeypatch.chdir(shadowed_clone)

    gate.main(["pre_push_gate.py", "--base", spelling])

    assert merge_bases == [_git(shadowed_clone, "rev-parse", ORIGIN_MAIN)]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_sweep_check_reads_origins_main(monkeypatch, spelling: str) -> None:
    sweep = _script(SCRIPTS / "sweep_check.py")
    read = []
    monkeypatch.setattr(sweep, "run", lambda base, git_dir: read.append(base) or sweep.EXIT_OK)

    assert sweep.main(["--base", spelling]) == sweep.EXIT_OK
    assert read == ["main"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_push_guard_guards_against_origins_main(spelling: str) -> None:
    guard = _script(SCRIPTS / "push_guard.py")

    assert guard._parse_args(["--base", spelling]).base == "main"
