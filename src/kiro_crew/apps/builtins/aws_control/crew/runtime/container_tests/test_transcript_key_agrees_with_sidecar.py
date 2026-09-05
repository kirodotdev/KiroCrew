"""The on-demand fetch must read exactly the key the sidecar wrote.

The fetch is the READER of an object the backup sidecar WRITES. Nothing at
runtime compares the two, so a disagreement is silent in the worst way: the
fetch misses, the turn is served with no history, and a returning customer is
indistinguishable from a new one. No error, no alarm, no log to grep.

This is the defect this project already shipped once. The first live deployment
doubled the crew name in every key (``crews/<crew>/<crew>/...``) because two
places each decided the same prefix. It survived twelve green gates because
writer and reader agreed with each other while both disagreed with the contract,
and because no test asserted a full key end to end. These tests are that
assertion, from both ends.
"""

from __future__ import annotations

from pathlib import Path

from container.backup import layout
from container.front import transcript


class _Settings:
    """The real relationship between the paths, not an invented one.

    ``sessions_dir`` is a property on the real Settings (``data_home /
    "sessions"``), so a fixture that puts an extra segment in it tests a shape
    the production code cannot produce. Getting this wrong is how a green test
    ends up proving nothing.
    """

    def __init__(self, prefix: str = "crews/", crew: str = "baymax") -> None:
        self.backup_prefix = prefix
        self.crew_name = crew
        self.data_home = Path("/var/lib/kirocrew")
        self.sessions_dir = self.data_home / "sessions"


def _writer_key(settings: _Settings, stem: str) -> str:
    """The key the sidecar produces, built only from the sidecar's own code."""
    return layout.full_key(settings, f"{layout.sessions_prefix(settings)}{stem}.jsonl")


def test_reader_key_is_byte_identical_to_the_writer_key() -> None:
    settings = _Settings()
    stem = "dashboard_cust-8831"
    assert transcript.object_key(settings, stem) == _writer_key(settings, stem)


def test_the_key_is_the_shape_the_live_bucket_actually_holds() -> None:
    """Pin the literal, because agreeing with each other is not enough.

    Both sides agreed while both were wrong once already. This asserts the
    contract's own shape: one crew segment, then ``data/sessions/``.
    """
    assert (
        transcript.object_key(_Settings(), "dashboard_cust-8831")
        == "crews/baymax/data/sessions/dashboard_cust-8831.jsonl"
    )


def test_the_crew_name_appears_exactly_once() -> None:
    """The regression that shipped. ``crews/baymax/baymax/`` must not come back."""
    key = transcript.object_key(_Settings(), "dashboard_x")
    assert key.count("baymax") == 1, key


def test_the_reader_follows_when_the_writer_moves(monkeypatch) -> None:
    """The drift guard.

    MUTATION: give ``transcript.object_key`` its own copy of the prefix join and
    this reddens while every other test stays green, which is exactly how the
    duplicated version looked before it was removed.
    """
    settings = _Settings()
    monkeypatch.setattr(layout, "object_prefix", lambda st: f"crews/v2/{st.crew_name}/")
    assert transcript.object_key(settings, "dashboard_x") == _writer_key(settings, "dashboard_x")
    assert transcript.object_key(settings, "dashboard_x").startswith("crews/v2/")


def test_an_unset_bucket_prefix_does_not_produce_a_leading_slash() -> None:
    """A crew deployed with no prefix configured still yields a usable key."""
    key = transcript.object_key(_Settings(prefix=""), "dashboard_x")
    assert not key.startswith("/"), key
    assert key == "baymax/data/sessions/dashboard_x.jsonl"
