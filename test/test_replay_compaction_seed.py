"""The compaction seed row inside the session replay (``context.build_session_replay``).

A rotation compaction leaves one ``compaction`` row behind: its content is the
digest of the history it dropped, ``meta.through_ts`` the newest row that digest
covers. The successor's replay renders the newest seed first on its own budget
and stops its tail walk at the rows the seed stands in for. Without a seed row
the replay is what it always was; the first test pins that.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import context as ctx
from kiro_crew.session_compaction_methods import SEED_META_KIND, SEED_ROLE, row_fingerprint

# The bare-path grammar is platform-gated (`image_refs._PATH_RE` reads the host's
# own shape), so a POSIX path is prose on Windows. Same spelling as upstream's
# test_replay_image_refs_11071.py.
_ABS_A = "C:\\tmp\\a.png" if os.name == "nt" else "/tmp/a.png"


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages_chained(self, key: str) -> list[dict]:
        return list(self._rows)


def _ts(second: int) -> str:
    return f"2026-09-15T12:00:{second:02d}+00:00"


def _row(role: str, content: str, second: int, **meta) -> dict:
    row = {"role": role, "content": content, "ts": _ts(second)}
    if meta:
        row["meta"] = meta
    return row


def _seed(
    content: str,
    second: int,
    through: int | None,
    method: str = "shake",
    through_row: dict | None = None,
) -> dict:
    """A seed row; *through_row* is the boundary row it names, as a real writer does."""
    meta = {"kind": SEED_META_KIND, "method": method, "dropped_rows": 2}
    if through is not None:
        meta["through_ts"] = _ts(through)
    if through_row is not None:
        meta["through_row"] = row_fingerprint(through_row)
    return _row(SEED_ROLE, content, second, **meta)


def _replay(rows: list[dict], **kw) -> str:
    out = ctx.build_session_replay(_Log(rows), "k", **kw)
    assert out is not None
    return out


class TestWithoutASeedNothingChanges:
    def test_conversation_and_inject_rows_render_as_before(self):
        rows = [
            _row("user", "hello", 1),
            _row("tool", "noise", 2),
            _row("assistant", "hi", 3),
            _row("inject", "[Cron notification] ran", 4),
        ]
        assert _replay(rows) == "User: hello\n\nAssistant: hi\n\nInject: [Cron notification] ran"

    def test_replay_rows_keep_the_two_key_shape(self):
        rows = [_row("user", "a", 1), _row("assistant", "b", 2)]
        assert ctx._replay_rows(_Log(rows), "k") == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]


class TestSeedRendering:
    def test_seed_opens_the_replay_and_names_its_method(self):
        rows = [
            _row("user", "old question", 1),
            _row("assistant", "old answer", 2),
            _seed("User: old question\n\nAssistant: [Output elided - 900 tokens]", 3, through=2),
            _row("user", "new question", 4),
        ]
        out = _replay(rows)
        assert out.startswith(
            "[Earlier history compacted via shake; large outputs elided]\n"
            "User: old question\n\nAssistant: [Output elided - 900 tokens]\n"
            "[End of compacted history]\n\n"
        )
        assert out.endswith("User: new question")

    def test_rows_the_seed_covers_are_left_out(self):
        covered_2 = _row("assistant", "covered-2", 2)
        rows = [
            _row("user", "covered-1", 1),
            covered_2,
            _row("user", "carried-3", 3),
            _seed("digest", 4, through=2, through_row=covered_2),
            _row("assistant", "carried-5", 5),
        ]
        out = _replay(rows)
        assert "covered-1" not in out and "covered-2" not in out
        assert "carried-3" in out and "carried-5" in out
        assert out.index("digest") < out.index("carried-3") < out.index("carried-5")

    def test_a_tail_row_sharing_the_boundary_stamp_is_carried(self):
        # Two streams merged into one replay can put two rows on one stamp. The
        # digest names the exact row it covers through; the row that merely
        # shares its stamp never entered the digest and must stay in the tail.
        covered = _row("user", "covered", 2)
        twin = _row("assistant", "same-stamp-twin", 2)
        rows = [
            covered,
            twin,
            _seed("digest", 3, through=2, through_row=covered),
            _row("user", "z", 4),
        ]
        out = _replay(rows)
        assert "covered" not in out.split("[End of compacted history]", 1)[1]
        assert "same-stamp-twin" in out
        assert out.endswith("User: z")

    def test_a_carried_row_repeating_the_boundary_delivery_id_stays_in_the_tail(self):
        # The walk runs newest-first and stops at the first row the seed covers,
        # judging every row older than the seed. Were the fingerprint the
        # delivery id alone, a tail row between the boundary and the seed that
        # repeated the boundary's ``mid`` would be taken for the boundary: the
        # walk would stop there and drop it, undigested, from the successor.
        covered = _row("user", "covered", 2, mid="dup")
        rows = [
            _row("assistant", "covered-1", 1),
            covered,
            _row("assistant", "carried-3-repeats-id", 3, mid="dup"),
            _row("user", "carried-4", 4),
            _seed("digest", 5, through=2, through_row=covered),
            _row("assistant", "carried-6", 6),
        ]
        out = _replay(rows)
        tail = out.split("[End of compacted history]", 1)[1]
        assert "covered" not in tail
        assert "carried-3-repeats-id" in tail and "carried-4" in tail
        assert tail.endswith("Assistant: carried-6")

    def test_a_legacy_seed_without_a_boundary_row_is_exclusive_at_its_stamp(self):
        # A seed written before ``through_row`` existed: the stamp alone decides,
        # and the safe side of the tie is to carry the row (a duplicate in the
        # digest and the tail loses nothing; a drop would).
        rows = [
            _row("user", "older", 1),
            _row("assistant", "at-the-stamp", 2),
            _seed("digest", 3, through=2),
            _row("user", "z", 4),
        ]
        out = _replay(rows)
        assert "older" not in out
        assert "at-the-stamp" in out

    def test_only_the_newest_seed_is_rendered(self):
        a = _row("user", "a", 1)
        b = _row("user", "b", 3)
        rows = [
            a,
            _seed("first digest", 2, through=1, through_row=a),
            b,
            _seed("second digest", 4, through=3, through_row=b),
            _row("user", "c", 5),
        ]
        out = _replay(rows)
        assert "second digest" in out
        assert "first digest" not in out
        assert "User: a" not in out and "User: b" not in out
        assert out.endswith("User: c")

    def test_only_the_newest_seed_is_rendered_even_without_coverage(self):
        # The newest seed names no ``through_ts``, so nothing is dropped as covered:
        # the older seed is left out by the one-seed rule alone.
        rows = [
            _row("user", "a", 1),
            _seed("first digest", 2, through=1),
            _row("user", "b", 3),
            _seed("second digest", 4, through=None),
            _row("user", "c", 5),
        ]
        out = _replay(rows)
        assert "second digest" in out
        assert "first digest" not in out
        assert "User: a" in out and "User: b" in out
        assert out.count("compacted via") == 1

    def test_a_seed_without_coverage_drops_nothing(self):
        rows = [_row("user", "kept", 1), _seed("digest", 2, through=None), _row("user", "z", 3)]
        out = _replay(rows)
        assert "User: kept" in out and "digest" in out and out.endswith("User: z")

    def test_an_unreadable_coverage_stamp_drops_nothing(self):
        seed = _seed("digest", 2, through=None)
        seed["meta"]["through_ts"] = "not a timestamp"
        rows = [_row("user", "kept", 1), seed, _row("user", "z", 3)]
        assert "User: kept" in _replay(rows)

    def test_an_empty_seed_is_ignored(self):
        rows = [_row("user", "kept", 1), _seed("", 2, through=1), _row("user", "z", 3)]
        out = _replay(rows)
        assert "compacted via" not in out and "User: kept" in out

    def test_a_seed_alone_is_a_replay(self):
        out = _replay([_seed("digest only", 1, through=None, method="soft")])
        assert out == (
            "[Earlier history compacted via soft; large outputs elided]\n"
            "digest only\n[End of compacted history]"
        )

    def test_a_row_without_a_timestamp_is_not_treated_as_covered(self):
        rows = [
            {"role": "user", "content": "legacy row"},
            _seed("digest", 2, through=1),
            _row("user", "z", 3),
        ]
        # Rows without ``ts`` are only merged in insertion order, so the legacy row
        # sits before the seed; with no stamp to compare it is carried, not dropped.
        assert "legacy row" in _replay(rows)


class TestSeedBudget:
    def test_the_seed_does_not_eat_the_conversation_budget(self):
        # A digest exactly at the seed's share, and a tail that fills the whole
        # conversation budget: both must survive whole.
        seed_share = ctx._REPLAY_BUDGET_CHARS // ctx._REPLAY_SEED_BUDGET_DIVISOR
        digest = "d" * seed_share
        tail_rows = [_row("user", f"t{i} " + "x" * 7_000, 10 + i) for i in range(12)]
        rows = [_seed(digest, 5, through=1), *tail_rows]
        out = _replay(rows)
        assert digest in out
        # The conversation walk keeps as many tail rows as 80K chars admit (~11),
        # the same count it keeps with no seed present.
        assert out.count("User: t") == _replay(tail_rows).count("User: t")

    def test_an_oversized_digest_is_clipped_to_its_share(self):
        seed_share = ctx._REPLAY_BUDGET_CHARS // ctx._REPLAY_SEED_BUDGET_DIVISOR
        digest = "head " + "d" * (2 * seed_share)
        out = _replay([_seed(digest, 1, through=None), _row("user", "z", 2)])
        block = out.split("\n[End of compacted history]")[0].split("\n", 1)[1]
        assert len(block) == seed_share
        assert block.startswith("head ") and block.endswith("...[truncated]")
        assert out.endswith("User: z")


class TestSeedImageReferences:
    """A seed is replayed history: its digest carries the marker, never the path.

    The stored row keeps the reference (``rotation_rows`` returns stored rows,
    and ``row_fingerprint`` hashes stored content), so the boundary identity a
    digest records still matches the row on disk.
    """

    def test_a_seed_digest_replays_with_the_marker_not_the_path(self):
        from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER

        rows = [
            _seed(
                "User: see ![shot](/tmp/shot.png) here\nAssistant: a red banner", 1, through=None
            ),
            _row("assistant", "carried", 2),
        ]
        out = _replay(rows)
        assert STRIPPED_IMAGE_MARKER in out
        assert "/tmp/shot.png" not in out
        assert out.endswith("Assistant: carried")

    def test_rotation_rows_keep_the_stored_reference(self):
        pictured = _row("user", "see ![shot](/tmp/shot.png) here", 1, mid="m1")
        got = ctx.rotation_rows(_Log([pictured, _row("assistant", "ok", 2)]), "k")
        assert got[0]["content"] == "see ![shot](/tmp/shot.png) here"
        assert row_fingerprint(got[0]) == row_fingerprint(pictured)

    def test_the_shared_renderer_strips_stored_rows_the_writer_hands_it(self):
        """The writer walks STORED rows through the replay's renderer, so it strips too."""
        from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER

        line_of = ctx.replay_walk_options(200_000)["line_of"]
        stored = _row("user", f"look {_ABS_A}", 1)
        assert line_of(stored) == f"User: look {STRIPPED_IMAGE_MARKER}"
        # Idempotent: the row the replay already stripped renders identically.
        assert line_of({"role": "user", "content": f"look {STRIPPED_IMAGE_MARKER}"}) == line_of(
            stored
        )


class TestRotationRows:
    def test_rows_keep_ts_and_meta_and_stop_at_the_seed_coverage(self):
        covered = _row("user", "covered", 1)
        rows = [
            covered,
            _seed("digest", 2, through=1, through_row=covered),
            _row("tool", "noise", 3),
            _row("assistant", "carried", 4),
        ]
        got = ctx.rotation_rows(_Log(rows), "k")
        assert [r["role"] for r in got] == [SEED_ROLE, "assistant"]
        assert got[0]["meta"]["through_ts"] == _ts(1)
        assert got[0]["meta"]["through_row"] == row_fingerprint(covered)
        assert got[1]["ts"] == _ts(4)

    def test_pending_rows_merge_before_the_split(self):
        disk = [_row("user", "on disk", 1)]
        live = [_row("assistant", "in the window", 2)]
        got = ctx.rotation_rows(_Log(disk), "k", pending_messages=live)
        assert [r["content"] for r in got] == ["on disk", "in the window"]


@pytest.mark.parametrize("role", ["tool", "system", "compacting", "done"])
def test_other_non_conversation_roles_stay_out_of_the_replay(role):
    rows = [_row("user", "a", 1), _row(role, "noise", 2), _row("assistant", "b", 3)]
    assert _replay(rows) == "User: a\n\nAssistant: b"
