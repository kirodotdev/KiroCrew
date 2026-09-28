"""Consumer assertions for the ``incognito-mix`` seed fixture."""

from pathlib import Path

from kiro_crew.history import ConversationLog, is_incognito_transcript
from kiro_crew.testing.fixtures import seeded_home


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
