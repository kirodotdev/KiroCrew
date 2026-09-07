"""The rotated+live concatenation must not serve a row twice.

Rotation archives the dropped lines FIRST and rewrites the live file's head
SECOND, and that order is deliberate: archiving is a precondition, so it fails
closed rather than deleting the only copy of those rows
(``history_rewrite._maybe_rotate``). The cost of that choice is a window in which
both files hold the same rows, and a crash or a kill inside it makes the
duplication permanent on disk.

``read_messages_chained_full`` is the pagination corpus — the index space
``before``/``next_before`` cursors and fork indices resolve against — so a
duplicated row is not cosmetic: it shifts every index above it, and a fork taken
at a rendered row lands on a different message.
"""

from __future__ import annotations

from kiro_crew.history_projection import drop_persisted_tail_prefix


def _m(mid: str, content: str = "", ts: str = "2026-09-04T00:00:00Z") -> dict:
    return {"ts": ts, "role": "user", "content": content or mid, "meta": {"mid": mid}}


def test_a_crash_window_duplication_is_served_once() -> None:
    # The archive holds rows 1-3; the live file still holds 2-3 because the
    # rewrite never landed. The corpus must read 1,2,3 — not 1,2,3,2,3.
    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("2"), _m("3"), _m("4")]
    assert drop_persisted_tail_prefix(rotated, live) == [_m("4")]


def test_a_clean_rotation_drops_nothing() -> None:
    # The normal case: the rewrite landed, so the live file starts after the
    # archive ends. Nothing may be removed here or the transcript loses rows.
    rotated = [_m("1"), _m("2")]
    live = [_m("3"), _m("4")]
    assert drop_persisted_tail_prefix(rotated, live) == live


def test_only_a_prefix_overlap_counts() -> None:
    # Both producers of this shape persist a PREFIX of the duplicated block, never
    # an interior slice, so an interior coincidence must NOT be treated as overlap.
    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("9"), _m("2"), _m("3")]
    assert drop_persisted_tail_prefix(rotated, live) == live


def test_rows_without_a_stable_id_need_ts_role_and_content() -> None:
    # A match DELETES a row, so (ts, role) alone is not enough: two distinct rows
    # can share a second and a speaker.
    a = {"ts": "2026-09-04T00:00:00Z", "role": "user", "content": "first"}
    b = {"ts": "2026-09-04T00:00:00Z", "role": "user", "content": "second"}
    assert drop_persisted_tail_prefix([a], [b]) == [b]
    assert drop_persisted_tail_prefix([a], [dict(a)]) == []


def test_the_identity_rule_is_shared_with_the_fork_path() -> None:
    # The fork rebuild reaches for the same rule through its own module. If these
    # ever diverge, one of the two duplication windows loses its guard silently.
    from kiro_crew.dashboard import chat_fork

    rotated = [_m("1"), _m("2")]
    live = [_m("2"), _m("3")]
    assert chat_fork.drop_persisted_tail_prefix(rotated, live) == drop_persisted_tail_prefix(
        rotated, live
    )


def test_read_messages_chained_full_applies_the_rule() -> None:
    """The APPLICATION, not just the rule.

    Pinning the predicate alone leaves the concatenation site free to stop calling
    it — which is exactly the state this fix found. Both readers are stubbed to
    report the crash-window overlap, so the assertion is about what the corpus
    serves rather than about disk layout.
    """
    from kiro_crew.history_projection import TranscriptReadProjection

    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("2"), _m("3"), _m("4")]

    class _Log:
        _lock = __import__("threading").RLock()
        _tab_id_index: dict | None = {}

        def get_metadata(self, key: str) -> dict:
            return {}

        def _read_messages(self, key: str) -> list[dict]:
            return list(live)

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log()  # type: ignore[attr-defined]
    proj.read_rotated_messages = lambda key: list(rotated)  # type: ignore[method-assign]

    got = proj.read_messages_chained_full("k")
    mids = [row["meta"]["mid"] for row in got]
    assert mids == ["1", "2", "3", "4"], mids
    assert len(mids) == len(set(mids)), f"a row is served twice: {mids}"


def test_the_fork_flat_prepend_branch_is_also_guarded() -> None:
    """The SECOND concatenation of the same shape, reached by a different branch.

    `read_messages_chained_full`'s own guard does not cover this one: the fork
    handler's flat prepend runs when that read was NOT used (`_rebuilt` false), so
    fixing only the function left the fork corpus exposed to the identical crash
    window. Reported by review after the first fix landed.

    Source-scanned because the branch sits inside an aiohttp handler whose
    surrounding code needs a slot, a snapshot loop and a request; the rule it must
    call is exercised behaviourally by the cases above.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "chat_fork.py"
    body = src.read_text(encoding="utf-8")
    assert "_rotated_head + drop_persisted_tail_prefix(" in body, (
        "the fork handler's flat prepend must dedupe the live prefix against the "
        "rotated head, or a crash-window duplication shifts every fork index"
    )
    assert "all_messages = _rotated_head + all_messages" not in body


class TestSegmentBoundaryDedupe:
    """The SAME crash window, one level down: segment N+1 vs segment N.

    Rotation archives the live file's head BEFORE rewriting the live file, so a
    hard crash between the two writes leaves the archived rows at the head of
    the live file too — and the NEXT rotation archives that same prefix again.
    ``read_rotated_messages`` used to concatenate segments blind, so the
    duplicate was already inside ``rotated`` before the archive-to-live guard
    ever ran. These pin that each segment is merged through
    ``drop_persisted_tail_prefix`` at the segment boundary.
    """

    @staticmethod
    def _projection(tmp_path):
        import pathlib

        from kiro_crew import history as history_mod
        from kiro_crew.history_projection import TranscriptReadProjection

        class _Log:
            def __init__(self, d: pathlib.Path) -> None:
                self._dir = pathlib.Path(d)

        proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
        proj._log = _Log(tmp_path)  # type: ignore[attr-defined]
        adir = pathlib.Path(history_mod._archive_dir(pathlib.Path(tmp_path)))
        adir.mkdir(parents=True, exist_ok=True)
        stem = history_mod._safe_key("slot-a") + history_mod.ARCHIVE_SEGMENT_DELIMITER
        return proj, adir, stem

    @staticmethod
    def _segment(adir, stem: str, stamp: str, rows: list) -> None:
        import json as _json

        lines = [_json.dumps({"_type": "archive", "reason": "rotate"})]
        lines += [_json.dumps(r) for r in rows]
        (adir / f"{stem}{stamp}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _read(self, proj) -> list:
        from kiro_crew.history_projection import TranscriptReadProjection

        return TranscriptReadProjection.read_rotated_messages(proj, "slot-a")

    def test_overlapping_segments_serve_each_row_once(self, tmp_path) -> None:
        # Segment N holds 1-3; the crash window re-archived its tail, so
        # segment N+1 begins with 2,3 before continuing 4,5.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), _m("2"), _m("3")])
        self._segment(adir, stem, "20260101-000100", [_m("2"), _m("3"), _m("4"), _m("5")])
        mids = [r["meta"]["mid"] for r in self._read(proj)]
        assert mids == ["1", "2", "3", "4", "5"], mids
        assert len(mids) == len(set(mids)), f"a row is served twice: {mids}"

    def test_partial_overlap_drops_only_the_shared_prefix(self, tmp_path) -> None:
        # Segment N+1 begins with only PART of N's tail: the helper's contract
        # is longest-prefix, so exactly that part goes, nothing more.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), _m("2"), _m("3")])
        self._segment(adir, stem, "20260101-000100", [_m("3"), _m("4")])
        mids = [r["meta"]["mid"] for r in self._read(proj)]
        assert mids == ["1", "2", "3", "4"], mids

    def test_non_overlapping_segments_lose_nothing(self, tmp_path) -> None:
        # The normal case: a clean rotation pair. A dedupe that removed anything
        # here would silently truncate the transcript head.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), _m("2")])
        self._segment(adir, stem, "20260101-000100", [_m("3"), _m("4")])
        mids = [r["meta"]["mid"] for r in self._read(proj)]
        assert mids == ["1", "2", "3", "4"], mids

    def test_the_deduped_corpus_is_what_gets_cached(self, tmp_path) -> None:
        # The merge happens inside the cached region: a cache hit must serve the
        # deduped rows, not a raw concatenation captured before the merge.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), _m("2")])
        self._segment(adir, stem, "20260101-000100", [_m("2"), _m("3")])
        first = self._read(proj)
        cached = proj._rotated_cache["slot-a"][1]  # type: ignore[attr-defined]
        assert [r["meta"]["mid"] for r in cached] == ["1", "2", "3"]
        assert self._read(proj) == first

    def test_id_less_coincidence_at_a_clean_boundary_is_preserved(self, tmp_path) -> None:
        """Deletion is worse than duplication at the segment boundary.

        Two DISTINCT id-less rows sharing (ts, role, content) across a clean
        rotation pair are a coincidence, not a re-archived prefix -- every row
        the rotation writer archives carries meta.mid, so an overlap that
        cannot be proven by mid must be preserved. Reported blocking by the
        GPT review lane (span 8b3eaa01b062): the fallback triple would have
        silently deleted the later row from the pagination/fork index space.
        """
        proj, adir, stem = self._projection(tmp_path)
        bare = {"ts": "2026-09-04T00:00:00Z", "role": "user", "content": "same words"}
        self._segment(adir, stem, "20260101-000000", [_m("1"), dict(bare)])
        self._segment(adir, stem, "20260101-000100", [dict(bare), _m("2")])
        rows = self._read(proj)
        contents = [r.get("content") for r in rows]
        assert contents == ["1", "same words", "same words", "2"], contents

    def test_mid_proven_overlap_still_drops_when_fallback_would_also_match(self, tmp_path) -> None:
        # The strict mode must not weaken the real case: a re-archived prefix
        # (mid-bearing) is still deduped exactly as before.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), _m("2")])
        self._segment(adir, stem, "20260101-000100", [_m("2"), _m("3")])
        assert [r["meta"]["mid"] for r in self._read(proj)] == ["1", "2", "3"]

    @staticmethod
    def _bare(content: str, ts: str) -> dict:
        return {"ts": ts, "role": "user", "content": content}

    def test_id_less_crash_overlap_is_deduped_by_provenance(self, tmp_path) -> None:
        """The round-2 scenario (GPT span 8b3eaa01b062): id-less crash window.

        A failed rewrite leaves segment N's rows at the live file's head, so
        rotation N+1 re-archives ALL of them as its verbatim prefix. Those
        duplicates must be dropped even though no row carries a mid, where
        identity alone could not prove them.
        """
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [self._bare("a", "t1"), self._bare("b", "t2")])
        self._segment(
            adir,
            stem,
            "20260101-000100",
            [self._bare("a", "t1"), self._bare("b", "t2"), self._bare("c", "t3")],
        )
        contents = [r["content"] for r in self._read(proj)]
        assert contents == ["a", "b", "c"], contents

    def test_id_less_partial_reprefix_is_deduped_by_provenance(self, tmp_path) -> None:
        # The rotation after the crash kept a tail, so segment N+1 is a
        # SHORTER verbatim prefix of N -- pure duplicates, no new rows.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(
            adir,
            stem,
            "20260101-000000",
            [self._bare("a", "t1"), self._bare("b", "t2"), self._bare("c", "t3")],
        )
        self._segment(adir, stem, "20260101-000100", [self._bare("a", "t1"), self._bare("b", "t2")])
        contents = [r["content"] for r in self._read(proj)]
        assert contents == ["a", "b", "c"], contents

    def test_chained_crash_overlaps_dedupe_each_link(self, tmp_path) -> None:
        # Two consecutive failed rewrites: N+2 contains N+1 which contains N.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [self._bare("a", "t1")])
        self._segment(adir, stem, "20260101-000100", [self._bare("a", "t1"), self._bare("b", "t2")])
        self._segment(
            adir,
            stem,
            "20260101-000200",
            [self._bare("a", "t1"), self._bare("b", "t2"), self._bare("c", "t3")],
        )
        contents = [r["content"] for r in self._read(proj)]
        assert contents == ["a", "b", "c"], contents

    def test_interior_id_less_coincidence_is_not_provenance(self, tmp_path) -> None:
        # Segments whose FIRST rows differ share no provenance prefix: an
        # interior id-less repeat stays, exactly as the round-1 fix pinned.
        proj, adir, stem = self._projection(tmp_path)
        self._segment(adir, stem, "20260101-000000", [_m("1"), self._bare("same", "tx")])
        self._segment(adir, stem, "20260101-000100", [self._bare("same", "tx"), _m("2")])
        contents = [r.get("content") for r in self._read(proj)]
        assert contents == ["1", "same", "same", "2"], contents
