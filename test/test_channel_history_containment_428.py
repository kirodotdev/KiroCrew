"""Channel-history path containment must be boundary-aware.

A bare ``str.startswith`` has no trailing-separator boundary, so a sibling
directory that merely shares the prefix (``.../hist-evil`` vs ``.../hist``)
would pass. ``Path.is_relative_to`` compares path components instead.
"""

from __future__ import annotations

import pytest

from kiro_crew.channel_history import ChannelHistory


def test_observe_path_accepts_child(tmp_path) -> None:
    hist = tmp_path / "hist"
    hist.mkdir()
    h = ChannelHistory(history_dir=hist)
    p = h._observe_path("C123")
    assert p is not None
    assert p == (hist / "C123.jsonl")


def test_older_generation_stays_beside_the_contained_live_file(tmp_path) -> None:
    """The ``.1`` generation is derived from the already-contained live path."""
    hist = tmp_path / "hist"
    hist.mkdir()
    h = ChannelHistory(history_dir=hist)
    live = h._observe_path("C123")
    assert live is not None
    older = h._older_generation(live)
    assert older == (hist / "C123.jsonl.1")
    assert older.parent == live.parent
    assert older.is_relative_to(hist.resolve())


@pytest.mark.asyncio
async def test_observe_path_does_not_prepare_root_on_event_loop(tmp_path) -> None:
    hist = tmp_path / "hist"
    hist.mkdir()
    h = ChannelHistory(history_dir=hist)

    assert h._history_root_snapshot() is None
    assert h._observe_path("C123") is None
    assert h._history_root_snapshot() is None


def test_observe_path_rejects_sibling_sharing_prefix(tmp_path) -> None:
    hist = tmp_path / "hist"
    hist.mkdir()
    (tmp_path / "hist-evil").mkdir()
    h = ChannelHistory(history_dir=hist)
    # Escapes to the sibling dir that shares the "hist" string prefix; the old
    # startswith guard accepted this, is_relative_to refuses it.
    assert h._observe_path("../hist-evil/x") is None


def test_observe_path_rejects_parent_escape(tmp_path) -> None:
    hist = tmp_path / "hist"
    hist.mkdir()
    h = ChannelHistory(history_dir=hist)
    assert h._observe_path("../../etc/passwd") is None
