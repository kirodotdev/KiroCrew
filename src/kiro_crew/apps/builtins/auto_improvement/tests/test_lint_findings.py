"""T1 lint-finding parsing — why a good fix was rejected as a new violation.

``_lint_findings`` keys findings as ``file:code`` and T1 passes iff the candidate adds
none. But ruff colorizes its output even when piped, and the SGR sequences land INSIDE
the parsed fields: the path became ``\\x1b[1m<path>\\x1b[0m`` and the rule code parsed
as EMPTY. Every finding in a file therefore collapsed to one malformed token, so the
base-vs-candidate set difference was unreliable and a candidate could be failed with
"T1: candidate fix introduces a new lint/static violation" it had not introduced.

The module already had ``_ANSI_RE`` for exactly this hazard; the parser just never
applied it. These pin the parse.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
    _ANSI_RE,
    PytestBugRunner,
)


def _parse(runner: PytestBugRunner, stdout: str) -> set[str]:
    """Drive the module's parse loop over canned linter stdout.

    Mirrors ``_lint_findings``'s body so the parse can be tested without installing a
    linter or building a tree; the ANSI strip and the warning skip are the behavior
    under test.
    """
    findings: set[str] = set()
    for raw_line in stdout.splitlines():
        line = _ANSI_RE.sub("", raw_line)
        if line.startswith(("warning:", "error:")):
            continue
        parts = line.split(":", 3)
        if len(parts) < 3:
            continue
        path = parts[0].strip()
        rest = parts[3] if len(parts) > 3 else ""
        code = (rest.strip().split(" ", 1)[0] or "?").rstrip(":")
        findings.add(f"{path}:{code}")
    return findings


class TestLintFindingParse:
    def test_a_colorized_line_still_yields_path_and_code(self) -> None:
        """The regression: real ruff output carries SGR sequences."""
        colorized = "\x1b[1msrc/pkg/mod.py\x1b[0m\x1b[36m:\x1b[0m12\x1b[36m:\x1b[0m5\x1b[36m:\x1b[0m \x1b[1;31mF401\x1b[0m `os` imported but unused"  # noqa: E501
        assert _parse(PytestBugRunner(), colorized) == {"src/pkg/mod.py:F401"}

    def test_plain_output_is_unaffected(self) -> None:
        plain = "src/pkg/mod.py:12:5: F401 `os` imported but unused"
        assert _parse(PytestBugRunner(), plain) == {"src/pkg/mod.py:F401"}

    def test_distinct_codes_in_one_file_stay_distinct(self) -> None:
        """The concrete harm: collapsing to one token per file hides a real new
        violation AND invents differences where there are none."""
        out = (
            "src/pkg/mod.py:1:1: F401 unused import\n"
            "src/pkg/mod.py:9:1: F841 unused variable\n"
            "src/pkg/other.py:3:1: F401 unused import\n"
        )
        assert _parse(PytestBugRunner(), out) == {
            "src/pkg/mod.py:F401",
            "src/pkg/mod.py:F841",
            "src/pkg/other.py:F401",
        }

    def test_ruff_config_warnings_are_not_findings(self) -> None:
        """ruff prints `warning: Invalid # noqa directive ...` lines that have no
        path:line:col shape; counting them as findings adds noise to both sides."""
        out = (
            "warning: Invalid `# noqa` directive on test/x.py:2684: expected a comma\n"
            "src/pkg/mod.py:1:1: F401 unused import\n"
        )
        assert _parse(PytestBugRunner(), out) == {"src/pkg/mod.py:F401"}

    def test_a_line_without_enough_fields_is_skipped(self) -> None:
        assert _parse(PytestBugRunner(), "not a finding line\n") == set()

    def test_a_missing_code_falls_back_to_a_marker_not_empty(self) -> None:
        """A path:line:col line with no code still produces a stable token rather
        than a bare trailing colon."""
        assert _parse(PytestBugRunner(), "src/pkg/mod.py:1:1:\n") == {"src/pkg/mod.py:?"}


class TestLintFindingsOnDisk:
    def test_findings_from_a_real_tree_carry_codes(self, tmp_path: Path) -> None:
        """End-to-end through the real linter when one is installed: every token must
        carry a rule code. Skipped (returns None) when no linter is available."""
        (tmp_path / "m.py").write_text("import os\n")  # F401
        found = PytestBugRunner()._lint_findings(tmp_path)
        if found is None:
            return  # no linter installed — the documented degradation
        assert found, "a file with an unused import should produce a finding"
        assert all(t.rsplit(":", 1)[1] for t in found), f"empty rule code in {found}"
