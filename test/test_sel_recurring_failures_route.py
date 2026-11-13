"""GET /api/sel/recurring-failures: owner only, audited, and never the command text."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import sel_failures


def _request(user: str) -> MagicMock:
    """A dashboard (empty-``app``) request whose user is *user*; the owner is ``owner-1``."""
    req = MagicMock()
    req.query = {}
    req.app = {"state": SimpleNamespace(owner_id="owner-1")}
    req.__contains__.side_effect = lambda key: key == "app"
    req.__getitem__.side_effect = lambda key: "" if key == "app" else None
    req.get.side_effect = lambda key, default=None: user if key == "user" else default
    return req


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.audits: list[dict] = []

    def recent(self, limit=100, *, since=None, until=None):
        return list(self.rows)

    def log_api_access(self, **kwargs):
        self.audits.append(kwargs)


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW


def _row(session: str) -> dict:
    return {
        "event_type": "tool_invocation",
        "timestamp": (NOW - timedelta(hours=1)).isoformat(),
        "caller_identity": session,
        "outcome": "denied",
        "tool_kind": "execute_bash",
        "operation": "curl https://example.invalid -H token-abc",
        "error": "hook_deny",
    }


@pytest.fixture
def fake_log(monkeypatch):
    log = _Log([_row("dashboard:secret-session-a"), _row("dashboard:secret-session-b")])
    monkeypatch.setattr(sel_failures, "_sel", lambda: log)
    monkeypatch.setattr(sel_failures, "discovery_executor", lambda: None)
    monkeypatch.setattr(sel_failures, "datetime", _FrozenDatetime)
    return log


@pytest.mark.asyncio
async def test_owner_gets_findings_without_session_keys_or_command_text(fake_log):
    resp = await sel_failures.api_sel_recurring_failures(_request("owner-1"))

    assert resp.status == 200
    body = resp.text
    assert "secret-session" not in body and "curl" not in body and "token-abc" not in body
    [row] = json.loads(body)["failures"]
    assert (row["hits"], row["sessions"], row["trend"]) == (2, 2, "new")
    assert [a["operation"] for a in fake_log.audits] == ["sel.failures.read"]


@pytest.mark.asyncio
async def test_a_non_owner_session_is_refused_before_the_log_is_read(fake_log):
    resp = await sel_failures.api_sel_recurring_failures(_request("someone-else"))

    assert resp.status == 403
    assert fake_log.audits == []
