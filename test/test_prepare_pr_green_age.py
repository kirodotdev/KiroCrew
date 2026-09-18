"""Green freshness is measured on real commits, not on a mocked diff.

``green_age.py`` answers one question -- did ``origin/main`` move in files this
PR also touches since the commit this head's CI ran on? -- and every part of the
answer comes from git: which files the base gained, which files the branch owns,
and the content of a moved file on the base. A test that stubbed git would be
asserting the stub's shape rather than git's, so the cases below run against two
real repositories on disk: an ``upstream`` that the base branch moves in, and a
``work`` clone that carries the feature branch.

One case per overlap class, one negative, and one environment error, because the
four classes are the whole of the script's judgement. Each class was
mutation-checked while it was written: disabling its branch in
``classify_overlap`` turns exactly one of these tests red and leaves the others
green.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "scripts"
    / "green_age.py"
)


@pytest.fixture()
def mod() -> ModuleType:
    return load_skill_script("prepare_pr_green_age", SCRIPT)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _identify(root: Path) -> None:
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Green Age Test")


class Pair:
    """An upstream repository and a clone of it, both real."""

    def __init__(self, upstream: Path, work: Path) -> None:
        self.upstream = upstream
        self.work = work

    def base_gains(self, files: dict[str, str], message: str = "base moves") -> str:
        for rel, text in files.items():
            _write(self.upstream, rel, text)
        return _commit(self.upstream, message)

    def branch_changes(self, files: dict[str, str], message: str = "branch work") -> str:
        _git(self.work, "switch", "-q", "-c", "feature")
        for rel, text in files.items():
            _write(self.work, rel, text)
        return _commit(self.work, message)


@pytest.fixture()
def pair(tmp_path: Path) -> Pair:
    """An upstream on ``main`` with one commit, plus a clone of it.

    The seed tree carries the two shapes the classes need: a module directory
    under ``src/kiro_crew/`` and a sibling test directory.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _identify(upstream)
    _write(upstream, "src/kiro_crew/ledger/store.py", "VALUE = 1\n")
    _write(upstream, "src/kiro_crew/ledger/kinds.py", "KINDS = ()\n")
    _write(upstream, "src/kiro_crew/chat/runner.py", "def go():\n    return 1\n")
    _write(upstream, "docs/readme.md", "hello\n")
    _commit(upstream, "seed")

    _git(tmp_path, "clone", "-q", str(upstream), "work")
    work = tmp_path / "work"
    _identify(work)
    return Pair(upstream, work)


def _run_main(mod: ModuleType, monkeypatch, pair: Pair, *argv: str) -> int:
    monkeypatch.chdir(pair.work)
    return mod.main(list(argv))


def _summary(mod: ModuleType) -> dict:
    """The same dict `pr_status.py` embeds, read where the CLI cannot show it.

    `green_age.py` has no `--json`: its only machine-readable consumer is
    `pr_status.py`, which imports `summarize()` and publishes the dict as
    `advisory.green_age`. The tests read the function the same way, so nothing is
    asserted through a flag no shipped caller passes.
    """
    return mod.summarize()


# --- the four overlap classes -------------------------------------------------


def test_same_file_edited_on_both_sides_is_stale(mod, monkeypatch, pair, capsys) -> None:
    """The most direct collision: the base changed a file this PR also changed."""
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"src/kiro_crew/ledger/store.py": "VALUE = 3\n"})

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_STALE
    assert payload["stale"] is True
    assert payload["commits"] == 1
    assert [(o["moved"], o["class"]) for o in payload["overlap"]] == [
        ("src/kiro_crew/ledger/store.py", "same-file")
    ]


def test_a_sibling_in_the_same_directory_is_stale(mod, monkeypatch, pair, capsys) -> None:
    """Two files in one module directory are one module's internals."""
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"src/kiro_crew/ledger/kinds.py": "KINDS = ('a',)\n"})

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_STALE
    assert [(o["moved"], o["class"]) for o in payload["overlap"]] == [
        ("src/kiro_crew/ledger/kinds.py", "same-dir")
    ]


def test_a_moved_caller_importing_a_changed_module_is_stale(mod, monkeypatch, pair, capsys) -> None:
    """The moved file is a caller whose behaviour this PR redefines."""
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains(
        {"src/kiro_crew/chat/runner.py": "from kiro_crew.ledger import store\n\nUSE = store\n"}
    )

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_STALE
    assert [(o["moved"], o["class"]) for o in payload["overlap"]] == [
        ("src/kiro_crew/chat/runner.py", "import")
    ]
    # The class reports WHICH changed module the import reached, not the path.
    assert payload["overlap"][0]["mine"] == "kiro_crew.ledger.store"


def test_a_moved_test_named_for_a_changed_directory_is_stale(
    mod, monkeypatch, pair, capsys
) -> None:
    """The incident's own shape: no shared path component, no import at all.

    ``test/test_ledger_retention.py`` and ``src/kiro_crew/ledger/store.py`` share
    nothing a path or import check can see, yet the new test hand-writes the very
    payloads the changed module now validates.
    """
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"test/test_ledger_retention.py": "def test_x():\n    assert True\n"})

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_STALE
    assert [(o["moved"], o["class"]) for o in payload["overlap"]] == [
        ("test/test_ledger_retention.py", "test-stem")
    ]
    assert payload["overlap"][0]["mine"] == "ledger"


# --- the negative and the environment error ----------------------------------


def test_a_relative_import_resolves_against_the_moved_files_own_package(mod) -> None:
    """Skipping a relative import is a false FRESH, which is the dangerous direction.

    `src/kiro_crew/knowledge/retrieval.py` writing `from .._sqlite_compat import x`
    binds `kiro_crew._sqlite_compat`. A PR changing that module is a real overlap,
    and an unresolved relative import reports it as no overlap at all.
    """
    moved = "src/kiro_crew/knowledge/retrieval.py"

    found = mod.imported_modules("from .._sqlite_compat import x\n", moved)
    assert "kiro_crew._sqlite_compat" in found

    overlap = mod.classify_overlap(
        [moved],
        ["src/kiro_crew/_sqlite_compat.py"],
        lambda _p: "from .._sqlite_compat import x\n",
    )
    assert [(o["moved"], o["class"]) for o in overlap] == [(moved, "import")]


def test_relative_import_resolution_follows_pythons_own_rules(mod) -> None:
    package = "kiro_crew.knowledge"

    assert mod._package_of("src/kiro_crew/knowledge/retrieval.py") == package
    # __init__.py's own package is the directory it defines, not its parent.
    assert mod._package_of("src/kiro_crew/knowledge/__init__.py") == package
    assert mod._package_of("docs/readme.md") is None

    assert mod._resolve_relative(".sibling", package) == "kiro_crew.knowledge.sibling"
    assert mod._resolve_relative(".._sqlite_compat", package) == "kiro_crew._sqlite_compat"
    assert mod._resolve_relative(".", package) == package
    # Climbing to or past the root is not a real import, so it resolves to nothing
    # rather than to a top-level guess.
    assert mod._resolve_relative("...deep", package) == ""
    assert mod._resolve_relative("..x", None) == ""


def test_the_skill_states_the_loops_own_bounds(mod) -> None:
    """A rule that can re-push every cycle needs a stated end, or it never settles.

    On a 19-minute CI against a ~1.4-minute merge gap, a PR overlapping a churned
    file could re-sync forever; and an exit 2 must not read as either verdict.
    """
    skill = " ".join(
        (ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "SKILL.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "three consecutive green-age re-syncs on one PR, hand it to the user" in skill
    # Exit 30 does not get to jump the queue in front of a settling round.
    assert "does NOT override the no-push-while-runs-are-in-flight rule" in skill
    # Exit 2 satisfies no criterion and blocks none.
    assert "exit 2 satisfies this criterion no more than it blocks it" in skill
    assert "after **three consecutive** 2s report the reason and hand the PR over" in skill


def test_a_flat_directory_is_not_a_module(mod, monkeypatch, pair, capsys) -> None:
    """`test/` holds thousands of unrelated files, so sharing it proves nothing.

    Measured on this repository: without this rule a PR touching one test file read
    as STALE against every one of the 17 unrelated test files a few hours of `main`
    had moved. A rule that fires on every PR is a rule that gets switched off.
    """
    pair.branch_changes({"test/test_mine.py": "def test_mine():\n    assert True\n"})
    pair.base_gains({"test/test_something_else.py": "def test_x():\n    assert True\n"})

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_FRESH
    assert payload["overlap"] == []
    # The module directory below a source root still counts.
    assert mod._module_dir("src/kiro_crew/ledger/store.py") == "src/kiro_crew/ledger"
    assert mod._module_dir("test/test_mine.py") == ""
    assert mod._module_dir(".github/black-baseline.txt") == ""
    assert mod._module_dir("README.md") == ""


def test_a_base_that_moved_somewhere_else_stays_fresh(mod, monkeypatch, pair, capsys) -> None:
    """Movement alone is not staleness, or every PR on a busy repo would rebase."""
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"docs/readme.md": "hello again\n"})

    code = _run_main(mod, monkeypatch, pair)
    out = capsys.readouterr().out
    payload = _summary(mod)

    assert code == mod.EXIT_FRESH
    assert payload["stale"] is False
    assert payload["overlap"] == []
    assert payload["commits"] == 1
    assert "overlap: none" in out
    assert "STATUS: FRESH" in out


def test_a_base_that_has_not_moved_is_fresh(mod, monkeypatch, pair, capsys) -> None:
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})

    code = _run_main(mod, monkeypatch, pair)
    payload = _summary(mod)

    assert code == mod.EXIT_FRESH
    assert payload["commits"] == 0
    assert payload["ci_base"] == payload["base_head"]


def test_outside_a_git_repository_is_an_environment_error(mod, monkeypatch, tmp_path) -> None:
    """Unknown must never read as fresh: no verdict is exit 2, not exit 0."""
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)

    assert mod.main([]) == mod.EXIT_ENV


def test_a_failed_fetch_is_an_environment_error_not_a_green(mod, monkeypatch, pair, capsys) -> None:
    """A stale origin/main cannot prove freshness, so the fetch fails closed."""
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    _git(pair.work, "remote", "set-url", "origin", str(pair.work / "does-not-exist"))

    code = _run_main(mod, monkeypatch, pair)

    assert code == mod.EXIT_ENV
    captured = capsys.readouterr()
    assert "green age: unavailable" in captured.err
    # The remedy names the operator's own terminal rather than reprinting git's
    # stderr, which is free text that can carry a credential.
    assert "run git fetch yourself" in captured.err


# --- the flags ---------------------------------------------------------------


def test_the_tested_base_is_the_merge_base_not_a_caller_supplied_sha(
    mod, monkeypatch, pair, capsys
) -> None:
    """The commit the green was measured on is inferred, never passed in.

    prepare-pr rebases before every push, so merge-base(HEAD, origin/base) IS
    the base tip at trigger time. There is no flag to override it: a caller
    that could pin an arbitrary commit could also pin today's tip and make a
    stale tree read fresh.
    """
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"src/kiro_crew/ledger/kinds.py": "KINDS = ('a',)\n"})

    assert _run_main(mod, monkeypatch, pair) == mod.EXIT_STALE
    with pytest.raises(SystemExit) as excinfo:
        _run_main(mod, monkeypatch, pair, "--ci-base", "0" * 40)
    assert excinfo.value.code == 2
    assert "unrecognized arguments: --ci-base" in capsys.readouterr().err


def test_pr_mode_takes_the_changed_files_from_the_host(mod, monkeypatch, pair, capsys) -> None:
    """--pr reads the PR's files, so the verdict does not depend on the checkout."""
    pair.branch_changes({"docs/readme.md": "local only\n"})
    pair.base_gains({"src/kiro_crew/ledger/kinds.py": "KINDS = ('a',)\n"})

    real_run = mod.run

    def fake_run(args):
        if args[:3] == ["gh", "pr", "view"]:
            return 0, "src/kiro_crew/ledger/store.py\n", ""
        return real_run(args)

    monkeypatch.setattr(mod, "run", fake_run)
    code = _run_main(mod, monkeypatch, pair, "--pr", "42")
    payload = mod.summarize(pr="42")

    assert code == mod.EXIT_STALE
    assert payload["pr_files"] == 1
    assert payload["overlap"][0]["class"] == "same-dir"


def test_pr_must_be_a_number(mod) -> None:
    with pytest.raises(mod.EnvError):
        mod._pr_files("42; rm -rf /")


def test_a_failed_gh_call_in_pr_mode_is_an_environment_error(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "run", lambda args: (1, "", "gh: not logged in"))
    with pytest.raises(mod.EnvError):
        mod._pr_files("42")


# --- the helpers the classes are built from ----------------------------------


def test_dotted_module_paths_drops_the_source_root_and_resolves_packages(mod) -> None:
    assert mod.dotted_module_paths(["src/kiro_crew/ledger/store.py"]) == {"kiro_crew.ledger.store"}
    # __init__.py names its package; no import ever spells the file.
    assert mod.dotted_module_paths(["src/kiro_crew/ledger/__init__.py"]) == {"kiro_crew.ledger"}
    # A module outside a source root keeps its own path.
    assert mod.dotted_module_paths(["scripts/tool.py"]) == {"scripts.tool"}
    # Non-Python and non-identifier paths carry no import name.
    assert mod.dotted_module_paths(["docs/readme.md", "src/kiro_crew/not-a-module/x.py"]) == set()


def test_imported_modules_reads_every_form_an_import_can_take(mod) -> None:
    text = (
        "import os, sys as system\n"
        "from kiro_crew.ledger import store, kinds as k\n"
        "from . import sibling\n"
        "from .relative.deep import thing\n"
        "from kiro_crew.chat import *\n"
        "    from kiro_crew.deep import nested\n"
        "from kiro_crew.parens import (one, two)\n"
    )
    found = mod.imported_modules(text)

    assert {"os", "sys"} <= found
    assert {"kiro_crew.ledger", "kiro_crew.ledger.store", "kiro_crew.ledger.kinds"} <= found
    # An indented import binds the same module as one at column 0.
    assert "kiro_crew.deep.nested" in found
    assert {"kiro_crew.parens.one", "kiro_crew.parens.two"} <= found
    # A star import names the package and nothing more.
    assert "kiro_crew.chat" in found
    # A relative import's target depends on the importing file's own package,
    # which this script does not resolve, so it is skipped rather than guessed.
    assert not any(name.startswith(".") for name in found)
    assert "sibling" not in found


def test_import_matching_runs_in_both_directions_on_a_dot_boundary(mod) -> None:
    # Module below a changed package.
    assert mod._imports_touch({"kiro_crew.ledger.store"}, {"kiro_crew.ledger"}) == (
        "kiro_crew.ledger"
    )
    # Package above a changed module.
    assert mod._imports_touch({"kiro_crew.ledger"}, {"kiro_crew.ledger.store"}) == (
        "kiro_crew.ledger.store"
    )
    # A shared prefix that is not a dot boundary is not a match.
    assert mod._imports_touch({"kiro_crew.ledgerx"}, {"kiro_crew.ledger"}) == ""


def test_test_stem_prefixes_are_cumulative_and_only_for_test_files(mod) -> None:
    assert mod.test_stem_prefixes("test/test_ledger_retention.py") == [
        "ledger",
        "ledger_retention",
    ]
    assert mod.test_stem_prefixes("src/kiro_crew/ledger/store.py") == []
    assert mod.test_stem_prefixes("test/helpers_test.py") == []


def test_changed_dir_names_ignores_the_names_that_describe_layout(mod) -> None:
    names = mod.changed_dir_names(["src/kiro_crew/ledger/store.py", "test/test_x.py"])

    assert "ledger" in names
    assert "kiro_crew" in names
    # "src" and "test" name a layout, so a test stem matching one says nothing
    # about which code the test covers.
    assert "src" not in names
    assert "test" not in names


def test_the_human_line_caps_the_overlap_list(mod) -> None:
    overlap = [{"moved": f"f{i}.py", "mine": f"f{i}.py", "class": "same-file"} for i in range(20)]
    line = mod.format_line(
        {
            "ok": True,
            "commits": 20,
            "ci_base": "a" * 40,
            "base_head": "b" * 40,
            "overlap": overlap,
        }
    )

    assert line.startswith("green age: base +20 commits (aaaaaaaa -> bbbbbbbb), overlap: ")
    assert "+8 more" in line
    assert "f19.py" not in line


def test_the_human_line_says_unavailable_rather_than_fresh(mod) -> None:
    line = mod.format_line({"ok": False, "reason": "not inside a git repository"})

    assert line == "green age: unavailable (not inside a git repository)"
    assert "FRESH" not in line


def test_summarize_never_reports_fresh_without_a_verdict(mod, monkeypatch, tmp_path) -> None:
    """The dict is always readable, and a failure carries ok=False plus a reason."""
    monkeypatch.chdir(tmp_path)
    summary = mod.summarize()

    assert summary["ok"] is False
    assert summary["stale"] is False
    assert summary["reason"]


def test_an_injected_runner_is_the_only_way_commands_are_issued(mod, pair, tmp_path) -> None:
    """pr_status.py embeds this script and passes its own runner; nothing leaks.

    Run from a directory that is not the repository at all: the verdict is still
    correct, which is only possible if every command went through the runner.
    """
    pair.branch_changes({"src/kiro_crew/ledger/store.py": "VALUE = 2\n"})
    pair.base_gains({"src/kiro_crew/ledger/store.py": "VALUE = 3\n"})
    seen: list[list[str]] = []

    def runner(args: list[str]) -> tuple[int, str, str]:
        seen.append(list(args))
        return mod.run(["git", "-C", str(pair.work), *args[1:]] if args[0] == "git" else args)

    summary = mod.summarize(runner=runner)

    assert summary["ok"] is True
    assert summary["stale"] is True
    assert seen and all(args[0] == "git" for args in seen)
