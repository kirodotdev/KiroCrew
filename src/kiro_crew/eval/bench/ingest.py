"""Load a benchmark corpus into a real ``VectorMemoryStore``.

This is the seam where the benchmark stops being a dataset and starts being a
measurement of Kiro Crew. Everything here is deliberate about one thing: the store
under test is the production store, unmodified, with production defaults, and
every knob that could move a score is an explicit recorded field rather than a
hidden default. Memory-benchmark numbers are dominated by answer model, judge and
*ingestion granularity*; a harness that leaves granularity implicit produces a
number nobody can reproduce or compare.

Three properties of the real store shape this module, and each one is a trap that
would silently corrupt a score if ignored.

**1. The embedder loads in the background and returns ``None`` until resident.**
``make_sync_embed_fn`` never blocks (embeddings.py) — callers get ``None`` while
the model is still loading. A harness that starts writing immediately would store
rows with NULL embeddings, degrade ``search_episodic`` to its FTS5 ``LIKE``
keyword fallback, and report a "retrieval score" that measured substring matching.
:func:`prepare_embedder` therefore blocks on ``wait_ready`` and refuses to ingest
without a working vector, rather than proceeding into a meaningless run.

**2. Retrieval scoring applies a recency decay of ``exp(-0.03 * days_old)``.**
This interacts violently with dated corpora. LoCoMo's sessions are stamped in
2023; backdated literally, ``days_old`` is ~1100 and the decay factor is ~4e-15 —
every score underflows toward zero and ranking collapses into float noise. Worse,
sessions *within* one LoCoMo conversation are months apart, so even without
underflow a 300-day-older session carries a 1.2e-4 relative penalty that dwarfs
any cosine difference: retrieval degenerates into "return the newest session".
That is a real property of the store, not a bug in the benchmark, so it is
surfaced rather than hidden — see :class:`Timeline` for the three modes and what
each one is good for.

**3. Dedup at cosine 0.88 can delete gold turns.** LoCoMo is full of near-identical
pleasantries ("Hey! Good to see you!"). The store tombstones near-duplicates, and
some of those are evidence turns for some question. Measuring with dedup ON is the
faithful choice — it is what a real user's memory does — but it puts a ceiling on
achievable recall that has nothing to do with ranking quality. So every dropped
fragment is counted and reported: an unreachable ceiling that is visible is a
finding, an unreachable ceiling that is silent is a wrong number.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from kiro_crew import vector_memory
from kiro_crew.vector_memory import VectorMemoryStore

from .corpus import BenchInstance, BenchTurn

logger = logging.getLogger(__name__)

EmbedFn = Callable[[str], "list[float] | None"]

Granularity = Literal["turn", "session"]
Timeline = Literal["now", "anchored", "literal"]

#: How much wall clock the embedder may take to become resident before we give up.
#: Generous because the first run on a fresh host downloads the model.
DEFAULT_EMBED_TIMEOUT_S = 900.0


@dataclass(frozen=True)
class IngestConfig:
    """Every input that can move a score, in one recordable object.

    ``granularity`` — ``"turn"`` writes one episodic fragment per utterance,
    ``"session"`` writes one per session (the whole transcript concatenated).
    Turn-level is the default because it is the only granularity at which
    turn-level recall means anything, and because it is the harder, more honest
    retrieval task: the store must find one utterance among thousands rather than
    one document among forty. Note that NEITHER matches what production
    consolidation writes — production writes LLM-summarized fragments. That
    difference is the whole reason :mod:`.pipeline` exists as a separate mode.

    ``timeline`` — see the module docstring. ``"anchored"`` preserves the corpus's
    relative time structure while placing its most recent session at "now", which
    exercises the decay term the way a real user's memory would without the
    absolute-age underflow that ``"literal"`` produces on a 2023 corpus.
    ``"now"`` flattens every fragment to the same instant, making the decay factor
    a constant and thereby isolating pure semantic ranking — the right mode for
    attributing a retrieval change to embedding/ranking rather than to recency.

    ``dedup_threshold`` defaults to the store's own production value. It is
    exposed, not hidden, because dedup can silently eat gold turns. Two caveats
    that took a real run to establish, and that bound what raising it can buy you:

    * ``write_episodic`` *also* rejects any text whose lowercased first 80
      characters already exist (``LOWER(SUBSTR(text, 1, 80))``), unconditionally
      and without consulting this threshold at all. Byte-identical fragments are
      therefore always dropped, at any threshold.
    * the cosine near-duplicate check is gated on a live FAISS index
      (``self._faiss_index.ntotal > 0``), so on a host where ``faiss`` is not
      importable this threshold is **inert in both directions** — near-duplicates
      are never rejected, and raising it changes nothing.

    So raising it above 1.0 disables *near*-duplicate rejection only, and only
    where FAISS is present.

    ``speaker_prefix`` — LoCoMo's speaker is a person's name and many of its
    questions are about *who* said or did something, so dropping the attribution
    would make a whole category unanswerable for reasons unrelated to memory.
    """

    granularity: Granularity = "turn"
    timeline: Timeline = "anchored"
    dedup_threshold: float = 0.88
    episodic_max: int = 10_000
    importance: float = 0.5
    speaker_prefix: bool = True
    embed_timeout_s: float = DEFAULT_EMBED_TIMEOUT_S

    def describe(self) -> dict[str, object]:
        return {
            "granularity": self.granularity,
            "timeline": self.timeline,
            "dedup_threshold": self.dedup_threshold,
            "episodic_max": self.episodic_max,
            "importance": self.importance,
            "speaker_prefix": self.speaker_prefix,
        }


@dataclass
class IngestReport:
    """What actually landed, including what did not.

    ``dropped_fragments`` is the count the store refused — dedup collisions or the
    capacity cap. ``dropped_gold`` is the subset that some query needed, which is
    the number that actually bounds achievable recall. Reporting only the first
    would understate the damage; reporting neither would make an unreachable
    ceiling look like a ranking failure.
    """

    instance_id: str
    attempted: int = 0
    written: int = 0
    dropped_fragments: int = 0
    dropped_gold: tuple[str, ...] = field(default_factory=tuple)
    null_embeddings: int = 0
    backdated: int = 0
    unparsed_timestamps: int = 0
    decay_span_days: int = 0

    @property
    def recall_ceiling_note(self) -> str:
        if not self.dropped_gold:
            return ""
        return (
            f"{len(self.dropped_gold)} gold fragment(s) were refused at ingest "
            "(dedup or capacity), so recall for the affected queries cannot reach "
            "1.0 regardless of ranking quality"
        )


class IngestError(RuntimeError):
    """Raised instead of producing a number that would be meaningless."""


# ── Embedder readiness ───────────────────────────────────────────────────────


def search_backend() -> str:
    """Which ``search_episodic`` path this environment will take.

    Recorded in every report because the store has three ranking backends chosen
    by which optional dependencies are importable — exact FAISS inner product,
    a stdlib cosine scan, or an FTS5 ``LIKE`` keyword match. The third is not a
    vector search at all and orders by ``created_at DESC``. Two hosts can
    therefore produce different numbers from identical code and data, so an A/B
    is only valid when both arms report the same backend.
    """
    if vector_memory._HAS_FAISS and vector_memory._HAS_NUMPY:
        return "faiss"
    if vector_memory._HAS_NUMPY:
        return "sqlite_cosine"
    return "sqlite_cosine_pure_python"


def prepare_embedder(*, timeout_s: float = DEFAULT_EMBED_TIMEOUT_S) -> EmbedFn:
    """Return a working embed function, or raise.

    The probe is the point: ``make_sync_embed_fn`` hands back a callable that
    returns ``None`` while the model loads in the background, and a ``None``
    embedding is written as a NULL row that is keyword-searchable but not
    semantically searchable. Rather than let that degrade a run into an
    unannounced substring benchmark, wait for residency and then confirm with a
    real call.
    """
    from kiro_crew.embeddings import get_shared_embedder, make_sync_embed_fn

    embedder = get_shared_embedder()
    waiter = getattr(embedder, "wait_ready", None)
    if callable(waiter):
        waiter(timeout=timeout_s)

    embed_fn = make_sync_embed_fn()
    probe = embed_fn("benchmark embedder readiness probe")
    if not probe:
        raise IngestError(
            "the embedding model is not resident, so every fragment would be "
            "stored with a NULL embedding and search_episodic would silently "
            "fall back to FTS5 keyword LIKE matching. That measures substring "
            "overlap, not memory retrieval.\n"
            f"Waited {timeout_s:.0f}s for the model. Check that the embedding "
            "model file is present (kirocrew doctor) before benchmarking."
        )
    return embed_fn


# ── Timestamp handling ───────────────────────────────────────────────────────

# LongMemEval: "2023/04/10 (Mon) 23:07"   LoCoMo: "1:56 pm on 8 May, 2023"
_LME_TS = re.compile(r"^(\d{4})/(\d{2})/(\d{2})\s+\([A-Za-z]{3}\)\s+(\d{1,2}):(\d{2})")
_LOCOMO_TS = re.compile(
    r"^(\d{1,2}):(\d{2})\s*(am|pm)\s+on\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", re.I
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        (
            "January February March April May June July "
            "August September October November December"
        ).split(),
        start=1,
    )
}


def parse_timestamp(raw: str) -> datetime | None:
    """Parse either dataset's format into an aware UTC datetime, else ``None``.

    Returns ``None`` rather than raising or guessing: an unparsed timestamp is
    counted in the report so a corpus whose dates the harness cannot read shows up
    as a visible gap instead of a silent flattening to "now" that would quietly
    neutralize the decay term for part of the haystack.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _LME_TS.match(raw)
    if m:
        year, month_num, day, hour24, minute = (int(g) for g in m.groups())
        try:
            return datetime(year, month_num, day, hour24, minute, tzinfo=timezone.utc)
        except ValueError:
            return None
    m = _LOCOMO_TS.match(raw)
    if m:
        h12, mins, ampm, dom, mon, yr = m.groups()
        month = _MONTHS.get(mon.lower())
        if month is None:
            return None
        hour = int(h12) % 12 + (12 if ampm.lower() == "pm" else 0)
        try:
            return datetime(int(yr), month, int(dom), hour, int(mins), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _session_times(inst: BenchInstance, timeline: Timeline) -> tuple[dict[str, datetime], int, int]:
    """Map session id -> the ``created_at`` its fragments should carry.

    Returns the mapping plus (unparsed count, decay span in days). In
    ``"anchored"`` mode the newest parsed session is shifted to now and every
    other session keeps its true offset behind it, which is what preserves the
    relative decay structure without the absolute-age underflow.
    """
    now = datetime.now(tz=timezone.utc)
    if timeline == "now":
        return ({s.session_id: now for s in inst.sessions}, 0, 0)

    parsed: dict[str, datetime] = {}
    unparsed = 0
    for s in inst.sessions:
        ts = parse_timestamp(s.timestamp)
        if ts is None:
            unparsed += 1
        else:
            parsed[s.session_id] = ts

    if not parsed:
        return ({s.session_id: now for s in inst.sessions}, unparsed, 0)

    newest = max(parsed.values())
    oldest = min(parsed.values())
    span_days = max(0, (newest - oldest).days)
    shift = (now - newest) if timeline == "anchored" else timedelta(0)

    out: dict[str, datetime] = {}
    for s in inst.sessions:
        base = parsed.get(s.session_id)
        # An unparsed session is placed at the corpus's newest point rather than
        # at "now": inventing a fresher timestamp than the rest of the haystack
        # would hand it an unearned decay advantage over everything real.
        out[s.session_id] = (base + shift) if base is not None else (newest + shift)
    return out, unparsed, span_days


# ── Fragment construction ────────────────────────────────────────────────────


def fragment_text(turn: BenchTurn, *, speaker_prefix: bool) -> str:
    if speaker_prefix and turn.speaker:
        return f"{turn.speaker}: {turn.text}"
    return turn.text


def _session_fragment(session_id: str, turns: tuple[BenchTurn, ...], *, speaker_prefix: bool) -> str:
    return "\n".join(fragment_text(t, speaker_prefix=speaker_prefix) for t in turns)


# ── Ingest ───────────────────────────────────────────────────────────────────


@dataclass
class IngestedInstance:
    """A loaded store plus the maps needed to score its retrieval results.

    ``text_to_turn`` is how a search hit is attributed back to a turn id.
    Deliberately keyed on the fragment text rather than on the store's row id or
    on a tag, for two reasons: ``write_episodic`` returns only a bool so the row
    id is not obtainable, and putting ids in ``tags`` would make them visible to
    the FTS5 fallback's ``tags LIKE`` clause — a label channel in the very field
    the ranker reads. ``conversation_id`` carries the session id because that
    field is returned by search and is not searched by any backend.
    """

    instance: BenchInstance
    store: VectorMemoryStore
    report: IngestReport
    text_to_turn: dict[str, str]

    def close(self) -> None:
        self.store.close()


def ingest_instance(
    inst: BenchInstance,
    *,
    db_path: Path,
    embed_fn: EmbedFn,
    config: IngestConfig | None = None,
) -> IngestedInstance:
    """Write one instance's haystack into its own store.

    One instance, one store — never a shared one. ``longmemeval_s`` carries ~40
    sessions across 500 instances; merged into a single store that overruns
    ``episodic_max`` (10 000) and the cap starts tombstoning by ``importance ASC,
    created_at ASC``, deleting the oldest evidence first. The measurement would
    then be reporting the eviction policy, not retrieval.
    """
    cfg = config or IngestConfig()
    report = IngestReport(instance_id=inst.instance_id)

    store = VectorMemoryStore(
        db_path=db_path,
        dedup_threshold=cfg.dedup_threshold,
        episodic_max=cfg.episodic_max,
    )
    store.init()
    store.embed_fn = embed_fn  # type: ignore[attr-defined]  # the wiring production uses

    times, unparsed, span = _session_times(inst, cfg.timeline)
    report.unparsed_timestamps = unparsed
    report.decay_span_days = span if cfg.timeline != "now" else 0

    gold_turns = {tid for q in inst.queries for tid in q.gold_turn_ids}
    text_to_turn: dict[str, str] = {}
    dropped_gold: list[str] = []

    for session in inst.sessions:
        if cfg.granularity == "session":
            units: list[tuple[str, str]] = [
                (
                    session.session_id,
                    _session_fragment(
                        session.session_id, session.turns, speaker_prefix=cfg.speaker_prefix
                    ),
                )
            ]
        else:
            units = [
                (t.turn_id, fragment_text(t, speaker_prefix=cfg.speaker_prefix))
                for t in session.turns
            ]

        for unit_id, text in units:
            if not text.strip():
                continue
            report.attempted += 1
            # A text collision means two fragments are byte-identical, so the
            # store's dedup would have collapsed them anyway; attributing a hit to
            # either is equally defensible, but silently overwriting the mapping
            # would make turn-level recall depend on iteration order. Keep the
            # first and let the drop be counted below.
            first_seen = text in text_to_turn
            embedding = embed_fn(text)
            if embedding is None:
                report.null_embeddings += 1
            ok = store.write_episodic(
                text,
                embedding=embedding,
                conversation_id=session.session_id,
                tags=["bench"],
                importance=cfg.importance,
                source="benchmark",
            )
            if ok and not first_seen:
                report.written += 1
                text_to_turn[text] = unit_id
            else:
                report.dropped_fragments += 1
                if unit_id in gold_turns:
                    dropped_gold.append(unit_id)

    report.dropped_gold = tuple(dropped_gold)

    if cfg.timeline != "now":
        report.backdated = _backdate(store, times, text_to_turn, inst)

    # Only meaningful when FAISS is present; returns 0 otherwise, which is why the
    # backend is reported separately rather than inferred from this call.
    store.build_faiss_index()

    if report.null_embeddings:
        logger.warning(
            "instance %s: %d/%d fragments stored without an embedding — those rows "
            "are keyword-searchable only",
            inst.instance_id,
            report.null_embeddings,
            report.attempted,
        )

    return IngestedInstance(inst, store, report, text_to_turn)


def _backdate(
    store: VectorMemoryStore,
    times: dict[str, datetime],
    text_to_turn: dict[str, str],
    inst: BenchInstance,
) -> int:
    """Rewrite ``created_at`` so the decay term sees the corpus's real structure.

    Done as a direct UPDATE because ``write_episodic`` has no ``created_at``
    parameter and ``import_memory`` just delegates to it. This is the one place
    the harness reaches past the public API, and it is confined to a column the
    ranker reads but does not own semantics for. The value must be an aware ISO
    string: the search path does ``datetime.fromisoformat(row["created_at"])`` and
    subtracts it from an aware ``now``, so a naive string raises at query time.
    """
    updated = 0
    with store._db_lock:  # same lock the store's own writers take
        for session in inst.sessions:
            when = times.get(session.session_id)
            if when is None:
                continue
            cur = store.db.execute(
                "UPDATE episodic_memories SET created_at = ? "
                "WHERE conversation_id = ? AND is_deleted = 0",
                (when.isoformat(), session.session_id),
            )
            updated += cur.rowcount or 0
        store.db.commit()
    return updated
