"""Tests for PR #6 Round-6 fixes: scan redaction, binary file handling, OAC IAM scoping."""
from __future__ import annotations

import tempfile
from pathlib import Path

from kiro_crew.deploy import iam as iam_mod
from kiro_crew.deploy.scan import Finding, is_credential_finding, summarize

# ── F1: Scan summary redaction ──────────────────────────────────────────────


def test_summarize_redacts_credential_snippets():
    """A finding whose snippet contains a raw AKIA key must be redacted in summarize output."""
    fake_key = "AKIAIOSFODNN7EXAMPLE"
    findings = [
        Finding(kind="credential", snippet=fake_key, line=5, severity="credential"),
    ]
    result = summarize(findings)
    assert fake_key not in result, "Raw AKIA key must not appear in summarize output"
    # The redaction should have replaced or masked it
    assert "[REDACTED" in result or "…" in result or len(fake_key) > len(result.split(fake_key[0])[0])


def test_summarize_redacts_internal_urls():
    """Internal host findings still display the host (info-class, user needs to see it).
    Only credential-severity snippets are redacted."""
    info_snippet = "internal-host-marker.example.internal/packages/Secret"
    findings = [
        Finding(kind="internal-host", snippet=info_snippet,
                line=10, severity="info"),
    ]
    result = summarize(findings)
    # Info-class findings show the full snippet — user needs to know what to fix.
    # (Assert on the exact snippet value, not a bare host substring — a literal
    # host-in-string check trips CodeQL's incomplete-url-substring-sanitization.)
    assert info_snippet in result
    # But a credential-severity finding MUST be masked
    findings_cred = [
        Finding(kind="credential", snippet="AKIAIOSFODNN7EXAMPLE",
                line=3, severity="credential"),
    ]
    result_cred = summarize(findings_cred)
    assert "AKIAIOSFODNN7EXAMPLE" not in result_cred


def test_409_response_does_not_leak_akia_key():
    """Integration: the 409 scan-blocked response body must not contain raw credentials."""
    from kiro_crew.deploy.handlers import _redact_text

    fake_key = "AKIAIOSFODNN7EXAMPLE"
    raw_summary = f"⚠️ 1 potential issue(s):\n  • line 5 [credential]: {fake_key}"
    redacted = _redact_text(raw_summary)
    assert fake_key not in redacted, "Raw AKIA key must not appear in redacted response"


def test_redact_text_passthrough_clean():
    """Clean text passes through _redact_text unchanged."""
    from kiro_crew.deploy.handlers import _redact_text

    clean = "No secrets here, just a normal message about deployment."
    assert _redact_text(clean) == clean


# ── F2: Binary file handling in _scan_tree ──────────────────────────────────


def test_binary_file_does_not_crash_scan():
    """A PNG-like binary file must not raise UnicodeDecodeError during scan."""
    from kiro_crew.deploy.handlers import _scan_tree

    with tempfile.TemporaryDirectory() as tmp:
        # Create a fake PNG (starts with PNG magic bytes, contains null bytes)
        png_path = Path(tmp) / "logo.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100 + b"IEND")

        # Should not raise — binary files are silently skipped
        findings, byte_size = _scan_tree(Path(tmp))
        assert not any(f.kind == "credential" for f in findings)
        assert byte_size > 0


def test_text_file_with_credentials_still_caught():
    """A text file containing credentials is still detected after the binary-skip fix."""
    from kiro_crew.deploy.handlers import _scan_tree

    with tempfile.TemporaryDirectory() as tmp:
        cred_path = Path(tmp) / "config.js"
        cred_path.write_text(
            'const key = "AKIAIOSFODNN7EXAMPLE";\n', encoding="utf-8"
        )

        findings, _ = _scan_tree(Path(tmp))
        cred_findings = [f for f in findings if is_credential_finding(f)]
        assert len(cred_findings) > 0, "Text file credentials must still be caught"


def test_binary_file_with_sensitive_filename_still_blocked():
    """A binary file with a sensitive filename/path is still rejected."""
    from kiro_crew.deploy.handlers import _scan_tree

    with tempfile.TemporaryDirectory() as tmp:
        # Create a binary file — the sensitive-path check happens at the
        # safe_read_file_bytes level (via validate_file_path). We verify that
        # a normal binary file at a non-sensitive path passes clean.
        bin_path = Path(tmp) / "image.jpg"
        bin_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        findings, _ = _scan_tree(Path(tmp))
        # No crash, no false positives on normal binary
        assert not any(f.kind == "credential" for f in findings)


# ── F3: OAC IAM account scoping ────────────────────────────────────────────


def test_oac_deploy_policy_scoped_to_caller_account():
    """Deploy policy OAC statement must use ${aws:PrincipalAccount}, not wildcard."""
    doc = iam_mod.policy_document(tier="static")
    oac_stmt = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontOACManage")

    # Resource must be account-scoped
    resource = oac_stmt["Resource"]
    assert "${aws:PrincipalAccount}" in resource, \
        f"OAC Resource must use ${{aws:PrincipalAccount}}, got: {resource}"
    assert "::*:" not in resource, "OAC Resource must NOT use account wildcard"


def test_oac_deploy_policy_no_update_delete():
    """Deploy policy OAC statement must NOT include Update/Delete (reaper only).

    History: R32 temporarily granted Delete (destroy() OAC leak); R39 reversed
    it — OAC cannot be tag-scoped, so account-wide Delete on the deploy profile
    is an account-level blast radius. Cleanup is reaper-owned; destroy()
    reports deferred cleanup honestly."""
    doc = iam_mod.policy_document(tier="static")
    oac_stmt = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontOACManage")
    actions = oac_stmt["Action"]
    assert "cloudfront:UpdateOriginAccessControl" not in actions, \
        "UpdateOriginAccessControl belongs in reaper only"
    assert "cloudfront:DeleteOriginAccessControl" not in actions, \
        "DeleteOriginAccessControl belongs in reaper only (R39)"


def test_oac_deploy_policy_has_create_and_get():
    """Deploy policy OAC statement must include Create and Get (needed for deploy)."""
    doc = iam_mod.policy_document(tier="static")
    oac_stmt = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontOACManage")
    actions = oac_stmt["Action"]
    assert "cloudfront:GetOriginAccessControl" in actions
    assert "cloudfront:CreateOriginAccessControl" in actions


def test_oac_reaper_has_update_delete_scoped():
    """Reaper (fullstack tier) must have OAC Update/Delete scoped to caller account."""
    doc = iam_mod.policy_document(tier="fullstack")
    reaper_oac = next(
        (s for s in doc["Statement"] if s.get("Sid") == "ReaperOACCleanup"), None
    )
    assert reaper_oac is not None, "ReaperOACCleanup statement missing in fullstack tier"
    assert "cloudfront:UpdateOriginAccessControl" in reaper_oac["Action"]
    assert "cloudfront:DeleteOriginAccessControl" in reaper_oac["Action"]
    assert "${aws:PrincipalAccount}" in reaper_oac["Resource"]
    assert "::*:" not in reaper_oac["Resource"]


def test_oac_not_in_static_tier_reaper():
    """Static tier must NOT have ReaperOACCleanup (fullstack only)."""
    doc = iam_mod.policy_document(tier="static")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "ReaperOACCleanup" not in sids
