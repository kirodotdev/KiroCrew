"""Track B pins for the front process: a non-string conversation id must never
become a fetch, and ``stream`` must be a real boolean.

Finding 1 is the TYPE door into the same isolation hole the punctuation door
(``dashboard:cust-1``) opened: ``{"id": 123}`` is folded to the legal shape
``"123"`` by ``str()``, sails through ``is_fetchable_slot_id``, and gets
fetched -- and only THEN does the backend reject the integer id, leaving the task
holding ``dashboard_123``, a conversation it never served. The fix makes a
non-string id produce an empty ``slot_id``, which maps to ``no_slot`` (no fetch),
while the raw ``id`` is still forwarded so the backend stays the one authority
that judges legality.

The pin asserts the reader is NEVER consulted, and the mutation (restore the old
``str(raw_id)`` coercion) is shown to fetch, so the guard is load-bearing.

Finding 3B: ``bool("false")`` is truthy, so a payload ``{"stream": "false"}``
would stream a turn the caller asked not to stream. The fix requires a real
boolean and refuses anything else with a 400.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from container.front import app as front_app
from container.front import transcript


class _Settings:
    def __init__(self, root: Path) -> None:
        self.backup_prefix = "crews/"
        self.crew_name = "acme"
        self.data_home = root
        self.sessions_dir = root / "sessions"


class _Reader:
    """Records every key asked for, so a fetch that should not happen is visible."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def get(self, key: str) -> bytes:
        self.keys.append(key)
        return b'{"role":"user"}\n'


@pytest.fixture
def settings(tmp_path: Path) -> _Settings:
    s = _Settings(tmp_path)
    s.sessions_dir.mkdir(parents=True)
    return s


# ---------------------------------------------------------------------------
# Finding 1: a non-string id becomes an empty slot_id, so it is never fetched.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw_id", [123, 12.5, True, {"x": 1}, ["a"]])
def test_a_non_string_id_yields_no_slot_id(raw_id) -> None:
    """``_forward_body`` must not fabricate a string slot id from a non-string.

    The raw id is still forwarded (the backend judges it); only the slot id used
    for the fetch/lock is emptied.
    """
    payload = {"model": "acme", "messages": [], "id": raw_id}
    body, slot_id, _ = front_app._forward_body(payload, "acme")
    assert slot_id == "", f"a non-string id produced a fetchable slot id: {slot_id!r}"
    assert body["id"] == raw_id, "the raw id must still reach the backend to be judged"


def test_the_integer_id_never_reaches_the_store(settings) -> None:
    """End to end through the transcript path: ``{"id": 123}`` consults no reader.

    ``_forward_body`` empties the slot id, so ``ensure_local_transcript`` sees an
    empty id and returns ``no_slot`` without a fetch.
    """
    reader = _Reader()
    _, slot_id, _ = front_app._forward_body({"model": "acme", "messages": [], "id": 123}, "acme")
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, slot_id, reader=reader))
    assert reader.keys == [], (
        "S3 was consulted for a non-string id; the task would hold a conversation "
        f"it never served: {reader.keys}"
    )
    assert outcome.action == "no_slot"
    assert not (settings.sessions_dir / "dashboard_123.jsonl").exists()


def test_a_legal_string_id_still_fetches(settings) -> None:
    """The safe asymmetry: a legal string id must still fetch (the dangerous
    direction is declining to fetch for an id the backend ACCEPTS)."""
    reader = _Reader()
    _, slot_id, _ = front_app._forward_body(
        {"model": "acme", "messages": [], "id": "cust-1"}, "acme"
    )
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, slot_id, reader=reader))
    assert len(reader.keys) == 1, f"a legal id was not fetched: {outcome.action}"


def test_REVERT_non_string_id_would_be_fetched(settings) -> None:
    """The reddening, in-line: the reverted coercion turns 123 into "123" and it
    is fetched.

    This mirrors the exact pre-fix expression rather than calling ``_forward_body``
    with the guard mutated, because the coercion is a single expression inside the
    function -- reproducing it here is the faithful revert.
    """
    raw_id = 123
    reverted_slot_id = (
        raw_id if isinstance(raw_id, str) else ("" if raw_id is None else str(raw_id))
    )
    assert reverted_slot_id == "123"
    reader = _Reader()
    asyncio.run(transcript.ensure_local_transcript(settings, reverted_slot_id, reader=reader))
    assert reader.keys == ["crews/acme/data/sessions/dashboard_123.jsonl"], (
        "the pre-fix coercion must fetch another conversation's transcript, "
        f"proving the guard is load-bearing: {reader.keys}"
    )


# ---------------------------------------------------------------------------
# Finding 3B: stream must be a real boolean.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["false", "true", "0", 1, 0, "no"])
def test_a_non_boolean_stream_is_refused(bad) -> None:
    with pytest.raises(front_app.BadForwardField):
        front_app._forward_body({"model": "acme", "messages": [], "stream": bad}, "acme")


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False)])
def test_a_real_boolean_stream_is_honoured(value, expected) -> None:
    payload = {"model": "acme", "messages": []}
    if value is not None:
        payload["stream"] = value
    _, _, stream = front_app._forward_body(payload, "acme")
    assert stream is expected


def test_REVERT_string_false_would_stream() -> None:
    """The reddening: the reverted ``bool(...)`` coercion turns "false" truthy."""
    assert bool("false") is True, "the reverted coercion would stream a non-streaming turn"
