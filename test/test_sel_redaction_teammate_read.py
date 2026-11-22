"""The security event log keeps equal teammate read rights, and redacts free text.

T1-T3: a Shared Crew teammate (an authorised, non-owner dashboard user) still reads
the three ``/api/sel/*`` routes, like the machine owner (equal rights). T4:
every free-text field and metadata key is redacted before the reader sees the row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    log_fixture,
    reset_singleton,
    sel_dir_fixture,
)
from shared_crew_host_isolation import host_isolation  # noqa: F401
from test_shared_crew_teammate_owner_access import _fresh, _request  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401

from kiro_crew.dashboard.handlers import core_security, sel_failures

pytestmark = pytest.mark.usefixtures("host_isolation")


def _fake_log() -> MagicMock:
    log = MagicMock()
    log.recent.return_value = []
    log.dropped_events = 0
    log.verify_integrity.return_value = SimpleNamespace(
        total=0, valid=0, history_verifiable=True, reason=""
    )
    return log


@pytest.fixture
def fake_log(monkeypatch):
    log = _fake_log()
    for module in (core_security, sel_failures):
        monkeypatch.setattr(module, "_sel", lambda: log)
        monkeypatch.setattr(module, "discovery_executor", lambda: None)
    with patch("kiro_crew.sel.sel", return_value=_fake_log()):
        yield log


_ROUTES = {
    "events": core_security.api_sel_events,
    "recurring_failures": sel_failures.api_sel_recurring_failures,
    "verify": core_security.api_sel_verify,
}


async def _call(name: str, who: str):
    request = _request(who, f"/api/sel/{name.replace('_', '-')}")
    return await _ROUTES[name](request)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_ROUTES))
@pytest.mark.parametrize("who", ["ssm", "tailscale", "relay", "owner"])
async def test_teammate_and_owner_both_read_sel(fake_log, name, who):
    """Pin: no SEL read route refuses an admitted teammate (equal rights)."""
    resp = await _call(name, who)
    assert resp.status == 200


# -- T4 --------------------------------------------------------------------------

_FAKE = "FAKE" + "0" * 28
_SECRET = "Authorization: Bearer " + _FAKE
_FIELDS = ("outcome", "tool_kind", "downstream_service", "request_id")


def test_sel_free_text_fields_are_redacted(log, sel_dir):
    from kiro_crew.sel import SecurityEvent

    event = SecurityEvent(
        event_id="evt-sel-t4",
        timestamp="2026-11-18T00:00:00+00:00",
        event_type="tool_invocation",
        caller_identity="dashboard:t4",
        agent="kirocrew",
        source="dashboard",
        operation="op",
        outcome=_SECRET,
        tool_kind=_SECRET,
        downstream_service=_SECRET,
        request_id=_SECRET,
        metadata={_SECRET: "v", "nested": {_SECRET: ["x"]}},
    )
    log.log(event)
    rows = log.recent(limit=10)
    raw = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
    assert _FAKE not in raw
    assert _FAKE not in json.dumps(rows)
    for field in _FIELDS:
        assert "[REDACTED" in rows[-1][field], field


def test_redaction_log_line_names_no_metadata_key(log, caplog):
    """The INFO line about a redacted event must not echo the original keys."""
    import logging

    from kiro_crew.sel import SecurityEvent

    event = SecurityEvent(
        event_id="evt-sel-t4-log",
        timestamp="2026-11-18T00:00:00+00:00",
        event_type="tool_invocation",
        caller_identity="dashboard:t4",
        agent="kirocrew",
        source="dashboard",
        operation="op",
        metadata={_SECRET: "v"},
    )
    with caplog.at_level(logging.INFO, logger="kiro_crew.sel"):
        log.log(event)
        log.recent(limit=10)
    assert "redacted metadata" in caplog.text
    assert _FAKE not in caplog.text
