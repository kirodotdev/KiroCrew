"""``diff_signals.py --check-body``: a complete ledger AND a short body are both gates.

Prose is a poor ledger for which areas a diff touches: a long walkthrough
hides a stray file better than a short body does, because the reader trusts the
walkthrough and skips the diff. The script keeps the two jobs apart and gates
both. **Accounting** -- every changed area is named somewhere in the body -- is
exit 20. **Length** -- the ``What changed`` prose over ``WORD_LIMIT`` -- is exit
21: with the ledger guaranteed complete, a cap cannot cut a true fact, only a
restated one. When both breach, 20 is returned and both findings print.

Pure helpers are tested directly; one end-to-end run against a throwaway git
repository pins the exit codes and the printed findings.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "diff_signals.py"
SKILL = PREPARE_PR / "SKILL.md"

ds = load_skill_script("prepare_pr_diff_signals_under_test", SCRIPT)


# ---------------------------------------------------------------- area_of


@pytest.mark.parametrize(
    "path, area",
    [
        ("src/kiro_crew/dashboard/handlers/x.py", "src/kiro_crew/dashboard"),
        ("src/kiro_crew/security.py", "src/kiro_crew/security.py"),
        ("website/src/components/Chat/a.tsx", "website/src/components"),
        ("website/package.json", "website"),
        (".github/workflows/ci.yml", ".github"),
        ("test/test_x.py", "test"),
        ("AGENTS.md", "AGENTS.md"),
    ],
)
def test_area_of_mirrors_the_pr_scope_unit(path: str, area: str) -> None:
    """Same split as the awk in ``.github/workflows/pr-scope.yml``: three levels
    under the two source roots, the top-level component everywhere else."""
    assert ds.area_of(path) == area


@pytest.mark.skipif(shutil.which("awk") is None, reason="awk not on PATH")
def test_area_of_agrees_with_the_pr_scope_awk_program() -> None:
    """The workflow and the script each spell the area unit; run the workflow's own
    awk on a corpus and require the same answers, so a change to one spelling
    fails here instead of diverging silently."""
    workflow = (ROOT / ".github" / "workflows" / "pr-scope.yml").read_text(encoding="utf-8")
    m = re.search(r"awk -F/ '(.*?)'\s*\|", workflow, re.DOTALL)
    assert m, "pr-scope.yml no longer carries the area awk program"
    program = m.group(1)
    corpus = [
        "src/kiro_crew/dashboard/handlers/x.py",
        "src/kiro_crew/security.py",
        "src/kiro_crew/_vendor/pkg/mod.py",
        "website/src/components/Chat/a.tsx",
        "website/src/main.tsx",
        "website/package.json",
        ".github/workflows/ci.yml",
        "test/test_x.py",
        "AGENTS.md",
        "temp-screenshots/shot.png",
    ]
    proc = subprocess.run(
        ["awk", "-F/", program],
        input="\n".join(corpus) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\n")[:-1] == [ds.area_of(p) for p in corpus]


# ------------------------------------------------------- unaccounted_areas


def test_area_named_by_any_of_the_three_forms_is_accounted() -> None:
    paths = [
        "src/kiro_crew/dashboard/handlers/chat.py",
        "website/src/components/Chat/Input.tsx",
        "test/test_chat.py",
    ]
    by_area = "touches src/kiro_crew/dashboard and website/src/components; see test/test_chat.py"
    assert ds.unaccounted_areas(paths, by_area) == []
    by_tail = "handlers/chat.py and Chat/Input.tsx changed, test_chat.py pins it"
    assert ds.unaccounted_areas(paths, by_tail) == []
    by_unique_basename = "chat.py, Input.tsx and test_chat.py"
    assert ds.unaccounted_areas(paths, by_unique_basename) == []


def test_unnamed_area_is_reported_with_its_files() -> None:
    paths = ["src/kiro_crew/dashboard/handlers/chat.py", "src/kiro_crew/security.py"]
    missing = ds.unaccounted_areas(paths, "handlers/chat.py now debounces the send button")
    assert missing == [("src/kiro_crew/security.py", ["src/kiro_crew/security.py"])]


def test_generic_basename_never_satisfies_the_accounting() -> None:
    """``__init__.py`` or ``SKILL.md`` alone says nothing about WHICH one changed."""
    paths = ["src/kiro_crew/apps/__init__.py"]
    assert ds.unaccounted_areas(paths, "__init__.py gained an export") == [
        ("src/kiro_crew/apps", paths)
    ]
    assert ds.unaccounted_areas(paths, "apps/__init__.py gained an export") == []


def test_duplicate_basename_across_areas_needs_a_directory() -> None:
    paths = ["src/kiro_crew/a/config.py", "src/kiro_crew/b/config.py"]
    # Bare name is ambiguous between the two areas: neither is accounted.
    missing = ds.unaccounted_areas(paths, "config.py reads the new key")
    assert [m[0] for m in missing] == ["src/kiro_crew/a", "src/kiro_crew/b"]
    assert ds.unaccounted_areas(paths, "a/config.py and b/config.py read the new key") == []


def test_a_shared_prefix_or_suffix_is_not_a_name() -> None:
    """Substring matching would let an unnamed area ride on a neighbour's name."""
    # Area `src/kiro_crew/chat` is not named by the sibling module `chat_runner.py`.
    paths = ["src/kiro_crew/chat/send.py"]
    assert ds.unaccounted_areas(paths, "src/kiro_crew/chat_runner.py retries") == [
        ("src/kiro_crew/chat", paths)
    ]
    # ...but a deeper path inside the area does name it.
    assert ds.unaccounted_areas(paths, "src/kiro_crew/chat/send.py retries") == []
    # Area `test` is not named by the template heading `## Tests` or by `pytest`.
    paths = ["test/test_send.py"]
    assert ds.unaccounted_areas(paths, "## Tests\n\nrun pytest\n") == [("test", paths)]
    assert ds.unaccounted_areas(paths, "see test/test_send.py") == []
    # A unique basename is not named by a longer name that merely contains it.
    paths = ["src/kiro_crew/chat/send.py"]
    assert ds.unaccounted_areas(paths, "resend.py and send.py.bak") == [
        ("src/kiro_crew/chat", paths)
    ]
    assert ds.unaccounted_areas(paths, "`send.py` now waits") == []
    # A sentence may end right after the name.
    assert ds.unaccounted_areas(paths, "Debounced send.py.") == []


def test_rename_counts_both_sides() -> None:
    ns = "R100\tsrc/kiro_crew/old/x.py\tsrc/kiro_crew/new/x.py\nM\ttest/test_x.py\n"
    paths = ds.changed_paths(ns)
    assert paths == ["src/kiro_crew/old/x.py", "src/kiro_crew/new/x.py", "test/test_x.py"]


# ------------------------------------------------------ what_changed_prose


BODY = """## Problem / Motivation

Broken.

## What changed (motivation → approach → change)

One two three four five.

```mermaid
flowchart LR
  A[one]:::ctx --> B[two]:::added
```

| case | Before | After |
|---|---|---|
| x | 🟥 fails | 🟩 passes |

![Settings page](./evidence/after.png)

<!-- a template comment -->

Six seven eight.

## Tests

Nine ten eleven twelve thirteen fourteen.
"""


def test_prose_excludes_fences_tables_images_and_comments() -> None:
    prose = ds.what_changed_prose(BODY)
    assert prose is not None
    assert ds.word_count(prose) == 8
    assert "flowchart" not in prose
    assert "🟩" not in prose
    assert "evidence" not in prose


def test_prose_is_none_without_the_section() -> None:
    assert ds.what_changed_prose("## Problem\n\nx\n\n## Tests\n\ny\n") is None


# -------------------------------------------------------------- check_body


def test_check_body_over_the_limit_is_twenty_one(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\n"
    rc = ds.check_body(BODY + "\nAlso x/y.py.\n", ns, word_limit=3)
    out = capsys.readouterr().out
    assert rc == 21
    assert "TOO LONG: What changed is 8 words of prose (limit 3)" in out
    assert "the diff is the evidence" in out
    # The ledger still passed; only the prose failed.
    assert "every changed area is named in the body" in out


def test_check_body_under_the_limit_is_zero(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\n"
    rc = ds.check_body(BODY + "\nAlso x/y.py.\n", ns, word_limit=400)
    out = capsys.readouterr().out
    assert rc == 0
    assert "What changed: 8 words of prose (limit 400)" in out
    assert "TOO LONG" not in out


def test_check_body_both_breaches_print_both_and_return_twenty(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\nM\tsrc/kiro_crew/z/w.py\n"
    rc = ds.check_body(BODY + "\nx/y.py changed.\n", ns, word_limit=3)
    out = capsys.readouterr().out
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/z" in out
    assert "TOO LONG: What changed is" in out


def test_check_body_missing_area_is_twenty_even_when_prose_is_short(capsys) -> None:
    ns = "M\tsrc/kiro_crew/x/y.py\nM\tsrc/kiro_crew/z/w.py\n"
    rc = ds.check_body(BODY + "\nx/y.py changed.\n", ns, word_limit=400)
    out = capsys.readouterr().out
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/z" in out
    assert "    src/kiro_crew/z/w.py" in out
    assert "1 unaccounted area(s)" in out


def test_check_body_without_what_changed_still_runs_accounting(capsys) -> None:
    rc = ds.check_body("## Problem\n\nnames src/kiro_crew/x\n", "M\tsrc/kiro_crew/x/y.py\n", 400)
    out = capsys.readouterr().out
    assert rc == 0
    assert "length check skipped" in out


# --------------------------------------------------------------- end to end


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_git_env())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One commit on ``origin/main`` (a synthetic remote ref), one commit on top
    touching two areas. Lives in a subdirectory so a test can create an
    outside-the-repo sibling without leaving ``tmp_path``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "src" / "kiro_crew" / "chat").mkdir(parents=True)
    (repo / "src" / "kiro_crew" / "chat" / "send.py").write_text("x = 1\n")
    (repo / "src" / "kiro_crew" / "security.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _run(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "main", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_end_to_end_inventory_alone_is_unchanged(repo: Path) -> None:
    rc, out = _run(repo)
    assert rc == 0
    assert "=== Files (name-status) ===" in out
    assert "Body check" not in out


def _body(repo: Path) -> Path:
    return repo / ".git" / ds.BODY_FILENAME


def test_end_to_end_body_missing_an_area_exits_twenty(repo: Path) -> None:
    _body(repo).write_text("## What changed\n\nchat/send.py sends once.\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 20
    assert "UNACCOUNTED src/kiro_crew/security.py" in out


def test_end_to_end_complete_but_long_body_exits_twenty_one(repo: Path) -> None:
    prose = "chat/send.py sends once. security.py allows it. " + "word " * ds.WORD_LIMIT
    _body(repo).write_text("## What changed\n\n" + prose + "\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 21
    assert "TOO LONG: What changed is {} words".format(ds.WORD_LIMIT + 6) in out
    assert "every changed area is named in the body" in out


def test_end_to_end_complete_short_body_exits_zero(repo: Path) -> None:
    _body(repo).write_text("## What changed\n\nchat/send.py sends once. security.py allows it.\n")
    rc, out = _run(repo, "--check-body")
    assert rc == 0
    assert "What changed: 6 words of prose (limit {})".format(ds.WORD_LIMIT) in out


def test_the_body_path_is_fixed_inside_the_git_dir() -> None:
    """One file, computed from git, never an argument -- so there is nothing for a
    caller to point at anything else."""
    assert ds.body_path("/some/repo/.git") == os.path.join("/some/repo/.git", "prepare-pr-body.md")
    assert ds.BODY_FILENAME == "prepare-pr-body.md"


def test_end_to_end_missing_body_is_an_env_error(repo: Path) -> None:
    rc, out = _run(repo, "--check-body")
    assert rc == 2
    assert "cannot read body file" in out
    assert ds.BODY_FILENAME in out


def test_end_to_end_a_file_argument_is_refused(repo: Path) -> None:
    """The body path is not an argument; a stray value is an error, not a target."""
    _body(repo).write_text("## What changed\n\nchat/send.py security.py\n")
    rc, out = _run(repo, "--check-body", str(repo / "other.md"))
    assert rc == 2
    assert "Body check" not in out


# ------------------------------------------------------------- the wiring


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_skill_wires_the_check_at_every_body_write_and_gates_both_checks() -> None:
    flat = _flat(SKILL)
    # Phase 1.5, Phase 2 amend, Phase 3 publish -- each names the flag.
    assert flat.count("diff_signals.py --check-body") >= 3
    assert "<body-file>" not in flat
    # The one body path, named once where the body is written.
    assert "$(git rev-parse --absolute-git-dir)/prepare-pr-body.md" in flat
    # The contract says both checks stop the loop, and why the cap is safe.
    assert "### Two checks, two strengths" in flat
    assert "**exit 20** — stop, fix the body or the diff" in flat
    assert "**exit 21** — stop, compress the prose" in flat
    assert "**WARN**, exit 0" not in flat
    assert "SOFT_WORDS" not in flat
    assert "never pad the prose to hide it" in flat
    # The scripts table advertises both exit codes.
    assert "20 unaccounted area · 21 `What changed` over `WORD_LIMIT`" in flat
    # The limit is a constant, not a knob nobody turns.
    assert "--word-limit" not in flat
    assert "--soft-words" not in flat
    # One bound for section 3: the contract's paragraph rule states the number the
    # script enforces, so the two cannot drift apart.
    assert (
        f"well under {ds.WORD_LIMIT} words of prose; `--check-body` stops past that (exit 21, `WORD_LIMIT`)"
        in flat
    )
    assert f"against the {ds.WORD_LIMIT} of section 3" in flat
    # Phase 3 publishes the same file the check read.
    assert (
        "`<body>` below is the checked file, `$(git rev-parse --absolute-git-dir)/prepare-pr-body.md`"
        in flat
    )


def test_skill_reads_the_register_before_the_first_body_write() -> None:
    """Phase 1 step 5 points at the Age 5 register BEFORE the first --check-body,
    and adds the two rules a recital-shaped body breaks."""
    text = SKILL.read_text(encoding="utf-8")
    step = text.index("5. **Reconcile code and description.**")
    first_check = text.index("diff_signals.py --check-body", step)
    register_pointer = text.index("read *Writing register: Age 5* below", step)
    assert register_pointer < first_check
    flat = _flat(SKILL)
    assert "the diff is the evidence, and the body never restates it" in flat
    assert "One general sentence may cover a whole area" in flat
    assert "Lead with a table when the change has more than one moving part" in flat


def test_skill_has_a_cold_reader_step_after_the_check_and_outside_local_review() -> None:
    """The only readability measure: one tool-less subagent reads What changed alone.
    It lives in the skill, not in local_review.py, which mirrors CI prompts byte-for-byte."""
    text = SKILL.read_text(encoding="utf-8")
    step = text.index("5. **Reconcile code and description.**")
    first_check = text.index("diff_signals.py --check-body", step)
    cold = text.index("**Cold reader.**", step)
    assert first_check < cold
    flat = _flat(SKILL)
    assert "In two sentences, what does this PR change for a user, and why?" in flat
    assert "agent `kirocrew-lite`" in flat
    local_review = (PREPARE_PR / "scripts" / "local_review.py").read_text(encoding="utf-8")
    assert "Cold reader" not in local_review
    assert "two sentences" not in local_review
