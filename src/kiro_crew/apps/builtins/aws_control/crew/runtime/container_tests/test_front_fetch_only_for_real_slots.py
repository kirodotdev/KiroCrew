"""A turn the backend will refuse must not put a conversation on this disk.

Found by an adversarial cross-model review of the ephemeral change, reproduced
before fixing. The front proxies a turn and the BACKEND decides whether an id is
legal, so the fetch runs first. ``id="dashboard:cust-1"`` is not a legal backend
id (``kiro_crew.session_storage._UNIT_ID_RE`` has no colon in its class), but the
fold turns it into ``dashboard_cust-1``, which names a real conversation. The old
code downloaded that transcript and the backend then rejected the turn, leaving
the task holding a conversation it never served. That is the exact property the
ephemeral change exists to establish, defeated by one punctuation mark.

The second half is the alias lock. ``cust-1`` and ``dashboard_cust-1`` are one
conversation and one file, but the serializer was keyed on the raw string, so the
two spellings took two different locks while reporting one turn per slot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
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


def test_an_id_the_backend_rejects_is_never_fetched(settings) -> None:
    """The reproduction. A colon makes the id illegal but the fold hides that."""
    reader = _Reader()
    outcome = asyncio.run(
        transcript.ensure_local_transcript(settings, "dashboard:cust-1", reader=reader)
    )
    assert reader.keys == [], (
        "S3 was consulted for an id the backend will refuse; the task would hold "
        f"a conversation it never served: {reader.keys}"
    )
    assert outcome.action == "not_a_slot_id"
    assert not (settings.sessions_dir / "dashboard_cust-1.jsonl").exists()


@pytest.mark.parametrize("bad", ["dashboard:cust-1", "../../etc/passwd", "x y", "a\tb", "café"])
def test_no_sanitized_id_reaches_s3(settings, bad: str) -> None:
    reader = _Reader()
    asyncio.run(transcript.ensure_local_transcript(settings, bad, reader=reader))
    assert reader.keys == [], f"{bad!r} reached S3 as {reader.keys}"


@pytest.mark.parametrize("good", ["cust-1", "dashboard_cust-1", "a.b_c-d", "S3", "x" * 200])
def test_every_legal_id_still_fetches(settings, good: str) -> None:
    """The dangerous direction.

    Refusing to fetch for an id the backend ACCEPTS would serve an empty history,
    and the sidecar's whole-object put would then overwrite that customer's real
    history in S3. So this half of the guard matters more than the other.
    """
    reader = _Reader()
    outcome = asyncio.run(transcript.ensure_local_transcript(settings, good, reader=reader))
    assert len(reader.keys) == 1, f"{good!r} was not fetched: {outcome.action}"


def test_two_spellings_of_one_slot_cannot_hold_the_lock_at_once(tmp_path: Path) -> None:
    """MUTATION: key the serializer on slot_id again and the peak becomes 2.

    The first version of this test only asserted what ``normalize_slot_key``
    returns, which passes under both implementations and therefore proved
    nothing. Occupancy is the thing that matters, so measure occupancy: enter
    ``prepared_turn`` concurrently under two spellings of one conversation and
    count how many bodies are inside at the same moment.
    """
    from container.front.slotlock import SlotSerializer

    async def run() -> int:
        root = tmp_path
        (root / "sessions").mkdir()
        settings = _Settings(root)
        serializer = SlotSerializer()
        inside = 0
        peak = 0

        async def turn(spelling: str) -> None:
            nonlocal inside, peak
            async with transcript.prepared_turn(serializer, settings, spelling, _Reader()):
                inside += 1
                peak = max(peak, inside)
                await asyncio.sleep(0.02)
                inside -= 1

        await asyncio.gather(turn("cust-1"), turn("dashboard_cust-1"))
        return peak

    assert asyncio.run(run()) == 1, (
        "two spellings of one conversation ran at once, so the per-slot lock did "
        "not serialize the turns it reported serializing"
    )


def test_the_guard_admits_exactly_what_the_sanitizer_leaves_alone() -> None:
    """The shape rule is a proxy for the backend's grammar, so pin the proxy.

    Every character the backend accepts must survive the sanitizer untouched, or
    the guard would start refusing legal ids and serving empty histories.
    """
    import string

    for ch in string.ascii_letters + string.digits + "._-":
        assert transcript.is_fetchable_slot_id(f"a{ch}b"), ch
    for ch in ":/ \t?*":
        assert not transcript.is_fetchable_slot_id(f"a{ch}b"), ch
