"""Tests for deploy Round 14 fixes.

F1: reaper.sh must not treat transient CloudFront errors as "distribution
    gone" (only NoSuchDistribution may fall through to bucket/OAC deletion).
F2: confirm/override_scan gates require strict boolean True (loose truthiness
    like "false"/"no"/0.1 must NOT satisfy the human-confirmation gate).
F4: per-profile listing failures degrade to warnings for ANY exception, not
    just engine.AWSError.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "kiro_crew"
HANDLERS = (SRC / "deploy" / "handlers.py").read_text(encoding="utf-8")
REAPER_SH = (
    SRC / "deploy" / "skills" / "artifact-deploy" / "scripts" / "reaper.sh"
).read_text(encoding="utf-8")


class TestReaperTransientErrorHandling:
    def test_no_blanket_gone_fallback(self):
        # The old bug: `2>/dev/null || echo "GONE"` collapsed every failure
        # (throttling, IAM, network) into the deletion path.
        assert '|| echo "GONE"' not in REAPER_SH

    def test_gone_requires_no_such_distribution(self):
        # GONE may only be set after positively matching NoSuchDistribution.
        assert "NoSuchDistribution" in REAPER_SH
        # And an inconclusive lookup must retry, not delete.
        assert "transient error querying distribution" in REAPER_SH

    def test_transient_path_continues_before_bucket_delete(self):
        # The retry `continue` must appear before the bucket-deletion section.
        transient_idx = REAPER_SH.index("transient error querying distribution")
        bucket_idx = REAPER_SH.index("Empty and delete per-site bucket")
        assert transient_idx < bucket_idx


class TestStrictConfirmGates:
    def test_no_loose_confirm_truthiness(self):
        assert 'if not params.get("confirm")' not in HANDLERS

    def test_confirm_gates_use_strict_is_not_true(self):
        assert len(re.findall(
            re.escape('params.get("confirm") is not True'), HANDLERS)) >= 3

    def test_override_scan_strict(self):
        assert 'params.get("override_scan") is not True' in HANDLERS
        # No loose-truthiness override_scan check remains.
        assert re.search(
            r'not params\.get\("override_scan"\)', HANDLERS) is None


class TestProfileListingErrorIsolation:
    def test_fetch_one_catches_broad_exception(self):
        # Docstring contract: a single bad profile degrades to a warning.
        assert "per-profile isolation" in HANDLERS
        # The broad handler must sit inside the fetch helper after AWSError.
        aws_idx = HANDLERS.index('except engine.AWSError as e:\n'
                                 '                return [], f"{entry')
        broad = HANDLERS.index("except Exception as e:  # noqa: BLE001",
                               aws_idx)
        assert broad - aws_idx < 400
