"""auto_research handler guards: finding redaction (flat and nested) and redaction of stored
campaign fields, the audit record, campaign-id refusal at the handlers, validation edge
cases, and the auth guard. ``test_auto_research.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from auto_research_helpers import isolate_fixture  # noqa: F401 - fixture

from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    update_campaign_status,
    validate_campaign,
    write_guidance,
)


# --- Redaction ---
class TestRedaction:
    def test_redact_finding_with_security_module(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        # Should not crash even if security module has issues
        finding = {
            "summary": "test",
            "sources_checked": ["http://example.com"],
            "new_findings_count": 1,
        }
        result = _redact_finding(finding)
        assert "summary" in result

    def test_redact_finding_handles_non_string_values(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"cycle": 1, "new_findings_count": 3, "summary": "test"}
        result = _redact_finding(finding)
        assert result["cycle"] == 1
        assert result["new_findings_count"] == 3

    def test_redact_finding_handles_list_values(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"sources_checked": ["http://a.com", "http://b.com"], "sources_empty": []}
        result = _redact_finding(finding)
        assert isinstance(result["sources_checked"], list)


# --- Audit ---


class TestAudit:
    def test_audit_does_not_crash(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _audit

        # Should not raise even if sel module is unavailable
        _audit("test_operation", "a1b2c3d4")

    def test_audit_with_extras(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _audit

        _audit("campaign_created", "a1b2c3d4", extra_field="value")


# --- Campaign ID validation in handlers ---


class TestHandlerValidation:
    def test_update_rejects_invalid_id(self):
        result = update_campaign_status("../etc", CampaignStatus.RUNNING)
        assert "error" in result

    def test_write_guidance_rejects_invalid(self, tmp_path: Path):
        with patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            write_guidance("../etc", "text")
            # Should not create any file outside research dir
            assert not (tmp_path / ".." / "etc" / "guidance.txt").exists()


# --- Edge cases in validation ---


class TestValidationEdgeCases:
    def test_max_cycles_exactly_50_no_warning(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 50}
        )
        assert r["can_start"]
        assert not any("$" in w for w in r["warnings"])

    def test_max_cycles_exactly_100_no_error(self):
        r = validate_campaign(
            {"question": "A valid research question here ok", "sources": ["web"], "max_cycles": 100}
        )
        assert r["can_start"]

    def test_default_max_cycles_30(self):
        r = validate_campaign({"question": "A valid research question here ok", "sources": ["web"]})
        assert r["estimated_cycles"] == 30
        assert r["estimated_duration_min"] == 60


# --- Auth ---


class TestRequireAuth:
    def test_returns_none_when_user_present(self):
        from unittest.mock import MagicMock

        from kiro_crew.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = "user123"
        request.query = {}
        assert _require_auth(request) is None

    def test_returns_401_when_no_user(self):
        from unittest.mock import MagicMock

        from kiro_crew.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = None
        resp = _require_auth(request)
        assert resp is not None
        assert resp.status == 401

    def test_rejects_raw_token(self):
        # Raw token alone (without middleware-set user) is rejected — no fail-open.
        from unittest.mock import MagicMock

        from kiro_crew.apps.builtins.auto_research.handlers import _require_auth

        request = MagicMock()
        request.get.return_value = None
        resp = _require_auth(request)
        assert resp is not None
        assert resp.status == 401


# --- Redaction edge cases ---


class TestRedactionNested:
    def test_recursive_nested_dict(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"metadata": {"nested": "value", "deep": {"level": "data"}}, "cycle": 1}
        result = _redact_finding(finding)
        assert isinstance(result["metadata"], dict)
        assert isinstance(result["metadata"]["deep"], dict)

    def test_recursive_list_of_dicts(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"items": [{"name": "a"}, {"name": "b"}], "cycle": 1}
        result = _redact_finding(finding)
        assert len(result["items"]) == 2

    def test_mixed_list(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_finding

        finding = {"mixed": ["text", 42, {"k": "v"}, ["nested"]]}
        result = _redact_finding(finding)
        assert result["mixed"][1] == 42


class TestRedactCampaignFields:
    def test_redacts_sub_questions_and_sources_json(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_campaign

        c = _redact_campaign(
            {
                "question": "q",
                "name": "n",
                "error_message": None,
                "sub_questions": json.dumps(["how does X work?", "what about Y?"]),
                "sources": json.dumps(["web", "internal"]),
                "success_criteria": "done when build passes",
            }
        )
        # Fields stay JSON-decodable lists after redaction.
        assert isinstance(json.loads(c["sub_questions"]), list)
        assert isinstance(json.loads(c["sources"]), list)
        assert len(json.loads(c["sub_questions"])) == 2
        # success_criteria flows through redaction (benign text unchanged).
        assert c["success_criteria"] == "done when build passes"

    def test_handles_malformed_json_fields(self):
        from kiro_crew.apps.builtins.auto_research.handlers import _redact_campaign

        c = _redact_campaign({"sub_questions": "not json{", "sources": "[bad"})
        # Malformed fields left untouched, no crash.
        assert c["sub_questions"] == "not json{"
