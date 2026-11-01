"""session_ledger_read must not hide a record that holds only events.

The emptiness gate checked goal/phase/next/tried/artifacts but omitted events,
so an event-only record (a sanctioned write -- a sweep, or a post-clear phase
note) read back as "no work ledger yet" and the event tail was dropped, even
though the route handler and the core projection both keep it.
"""

import json

from kiro_crew.mcp_tools import ledger

_EVENT = {"ts": "2026-10-31T00:00:00Z", "kind": "progress", "text": "checked CI"}


def _patch(monkeypatch, payload):
    monkeypatch.setattr(ledger, "_strict_session_key", lambda: ("chat-x-1", ""))
    monkeypatch.setattr(ledger.mcp_core, "_get", lambda *a, **k: payload)


def test_events_only_record_is_returned_not_hidden(monkeypatch):
    _patch(
        monkeypatch,
        {
            "state": {
                "goal": "",
                "phase": "",
                "next": "",
                "tried": [],
                "artifacts": {},
                "events": [_EVENT],
            },
            "events": [_EVENT],
        },
    )
    out = ledger.session_ledger_read("session_ledger_read", {})
    assert "no work ledger yet" not in out
    assert json.loads(out)["events"] == [_EVENT]


def test_fully_empty_still_shows_the_hint(monkeypatch):
    _patch(monkeypatch, {"state": {}, "events": []})
    out = ledger.session_ledger_read("session_ledger_read", {})
    assert "no work ledger yet" in out
