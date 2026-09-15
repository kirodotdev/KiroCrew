"""Consumer coverage for the sessions-a-few seed fixture."""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.dashboard.chat_persistence import _prefetch_recent_session
from kiro_crew.events.backfill import backfill_transcripts
from kiro_crew.events.kinds import SessionMessage
from kiro_crew.history import ConversationLog
from kiro_crew.testing.fixtures import seeded_home


def test_sessions_a_few_reaches_each_session_bucket() -> None:
    """The real readers classify all four seeded session states."""
    with seeded_home("sessions-a-few") as home:
        sessions_dir = home / "sessions"
        mtimes = {
            "dashboard_pinned-work.jsonl": 1_000.0,
            "dashboard_open-slot.jsonl": 2_000_000_000.0,
            "dashboard_closed-thread.jsonl": 2_000_000_001.0,
        }
        for name, mtime in mtimes.items():
            os.utime(sessions_dir / name, (mtime, mtime))

        log = ConversationLog(base_dir=sessions_dir)
        listed = {session["key"]: session for session in log.list_sessions()}
        assert set(listed) == {
            "dashboard_closed-thread",
            "dashboard_open-slot",
            "dashboard_pinned-work",
        }

        buckets: dict[str, set[str]] = {
            "pinned": set(),
            "open": set(),
            "history": set(),
            "archived": set(),
        }
        cutoff = 2_000_000_000.0 - 60.0
        for key, session in listed.items():
            metadata = log.get_metadata(key)
            prefetched_metadata, messages, _ = _prefetch_recent_session(
                log,
                key,
                session,
                folders_only=False,
                cutoff=cutoff,
            )
            if metadata.get("closed"):
                assert prefetched_metadata is None
                assert messages is None
                assert [row["role"] for row in log.read_messages(key)] == [
                    "user",
                    "assistant",
                ]
                buckets["history"].add(key)
            elif metadata.get("pinned"):
                assert messages
                buckets["pinned"].add(key)
            else:
                assert messages
                buckets["open"].add(key)

        report = backfill_transcripts(home)
        archived_events = [
            event
            for event in report.events
            if isinstance(event, SessionMessage) and event.key == "archived-thread"
        ]
        assert [(event.role, event.content_chars) for event in archived_events] == [
            ("user", len("Earlier turn, archived.")),
            ("assistant", len("An archive slice, used by retention paths.")),
        ]
        buckets["archived"] = {event.key for event in archived_events}

        assert buckets == {
            "pinned": {"dashboard_pinned-work"},
            "open": {"dashboard_open-slot"},
            "history": {"dashboard_closed-thread"},
            "archived": {"archived-thread"},
        }
        assert not Path(home).joinpath("memory.db").exists()
