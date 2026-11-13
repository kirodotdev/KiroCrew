"""Consumer assertions for the ``incognito-mix`` seed fixture."""

import asyncio
import json
from pathlib import Path

from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.history import ConversationLog, is_incognito_transcript
from kiro_crew.testing.fixtures import seeded_home

_TRANSCRIPTS = ("dashboard_normal", "dashboard_incognito", "dashboard_temporary")


def _memory_mode(home: Path, session_key: str) -> object:
    metadata = ConversationLog(base_dir=home / "sessions").get_metadata(session_key)
    return metadata["memory_mode"]


def test_normal_session_is_included() -> None:
    with seeded_home("incognito-mix") as home:
        memory_mode = _memory_mode(home, "dashboard_normal")

    assert memory_mode == "persistent"
    assert not is_incognito_transcript(memory_mode)


def test_incognito_session_is_private() -> None:
    with seeded_home("incognito-mix") as home:
        memory_mode = _memory_mode(home, "dashboard_incognito")

    assert memory_mode == "incognito"
    assert is_incognito_transcript(memory_mode)


def test_temporary_session_is_private() -> None:
    with seeded_home("incognito-mix") as home:
        memory_mode = _memory_mode(home, "dashboard_temporary")

    assert memory_mode == "temporary"
    assert is_incognito_transcript(memory_mode)


def test_transcripts_differ_only_in_memory_mode() -> None:
    with seeded_home("incognito-mix") as home:
        lines = {
            key: (home / "sessions" / f"{key}.jsonl").read_text(encoding="utf-8").splitlines()
            for key in _TRANSCRIPTS
        }

    headers = {key: json.loads(rows[0]) for key, rows in lines.items()}
    modes = {key: header.pop("memory_mode") for key, header in headers.items()}
    assert modes == {
        "dashboard_normal": "persistent",
        "dashboard_incognito": "incognito",
        "dashboard_temporary": "temporary",
    }
    assert headers["dashboard_normal"] == headers["dashboard_incognito"]
    assert headers["dashboard_normal"] == headers["dashboard_temporary"]
    assert lines["dashboard_normal"][1:] == lines["dashboard_incognito"][1:]
    assert lines["dashboard_normal"][1:] == lines["dashboard_temporary"][1:]


def test_open_slots_restore_the_two_parents_as_tabs() -> None:
    """A default-config startup restores ``open_slots.json`` before anything else."""
    with seeded_home("incognito-mix") as home:
        state = _make_state(home / "sessions")
        restored = asyncio.run(cp.restore_open_slots_async(state))
        tabs = {key: (slot.memory_mode, slot.pinned) for key, slot in state._slots.items()}

    assert restored == 2
    assert tabs == {"normal": ("persistent", False), "incognito": ("incognito", False)}
