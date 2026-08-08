"""Ingest into a real ``VectorMemoryStore``, with the toy embedder.

Real stores, ``tmp_path`` db files, and :func:`toy_embed_fn` — no model download,
no network, and no dataset file. The real embedder cannot load on this host
(``libllama.so`` is absent from the vendored Linux payload), which is precisely
the state :func:`prepare_embedder` exists to refuse, so that refusal is tested
directly rather than depended on.

Two things every fixture here has to respect, because the store enforces them and
a violation looks like a bug in the ingester:

* ``write_episodic`` rejects text shorter than 10 characters outright.
* it dedups unconditionally on ``LOWER(SUBSTR(text, 1, 80))`` — a text-prefix
  match, entirely separate from the cosine ``dedup_threshold``. Two fragments
  sharing their first 80 characters are collapsed no matter what the threshold
  says, so fixture texts must differ early.

That second point is load-bearing for the dedup pair at the bottom of this file,
where it turns out to be the mechanism actually doing the dropping.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.eval.bench import ingest as ingest_mod
from kiro_crew.eval.bench.corpus import (
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
)
from kiro_crew.eval.bench.ingest import (
    DEFAULT_EMBED_TIMEOUT_S,
    IngestConfig,
    IngestedInstance,
    IngestError,
    _session_times,
    fragment_text,
    ingest_instance,
    parse_timestamp,
    prepare_embedder,
    search_backend,
)
from kiro_crew.eval.bench.toy_embedder import toy_embed_fn

EMBED = toy_embed_fn()

LME_TS = "2023/04/10 (Mon) 23:07"
LOCOMO_TS = "1:56 pm on 8 May, 2023"


def _turn(turn_id: str, session_id: str, text: str, speaker: str = "Alice") -> BenchTurn:
    return BenchTurn(turn_id=turn_id, session_id=session_id, speaker=speaker, text=text)


def _query(**kw: object) -> BenchQuery:
    base: dict[str, object] = {
        "query_id": "q1",
        "question": "what did they discuss?",
        "category": CAT_SINGLE_HOP,
    }
    base.update(kw)
    return BenchQuery(**base)  # type: ignore[arg-type]


def _rows(loaded: IngestedInstance) -> list[sqlite3.Row]:
    """Read created_at back out of sqlite — the column the decay term reads."""
    return loaded.store.db.execute(
        "SELECT text, conversation_id, created_at, importance, tags "
        "FROM episodic_memories WHERE is_deleted = 0 ORDER BY rowid"
    ).fetchall()


def _created_by_session(loaded: IngestedInstance) -> dict[str, datetime]:
    return {
        r["conversation_id"]: datetime.fromisoformat(r["created_at"]) for r in _rows(loaded)
    }


# ── parse_timestamp ──────────────────────────────────────────────────────────


def test_longmemeval_format_parses_to_aware_utc() -> None:
    assert parse_timestamp(LME_TS) == datetime(2023, 4, 10, 23, 7, tzinfo=timezone.utc)


def test_locomo_format_parses_to_aware_utc() -> None:
    """1:56 pm is 13:56, and the day/month order is day-first."""
    assert parse_timestamp(LOCOMO_TS) == datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)


def test_parsed_timestamps_are_always_aware() -> None:
    """A naive value would raise at query time, not at ingest time.

    ``search_episodic`` does ``datetime.fromisoformat(row["created_at"])`` and
    subtracts it from an aware ``now``; a naive datetime reaching the store makes
    every later search raise ``TypeError``, far from the code that caused it.
    """
    for raw in (LME_TS, LOCOMO_TS):
        parsed = parse_timestamp(raw)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("raw", "hour"),
    [
        ("12:30 am on 8 May, 2023", 0),
        ("12:30 pm on 8 May, 2023", 12),
        ("1:00 am on 8 May, 2023", 1),
        ("11:59 pm on 8 May, 2023", 23),
    ],
)
def test_twelve_hour_clock_edges(raw: str, hour: int) -> None:
    """``% 12`` then ``+12 if pm`` — noon and midnight are where that goes wrong.

    A naive ``int(hh) + 12 if pm`` maps 12 pm to 24 (ValueError) and 12 am to 12
    (twelve hours off, silently).
    """
    parsed = parse_timestamp(raw)
    assert parsed is not None
    assert parsed.hour == hour
    assert parsed.minute == 30 if raw.startswith("12") else True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "yesterday",
        "not a date at all",
        "2023-04-10T23:07:00Z",  # ISO is neither dataset's format
        "10 April 2023",
        "1:56 pm on 8 Maybe, 2023",  # month-like but not a month
    ],
)
def test_unparseable_returns_none_rather_than_guessing(raw: str) -> None:
    """None is counted in the report; a guess would silently move the decay term."""
    assert parse_timestamp(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "2023/02/30 (Thu) 10:00",
        "2023/13/01 (Mon) 10:00",
        "10:00 am on 30 February, 2023",
        "10:00 am on 32 May, 2023",
    ],
)
def test_impossible_date_returns_none_instead_of_raising(raw: str) -> None:
    """A calendar-impossible date matches the regex; ``datetime()`` is what rejects it.

    Letting the ValueError escape would abort a whole 500-instance ingest over one
    malformed row.
    """
    assert parse_timestamp(raw) is None


def test_none_input_is_tolerated() -> None:
    assert parse_timestamp(None) is None  # type: ignore[arg-type]


# ── Fixtures for the timeline tests ──────────────────────────────────────────


def _dated_instance() -> BenchInstance:
    """Three sessions ten days apart, the third with an unreadable timestamp."""
    return BenchInstance(
        instance_id="dated",
        sessions=(
            BenchSession(
                "s1",
                (_turn("t1", "s1", "we booked the scuba diving lessons in Malta for June"),),
                "2023/04/10 (Mon) 12:00",
            ),
            BenchSession(
                "s2",
                (_turn("t2", "s2", "the espresso machine needed a new pressure gasket"),),
                "2023/04/20 (Thu) 12:00",
            ),
            BenchSession(
                "s3",
                (_turn("t3", "s3", "planting heirloom tomatoes on the south balcony"),),
                "sometime last spring",
            ),
        ),
        queries=(),
    )


def _two_turn_instance() -> BenchInstance:
    return BenchInstance(
        instance_id="gran",
        sessions=(
            BenchSession(
                "s1",
                (
                    _turn("t1", "s1", "we booked the scuba diving lessons in Malta"),
                    _turn("t2", "s1", "then argued about the espresso machine", "Bob"),
                ),
                LME_TS,
            ),
            BenchSession(
                "s2",
                (
                    _turn("t3", "s2", "planting heirloom tomatoes on the balcony"),
                    _turn("t4", "s2", "adopting a rescue greyhound named Pip", "Bob"),
                ),
                "2023/04/20 (Thu) 12:00",
            ),
        ),
        queries=(),
    )


# ── timeline="now" ───────────────────────────────────────────────────────────


def test_timeline_now_maps_every_session_to_one_identical_instant() -> None:
    """The contract at the function that owns it: one instant, no span, no unparsed.

    ``"now"`` short-circuits before parsing, so an unreadable timestamp is not even
    counted — there is nothing for it to be counted against.
    """
    times, unparsed, span = _session_times(_dated_instance(), "now")
    assert len(set(times.values())) == 1
    assert set(times) == {"s1", "s2", "s3"}
    assert (unparsed, span) == (0, 0)


def test_timeline_now_reports_a_zero_decay_span_and_a_constant_decay_factor(
    tmp_path: Path,
) -> None:
    """What ``"now"`` actually buys: ``days_old`` is 0 for every row, so decay is constant.

    Note the stored ``created_at`` strings are NOT byte-identical: ``"now"`` skips
    the backdating UPDATE entirely, so each row keeps the ``_now_iso()`` stamp
    ``write_episodic`` gave it, microseconds apart. That is not a flaw — the decay
    term is ``exp(-0.03 * (now - created).days)`` and a microsecond spread cannot
    move an integer day count. The invariant to hold is the constant decay factor,
    not string equality, so that is what is asserted.
    """
    loaded = ingest_instance(
        _dated_instance(), db_path=tmp_path / "now.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        assert loaded.report.decay_span_days == 0
        assert loaded.report.unparsed_timestamps == 0
        assert loaded.report.backdated == 0  # the UPDATE is skipped altogether
        now = datetime.now(tz=timezone.utc)
        ages = {max(0, (now - c).days) for c in _created_by_session(loaded).values()}
        assert ages == {0}
    finally:
        loaded.close()


# ── timeline="anchored" ──────────────────────────────────────────────────────


def test_anchored_preserves_relative_gaps_and_lands_the_newest_at_now(
    tmp_path: Path,
) -> None:
    """Read created_at back out of sqlite and compare gaps to the fixture's.

    The fixture's two readable sessions are exactly ten days apart. Anchoring must
    shift both by the same amount, so the gap survives while the absolute age — the
    thing that underflows ``exp(-0.03 * days)`` to 4e-15 on a 2023 corpus — does
    not.
    """
    inst = _dated_instance()
    loaded = ingest_instance(
        inst, db_path=tmp_path / "anchored.db", embed_fn=EMBED,
        config=IngestConfig(timeline="anchored"),
    )
    try:
        created = _created_by_session(loaded)
        fixture_gap = parse_timestamp("2023/04/20 (Thu) 12:00") - parse_timestamp(  # type: ignore[operator]
            "2023/04/10 (Mon) 12:00"
        )
        assert created["s2"] - created["s1"] == fixture_gap == timedelta(days=10)

        now = datetime.now(tz=timezone.utc)
        assert timedelta(0) <= now - created["s2"] < timedelta(minutes=5)
        assert loaded.report.decay_span_days == 10
        assert loaded.report.backdated == 3
    finally:
        loaded.close()


def test_anchored_stores_created_at_as_an_aware_iso_string(tmp_path: Path) -> None:
    """A naive value here raises inside ``search_episodic``, not inside ingest."""
    loaded = ingest_instance(
        _dated_instance(), db_path=tmp_path / "aware.db", embed_fn=EMBED,
        config=IngestConfig(timeline="anchored"),
    )
    try:
        for row in _rows(loaded):
            parsed = datetime.fromisoformat(row["created_at"])
            assert parsed.tzinfo is not None, row["created_at"]
            # Proves it is comparable with the aware `now` the search path builds.
            assert parsed <= datetime.now(tz=timezone.utc) + timedelta(minutes=1)
    finally:
        loaded.close()


def test_search_after_anchored_ingest_does_not_raise_on_the_decay_subtraction(
    tmp_path: Path,
) -> None:
    """The end-to-end proof of the aware-string contract.

    ``search_episodic`` subtracts ``fromisoformat(created_at)`` from an aware
    ``now``. If the backdating UPDATE ever wrote a naive string, every query
    against a backdated store would raise ``TypeError: can't subtract offset-naive
    and offset-aware datetimes`` — so the assertion is that a real search runs and
    returns the right session, not just that the column looks right.
    """
    loaded = ingest_instance(
        _dated_instance(), db_path=tmp_path / "search.db", embed_fn=EMBED,
        config=IngestConfig(timeline="anchored"),
    )
    try:
        hits = loaded.store.search_episodic(
            query_embedding=EMBED("scuba diving lessons in Malta"),
            query_text="scuba diving lessons in Malta",
            limit=5,
        )
        assert hits
        assert hits[0]["conversation_id"] == "s1"
    finally:
        loaded.close()


# ── Unparsed sessions get no unearned freshness ──────────────────────────────


def test_unparsed_session_is_placed_at_the_corpus_newest_point_not_at_now(
    tmp_path: Path,
) -> None:
    """``timeline="literal"`` is where "newest point" and "now" are distinguishable.

    Under ``"anchored"`` the shift moves the newest session to now, so "newest
    point" and "now" coincide and the test would prove nothing. Under ``"literal"``
    the shift is zero, so an implementation that reached for ``now`` would stamp
    2026 onto a 2023 corpus and hand that session a decay advantage of
    ``exp(0.03 * ~1100)`` over every real one.
    """
    loaded = ingest_instance(
        _dated_instance(), db_path=tmp_path / "literal.db", embed_fn=EMBED,
        config=IngestConfig(timeline="literal"),
    )
    try:
        created = _created_by_session(loaded)
        newest = parse_timestamp("2023/04/20 (Thu) 12:00")
        assert created["s3"] == newest
        assert created["s3"] == created["s2"]
        assert created["s3"] < datetime.now(tz=timezone.utc) - timedelta(days=365)
        assert loaded.report.unparsed_timestamps == 1
    finally:
        loaded.close()


def test_anchored_never_puts_an_unparsed_session_ahead_of_the_newest_real_one(
    tmp_path: Path,
) -> None:
    """The same invariant expressed the way anchored mode can express it."""
    loaded = ingest_instance(
        _dated_instance(), db_path=tmp_path / "anch2.db", embed_fn=EMBED,
        config=IngestConfig(timeline="anchored"),
    )
    try:
        created = _created_by_session(loaded)
        assert created["s3"] == max(created.values()) == created["s2"]
        assert loaded.report.unparsed_timestamps == 1
    finally:
        loaded.close()


def test_a_corpus_with_no_readable_timestamp_at_all_flattens_to_now(
    tmp_path: Path,
) -> None:
    """No parsed anchor means no relative structure to preserve; say so via the span."""
    inst = BenchInstance(
        "undated",
        (
            BenchSession("s1", (_turn("t1", "s1", "the scuba lessons in Malta"),), "who knows"),
            BenchSession("s2", (_turn("t2", "s2", "the espresso machine gasket"),), ""),
        ),
        (),
    )
    times, unparsed, span = _session_times(inst, "anchored")
    assert (unparsed, span) == (2, 0)
    assert len(set(times.values())) == 1


# ── Granularity ──────────────────────────────────────────────────────────────


def test_turn_granularity_writes_one_fragment_per_turn(tmp_path: Path) -> None:
    loaded = ingest_instance(
        _two_turn_instance(), db_path=tmp_path / "turn.db", embed_fn=EMBED,
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert (loaded.report.attempted, loaded.report.written) == (4, 4)
        assert len(_rows(loaded)) == 4
        # The attribution map is keyed on fragment text and resolves to turn ids.
        assert sorted(loaded.text_to_turn.values()) == ["t1", "t2", "t3", "t4"]
    finally:
        loaded.close()


def test_session_granularity_writes_one_fragment_per_session(tmp_path: Path) -> None:
    """And the fragment is the whole transcript, newline-joined, in turn order."""
    loaded = ingest_instance(
        _two_turn_instance(), db_path=tmp_path / "sess.db", embed_fn=EMBED,
        config=IngestConfig(granularity="session", timeline="now"),
    )
    try:
        assert (loaded.report.attempted, loaded.report.written) == (2, 2)
        rows = _rows(loaded)
        assert len(rows) == 2
        assert sorted(loaded.text_to_turn.values()) == ["s1", "s2"]
        by_session = {r["conversation_id"]: r["text"] for r in rows}
        assert by_session["s1"] == (
            "Alice: we booked the scuba diving lessons in Malta\n"
            "Bob: then argued about the espresso machine"
        )
    finally:
        loaded.close()


def test_a_whitespace_only_turn_is_skipped_before_it_is_attempted(tmp_path: Path) -> None:
    inst = BenchInstance(
        "blank",
        (
            BenchSession(
                "s1",
                (
                    _turn("t1", "s1", "we booked the scuba diving lessons in Malta"),
                    _turn("t2", "s1", "   ", "Bob"),
                ),
            ),
        ),
        (),
    )
    loaded = ingest_instance(
        inst, db_path=tmp_path / "blank.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", speaker_prefix=False),
    )
    try:
        assert (loaded.report.attempted, loaded.report.written) == (1, 1)
        assert loaded.report.dropped_fragments == 0
    finally:
        loaded.close()


# ── speaker_prefix ───────────────────────────────────────────────────────────


def test_speaker_prefix_changes_the_stored_text(tmp_path: Path) -> None:
    """LoCoMo asks *who* said things, so dropping attribution breaks a category."""
    inst = _two_turn_instance()
    with_prefix = ingest_instance(
        inst, db_path=tmp_path / "sp_on.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", speaker_prefix=True),
    )
    without = ingest_instance(
        inst, db_path=tmp_path / "sp_off.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", speaker_prefix=False),
    )
    try:
        on = [r["text"] for r in _rows(with_prefix)]
        off = [r["text"] for r in _rows(without)]
        assert on == [
            "Alice: we booked the scuba diving lessons in Malta",
            "Bob: then argued about the espresso machine",
            "Alice: planting heirloom tomatoes on the balcony",
            "Bob: adopting a rescue greyhound named Pip",
        ]
        assert off == [
            "we booked the scuba diving lessons in Malta",
            "then argued about the espresso machine",
            "planting heirloom tomatoes on the balcony",
            "adopting a rescue greyhound named Pip",
        ]
        assert set(with_prefix.text_to_turn) != set(without.text_to_turn)
    finally:
        with_prefix.close()
        without.close()


def test_fragment_text_drops_the_prefix_for_an_empty_speaker() -> None:
    """No dangling ": " when the dataset supplies no speaker."""
    turn = _turn("t1", "s1", "a caption with no speaker", speaker="")
    assert fragment_text(turn, speaker_prefix=True) == "a caption with no speaker"
    assert fragment_text(turn, speaker_prefix=False) == "a caption with no speaker"


# ── The dedup pair: "never stored" vs "ranking missed it" ─────────────────────

_DUP = "the identical utterance about kayaking down the Sjoa river in Norway"


def _dup_instance(*, gold: bool) -> BenchInstance:
    """Two sessions carrying byte-identical turn text; t2 is optionally gold."""
    return BenchInstance(
        "dup",
        (
            BenchSession("s1", (_turn("t1", "s1", _DUP),), "2023/04/10 (Mon) 12:00"),
            BenchSession("s2", (_turn("t2", "s2", _DUP),), "2023/04/20 (Thu) 12:00"),
        ),
        (
            (
                _query(gold_session_ids=("s2",), gold_turn_ids=("t2",)),
            )
            if gold
            else ()
        ),
    )


def test_byte_identical_turns_are_counted_as_a_dropped_fragment(tmp_path: Path) -> None:
    """One survives, one is refused, and the refusal is counted rather than hidden."""
    loaded = ingest_instance(
        _dup_instance(gold=False), db_path=tmp_path / "dup.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        assert loaded.report.attempted == 2
        assert loaded.report.written == 1
        assert loaded.report.dropped_fragments == 1
        assert len(_rows(loaded)) == 1
        assert loaded.report.dropped_gold == ()
        assert loaded.report.recall_ceiling_note == ""
    finally:
        loaded.close()


def test_a_dropped_gold_turn_bounds_recall_and_says_so(tmp_path: Path) -> None:
    """This is the number that separates an unreachable ceiling from a ranking failure.

    Without ``dropped_gold`` and the note, a query whose evidence was never stored
    looks exactly like a query the ranker failed on — and no amount of embedding
    work would ever move it.
    """
    loaded = ingest_instance(
        _dup_instance(gold=True), db_path=tmp_path / "dupgold.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        assert loaded.report.dropped_gold == ("t2",)
        note = loaded.report.recall_ceiling_note
        assert note
        assert "1 gold fragment(s) were refused at ingest" in note
        assert "cannot reach" in note
    finally:
        loaded.close()


def test_raising_dedup_threshold_above_one_does_not_rescue_identical_text(
    tmp_path: Path,
) -> None:
    """``dedup_threshold`` is a COSINE gate; byte-identical text never reaches it.

    ``IngestConfig.dedup_threshold`` is documented as "raising it above 1.0
    disables dedup entirely, which is useful to separate 'ranking missed it' from
    'it was never stored'". That is true only of the store's *similarity* dedup.
    Two other mechanisms drop a byte-identical fragment first and neither consults
    the threshold:

    1. ``write_episodic`` rejects any text whose lowercased first 80 characters
       already exist (``LOWER(SUBSTR(text, 1, 80))``), unconditionally.
    2. ``ingest_instance`` keeps only the first occurrence of a given text in
       ``text_to_turn`` (``first_seen``), so a repeat is counted as dropped even
       if the store had accepted it — deliberately, since otherwise turn-level
       attribution would depend on iteration order.

    So the escape hatch works for *near*-duplicates and not for exact ones. Pinned
    here rather than left to be rediscovered as a mystery ceiling in a report.
    """
    loose = ingest_instance(
        _dup_instance(gold=True), db_path=tmp_path / "loose.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", dedup_threshold=1.01),
    )
    try:
        assert loose.report.written == 1
        assert loose.report.dropped_fragments == 1
        assert loose.report.dropped_gold == ("t2",)
    finally:
        loose.close()


def test_the_threshold_is_recorded_in_the_config_description(tmp_path: Path) -> None:
    """Whatever it does or does not change, the report must state which value ran."""
    described = IngestConfig(dedup_threshold=1.01).describe()
    assert described["dedup_threshold"] == 1.01
    assert set(described) == {
        "granularity",
        "timeline",
        "dedup_threshold",
        "episodic_max",
        "importance",
        "speaker_prefix",
    }


def test_near_duplicates_that_differ_early_both_survive(tmp_path: Path) -> None:
    """The complement: distinct text is stored twice, so the drop above is dedup.

    Both fragments here are semantically near-identical but differ inside their
    first 80 characters, so the prefix rule does not fire. On this host the cosine
    dedup path does not fire either — it is gated on a live FAISS index and faiss
    is not importable, which is exactly why ``search_backend()`` is reported with
    every run.
    """
    inst = BenchInstance(
        "near",
        (
            BenchSession(
                "s1",
                (
                    _turn("t1", "s1", "kayaking down the Sjoa river in Norway last summer"),
                    _turn("t2", "s1", "rafting down the Sjoa river in Norway last summer"),
                ),
            ),
        ),
        (),
    )
    loaded = ingest_instance(
        inst, db_path=tmp_path / "near.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", speaker_prefix=False),
    )
    try:
        assert (loaded.report.written, loaded.report.dropped_fragments) == (2, 0)
        assert len(_rows(loaded)) == 2
    finally:
        loaded.close()


# ── prepare_embedder: the guard that refuses a meaningless run ────────────────


class _FakeEmbedder:
    def __init__(self) -> None:
        self.waited_with: float | None = None

    def wait_ready(self, timeout: float = 0.0) -> bool:
        self.waited_with = timeout
        return False


@pytest.fixture()
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeEmbedder]:
    """Stub the embeddings module so no model load is attempted on this host."""
    import kiro_crew.embeddings as embeddings

    fake = _FakeEmbedder()
    monkeypatch.setattr(embeddings, "get_shared_embedder", lambda: fake)
    yield fake


def test_prepare_embedder_refuses_when_the_model_is_not_resident(
    monkeypatch: pytest.MonkeyPatch, fake_embeddings: _FakeEmbedder
) -> None:
    """A None embedding degrades search_episodic to FTS5 LIKE. Refuse, don't measure.

    This is the difference between reporting "retrieval recall" and reporting
    substring overlap under that name, and nothing downstream can detect it from
    the number alone — which is why the guard, not a warning.
    """
    import kiro_crew.embeddings as embeddings

    monkeypatch.setattr(embeddings, "make_sync_embed_fn", lambda: (lambda _text: None))
    with pytest.raises(IngestError) as exc:
        prepare_embedder(timeout_s=0.25)
    msg = str(exc.value)
    assert "NULL embedding" in msg
    assert "FTS5 keyword LIKE matching" in msg
    assert "substring overlap, not memory retrieval" in msg
    assert "kirocrew doctor" in msg
    assert fake_embeddings.waited_with == 0.25


def test_prepare_embedder_refuses_an_empty_vector_too(
    monkeypatch: pytest.MonkeyPatch, fake_embeddings: _FakeEmbedder
) -> None:
    """``if not probe`` — an empty list is as unusable as None and must not pass."""
    import kiro_crew.embeddings as embeddings

    monkeypatch.setattr(embeddings, "make_sync_embed_fn", lambda: (lambda _t: []))
    with pytest.raises(IngestError):
        prepare_embedder(timeout_s=0.25)


def test_prepare_embedder_returns_the_fn_when_the_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch, fake_embeddings: _FakeEmbedder
) -> None:
    import kiro_crew.embeddings as embeddings

    monkeypatch.setattr(embeddings, "make_sync_embed_fn", toy_embed_fn)
    fn = prepare_embedder(timeout_s=0.25)
    assert len(fn("a probe of the readiness path")) == 1024


def test_default_embed_timeout_is_generous_enough_for_a_cold_model_download() -> None:
    assert DEFAULT_EMBED_TIMEOUT_S >= 600.0


# ── search_backend ───────────────────────────────────────────────────────────


def test_search_backend_names_one_of_the_three_documented_paths() -> None:
    """Two hosts can rank identical data differently, so an A/B must compare this."""
    assert search_backend() in {"faiss", "sqlite_cosine", "sqlite_cosine_pure_python"}


@pytest.mark.parametrize(
    ("has_faiss", "has_numpy", "expected"),
    [
        (True, True, "faiss"),
        (False, True, "sqlite_cosine"),
        (True, False, "sqlite_cosine_pure_python"),
        (False, False, "sqlite_cosine_pure_python"),
    ],
)
def test_search_backend_maps_every_dependency_combination(
    monkeypatch: pytest.MonkeyPatch, has_faiss: bool, has_numpy: bool, expected: str
) -> None:
    """FAISS without numpy is still the pure-python path — the code needs both."""
    monkeypatch.setattr(ingest_mod.vector_memory, "_HAS_FAISS", has_faiss)
    monkeypatch.setattr(ingest_mod.vector_memory, "_HAS_NUMPY", has_numpy)
    assert search_backend() == expected


# ── Report plumbing ──────────────────────────────────────────────────────────


def test_store_receives_the_configured_importance_and_the_bench_tag(tmp_path: Path) -> None:
    """importance feeds the decay score directly, so it is a recorded knob."""
    loaded = ingest_instance(
        _two_turn_instance(), db_path=tmp_path / "knobs.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now", importance=0.9),
    )
    try:
        rows = _rows(loaded)
        assert {r["importance"] for r in rows} == {0.9}
        assert all("bench" in (r["tags"] or "") for r in rows)
    finally:
        loaded.close()


def test_null_embeddings_are_counted_when_the_embed_fn_returns_none(
    tmp_path: Path,
) -> None:
    """The count exists so a degraded run is visible in the report, not just logs."""
    loaded = ingest_instance(
        _two_turn_instance(), db_path=tmp_path / "null.db", embed_fn=lambda _t: None,
        config=IngestConfig(timeline="now"),
    )
    try:
        assert loaded.report.null_embeddings == 4
        assert loaded.report.written == 4
    finally:
        loaded.close()
