"""Tests for reconciling a generated procedure's cited scripts with the shipped ones.

Covers ``skills_script_refs``: which reference shapes count as citing a script,
where the marker lands, idempotence, and the bounds on the refused-script report.
The consolidator path end to end is in ``test_skill_script_refs_staging.py``.
"""

from __future__ import annotations

import importlib
import json

import pytest

from kiro_crew.skills_auto_model import _PENDING_DOCUMENT_MAX_BYTES


@pytest.fixture
def refs():
    """The module under test, imported per test so a missing module fails at setup."""
    return importlib.import_module("kiro_crew.skills_script_refs")


class TestReconcileScriptReferences:
    @pytest.mark.parametrize(
        "line, name",
        [
            ("1. Run `python3 scripts/foo_check.py --flag`", "foo_check.py"),
            ("1. scripts/deploy.sh does the push", "deploy.sh"),
            ("1. python3 cd_probe.py --sid x", "cd_probe.py"),
            ("1. python -u cd_probe.py", "cd_probe.py"),
            ("1. bash gate.sh", "gate.sh"),
            ("1. ./gate.sh", "gate.sh"),
            ("1. python3 scripts/_probe.py", "_probe.py"),
            ("1. Run `cron_alert_gate.py` before paging", "cron_alert_gate.py"),
            ("1. Then execute check_diff_confined.py.", "check_diff_confined.py"),
        ],
    )
    def test_invocation_shapes_are_cited(self, refs, line, name):
        body, missing = refs.reconcile_script_references(line, [])
        assert missing == [name]
        assert body == f"{line} {refs.marker_for(name)}"

    @pytest.mark.parametrize(
        "line",
        [
            "1. Edit `history.py` to add the field",
            "1. python3 src/kiro_crew/tool.py",
            "1. see src/scripts/foo.py",
            "1. python3 -m pytest test/test_x.py",
            "1. run the suite",
            "1. run foo.pyc",
        ],
    )
    def test_mentions_that_are_not_skill_script_invocations_are_left_alone(self, refs, line):
        assert refs.reconcile_script_references(line, []) == (line, [])

    def test_shipped_script_is_not_marked(self, refs):
        body = "## Steps\n1. python3 scripts/run.py\n"
        assert refs.reconcile_script_references(body, ["run.py"]) == (body, [])

    def test_fenced_reference_gets_marker_after_the_fence(self, refs):
        body = "## Steps\n```bash\npython3 scripts/a.py\nbash scripts/b.sh\n```\n2. done\n"
        out, missing = refs.reconcile_script_references(body, ["b.sh"])
        assert missing == ["a.py"]
        assert out == (
            "## Steps\n```bash\npython3 scripts/a.py\nbash scripts/b.sh\n```\n"
            f"{refs.marker_for('a.py')}\n2. done\n"
        )

    def test_unterminated_fence_still_carries_its_marker(self, refs):
        out, missing = refs.reconcile_script_references("```\npython3 scripts/a.py", [])
        assert missing == ["a.py"]
        assert out.endswith(refs.marker_for("a.py"))

    def test_reconcile_is_idempotent_and_dedupes(self, refs):
        body = "1. python3 scripts/a.py then scripts/a.py again\n2. scripts/a.py\n"
        once, missing = refs.reconcile_script_references(body, [])
        assert missing == ["a.py"]
        assert once.count(refs.marker_for("a.py")) == 2  # one per citing line
        assert refs.reconcile_script_references(once, []) == (once, ["a.py"])


class TestRejectedReportBounds:
    def test_report_is_cut_to_its_entry_finding_and_string_bounds(self, refs):
        report = {f"s{i}.py": ["x" * 5000] * 50 for i in range(40)}
        bounded = refs.bound_rejected_scripts(report)
        assert len(bounded) <= refs.MAX_REJECTED_ENTRIES + 1  # plus the truncation note
        assert refs.TRUNCATED_KEY in bounded
        assert all(len(v) <= refs.MAX_REJECTED_FINDINGS for v in bounded.values())
        assert all(len(f) <= refs.MAX_REJECTED_CHARS for v in bounded.values() for f in v)
        assert len(json.dumps(bounded, indent=2)) <= _PENDING_DOCUMENT_MAX_BYTES // 2

    def test_serialized_size_bound_holds_for_non_ascii_text(self, refs):
        report = {f"s{i}.py": ["\U0001f600" * 500] * 50 for i in range(40)}
        bounded = refs.bound_rejected_scripts(report)
        assert len(bounded) > 1 and refs.TRUNCATED_KEY in bounded
        assert len(json.dumps(bounded, indent=2)) <= _PENDING_DOCUMENT_MAX_BYTES // 2

    def test_small_report_is_kept_whole(self, refs):
        report = {"wipe.py": ["dangerous call: .system()"]}
        assert refs.bound_rejected_scripts(report) == report

    def test_a_long_filename_is_cut_and_the_cut_recorded(self, refs):
        bounded = refs.bound_rejected_scripts(
            {"a" * 26000 + ".js": ["unsupported: " + "a" * 26000]}
        )
        assert refs.TRUNCATED_KEY in bounded
        assert all(len(k) <= refs.MAX_REJECTED_CHARS for k in bounded)
