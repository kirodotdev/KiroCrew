"""R39 regression (round-39 Codex findings on c0a00e7).

F1a: the DEPLOY policy must not grant account-wide OAC deletion; destroy()
     reports deferred cleanup instead of claiming completeness.
F1b: unconditioned apigateway:POST is limited to the /apis collection; POST
     under /apis/* is a mutation and sits behind the managed-tag condition.
F2:  an unreachable manifest fails teardown retryably for finite-TTL deploys
     too — never tombstone while the reaper cannot see the expiry.
"""
from pathlib import Path
from unittest.mock import patch

from kiro_crew.deploy import engine
from kiro_crew.deploy import iam as iam_mod

REPO = Path(__file__).resolve().parents[1]
HANDLERS = (REPO / "src" / "kiro_crew" / "deploy" / "handlers.py").read_text()


def _statements(doc):
    return {st.get("Sid", ""): st for st in doc["Statement"]}


class TestF1aOacDeleteRemoved:
    def test_no_oac_delete_any_tier(self):
        for tier in ("static", "fullstack"):
            doc = iam_mod.policy_document(tier=tier)
            st = _statements(doc)["CloudFrontOACManage"]
            assert "cloudfront:DeleteOriginAccessControl" not in st["Action"], tier

    def test_destroy_reports_deferred_oac_cleanup(self):
        with patch.object(engine, "find_site_by_tag",
                          return_value={"bucket": "", "distribution_id": "", "distribution_arn": ""}):
            with patch.object(engine, "_delete_oac_for_site", return_value=False):
                out = engine.destroy("site", "prof")
        assert out["oac_cleanup"] == "deferred-to-reaper"

    def test_destroy_reports_done_when_cleaned(self):
        with patch.object(engine, "find_site_by_tag",
                          return_value={"bucket": "", "distribution_id": "", "distribution_arn": ""}):
            with patch.object(engine, "_delete_oac_for_site", return_value=True):
                out = engine.destroy("site", "prof")
        assert out["oac_cleanup"] == "done"


class TestF2UnreachableManifestFailsRetryably:
    def test_no_persistent_only_tolerance(self):
        # the unreachable branch must not be gated on is_persistent anymore
        idx = HANDLERS.index('manifest_status == "unreachable"')
        line = HANDLERS[idx:idx + 80]
        assert "is_persistent" not in line, (
            "finite-TTL teardown must also fail retryably on unreachable manifest"
        )

    def test_retry_response_precedes_tombstone(self):
        unreachable_idx = HANDLERS.index('manifest_status == "unreachable"')
        tombstone_idx = HANDLERS.index("mark_webapp_expired")
        assert unreachable_idx < tombstone_idx, (
            "the retryable failure must be returned before any tombstoning"
        )
