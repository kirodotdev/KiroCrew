"""Discovery reading FOCUS — the unscoped, edit-allowlist-narrowed case.

When a run is UNSCOPED (no ``scopeDiffBase``) but the operator narrowed the edit
allowlist to a subtree (the blast-radius control used to dogfood the app on its
own repo), discovery must read only that subtree. Reading the whole tree while
the fence confines fixes to one subdir makes the agent spend its budget on files
it cannot touch, so it finds nothing fixable and returns ``[]`` every cycle —
the exact symptom seen dogfooding auto-improvement on itself. These pin the file
selection and the prompt-shape switch that fix it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import agent_discovery as AD


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repo: two product files under a confined subdir, one outside it,
    plus a test file (which discovery must always drop)."""
    root = tmp_path / "repo"
    (root / "src" / "app" / "sub").mkdir(parents=True)
    (root / "src" / "other").mkdir(parents=True)
    (root / "src" / "app" / "engine.py").write_text("def f():\n    return 1\n")
    (root / "src" / "app" / "sub" / "helper.py").write_text("def g():\n    return 2\n")
    (root / "src" / "other" / "unrelated.py").write_text("def h():\n    return 3\n")
    (root / "src" / "app" / "test_engine.py").write_text("def test_f():\n    assert True\n")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    return root


class TestAllowlistedPyFiles:
    def test_only_files_matching_the_globs_are_returned(self, repo: Path) -> None:
        # ``src/app/*.py`` is the real-config glob shape. fnmatch's ``*`` crosses ``/``
        # (and ``**`` is NOT recursive — the fence's documented semantics), so this one
        # glob matches BOTH the top-level engine.py and the nested sub/helper.py, exactly
        # as the edit fence would; src/other/unrelated.py is out; the test file is dropped.
        files = AD.allowlisted_py_files(repo, ["src/app/*.py"])
        assert set(files) == {"src/app/engine.py", "src/app/sub/helper.py"}

    def test_no_globs_is_empty_not_the_whole_tree(self, repo: Path) -> None:
        # A None/empty allowlist must NOT be read as "focus on everything" — the
        # caller uses the empty return to mean "no focus, read the tree as before".
        assert AD.allowlisted_py_files(repo, []) == []

    def test_basename_glob_matches_nested_paths(self, repo: Path) -> None:
        # A bare ``helper.py`` glob must catch the nested path, matching the fence's
        # own path-and-basename semantics (RepoEditAllowlist._matches).
        files = AD.allowlisted_py_files(repo, ["helper.py"])
        assert files == ["src/app/sub/helper.py"]

    def test_a_glob_matching_nothing_returns_empty(self, repo: Path) -> None:
        assert AD.allowlisted_py_files(repo, ["does/not/exist/**/*.py"]) == []


class TestUnscopedFocusActivation:
    """The end-to-end switch: an unscoped run with narrowed globs reads the fixable
    region and renders the allowlist-focus prompt; without globs it reads the tree."""

    def _run_capture(self, repo: Path, monkeypatch, **kwargs) -> dict:
        """Drive discover_surfaces_via_agent with a runner that records the prompt +
        cwd it was handed and returns an empty finding set."""
        captured: dict = {}

        class _Runner:
            def run(self, prompt, *, cwd, **_kw):
                captured["prompt"] = prompt
                captured["cwd"] = cwd
                from kiro_crew.apps.builtins.auto_improvement.spine.agent_runner import (
                    AgentResult,
                )

                return AgentResult(ok=True, text="[]")

        AD.discover_surfaces_via_agent(_Runner(), clone=repo, **kwargs)
        return captured

    def test_narrowed_allowlist_focuses_the_prompt_on_the_subtree(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._run_capture(repo, monkeypatch, edit_globs=["src/app/*.py"])
        prompt = cap["prompt"]
        # The allowlist-focus wording is used, and only the in-subtree files appear.
        assert "ONLY region a fix may land in" in prompt
        assert "src/app/engine.py" in prompt
        assert "src/app/sub/helper.py" in prompt
        assert "src/other/unrelated.py" not in prompt

    def test_no_globs_reads_the_whole_tree_as_before(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = self._run_capture(repo, monkeypatch, edit_globs=None)
        # Falls back to the unscoped whole-tree prompt (no focus list, no
        # allowlist-focus wording).
        assert "ONLY region a fix may land in" not in cap["prompt"]
        assert "reviewing a Python codebase" in cap["prompt"]

    def test_a_diff_scope_still_wins_over_the_allowlist_focus(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When scoped, the diff focus is more specific and must take precedence — the
        # allowlist-focus wording must NOT appear even if globs are also passed.
        def _fake_changed(_clone, _base):
            return ["src/app/engine.py"]

        monkeypatch.setattr(AD, "changed_py_files", _fake_changed)
        cap = self._run_capture(
            repo, monkeypatch, scope_base="origin/main", edit_globs=["src/app/*.py"]
        )
        assert "ONLY region a fix may land in" not in cap["prompt"]
        assert "This branch introduces/changes" in cap["prompt"]
