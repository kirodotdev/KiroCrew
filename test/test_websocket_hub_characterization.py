"""Source-level compatibility guards for the extracted WebSocket hub."""

from pathlib import Path


def test_every_allowed_event_snapshot_read_is_live_narrowed() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "websocket_hub.py"
    ).read_text(encoding="utf-8")

    snapshot_reads = source.count('ws.get("_allowed_events", frozenset())')
    live_narrowing = source.count("effective_allowed_events(ws_app, snapshot)")

    assert snapshot_reads == live_narrowing == 3
