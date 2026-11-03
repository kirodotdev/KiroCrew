"""A NaN or infinite value in a push's ``meta`` must not break the feed.

The push route parses with plain ``json.loads``, which accepts ``NaN`` and
``Infinity``. Stored as is, the note serialises back to ``NaN`` literally, and
the feed response is then JSON no browser parser accepts, for every client.
The bus keeps the key and the note, and reads such a value as ``null``.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.notifications.bus import NotificationBus, NotificationPayload


def _push(meta: dict[str, Any]) -> dict[str, Any]:
    notes: list[dict[str, Any]] = []
    bus = NotificationBus(sink=notes.append)
    bus.register_channel("ledger.alerts")
    bus.push(
        NotificationPayload(
            source="app:ledger", channel="ledger.alerts", title="Report", body="", meta=meta
        )
    )
    assert len(notes) == 1
    return notes[0]


def test_a_nan_or_infinite_meta_value_is_stored_as_null() -> None:
    note = _push({"score": float("nan"), "big": float("inf"), "small": float("-inf")})
    assert note["score"] is None and note["big"] is None and note["small"] is None
    json.dumps(note, allow_nan=False)


def test_a_nested_non_finite_value_is_stored_as_null() -> None:
    note = _push({"stats": {"p50": 1.5, "p99": float("nan")}, "series": [1.0, float("inf")]})
    assert note["stats"] == {"p50": 1.5, "p99": None}
    assert note["series"] == [1.0, None]
    json.dumps(note, allow_nan=False)


def test_finite_meta_values_are_kept_as_given() -> None:
    meta = {"score": 0.25, "count": 3, "ok": True, "label": "x", "nested": {"a": [1, 2.5]}}
    note = _push(meta)
    for key, value in meta.items():
        assert note[key] == value


def test_the_note_from_a_parsed_nan_body_serialises_as_strict_json() -> None:
    body = json.loads('{"meta": {"score": NaN, "big": Infinity}}')
    note = _push(body["meta"])
    assert json.loads(json.dumps(note, allow_nan=False))["score"] is None
