"""The CWD-relative-read gate's rules, pinned from outside the gate's own file.

``scripts/check_cwd_relative_repo_reads.py`` carries a ``--test`` self-test, but that
self-test lives in the same file as the rules it probes -- a commit that weakens a
rule can weaken its probe in the same edit and nothing else goes red. This file is
the external exerciser, mirroring the sibling gate twins
(``test_testpaths_coverage_gate.py`` and friends): importlib-load the script and
assert the classification judgments that make the gate worth having.

Both directions are asserted throughout. A gate that only proves it catches things
can be weakened into catching everything, and this one blocks a merge: a false
positive on a legitimate relative read inside a test that changed directory on
purpose would be worse than no gate at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GATE = _REPO / "scripts" / "check_cwd_relative_repo_reads.py"

#: The judgments below are about the RULE, so they are measured against a fixed
#: top-level set rather than whatever the checkout happens to hold.
_TOP_LEVEL = {"src", "scripts", "test", "docs", ".github", "setup.cfg", "pyproject.toml"}


def _load_gate():
    spec = importlib.util.spec_from_file_location("_cwd_relative_repo_reads_gate", _GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _flagged(source: str) -> list[tuple[int, str, str]]:
    return gate.find_violations(source, _TOP_LEVEL)


class TestTheGateScriptIsWhereTheWorkflowLooks:
    def test_the_script_exists_at_the_path_the_workflow_runs(self) -> None:
        """A renamed script would make the workflow step a no-op that still exits 0."""
        assert _GATE.is_file()

    def test_the_fast_gate_runs_both_the_selftest_and_the_scan(self) -> None:
        workflow = (_REPO / ".github" / "workflows" / "fast-gate.yml").read_text(encoding="utf-8")
        assert "scripts/check_cwd_relative_repo_reads.py --test" in workflow
        assert "python3 scripts/check_cwd_relative_repo_reads.py\n" in workflow


class TestWhatCountsAsReachingTheRepoThroughTheCwd:
    def test_a_bare_relative_read_is_a_violation(self) -> None:
        source = 'from pathlib import Path\nPath("src/kiro_crew/x.py").read_text()\n'
        assert [(line, literal) for line, literal, _ in _flagged(source)] == [
            (2, "src/kiro_crew/x.py")
        ]

    def test_the_module_qualified_spelling_is_caught_too(self) -> None:
        source = 'import pathlib\npathlib.Path("scripts/x.py").read_text()\n'
        assert _flagged(source)

    def test_a_builtin_open_is_caught(self) -> None:
        assert _flagged('open("setup.cfg").read()\n')

    def test_a_literal_bound_to_a_name_first_is_caught(self) -> None:
        """The defect survives being given a name, so the scan has to follow one."""
        source = 'from pathlib import Path\nSRC = Path("src/kiro_crew/x.py")\nSRC.read_text()\n'
        assert _flagged(source)

    def test_a_join_onto_the_literal_is_caught(self) -> None:
        source = 'from pathlib import Path\n(Path("src") / "kiro_crew" / "x.py").read_text()\n'
        assert _flagged(source)

    def test_a_relative_write_into_the_checkout_is_caught(self) -> None:
        source = 'from pathlib import Path\nPath("src/kiro_crew/x.py").write_text("")\n'
        assert _flagged(source)

    def test_a_non_reading_access_verb_is_caught(self) -> None:
        source = 'from pathlib import Path\nassert Path("docs/x.md").exists()\n'
        assert _flagged(source)

    @pytest.mark.parametrize("verb", sorted(gate.ACCESS_VERBS))
    def test_every_verb_in_the_access_set_is_actually_matched(self, verb: str) -> None:
        """The set is the rule's whole reach, so an entry that matches nothing is a hole."""
        source = f'from pathlib import Path\nPath("src/x.py").{verb}()\n'
        assert _flagged(source), verb


class TestWhatIsNotAViolation:
    def test_a_relative_literal_only_compared_is_clean(self) -> None:
        """The repository keeps allowlists of relative paths and compares them.

        Flagging those would make the gate wrong in the one place a relative literal
        is the correct thing to write, and the allowlist would then grow a pragma per
        entry for no gain.
        """
        source = (
            "from pathlib import Path\n"
            'ALLOWED = {Path("src/kiro_crew/x.py"), Path("scripts/y.py")}\n'
            'assert Path("a/b") in ALLOWED\n'
        )
        assert _flagged(source) == []

    def test_the_repository_resolved_from_the_test_file_is_clean(self) -> None:
        source = (
            "from pathlib import Path\n"
            "_REPO = Path(__file__).resolve().parents[1]\n"
            '(_REPO / "src/kiro_crew/x.py").read_text()\n'
        )
        assert _flagged(source) == []

    def test_the_module_under_test_asked_where_it_lives_is_clean(self) -> None:
        """The form the gate's message recommends must itself pass the gate."""
        source = (
            "from pathlib import Path\n"
            "from kiro_crew.dashboard import ws\n"
            "Path(ws.__file__).read_text()\n"
        )
        assert _flagged(source) == []

    def test_a_relative_name_that_is_not_a_repository_entry_is_clean(self) -> None:
        """A test that changed directory reads its own output relatively, on purpose."""
        assert _flagged('from pathlib import Path\nPath("out.json").read_text()\n') == []

    def test_an_absolute_path_is_clean(self) -> None:
        assert _flagged('from pathlib import Path\nPath("/srv/src/x.py").read_text()\n') == []

    def test_a_windows_drive_path_is_clean(self) -> None:
        assert _flagged('from pathlib import Path\nPath("C:/src/x.py").read_text()\n') == []

    def test_a_file_that_changes_directory_is_skipped_whole(self) -> None:
        source = (
            "import os\n"
            "from pathlib import Path\n"
            "os.chdir('/elsewhere')\n"
            'Path("src/kiro_crew/x.py").read_text()\n'
        )
        assert _flagged(source) == []

    def test_the_monkeypatch_spelling_also_skips_the_file(self) -> None:
        source = (
            "from pathlib import Path\n"
            "def test_x(monkeypatch, tmp_path):\n"
            "    monkeypatch.chdir(tmp_path)\n"
            '    Path("src/kiro_crew/x.py").read_text()\n'
        )
        assert _flagged(source) == []

    def test_the_pragma_exempts_its_own_line_and_the_next(self) -> None:
        on_the_line = 'from pathlib import Path\nPath("src/x.py").read_text()  # cwd-ok: reason\n'
        above_the_line = (
            'from pathlib import Path\n# cwd-ok: reason\nPath("src/x.py").read_text()\n'
        )
        assert _flagged(on_the_line) == []
        assert _flagged(above_the_line) == []

    def test_the_pragma_does_not_exempt_a_later_line(self) -> None:
        """Two lines of reach, or one pragma silences the rest of a file."""
        source = (
            "from pathlib import Path\n"
            "# cwd-ok: reason\n"
            "x = 1\n"
            'Path("src/x.py").read_text()\n'
        )
        assert _flagged(source)


class TestTheScanScope:
    def test_helper_modules_under_the_test_tree_are_scanned(self) -> None:
        """They are imported by the tests that read the repository and carry the same
        literals, so excluding them would leave the defect one import away."""
        assert gate.is_test_file(Path("test/macos_lane_helpers.py"))
        assert gate.is_test_file(Path("test/e2e/scenarios/conftest.py"))

    def test_a_test_file_inside_a_source_testpath_is_scanned(self) -> None:
        assert gate.is_test_file(Path("src/kiro_crew/apps/builtins/x/test_y.py"))

    def test_production_source_is_not_scanned(self) -> None:
        """Production code resolves relative paths against a user's project on purpose."""
        assert not gate.is_test_file(Path("src/kiro_crew/dashboard/ws.py"))

    def test_vendored_code_is_excluded(self) -> None:
        assert not gate.is_test_file(Path("src/kiro_crew/_vendor/test_thing.py"))

    def test_a_suffix_named_test_file_is_scanned(self) -> None:
        assert gate.is_test_file(Path("src/kiro_crew/apps/builtins/x/y_test.py"))


class TestTheGateFailsClosed:
    def test_missing_testpaths_exits_rather_than_scanning_nothing(self) -> None:
        with pytest.raises(SystemExit):
            gate._resolve_roots("[tool:pytest]\naddopts = -q\n")

    def test_the_real_setup_cfg_still_carries_the_pin(self) -> None:
        roots = gate.parse_testpaths((_REPO / "setup.cfg").read_text(encoding="utf-8"))
        assert roots, "setup.cfg lost its [tool:pytest] testpaths pin"

    def test_the_top_level_set_is_read_from_the_repository(self) -> None:
        """A hand-kept list would go stale the first time a directory is added."""
        top_level = gate.repo_top_level(_REPO)
        assert {"src", "scripts", "test", "setup.cfg"} <= top_level
        assert "_vendor" not in top_level

    def test_unparseable_source_is_skipped_rather_than_crashing_the_gate(self) -> None:
        assert _flagged("def (:\n") == []

    def test_the_selftest_passes(self) -> None:
        """Run here as well, so a broken probe set reddens the suite and not only CI."""
        assert gate.selftest() == 0
