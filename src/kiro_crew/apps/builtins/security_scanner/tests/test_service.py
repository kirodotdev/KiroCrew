"""Tests for the service coordination layer, reporter, and backend route gating.

The scan orchestration is exercised through a fake dispatcher + fake notifier,
so locking, recovery, dedup-notification, and ingestion are all deterministic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from kiro_crew.apps.builtins.security_scanner.lib.models import Finding, ScanRecord, utcnow_iso
from kiro_crew.apps.builtins.security_scanner.lib.reporter import (
    format_notification,
    is_actionable,
    select_new_actionable,
)
from kiro_crew.apps.builtins.security_scanner.lib.scan import TopicAgentResult
from kiro_crew.apps.builtins.security_scanner.lib.service import ScannerService


def _dispatcher(mapping):
    def dispatch(jobs):
        return [
            mapping.get(j.topic_id, TopicAgentResult(topic_id=j.topic_id, raw="[]"))
            for j in jobs
        ]
    return dispatch


class _Notifier:
    def __init__(self):
        self.messages = []

    def __call__(self, msg):
        self.messages.append(msg)


# ---- reporter ---------------------------------------------------------------


def test_is_actionable_rules():
    assert is_actionable(Finding(topic="t", title="x", location="f:1", severity="high"))
    assert is_actionable(Finding(topic="t", title="x", location="f:1", severity="low", status="exploited"))
    assert not is_actionable(Finding(topic="t", title="x", location="f:1", severity="low"))


def test_select_new_actionable_dedups_and_filters():
    findings = [
        Finding(topic="a", title="crit", location="f:1", severity="critical"),
        Finding(topic="a", title="lownoise", location="f:2", severity="low"),
    ]
    new = select_new_actionable(findings, already_notified_ids=set())
    assert [f.title for f in new] == ["crit"]
    # Already-notified is skipped.
    assert select_new_actionable(findings, {findings[0].id}) == []


def test_format_notification_truncates():
    findings = [Finding(topic="a", title=f"f{i}", location=f"x:{i}", severity="high") for i in range(15)]
    msg = format_notification(findings, "scan-1")
    assert "15 new actionable" in msg
    assert "and 5 more" in msg


# ---- scan lock + recovery ---------------------------------------------------


def test_lock_blocks_second_scan(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    assert svc.lock.acquire("scan-a") is True
    assert svc.lock.acquire("scan-b") is False  # held
    svc.lock.release()
    assert svc.lock.acquire("scan-c") is True


def test_stale_lock_is_reclaimed(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    svc.lock.ttl_s = 0.0  # everything is instantly stale
    assert svc.lock.acquire("scan-a") is True
    assert svc.lock.acquire("scan-b") is True  # prior lock treated as stale


def test_recover_interrupted_marks_stuck_running_as_failed(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    old = ScanRecord(id="scan-old", status="running", started_at="2000-01-01T00:00:00Z")
    svc.findings.save_scan(old)
    recovered = svc.recover_interrupted(stale_s=1.0)
    assert recovered == 1
    scan = svc.findings.get_scan("scan-old")
    assert scan is not None and scan.status == "failed"


def test_recover_leaves_fresh_running_alone(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    fresh = ScanRecord(id="scan-fresh", status="running", started_at=utcnow_iso())
    svc.findings.save_scan(fresh)
    assert svc.recover_interrupted(stale_s=3600) == 0
    scan = svc.findings.get_scan("scan-fresh")
    assert scan is not None and scan.status == "running"


# ---- run_scan_locked --------------------------------------------------------


def test_run_scan_locked_notifies_only_new_actionable(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    notifier = _Notifier()
    mapping = {
        "path-traversal": TopicAgentResult(
            topic_id="path-traversal",
            raw='[{"title":"Crit escape","location":"a.py:1","severity":"critical"}]',
        ),
    }
    result = svc.run_scan_locked(_dispatcher(mapping), notifier, topic_ids=["path-traversal"])
    assert result is not None
    assert len(notifier.messages) == 1
    assert "Crit escape" in notifier.messages[0]

    # Second scan finds the SAME finding -> no new notification (dedup).
    result2 = svc.run_scan_locked(_dispatcher(mapping), notifier, topic_ids=["path-traversal"])
    assert result2 is not None
    assert len(notifier.messages) == 1  # unchanged


def test_run_scan_locked_returns_none_when_already_running(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    svc.lock.acquire("in-flight")  # simulate a scan already running
    notifier = _Notifier()
    result = svc.run_scan_locked(_dispatcher({}), notifier, topic_ids=["path-traversal"])
    assert result is None
    assert notifier.messages == []


def test_run_scan_locked_releases_lock_after(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    svc.run_scan_locked(_dispatcher({}), _Notifier(), topic_ids=["path-traversal"])
    assert svc.lock.is_held() is False  # released in finally


# ---- status + ingest --------------------------------------------------------


def test_status_reports_counts(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    svc.findings.upsert(Finding(topic="a", title="x", location="f:1", severity="high", status="confirmed"))
    st = svc.status()
    assert st["findings_total"] == 1
    assert st["findings_by_severity"]["high"] == 1
    assert st["running"] is False


def test_ingest_json_report(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    text = '[{"topic":"auth-bypass","pattern":"HMAC compared with == (timing)","tags":["auth"]}]'
    res = svc.ingest_report_text(text)
    assert res["added"] == 1
    assert any(p.source == "external-report" for p in svc.knowledge.all_patterns())


def test_ingest_freetext_report(tmp_path):
    svc = ServiceUnderTest(tmp_path)
    text = "# Heading skipped\n- User path reaches open() without normalization\nshort\n"
    res = svc.ingest_report_text(text, topic_hint="path-traversal")
    assert res["parsed"] == 1  # heading + too-short line skipped


# ---- backend route enable-gate ----------------------------------------------


def test_routes_denied_when_disabled(tmp_path, monkeypatch):
    """Builtin routes are deny-by-default: ``_require_enabled`` returns 403 while
    the app is disabled (it ships ``defaultEnabled: false``) and passes through
    to the handler once enabled. Auth for the ``/api/apps/*`` surface is the
    dashboard middleware's job, not the handler's."""
    monkeypatch.setenv("SECURITY_SCANNER_DATA", str(tmp_path))
    from kiro_crew.apps.builtins.security_scanner.backend import routes

    class _Req:
        def __init__(self):
            self.query = {}
            self.match_info = {}

    # Disabled -> 403 (deny-by-default).
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
    resp = asyncio.run(routes._require_enabled(routes._status)(_Req()))
    assert resp.status == 403

    # Enabled -> handler runs against the tmp data dir, 200.
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    resp2 = asyncio.run(routes._require_enabled(routes._status)(_Req()))
    assert resp2.status == 200


# ---- helper: service pointed at a tmp dir -----------------------------------


def ServiceUnderTest(tmp_path) -> ScannerService:
    return ScannerService(Path(tmp_path))
