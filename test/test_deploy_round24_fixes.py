"""Regression tests for Round-24 advisories (KiroCrew PR #6).

F1: scan-blocked MCP previews with OVERRIDABLE (non-credential) findings are
    persisted as pending entries flagged override_scan_required, so the human
    can perform the documented override from the dashboard; credential
    findings remain a hard block (no pending entry).
F2: reaper identity mismatches quarantine the untrusted manifest instead of
    re-processing it forever.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


# --- F1: overridable blocked previews reach the pending list ---


def test_f1_pending_schema_carries_override_flag(tmp_path, monkeypatch):
    from kiro_crew.deploy import pending
    monkeypatch.setattr(pending, "_store_path",
                        lambda: tmp_path / "pending.json")
    entry = pending.add_pending({
        "site_id": "s", "override_scan_required": True,
    })
    assert entry["override_scan_required"] is True
    # Default is False for normal previews.
    entry2 = pending.add_pending({"site_id": "s2"})
    assert entry2["override_scan_required"] is False


def test_f1_mcp_blocked_path_distinguishes_credential():
    src = (_ROOT / "src/kiro_crew/mcp_core.py").read_text(encoding="utf-8")
    blocked = src.split('if d.get("blocked"):', 1)[1][:2000]
    # Credential findings: hard block, no pending.
    assert 'if d.get("credential"):' in blocked
    # Non-credential: pending entry flagged for human override.
    assert '"override_scan_required": True' in blocked


def test_f1_confirm_handler_requires_explicit_human_override():
    src = (_ROOT / "src/kiro_crew/deploy/handlers.py").read_text(encoding="utf-8")
    seg = src.split('if entry.get("override_scan_required"):', 1)[1][:600]
    # override_scan only set from the request body (human action), and only
    # when it is literally True.
    assert 'body.get("override_scan") is True' in seg


def test_f1_frontend_sends_override_and_labels_action():
    src = (_ROOT / "website/src/pages/ArtifactDeployPage.tsx").read_text(encoding="utf-8")
    assert "override_scan_required" in src
    assert "Deploy anyway" in src
    assert "override_scan: true" in src


# --- F2: mismatch manifests quarantined, not retried forever ---


def test_f2_reaper_quarantines_on_every_identity_mismatch():
    src = (
        _ROOT / "src/kiro_crew/deploy/skills/artifact-deploy/scripts/"
        "reaper_lambda/index.py"
    ).read_text(encoding="utf-8")
    assert "def _quarantine_manifest" in src
    # Wired at the slug-forgery gate, dist/bucket tag gates, and the app-arch
    # stack tag gate (4 call sites + the def).
    assert src.count("_quarantine_manifest(slug)") >= 4
    # Quarantine preserves content (copy to _quarantine/ before delete).
    assert "_quarantine/" in src
