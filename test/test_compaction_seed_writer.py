"""The dashboard's ``shake`` seed writer (``dashboard.compaction_seed``).

``build_seed`` is a table over row lists: which rows the tail keeps, what the
digest of the rest looks like, and what the seed row's ``meta`` records.
``write_seed`` is the slot-side glue, driven here with a fake slot and a fake
transcript so the only thing under test is what it reads and what it appends.

The load-bearing contract is ``TestNoGapBetweenDigestAndTail``: the writer
cuts its tail with the replay's own window-scaled budget, so on ANY window the
first row the successor replays verbatim is the row right after the newest row
the digest covers. A second tail figure (a token key, a reference-window
constant) is exactly what would let rows fall between the two.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from kiro_crew.context import build_session_replay
from kiro_crew.context_budget import replay_budget_chars
from kiro_crew.dashboard import compaction_seed as cs
from kiro_crew.session_compaction_methods import (
    SEED_BUDGET_DIVISOR,
    SEED_META_KIND,
    SEED_ROLE,
    replay_line,
    row_fingerprint,
)

# A 200K-token model: the smallest replay budget the caps resolve to (16K chars,
# a fifth of the 80K reference), and the window Opus's finding was written for.
SMALL_WINDOW = 200_000

# The bare-path grammar is platform-gated (`image_refs._PATH_RE` reads the host's
# own shape), so a POSIX path is prose on Windows. Same spelling as upstream's
# test_replay_image_refs_11071.py.
_ABS_SHOT = "C:\\tmp\\shot.png" if os.name == "nt" else "/tmp/shot.png"


def _ts(second: int) -> str:
    return f"2026-09-15T12:{second // 60:02d}:{second % 60:02d}+00:00"


def _rows(n: int, size: int = 500) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"row{i:03d} " + ("x" * size),
            "ts": _ts(i),
        }
        for i in range(n)
    ]


class TestBuildSeed:
    def test_nothing_older_than_the_tail_means_no_seed(self):
        rows = _rows(4)  # ~2K chars: everything fits any window's tail
        assert cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake") is None

    def test_empty_rows_mean_no_seed(self):
        assert cs.build_seed([], window_tokens=SMALL_WINDOW, method="shake") is None

    def test_dropped_rows_become_a_bounded_digest_with_meta(self):
        rows = _rows(80)  # ~40K chars against a 16K tail: ~31 kept, ~49 dropped
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        content, meta = seed
        budget = replay_budget_chars(SMALL_WINDOW)
        assert len(content) <= budget // SEED_BUDGET_DIVISOR
        assert content.startswith("User: row000 ")
        dropped = meta["dropped_rows"]
        assert 10 <= dropped < 80
        # The newest dropped row is the one right before the tail, and its ts is
        # what the replay stops at.
        assert meta["through_ts"] == rows[dropped - 1]["ts"]
        assert rows[dropped]["content"] not in content, "tail rows are not digested"
        assert meta["kind"] == SEED_META_KIND and meta["method"] == "shake"

    def test_a_larger_window_keeps_a_larger_tail(self):
        rows = _rows(80)
        small = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        large = cs.build_seed(rows, window_tokens=1_000_000, method="shake")
        assert small is not None
        # 40K chars fit whole in the 80K reference tail: nothing to digest.
        assert large is None
        assert small[1]["dropped_rows"] > 0

    def test_an_earlier_seed_is_carried_first_without_a_speaker_label(self):
        prior = {
            "role": SEED_ROLE,
            "content": "User: the goal\n\nAssistant: [Output elided - 40 tokens]",
            "ts": _ts(0),
            "meta": {"kind": SEED_META_KIND, "method": "shake", "dropped_rows": 7},
        }
        rows = [prior, *_rows(80)[1:]]
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        content, meta = seed
        assert content.startswith(
            "[Digest carried from an earlier compaction, standing in for 7 rows]\n\n"
            "User: the goal\n\nAssistant: [Output elided - 40 tokens]\n\n"
        )
        assert "Compaction:" not in content
        assert meta["dropped_rows"] > 7, "the 7 rows the earlier seed stood in for are counted"

    def test_a_large_earlier_digest_is_carried_clipped_and_never_omitted(self):
        """Round-8 finding (GPT/Opus): a first shake's digest may be as large as
        the whole digest budget; folded as an ordinary row it overflowed the
        head share and vanished into the omitted-rows marker while the new
        boundary bounded the replay past it. Carried by kind it is present,
        visibly truncated, and its rows stay in the count."""
        budget = replay_budget_chars(SMALL_WINDOW) // SEED_BUDGET_DIVISOR  # 8K chars
        prior_body = "User: earliest goal " + ("g" * (budget - 200))  # near the full budget
        prior = {
            "role": SEED_ROLE,
            "content": prior_body,
            "ts": _ts(0),
            "meta": {"kind": SEED_META_KIND, "method": "shake", "dropped_rows": 120},
        }
        rows = [prior, *_rows(80)[1:]]  # ~40K chars of newer conversation
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        content, meta = seed
        lines = content.split("\n\n")
        assert lines[0] == "[Digest carried from an earlier compaction, standing in for 120 rows]"
        assert lines[1].startswith("User: earliest goal ")
        assert lines[1].endswith("…[truncated]"), "shortened visibly, not left out"
        assert len(lines[1]) <= budget // 2, "the carried digest stays within its share"
        assert "row001" in content or "row002" in content, "the newest conversation still digests"
        assert len(content) <= budget + 2
        assert meta["dropped_rows"] >= 120 + 1, "counts the earlier seed's rows plus the new ones"
        assert meta["dropped_rows"] < 120 + 79
        assert meta["through_row"] != row_fingerprint(prior), "the boundary is a newer row"

    def test_replay_line_splices_a_seed_row_as_its_content(self):
        assert replay_line({"role": SEED_ROLE, "content": "User: a"}) == "User: a"
        assert replay_line({"role": "user", "content": "a"}) == "User: a"


class _Slot:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.appended: list[tuple] = []
        self.withdrawn: list[dict] = []

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None, **kw):
        self.appended.append((role, content, cls, meta))
        self.broadcasts = getattr(self, "broadcasts", []) + [broadcast]
        row = {"role": role, "content": content, "meta": meta}
        self.messages.append(row)
        return row

    def withdraw(self, row) -> bool:
        self.withdrawn.append(row)
        self.messages = [m for m in self.messages if m is not row]
        return True


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def read_messages_chained(self, key: str) -> list[dict]:
        return list(self.rows)


def _state(slot, rows_on_disk, *, save_fails: Exception | None = None):
    saved: list = []

    def save_slot_strict(target) -> None:
        if save_fails is not None:
            raise save_fails
        saved.append((target, len(target.messages)))

    return SimpleNamespace(
        get_slot=lambda name: slot if name == "chat-7" else None,
        conversation_log=_Log(rows_on_disk),
        save_slot_strict=save_slot_strict,
        saved=saved,
    )


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
        lambda key: "chat-7" if key == "dashboard:chat-7" else "",
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: "hist")


class TestWriteSeed:
    @pytest.mark.asyncio
    async def test_appends_one_seed_row_with_meta(self, routed):
        disk = _rows(76)
        live = _rows(80)[76:]  # four newer rows only in the window
        slot = _Slot(live)
        wrote = await cs.write_seed(_state(slot, disk), "dashboard:chat-7", "shake", SMALL_WINDOW)
        assert wrote == cs.SEED_WRITTEN
        assert len(slot.appended) == 1
        role, content, cls, meta = slot.appended[0]
        assert role == SEED_ROLE and cls == ""
        assert meta["kind"] == SEED_META_KIND and meta["method"] == "shake"
        assert content.startswith("User: row000 ")
        assert live[-1]["content"] not in content, "live tail rows stay verbatim"

    @pytest.mark.asyncio
    async def test_the_seed_is_on_disk_before_the_writer_answers_written(self, routed):
        # The answer lets the coordinator drop the native conversation: a
        # restart between the recycle and the periodic flush would otherwise
        # reload a transcript with no seed. The strict save runs with the seed
        # row already in the window, and only then does the writer answer.
        slot = _Slot(_rows(80)[76:])
        state = _state(slot, _rows(76))
        assert (
            await cs.write_seed(state, "dashboard:chat-7", "shake", SMALL_WINDOW) == cs.SEED_WRITTEN
        )
        assert [(s is slot, n) for s, n in state.saved] == [(True, 5)], "saved once, seed included"
        assert slot.withdrawn == []

    @pytest.mark.asyncio
    async def test_a_seed_that_cannot_be_made_durable_is_withdrawn(self, routed):
        # Nothing may be acknowledged on the strength of a write that did not
        # happen: the row leaves the window (a later shake must not carry a
        # digest of rows that were never dropped) and the ladder hands to
        # native, which keeps the history.
        slot = _Slot(_rows(80)[76:])
        state = _state(slot, _rows(76), save_fails=OSError("disk full"))
        assert (
            await cs.write_seed(state, "dashboard:chat-7", "shake", SMALL_WINDOW)
            == cs.SEED_UNSUPPORTED
        )
        assert len(slot.appended) == 1 and len(slot.withdrawn) == 1
        assert slot.withdrawn[0]["role"] == SEED_ROLE
        assert all(m["role"] != SEED_ROLE for m in slot.messages)

    @pytest.mark.asyncio
    async def test_the_seed_row_is_not_broadcast_as_a_live_frame(self, routed):
        # Hidden by contract: the transcript registries claim the role undrawn,
        # so the row must not reach the transcript view as a chat_message frame.
        slot = _Slot(_rows(80)[76:])
        assert (
            await cs.write_seed(_state(slot, _rows(76)), "dashboard:chat-7", "shake", SMALL_WINDOW)
            == cs.SEED_WRITTEN
        )
        assert slot.broadcasts == [False]

    @pytest.mark.asyncio
    async def test_a_channel_session_is_refused_before_any_tab_lookup(self, monkeypatch):
        # A channel-born conversation keeps its own key while a tab mirrors it,
        # so the writer answers unsupported for the key even when
        # dashboard_slot_key would answer with the mirroring tab: the ladder
        # then hands to native rather than digesting a channel's history into a
        # tab its user may never open.
        looked_up: list[str] = []

        def _slot_key(key: str) -> str:
            looked_up.append(key)
            return "chat-7"

        monkeypatch.setattr("kiro_crew.dashboard.chat_utils.dashboard_slot_key", _slot_key)
        monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: "hist")
        slot = _Slot(_rows(80)[76:])
        for key in ("slack:1726000000.000100", "discord:kirocrew:direct:42", "telegram:7"):
            assert cs.supports_seed(_state(slot, _rows(76)), key) is False
            assert (
                await cs.write_seed(_state(slot, _rows(76)), key, "shake", SMALL_WINDOW)
                == cs.SEED_UNSUPPORTED
            )
        assert looked_up == [] and slot.appended == []

    @pytest.mark.asyncio
    async def test_no_tab_for_the_session_is_unsupported(self, routed):
        # A cron-born key is no channel, and no tab shows it: nothing here can
        # hold its digest.
        slot = _Slot([])
        state = _state(slot, _rows(80))
        assert cs.supports_seed(state, "cron:job-7") is False
        assert (
            await cs.write_seed(state, "cron:job-7", "shake", SMALL_WINDOW) == cs.SEED_UNSUPPORTED
        )
        assert slot.appended == []

    @pytest.mark.asyncio
    async def test_a_tab_that_is_gone_is_unsupported(self, routed):
        state = SimpleNamespace(get_slot=lambda name: None, conversation_log=_Log(_rows(80)))
        assert cs.supports_seed(state, "dashboard:chat-7") is False
        assert (
            await cs.write_seed(state, "dashboard:chat-7", "shake", SMALL_WINDOW)
            == cs.SEED_UNSUPPORTED
        )

    @pytest.mark.asyncio
    async def test_everything_in_the_tail_is_nothing_to_digest(self, routed):
        # Supported (a tab holds the transcript), yet nothing is older than the
        # tail: the one answer that lets the ladder reach soft.
        slot = _Slot([])
        state = _state(slot, _rows(3))
        assert cs.supports_seed(state, "dashboard:chat-7") is True
        assert (
            await cs.write_seed(state, "dashboard:chat-7", "shake", SMALL_WINDOW) == cs.SEED_NOTHING
        )
        assert slot.appended == []

    def test_supports_reads_no_transcript(self, routed):
        # The coordinator asks this before projecting, on every threshold
        # crossing: it must stay a lookup, never a file parse.
        class _NoRead:
            def read_messages_chained(self, key: str) -> list[dict]:
                raise AssertionError("supports must not read the transcript")

        state = SimpleNamespace(get_slot=lambda name: _Slot([]), conversation_log=_NoRead())
        assert cs.supports_seed(state, "dashboard:chat-7") is True

    @pytest.mark.asyncio
    async def test_the_registered_writer_binds_both_answers_to_one_state(self, routed):
        slot = _Slot(_rows(80)[76:])
        writer = cs.DashboardSeedWriter(_state(slot, _rows(76)))
        assert writer.supports("dashboard:chat-7") is True
        assert writer.supports("slack:1726000000.000100") is False
        assert await writer("dashboard:chat-7", "shake", SMALL_WINDOW) == cs.SEED_WRITTEN
        assert len(slot.appended) == 1


class TestNoGapBetweenDigestAndTail:
    """The exploit class GPT/Opus named: a tail cut with one budget, replayed with another.

    A 200K model replays a 16K-char tail. A writer that cut at the 80K reference
    figure (or at any separate key) would stamp ``through_ts`` 64K chars
    further back than the replay reaches, and every row in between would be in
    neither the digest nor the tail. With one budget the two cuts coincide.
    """

    @pytest.mark.parametrize("window", [SMALL_WINDOW, 1_000_000, None])
    def test_every_row_newer_than_the_digest_is_replayed_verbatim(self, window):
        rows = _rows(300)  # ~150K chars: overflows the 80K reference tail too
        seed = cs.build_seed(rows, window_tokens=window, method="shake")
        assert seed is not None
        content, meta = seed
        dropped = meta["dropped_rows"]
        seeded = [
            *rows,
            {"role": SEED_ROLE, "content": content, "ts": _ts(1000), "meta": meta},
        ]
        replay = build_session_replay(_Log(seeded), "k", model_window=window)
        assert replay is not None
        tail_part = replay.split("[End of compacted history]", 1)[1]
        # The first replayed conversation row is the row right after the newest
        # row the digest covers, and every row after it is there too: no gap.
        assert tail_part.lstrip().startswith(replay_line(rows[dropped]))
        for row in rows[dropped:]:
            assert row["content"] in tail_part
        assert rows[dropped - 1]["content"] not in tail_part, "digested rows are not replayed"

    def test_rows_holding_image_references_split_and_replay_with_no_gap(self):
        """The replay strips image references; the writer walks stored rows.

        A bare path and its replacement marker differ in length, so a renderer
        that stripped on one side only would measure the two walks differently
        at the budget edge and a row could sit in neither the digest nor the
        tail. Both walks render through the same stripping ``line_of``.
        """
        from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER

        rows = _rows(300)
        for row in rows:
            row["content"] += f" {_ABS_SHOT}"  # every row carries a bare image path
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        content, meta = seed
        dropped = meta["dropped_rows"]
        seeded = [*rows, {"role": SEED_ROLE, "content": content, "ts": _ts(1000), "meta": meta}]
        replay = build_session_replay(_Log(seeded), "k", model_window=SMALL_WINDOW)
        assert replay is not None
        assert _ABS_SHOT not in replay, "no vehicle carries the path"
        digest_part, tail_part = replay.split("[End of compacted history]", 1)
        assert STRIPPED_IMAGE_MARKER in digest_part
        assert tail_part.lstrip().startswith(
            f"User: row{dropped:03d} "
        ) or tail_part.lstrip().startswith(f"Assistant: row{dropped:03d} ")
        for row in rows[dropped:]:
            assert f"row{int(row['content'][3:6]):03d} " in tail_part
        assert f"row{dropped - 1:03d} " not in tail_part, "digested rows are not replayed"

    def test_the_writer_and_the_replay_walk_with_one_set_of_options(self):
        # Pins the mechanism, not just the outcome: the writer's module has no
        # budget, clipping or reserve arithmetic of its own to drift.
        import inspect

        source = inspect.getsource(cs.build_seed)
        assert "replay_walk_options(window_tokens)" in source
        assert "split_tail(rows, **options)" in source
        assert "CHARS_PER_TOKEN" not in source and "80_000" not in source

    def test_the_digest_names_the_exact_row_it_covers_through(self):
        rows = _rows(300)
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        _, meta = seed
        boundary = rows[meta["dropped_rows"] - 1]
        assert meta["through_row"] == row_fingerprint(boundary)
        assert meta["through_ts"] == boundary["ts"]

    def test_two_rows_on_one_stamp_at_the_boundary_lose_nothing(self):
        # The row right after the digest's last row shares its timestamp (two
        # merged streams). It is not in the digest, so it must be in the tail.
        rows = _rows(300)
        probe = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert probe is not None
        cut = probe[1]["dropped_rows"]
        rows[cut]["ts"] = rows[cut - 1]["ts"]  # collide the stamps at the boundary
        content, meta = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert meta["dropped_rows"] == cut, "a stamp collision does not move the cut"
        seeded = [*rows, {"role": SEED_ROLE, "content": content, "ts": _ts(1000), "meta": meta}]
        replay = build_session_replay(_Log(seeded), "k", model_window=SMALL_WINDOW)
        assert replay is not None
        tail_part = replay.split("[End of compacted history]", 1)[1]
        assert rows[cut]["content"] in tail_part, "the equal-stamp tail row is replayed"
        assert rows[cut - 1]["content"] not in tail_part, "the digested twin is not"

    def test_an_oversized_newest_inject_row_cannot_push_conversation_into_the_digest(self):
        # The replay clips an inject row to its per-row ceiling and gives inject
        # rows a reserved share; a writer walking without those rules would let a
        # huge newest inject eat the whole budget and digest recent conversation
        # the successor replays verbatim.
        rows = _rows(60)  # ~30K chars of conversation, over the 16K tail
        rows.append({"role": "inject", "content": "n" * 100_000, "ts": _ts(60)})
        seed = cs.build_seed(rows, window_tokens=SMALL_WINDOW, method="shake")
        assert seed is not None
        content, meta = seed
        cut = meta["dropped_rows"]
        assert cut < 60, "recent conversation stays in the tail beside the clipped inject"
        seeded = [*rows, {"role": SEED_ROLE, "content": content, "ts": _ts(1000), "meta": meta}]
        replay = build_session_replay(_Log(seeded), "k", model_window=SMALL_WINDOW)
        assert replay is not None
        tail_part = replay.split("[End of compacted history]", 1)[1]
        for row in rows[cut:60]:
            assert row["content"] in tail_part
        assert rows[cut - 1]["content"] not in tail_part
