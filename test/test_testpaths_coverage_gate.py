"""The testpaths-coverage gate's rules, pinned from outside the gate's own file.

``scripts/check_testpaths_coverage.py`` carries a ``--test`` self-test, but that
self-test lives in the same file as the rules it probes — a commit that weakens
a rule can weaken its probe in the same edit and nothing else goes red. This
file is the external exerciser, mirroring the sibling gate twins
(``test_changelog_history_gate.py`` and friends): importlib-load the script and
assert the classification judgments that make the gate worth having.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GATE = _REPO / "scripts" / "check_testpaths_coverage.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("_testpaths_coverage_gate", _GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

_ROOTS = ["test", "src/kiro_crew/apps/builtins"]


def _violations(files, texts=None):
    texts = texts or {}
    violations, _ = gate.find_violations(files, _ROOTS, lambda p: texts.get(p, ""))
    return violations


class TestParseTestpaths:
    def test_reads_the_pin_from_setup_cfg(self) -> None:
        cfg = "[tool:pytest]\ntestpaths = test src/kiro_crew/apps/builtins\n"
        assert gate.parse_testpaths(cfg) == _ROOTS

    def test_missing_testpaths_fails_closed(self) -> None:
        with pytest.raises(SystemExit):
            gate._resolve_roots("[tool:pytest]\naddopts = -q\n")

    def test_the_real_setup_cfg_still_carries_the_pin(self) -> None:
        """The gate reads the live setup.cfg; an empty parse would exit 2 in CI."""
        roots = gate.parse_testpaths((_REPO / "setup.cfg").read_text(encoding="utf-8"))
        assert roots, "setup.cfg lost its [tool:pytest] testpaths pin"


class TestClassification:
    def test_a_stray_test_file_is_a_violation(self) -> None:
        assert _violations(["tests/test_x.py"]) == ["tests/test_x.py"]

    def test_suffix_naming_is_also_caught(self) -> None:
        assert _violations(["tools/smoke_test.py"]) == ["tools/smoke_test.py"]

    def test_collected_roots_are_clean(self) -> None:
        assert (
            _violations(["test/test_x.py", "src/kiro_crew/apps/builtins/foo/tests/test_y.py"]) == []
        )

    def test_a_directory_name_sharing_the_prefix_does_not_collect(self) -> None:
        # "testing/" is not the root "test" — prefix string matching must not
        # quietly widen the collected set.
        assert _violations(["testing/test_x.py"]) == ["testing/test_x.py"]

    def test_non_test_python_files_are_ignored(self) -> None:
        assert _violations(["tests/conftest.py", "tests/fixtures/payload.py"]) == []


class TestExemptions:
    def test_a_nested_distribution_is_exempt_and_reported(self) -> None:
        files = ["packages/client/pyproject.toml", "packages/client/tests/test_client.py"]
        violations, exempted = gate.find_violations(files, _ROOTS, lambda p: "")
        assert violations == []
        assert exempted == ["packages/client/tests/test_client.py (nested distribution)"]

    def test_the_repo_root_config_exempts_nothing(self) -> None:
        assert _violations(["setup.cfg", "tests/test_x.py"]) == ["tests/test_x.py"]

    def test_a_marker_comment_with_a_reason_exempts(self) -> None:
        text = "# testpaths-ok: manual probe, not pytest\n"
        assert gate.has_marker(text) is True

    def test_a_bare_marker_does_not_exempt(self) -> None:
        assert gate.has_marker("# testpaths-ok:\n") is False

    def test_prose_in_a_docstring_does_not_exempt(self) -> None:
        # A file documenting the convention must not self-exempt: the marker
        # only counts on a comment line.
        assert gate.has_marker('"""Documents the testpaths-ok: convention."""\n') is False

    def test_a_marker_below_the_window_does_not_exempt(self) -> None:
        text = ("#\n" * gate._MARKER_WINDOW_LINES) + "# testpaths-ok: too late\n"
        assert gate.has_marker(text) is False
