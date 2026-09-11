"""The comment-history gate must be real, wired into CI, and diff-scoped.

``docs/system-specs/common/code-style.md`` forbids change history in comments and
docstrings. ``scripts/check_comment_history.py`` is what makes that rule
enforceable. These tests pin the halves that must stay true together: CI actually
runs the gate (a gate that exists only on disk is not a gate), the detector reads
comments and docstrings but NOT ordinary string literals, and the verdict covers
exactly the lines a change adds -- a marker on a pre-existing line is the base
branch's, and a marker on an added line fails whoever added it.
"""

from __future__ import annotations

import importlib.util
import tokenize
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_comment_history.py"
FAST_GATE = ROOT / ".github" / "workflows" / "fast-gate.yml"
CODE_STYLE = ROOT / "docs" / "system-specs" / "common" / "code-style.md"

SPEC = importlib.util.spec_from_file_location("check_comment_history", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _gate_step() -> dict:
    workflow = yaml.safe_load(FAST_GATE.read_text(encoding="utf-8"))
    job = workflow["jobs"].get("comment-history-lint")
    assert job, "fast-gate.yml has no comment-history-lint job"
    for step in job.get("steps") or []:
        if "scripts/check_comment_history.py" in str(step.get("run", "")):
            return step
    raise AssertionError("comment-history-lint runs no check_comment_history.py step")


class TestCiWiring:
    def test_fast_gate_actually_runs_the_gate(self) -> None:
        step = _gate_step()
        assert "scripts/check_comment_history.py" in str(step.get("run", ""))

    def test_the_gate_runs_the_self_test_first(self) -> None:
        # The self-test plants one probe per rule family, so a typo that
        # silently disables a rule fails in CI instead of shipping green.
        assert "--test" in str(_gate_step().get("run", ""))

    def test_the_gate_is_diff_scoped_via_the_base_ref_env(self) -> None:
        # Without the env the script only reports. A step that forgot to export
        # it would print counts and pass every PR.
        step = _gate_step()
        assert gate.BASE_ENV in (step.get("env") or {}), "step does not set the base ref"
        assert f"export {gate.BASE_ENV}" in str(step.get("run", ""))

    def test_scope_resolver_coupling_is_alive(self) -> None:
        # The gate loads scripts/ratchet_scope.py, which OWNS the diff answers. A
        # rename there must fail HERE, not as an AttributeError inside a CI run.
        scope = gate._load_scope()
        assert callable(scope.resolve_base)
        assert callable(scope.changed_paths_at)
        assert callable(scope.added_lines_at)

    def test_code_style_doc_names_every_enforced_phrase(self) -> None:
        # The doc's DO-NOT list IS the rule set. A pattern the doc does not name
        # is a rule a contributor cannot discover, and the gate is then the
        # authority instead of the spec.
        text = CODE_STYLE.read_text(encoding="utf-8").lower()
        for phrase in (
            "previously",
            "used to",
            "we now",
            "no longer",
            "historically",
            "status: implemented",
            "hotfix",
            "follow-up to",
            "regression for",
            "round n",
            "gpt round",
            "review round",
            "commit sha",
            "incident date",
            "ticket id",
        ):
            assert phrase in text, f"code-style.md does not name {phrase!r}"

    def test_code_style_doc_names_the_gate_and_its_base_ref(self) -> None:
        # The rule and its enforcement must be discoverable from one another:
        # a contributor who reads the rule needs the command that checks it.
        text = CODE_STYLE.read_text(encoding="utf-8")
        assert "scripts/check_comment_history.py" in text
        assert gate.BASE_ENV in text
        assert "comment-history-baseline.json" not in text, "the baseline is gone"


class TestRuleFamilies:
    """One probe per rule family, through the real detector."""

    def _found(self, source: str) -> list[tuple[int, str]]:
        return gate.violations_in_source(source)

    def test_flags_a_violating_comment(self) -> None:
        assert self._found("x = 1  # widen the timeout (#4211)\n") == [(1, "(#4211)")]

    def test_flags_a_violating_docstring(self) -> None:
        source = "def f():\n" '    """Return the path. Previously it read the cache."""\n'
        assert self._found(source) == [(2, "Previously")]

    def test_flags_a_module_docstring(self) -> None:
        assert self._found('"""Parse the manifest. Hotfix for the launch."""\n') == [(1, "Hotfix")]

    def test_flags_a_class_docstring(self) -> None:
        source = "class C:\n" '    """Holds state. We now resolve symlinks."""\n' "    x = 1\n"
        assert self._found(source) == [(2, "We now")]

    def test_non_docstring_string_literal_is_not_scanned(self) -> None:
        # A user-facing message that happens to use one of these phrases is
        # BEHAVIOR, not narration. Scanning every string would make the gate
        # wrong in the one place the words are legitimate.
        assert self._found('MESSAGE = "this token is no longer valid"\n') == []

    def test_string_literal_after_a_docstring_is_not_scanned(self) -> None:
        # The first statement is the docstring; the assignment below it is not,
        # even though both are string literals at module level.
        source = '"""Module."""\n' 'HINT = "the flag was previously named --slow"\n'
        assert self._found(source) == []

    def test_docstring_position_is_what_makes_it_a_docstring(self) -> None:
        # Same literal, second statement: not a docstring, so not scanned.
        source = "x = 1\n" '"""Previously this parsed lazily."""\n'
        assert self._found(source) == []

    @pytest.mark.parametrize(
        "pragma",
        [
            "x: int = 1  # type: ignore[assignment]",
            "import os  # noqa: F401",
            "x = [1]  # fmt: off",
        ],
    )
    def test_pragma_comments_are_exempt(self, pragma: str) -> None:
        assert self._found(pragma + "\n") == []

    def test_pragma_no_cover_is_exempt(self) -> None:
        assert self._found("if False:  # pragma: no cover\n    pass\n") == []

    def test_a_marker_reports_the_line_it_sits_on_inside_a_docstring(self) -> None:
        # The added-line rule compares against the lines a diff touched. A hit
        # reported at the docstring's FIRST line is invisible to it, so swapping
        # a fresh marker into an old multi-line docstring would pass.
        source = '"""Head.\n\nTail: previously it blocked.\n"""\n'
        assert self._found(source) == [(3, "previously")]

    def test_a_marker_wrapped_across_docstring_lines_spans_both(self) -> None:
        # The regex for "issue #N" tolerates whitespace, including a newline, so
        # a docstring can carry ``issue`` at the end of one line and the number at
        # the start of the next. Both lines must be reported as covered.
        source = '"""Head.\n\nSee issue\n#4211 for the shape.\n"""\n'
        assert gate.violation_spans(source) == [(3, 4, "issue\n#4211")]
        # The report shape still names the start line only.
        assert gate.violations_in_source(source) == [(3, "issue\n#4211")]

    def test_present_tense_purpose_is_not_narration(self) -> None:
        # Naming the incident a change answers is narration; naming what a test
        # pins is purpose. code-style.md forbids the first, not the second.
        assert self._found("x = 1  # regression test pins this shape\n") == []
        assert self._found("x = 1  # regression for the truncated parse\n")

    @pytest.mark.parametrize("broken", ["def broken(:\n", "x = (\n"])
    def test_unparseable_source_raises_instead_of_reading_clean(self, broken: str) -> None:
        # A parse failure reading as "zero violations" would let a broken file
        # pass the gate. Which of the two exceptions comes out depends on whether
        # tokenize or ast gives up first, so the scanner catches both and this
        # pins both.
        with pytest.raises((SyntaxError, tokenize.TokenError)):
            self._found(broken)

    def test_self_test_passes(self) -> None:
        assert gate._self_test() == 0

    def test_prefilter_cannot_fall_behind_the_pattern_tuple(self) -> None:
        # The pre-filter skips tokenize/ast for a file it cannot match, so a
        # pattern missing from it would be silently unenforced.
        for pattern in gate.PATTERNS:
            assert pattern.pattern in gate._ANY_MARKER.pattern


class TestScopeExclusions:
    def test_vendor_directory_is_excluded(self) -> None:
        # Vendored third-party code is not ours to rewrite, and code-style.md
        # exempts it.
        assert gate._excluded("src/kiro_crew/_vendor/anyio/_core.py")

    def test_first_party_paths_are_not_excluded(self) -> None:
        assert not gate._excluded("src/kiro_crew/agent.py")
        assert not gate._excluded("test/test_agent.py")

    def test_a_path_merely_containing_vendor_is_not_excluded(self) -> None:
        # The exclusion is a directory prefix, not a substring: a first-party
        # test ABOUT vendored code must still be judged.
        assert not gate._excluded("test/test_vendored_llama_payload.py")

    def test_targets_are_the_documented_two_trees(self) -> None:
        assert gate.DEFAULT_TARGETS == ("src/kiro_crew", "test")


class TestAddedLineVerdict:
    """The verdict on synthetic inputs: added lines only, at any count."""

    def test_a_marker_on_an_added_line_is_an_offender(self) -> None:
        offenders = gate.added_line_violations(
            {"src/x.py": [(10, 10, "previously")]}, {"src/x.py": {10}}
        )
        assert offenders == {"src/x.py": [(10, "previously")]}

    def test_a_marker_on_a_pre_existing_line_is_not_judged(self) -> None:
        # The base branch's line, not this contributor's. CI evaluates a merge
        # ref, so a pre-existing marker in a touched file must not colour the PR.
        offenders = gate.added_line_violations(
            {"src/x.py": [(10, 10, "previously")]}, {"src/x.py": {30, 31}}
        )
        assert offenders == {}

    def test_swapping_one_marker_for_another_is_caught(self) -> None:
        # Delete one old marker, write one new one: the file's count is level,
        # and only an added-line rule can see that the new one is new.
        offenders = gate.added_line_violations(
            {"src/x.py": [(10, 10, "previously"), (30, 30, "we now")]}, {"src/x.py": {30}}
        )
        assert offenders == {"src/x.py": [(30, "we now")]}

    def test_a_marker_wrapped_onto_an_added_line_is_an_offender(self) -> None:
        # ``issue`` on old line 10, the number on added line 11: the match starts on
        # a pre-existing line, so attributing it to its start alone would pass.
        offenders = gate.added_line_violations(
            {"src/x.py": [(10, 11, "issue\n#4211")]}, {"src/x.py": {11}}
        )
        assert offenders == {"src/x.py": [(10, "issue\n#4211")]}

    def test_a_file_with_no_added_lines_is_not_judged(self) -> None:
        offenders = gate.added_line_violations({"src/x.py": [(10, 10, "previously")]}, {})
        assert offenders == {}

    def test_only_scanned_trees_are_in_targets(self) -> None:
        assert gate._in_targets("src/kiro_crew/agent.py")
        assert gate._in_targets("test/test_agent.py")
        assert not gate._in_targets("scripts/check_comment_history.py")
        assert not gate._in_targets("src/kiro_crew/_vendor/x.py")
        assert not gate._in_targets("src/kiro_crew/static/dist/index.js")


class TestEnforceDiff:
    """``enforce_diff`` end to end on a synthetic repo.

    Builds a base commit carrying one legacy marker, a topic commit that appends
    clean lines, then edits the working tree and asks the real gate -- scope
    resolution through a fresh ``ratchet_scope`` pointed at the repo, detection
    through ``violations_in_source``, verdict through ``added_line_violations``
    -- the same pipeline ``enforce_diff`` wires together. A gate that only ever
    saw its unit tests could still pass every PR through a mis-wired scope.
    """

    BASE_SOURCE = "# hotfix\nvalue = 1\n"
    COMMITTED_SOURCE = "# hotfix\nvalue = 1\nextra_a = 2\nextra_b = 3\nextra_c = 4\n"

    @pytest.fixture()
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import subprocess

        from test_ratchet_scope import _fixture_git_env

        from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

        # The module under test runs git with the ambient environment; an
        # exported GIT_DIR would answer for the wrong repository.
        for var in _GIT_LOCATION_VARS:
            monkeypatch.delenv(var, raising=False)

        repo = tmp_path / "repo"
        (repo / "src" / "kiro_crew").mkdir(parents=True)

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                env=_fixture_git_env(),
            )

        target = repo / "src" / "kiro_crew" / "pkg.py"
        git("init", "-b", "main", ".")
        target.write_text(self.BASE_SOURCE, encoding="utf-8")
        git("add", "src/kiro_crew/pkg.py")
        git("commit", "-m", "base file with one legacy marker")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("checkout", "-b", "topic")
        target.write_text(self.COMMITTED_SOURCE, encoding="utf-8")
        git("add", "src/kiro_crew/pkg.py")
        git("commit", "-m", "append clean lines below the marker")

        spec = importlib.util.spec_from_file_location(
            "ratchet_scope_for_gate", ROOT / "scripts" / "ratchet_scope.py"
        )
        assert spec and spec.loader
        rs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rs)
        monkeypatch.setattr(rs, "ROOT", repo)
        monkeypatch.setattr(gate, "ROOT", repo)
        monkeypatch.setattr(gate, "_load_scope", lambda: rs)
        return repo, target

    def test_a_legacy_marker_the_change_did_not_write_passes(
        self, repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The committed diff only appended clean lines below the marker.
        assert gate.enforce_diff("origin/main") == 0
        assert "passed" in capsys.readouterr().out

    def test_a_shifted_legacy_marker_is_not_flagged(self, repo) -> None:
        # Uncommitted insert ABOVE the marker moves it to line 3, inside the
        # committed diff's added range {3, 4, 5}. The scope must describe the
        # working tree, so it reads the marker as the pre-existing line it is.
        _, target = repo
        target.write_text("wip_a = 0\nwip_b = 0\n" + self.COMMITTED_SOURCE, encoding="utf-8")
        assert gate.enforce_diff("origin/main") == 0

    def test_a_marker_the_change_writes_fails(self, repo, capsys) -> None:
        _, target = repo
        target.write_text(self.COMMITTED_SOURCE + "# previously this parsed lazily\n", "utf-8")
        assert gate.enforce_diff("origin/main") == 1
        out = capsys.readouterr().out
        assert "src/kiro_crew/pkg.py:6: previously" in out
        assert "::error file=src/kiro_crew/pkg.py" in out

    def test_swapping_the_legacy_marker_for_a_fresh_one_fails(self, repo) -> None:
        # Delete the old marker, write a new one at the end: the count is level
        # at 1, so only the added-line rule catches it -- and it must.
        _, target = repo
        source = self.COMMITTED_SOURCE.replace("# hotfix\n", "") + "# hotfix\n"
        target.write_text(source, encoding="utf-8")
        assert gate.enforce_diff("origin/main") == 1

    def test_appending_the_second_half_of_a_wrapped_marker_fails(self, repo) -> None:
        # Base carries a docstring ending in "See issue" -- no marker on its own,
        # "issue" alone matches nothing. The change appends a line with the number.
        # The match now spans an old line and an added one; a start-line rule
        # would attribute it to the old line and pass. It must fail.
        repo_dir, target = repo
        import subprocess

        from test_ratchet_scope import _fixture_git_env

        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                env=_fixture_git_env(),
            )

        base = '"""Module.\n\nSee issue\n"""\nvalue = 1\n'
        target.write_text(base, encoding="utf-8")
        git("add", "src/kiro_crew/pkg.py")
        git("commit", "-m", "docstring ends mid-phrase")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        assert gate.violation_spans(base) == [], "the base alone must be clean"

        target.write_text(
            '"""Module.\n\nSee issue\n#4211 for the shape.\n"""\nvalue = 1\n', "utf-8"
        )
        assert gate.enforce_diff("origin/main") == 1

    def test_a_marker_in_a_file_outside_the_scanned_trees_is_not_judged(self, repo) -> None:
        repo_dir, _ = repo
        other = repo_dir / "notes.py"
        other.write_text("# previously\n", encoding="utf-8")
        assert gate.enforce_diff("origin/main") == 0

    def test_a_deleted_file_adds_nothing(self, repo) -> None:
        _, target = repo
        target.unlink()
        assert gate.enforce_diff("origin/main") == 0

    def test_an_unreadable_base_fails_closed(self, repo) -> None:
        with pytest.raises(SystemExit):
            gate.enforce_diff("refs/heads/does-not-exist")


class TestReportMode:
    def test_without_the_env_the_script_reports_and_does_not_enforce(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A local run on a tree that still carries the legacy set must not be
        # red; the number is printed to watch, and the env is named to gate.
        monkeypatch.delenv(gate.BASE_ENV, raising=False)
        monkeypatch.setattr(gate, "_scan_tree", lambda: {"src/x.py": [(1, "a"), (2, "b")]})
        assert gate.main([]) == 0
        out = capsys.readouterr().out
        assert "2 marker(s) in 1 file(s)" in out
        assert "Not enforced" in out
        assert gate.BASE_ENV in out

    def test_with_the_env_the_script_enforces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(gate.BASE_ENV, "some-ref")
        seen: list[str] = []
        monkeypatch.setattr(gate, "enforce_diff", lambda base: seen.append(base) or 0)
        assert gate.main([]) == 0
        assert seen == ["some-ref"]
