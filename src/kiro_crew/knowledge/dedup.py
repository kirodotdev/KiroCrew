"""Cross-source document de-duplication for the Knowledge Base.

When the same document is ingested from more than one source -- e.g. uploaded
directly AND synced via a folder -- this collapses the duplicates down to a single
canonical copy so retrieval stops returning the same content twice.

Two-tier match (a pair is a duplicate if EITHER tier matches):
  Tier 1 (exact): identical whole-doc extracted-text ``content_hash``. Name-independent.
  Tier 2 (fuzzy): a filename near-match AND a doc-level embedding cosine >= threshold,
    compared only between docs sharing the same ``embedding_sig``. The filename gate is
    an AND -- it can only reduce false collapses, never create new ones.

Priority (which copy survives): a persistent source (a folder / obsidian_vault / quip
-- something that re-syncs) beats a transient one (a one-shot upload or chat capture);
within a class the newest ``mtime`` wins; on an ``mtime`` tie the oldest-resident copy
wins (stable, deterministic).

Action: the losing document is hard-deleted (its KB entry, chunk rows, and -- for a
whole-source doc -- its source row). The underlying file on disk is never touched.

Granularity: a "document" is one folder file (a ``folder_file_state`` row) for folder
sources, or the whole source for upload/chat sources. All chunk rows of a document
share one ``content_hash``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher

from kiro_crew.sel import sel

from .embedder import bytes_to_floats

logger = logging.getLogger(__name__)

# Source types whose documents persist and re-sync on the next scan; they outrank
# transient one-shot sources (a manual upload or a chat capture) when the same
# document exists in both. A file-backed artifact source will join this set when
# that source type lands.
PERSISTENT_SOURCE_TYPES = frozenset({"local_folder", "obsidian_vault", "quip"})

# Aggregate sources bundle MANY logical documents under ONE sources row
# (auto-ingested artifacts store every artifact's chunks under a single
# source_type="artifact", file_path=None row, grouped per-slug only in
# artifact_item_state). They must NOT be treated as one whole-source dedup unit:
# _build_doc would hash only the FIRST item, and a single artifact matching a
# watched folder file would make the aggregate the dedup loser, so
# _delete_doc -> delete_source_cascade would wipe EVERY artifact's KB entry.
# Excluding them from enumerate_docs is the surgical fix (per-slug dedup is a
# larger follow-up).
_AGGREGATE_SOURCE_TYPES = frozenset({"artifact"})

# Tier-2 fuzzy match floor. Validated against a live KB: true duplicates and
# version-drift copies land at cosine >= 0.95, while same-topic-but-different docs
# top out around 0.89 -- so 0.95 separates them with margin.
DEFAULT_FUZZY_THRESHOLD = 0.95

# Filename near-match floor: ratio on the normalized stem for names that are not
# exactly equal after normalization (tolerates minor edits/typos).
_FILENAME_RATIO_FLOOR = 0.9

# Copy/duplicate filename modifiers stripped before comparison: " (1)", " (copy)",
# " - copy", " copy 2", "copy of ".
_COPY_MODIFIER_RE = re.compile(
    r"(?:\s*\(\d+\)"                   # " (1)"
    r"|\s*\(copy\)"                    # " (copy)"
    r"|\bcopy\s+of\s+"                 # "copy of " -- must precede the bare-copy branch
    r"|\s*-?\s*\bcopy\b(?:\s+\d+)?)",  # " copy", " - copy", " copy 2"
    re.IGNORECASE)
# Date-like tokens are stripped from the stem (so "Report Apr 2026" and
# "Report Dec 2025" reduce to the same stem) and then compared SEPARATELY by
# _dates_compatible -- the stem must match AND the dates must be close. Stripping
# alone is deliberately not the guard: a placeholder would make every dated
# instance of a series look identical.
_DATE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{4}[-_./]?\d{2}[-_./]?\d{2}"   # 20260414 / 2026-04-14
    r"|\d{1,2}[-_./]\d{1,2}(?:[-_./]\d{2,4})?"             # 4/14 / 04-14-2026
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{0,4})"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE)
_SEP_RE = re.compile(r"[\s\-_.]+")

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Two dated filenames count as the same document only if their closest date pair
# is within this many days. A few days apart (a re-save, an off-by-N-day revision,
# a cross-month-boundary nudge) is the same doc; a month or more apart is a
# distinct instance in a series (e.g. monthly program updates) and must NOT be
# collapsed. Month-only dates are pinned to mid-month (day 15), so adjacent months
# land ~30 days apart and are correctly rejected, while same-month dates pass.
_DATE_MATCH_MAX_DAYS = 7

# Date extractors used ONLY to block an over-eager fuzzy filename match -- they can
# never create a new match. ISO (2026-04-14), US numeric (04_14 / 04-14-2026), and
# month-name (Apr 2026 / Dec25 / April 29 / Apr 14 2026) forms. The (?<![A-Za-z0-9])
# / (?![A-Za-z0-9]) boundaries (rather than \b) treat "_" as a separator, so
# underscore-delimited names like "Status_Apr_2026" are not invisible to the gate.
_ISO_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{4})[-_./]?(\d{2})[-_./]?(\d{2})(?![A-Za-z0-9])")
_NUM_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})[-_./](\d{1,2})(?:[-_./](\d{2,4}))?(?![A-Za-z0-9])")
_MONTH_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"(?P<gap>\s*)(?P<n1>\d{1,4})?(?:[\s,]+(?P<n2>\d{2,4}))?",
    re.IGNORECASE)


def _norm_year(value: int) -> int:
    """Map a 2-digit year to 20xx; leave 4-digit years untouched."""
    return 2000 + value if value < 100 else value


@dataclass
class DocRef:
    """A single de-dup unit: a folder file, or a whole upload/chat source."""

    source_id: str
    source_type: str
    filename: str
    item_ids: list[str]
    content_hash: str | None
    embedding_sig: str | None
    recency: float          # mtime epoch (folder) or updated_at epoch (otherwise)
    resident_since: float   # earliest item created_at epoch (tiebreak)
    file_path: str | None = None  # set only for folder-file docs
    embedding: list[float] | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_id, self.file_path or "")


def normalize_filename(name: str) -> str:
    """Lowercase, drop the extension, strip copy/date modifiers, collapse separators.

    Dates are stripped (not placeheld) so the stem captures only the title; the
    date itself is compared separately by _dates_compatible.
    """
    stem = name.rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    stem = stem.lower()
    stem = _COPY_MODIFIER_RE.sub(" ", stem)
    stem = _DATE_TOKEN_RE.sub(" ", stem)
    stem = _SEP_RE.sub(" ", stem).strip()
    return stem


def _extract_dates(name: str) -> list[tuple[int | None, int, int | None]]:
    """Pull every date-like token from a filename as (year|None, month, day|None).

    Used ONLY to block a fuzzy filename match when two otherwise-similar names
    carry materially different dates; it can never create a new match.
    """
    stem = name.rsplit("/", 1)[-1]
    out: list[tuple[int | None, int, int | None]] = []

    for m in _ISO_DATE_RE.finditer(stem):
        iyear, imonth, iday = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= imonth <= 12 and 1 <= iday <= 31:
            out.append((iyear, imonth, iday))

    for m in _NUM_DATE_RE.finditer(stem):
        g1, g2 = int(m.group(1)), int(m.group(2))
        nyear: int | None = _norm_year(int(m.group(3))) if m.group(3) else None
        nmonth, nday = (g2, g1) if g1 > 12 and g2 <= 12 else (g1, g2)  # tolerate DD/MM
        if 1 <= nmonth <= 12 and 1 <= nday <= 31:
            out.append((nyear, nmonth, nday))

    for m in _MONTH_NAME_RE.finditer(stem):
        mmonth = _MONTH_MAP[m.group(1).lower()[:3]]
        n1, n2, gap = m.group("n1"), m.group("n2"), m.group("gap")
        myear: int | None = None
        mday: int | None = None
        if n1 and n2:                       # "Apr 14 2026" -> day + year
            mday, myear = int(n1), _norm_year(int(n2))
        elif n1 and len(n1) == 4:           # "Apr 2026" -> 4-digit year
            myear = int(n1)
        elif n1 and not gap:                # "Dec25"/"Apr26" -> glued 2-digit year
            myear = _norm_year(int(n1))
        elif n1:                            # "April 29" -> spaced day
            mday = int(n1)
        if mday is not None and not (1 <= mday <= 31):
            mday = None
        out.append((myear, mmonth, mday))

    return out


def _date_distance_days(d1: tuple[int | None, int, int | None],
                        d2: tuple[int | None, int, int | None]) -> int:
    """Day-distance between two extracted dates. Missing year => assume the other's
    (or a common year if both lack one); missing day => mid-month (15)."""
    y1, m1, dd1 = d1
    y2, m2, dd2 = d2
    ry1 = y1 if y1 is not None else y2
    ry2 = y2 if y2 is not None else y1
    if ry1 is None:
        ry1 = 2000
    if ry2 is None:
        ry2 = 2000
    day1 = min(dd1 or 15, 28)
    day2 = min(dd2 or 15, 28)
    return abs((date(ry1, m1, day1) - date(ry2, m2, day2)).days)


def _dates_compatible(a: str, b: str) -> bool:
    """False only when BOTH names carry dates and the closest pair is more than
    _DATE_MATCH_MAX_DAYS apart. If either name has no date, dates don't block."""
    da, db = _extract_dates(a), _extract_dates(b)
    if not da or not db:
        return True
    closest = min(_date_distance_days(x, y) for x in da for y in db)
    return closest <= _DATE_MATCH_MAX_DAYS


def filename_near_match(a: str, b: str) -> bool:
    """True if two filenames are "very close": their date-free stems are equal (or
    a high difflib ratio), AND -- when both carry dates -- those dates are within
    _DATE_MATCH_MAX_DAYS. The date gate stops distinct instances of a series that
    share an identical stem (e.g. "...Apr 2026" vs "...Dec25") from collapsing."""
    na, nb = normalize_filename(a), normalize_filename(b)
    if not na or not nb:
        return False
    if na == nb or SequenceMatcher(None, na, nb).ratio() >= _FILENAME_RATIO_FLOOR:
        return _dates_compatible(a, b)
    return False


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _doc_embedding(store, doc: DocRef) -> list[float] | None:
    """Mean-pool the stored chunk embeddings of a document into one vector."""
    if doc.embedding is not None:
        return doc.embedding
    if not doc.item_ids:
        return None
    placeholders = ",".join("?" * len(doc.item_ids))
    rows = store.db.execute(
        f"SELECT embedding FROM items WHERE id IN ({placeholders}) "  # noqa: S608
        "AND embedding IS NOT NULL",
        doc.item_ids).fetchall()
    vecs = [bytes_to_floats(r["embedding"]) for r in rows if r["embedding"]]
    vecs = [v for v in vecs if v]
    if not vecs:
        return None
    dim = len(vecs[0])
    if any(len(v) != dim for v in vecs):
        return None
    pooled = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    doc.embedding = pooled
    return pooled


def _build_doc(store, *, source_id, source_type, filename, item_ids, file_path,
               recency) -> DocRef | None:
    if not item_ids:
        return None
    placeholders = ",".join("?" * len(item_ids))
    rows = store.db.execute(
        f"SELECT content_hash, embedding_sig, created_at FROM items "  # noqa: S608
        f"WHERE id IN ({placeholders})",
        item_ids).fetchall()
    if not rows:
        return None
    content_hash = next((r["content_hash"] for r in rows if r["content_hash"]), None)
    embedding_sig = next((r["embedding_sig"] for r in rows if r["embedding_sig"]), None)
    resident_since = min((_iso_to_epoch(r["created_at"]) for r in rows), default=0.0)
    return DocRef(
        source_id=source_id, source_type=source_type, filename=filename,
        item_ids=list(item_ids), content_hash=content_hash, embedding_sig=embedding_sig,
        recency=recency or 0.0, resident_since=resident_since, file_path=file_path)


def enumerate_docs(store) -> list[DocRef]:
    """Build the de-dup unit list: one DocRef per folder file, one per non-folder source."""
    docs: list[DocRef] = []
    folder_source_ids: set[str] = set()
    folder_rows = store.db.execute(
        "SELECT ffs.source_id, ffs.file_path, ffs.item_ids, ffs.mtime, s.source_type "
        "FROM folder_file_state ffs JOIN sources s ON s.id = ffs.source_id "
        "WHERE ffs.status = 'done'").fetchall()
    for r in folder_rows:
        folder_source_ids.add(r["source_id"])
        item_ids = json.loads(r["item_ids"] or "[]")
        doc = _build_doc(
            store, source_id=r["source_id"], source_type=r["source_type"],
            filename=r["file_path"].rsplit("/", 1)[-1], item_ids=item_ids,
            file_path=r["file_path"], recency=r["mtime"])
        if doc:
            docs.append(doc)

    for s in store.db.execute(
            "SELECT id, name, source_type, updated_at FROM sources").fetchall():
        if s["id"] in folder_source_ids:
            continue  # its documents are enumerated per-file above
        if s["source_type"] in _AGGREGATE_SOURCE_TYPES:
            # Aggregate row bundling many artifacts — a single whole-source
            # DocRef here risks delete_source_cascade wiping them all.
            continue
        item_ids = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (s["id"],)).fetchall()]
        doc = _build_doc(
            store, source_id=s["id"], source_type=s["source_type"],
            filename=s["name"], item_ids=item_ids, file_path=None,
            recency=_iso_to_epoch(s["updated_at"]))
        if doc:
            docs.append(doc)
    return docs


def _is_persistent(source_type: str) -> bool:
    return source_type in PERSISTENT_SOURCE_TYPES


# Extraction-quality ranking, used ONLY to choose the survivor when a CROSS-FORMAT
# duplicate collapses (e.g. the same document ingested as both .docx and .pdf). A
# higher rank means cleaner extracted text and therefore better retrieval recall:
# raw text/markdown is verbatim, .docx preserves heading structure and paragraph
# boundaries, .html is decent, .pdf loses structure and carries page-header/footer/
# line-wrap chrome, and .pptx text is fragmentary. Extensions absent from the map get
# DEFAULT_FORMAT_RANK. This never changes WHICH pairs collapse -- only which copy of
# an already-matched pair survives.
FORMAT_RECALL_RANK = {
    "txt": 100, "text": 100, "md": 100, "markdown": 100,
    "docx": 80,
    "html": 70, "htm": 70,
    "pptx": 40,
    "pdf": 30,
}
DEFAULT_FORMAT_RANK = 60


def _file_ext(name: str) -> str:
    """Lowercased filename extension without the dot ('' if the name has none)."""
    stem = name.rsplit("/", 1)[-1]
    if "." not in stem:
        return ""
    return stem.rsplit(".", 1)[-1].lower()


def _format_rank(filename: str) -> int:
    return FORMAT_RECALL_RANK.get(_file_ext(filename), DEFAULT_FORMAT_RANK)


def pick_winner(a: DocRef, b: DocRef) -> tuple[DocRef, DocRef]:
    """Return (winner, loser): persistent > transient; then -- for a CROSS-FORMAT
    pair only -- the better-recall format (so a .docx beats a .pdf of the same
    content regardless of which was added more recently); then newest recency; then
    oldest-resident; then a stable key order as the final deterministic tiebreak.

    The format-rank step is gated on the two docs having different extensions, so
    same-format pairs fall straight through to the original recency ordering with an
    unchanged winner. It sits ABOVE recency because a collapsing cross-format pair is
    near-identical content (cosine >= the fuzzy floor AND a compatible filename date),
    so 'which format extracts cleaner' is a better signal than 'which file happened to
    land in the folder last'."""
    pa, pb = _is_persistent(a.source_type), _is_persistent(b.source_type)
    if pa != pb:
        return (a, b) if pa else (b, a)
    if _file_ext(a.filename) != _file_ext(b.filename):
        ra, rb = _format_rank(a.filename), _format_rank(b.filename)
        if ra != rb:
            return (a, b) if ra > rb else (b, a)
    if a.recency != b.recency:
        return (a, b) if a.recency > b.recency else (b, a)
    if a.resident_since != b.resident_since:
        return (a, b) if a.resident_since < b.resident_since else (b, a)
    return (a, b) if a.key <= b.key else (b, a)


def _match_reason(store, a: DocRef, b: DocRef, threshold: float) -> str | None:
    """Return "exact" / "fuzzy:<cosine>" if a and b are duplicate documents, else None."""
    if a.key == b.key:
        return None
    if a.content_hash and b.content_hash and a.content_hash == b.content_hash:
        return "exact"
    if not filename_near_match(a.filename, b.filename):
        return None
    if not a.embedding_sig or a.embedding_sig != b.embedding_sig:
        return None
    ea, eb = _doc_embedding(store, a), _doc_embedding(store, b)
    cos = _cosine(ea, eb)
    if cos >= threshold:
        return f"fuzzy:{cos:.4f}"
    return None


@dataclass
class DedupAction:
    winner: DocRef
    loser: DocRef
    reason: str


def find_duplicates(store, docs: list[DocRef], threshold: float) -> list[DedupAction]:
    """Greedy pairwise collapse. Each loser is removed from further consideration, and a
    document that has already won a collapse is protected from later becoming a loser --
    so a deleted document's designated survivor is never itself deleted, and content
    always survives in exactly one copy."""
    actions: list[DedupAction] = []
    removed: set[tuple[str, str]] = set()
    survivors: set[tuple[str, str]] = set()  # designated winners; must not be deleted
    for i in range(len(docs)):
        if docs[i].key in removed:
            continue
        for j in range(i + 1, len(docs)):
            if docs[j].key in removed:
                continue
            reason = _match_reason(store, docs[i], docs[j], threshold)
            if not reason:
                continue
            winner, loser = pick_winner(docs[i], docs[j])
            if loser.key in survivors:
                # Deleting this loser would orphan an earlier loser whose survivor it
                # is. Leave the pair uncollapsed (a stray duplicate is far better than
                # deleting every copy).
                continue
            actions.append(DedupAction(winner=winner, loser=loser, reason=reason))
            removed.add(loser.key)
            survivors.add(winner.key)
            if docs[i].key in removed:
                break  # docs[i] itself lost; stop pairing it
    return actions


def _delete_doc(store, doc: DocRef) -> None:
    """Hard-delete a losing document's KB entry. Never touches the file on disk."""
    if doc.source_type in _AGGREGATE_SOURCE_TYPES:
        # Belt-and-suspenders: never cascade-delete an aggregate source (it
        # bundles many artifacts). enumerate_docs already excludes these, but a
        # future caller building a DocRef directly must not be able to wipe the
        # whole Artifacts library because one artifact duplicated a folder file.
        logger.warning(
            "dedup: refusing to delete aggregate source %s (type=%s)",
            doc.source_id, doc.source_type)
        return
    if doc.file_path is None:
        # Whole-source document (upload / chat / manual): remove its source + items.
        store.delete_source_cascade(doc.source_id)
    else:
        # One file within a folder source: drop its items, but KEEP the
        # folder_file_state row marked 'deduped' (empty item_ids). The file is still
        # on disk, so deleting the row would make the next scan treat it as new and
        # re-ingest -> re-dedup it every cycle. The watcher skips 'deduped' files.
        store.delete_items_batch(doc.item_ids)
        store.db.execute(
            "UPDATE folder_file_state SET status = 'deduped', item_ids = '[]' "
            "WHERE source_id = ? AND file_path = ?",
            (doc.source_id, doc.file_path))
        store.db.commit()


def _action_dict(action: DedupAction) -> dict:
    return {
        "loser": action.loser.filename,
        "loser_source_id": action.loser.source_id,
        "loser_file_path": action.loser.file_path,
        "winner": action.winner.filename,
        "winner_source_id": action.winner.source_id,
        "reason": action.reason,
        "items_deleted": len(action.loser.item_ids),
    }


def _audit_collapse(action: DedupAction) -> None:
    """Emit the SEL audit event + info log for a single collapse."""
    sel().log_tool_invocation(
        session_key="knowledge-dedup", agent="knowledge-dedup",
        tool_name="knowledge.dedup.collapse", outcome="ok",
        resources=(
            f"loser_source_id={action.loser.source_id} "
            f"loser_file={action.loser.file_path or '(whole-source)'} "
            f"winner_source_id={action.winner.source_id} reason={action.reason} "
            f"items_deleted={len(action.loser.item_ids)}"),
    )
    logger.info(
        "KB dedup collapsed %r into %r (%s, %d items)",
        action.loser.filename, action.winner.filename, action.reason,
        len(action.loser.item_ids))


def dedup_sweep(store, *, threshold: float = DEFAULT_FUZZY_THRESHOLD,
                apply: bool = False) -> list[dict]:
    """Find (and optionally collapse) all cross-source duplicate documents.

    O(n^2) full-corpus pass intended for the one-time backfill (CLI/MCP) and the
    end-of-folder-scan sweep. For the per-ingest hot path use ``dedup_document``,
    which is O(n). Returns one result dict per matched pair describing the loser, the
    surviving winner, the match reason, and how many chunk rows would be (or were)
    deleted. ``apply=False`` is a dry-run preview that mutates nothing; ``apply=True``
    performs the hard deletes and emits a SEL audit event per collapse. Idempotent: a
    second run after an apply finds nothing.
    """
    docs = enumerate_docs(store)
    actions = find_duplicates(store, docs, threshold)
    results = [_action_dict(a) for a in actions]
    if apply:
        for action in actions:
            _delete_doc(store, action.loser)
            _audit_collapse(action)
    return results


def _build_doc_for(store, source_id: str, file_path: str | None) -> DocRef | None:
    """Build the DocRef for a single just-ingested document."""
    s = store.db.execute(
        "SELECT name, source_type, updated_at FROM sources WHERE id = ?",
        (source_id,)).fetchone()
    if not s:
        return None
    # Aggregate sources are not dedup units (see enumerate_docs): a whole-
    # aggregate DocRef keyed on the first item's hash misrepresents the many
    # artifacts inside. Without this, every artifact save produced phantom
    # DedupActions that _delete_doc silently refused — and if the aggregate
    # ever WON a pair, a legitimate upload was hard-deleted against a hash
    # representing one artifact out of many. Per-item file_path builds are
    # fine; it is only the whole-source build that lies about identity.
    if file_path is None and s["source_type"] in _AGGREGATE_SOURCE_TYPES:
        return None
    if file_path is not None:
        row = store.db.execute(
            "SELECT item_ids, mtime FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?", (source_id, file_path)).fetchone()
        if not row:
            return None
        return _build_doc(
            store, source_id=source_id, source_type=s["source_type"],
            filename=file_path.rsplit("/", 1)[-1],
            item_ids=json.loads(row["item_ids"] or "[]"),
            file_path=file_path, recency=row["mtime"])
    item_ids = [r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
    return _build_doc(
        store, source_id=source_id, source_type=s["source_type"], filename=s["name"],
        item_ids=item_ids, file_path=None, recency=_iso_to_epoch(s["updated_at"]))


def dedup_document(store, source_id: str, *, file_path: str | None = None,
                   threshold: float = DEFAULT_FUZZY_THRESHOLD,
                   apply: bool = False) -> list[dict]:
    """Dedup one just-ingested document against the existing corpus.

    O(n) per ingest (the one new document vs the rest) -- the targeted counterpart to
    the O(n^2) ``dedup_sweep`` used for the one-time backfill. A Tier-1 exact match
    keys off the indexed ``content_hash``; Tier-2 compares the new document's embedding
    against the others. Returns the collapse actions that involved this document.
    """
    new_doc = _build_doc_for(store, source_id, file_path)
    if new_doc is None:
        return []
    results: list[dict] = []
    new_doc_won = False  # once the new doc absorbs a duplicate it must not be deleted
    for other in enumerate_docs(store):
        if other.key == new_doc.key:
            continue
        reason = _match_reason(store, new_doc, other, threshold)
        if not reason:
            continue
        winner, loser = pick_winner(new_doc, other)
        if loser.key == new_doc.key and new_doc_won:
            # The new doc already absorbed an earlier duplicate; deleting it now would
            # orphan that copy. Leave this pair uncollapsed.
            continue
        action = DedupAction(winner=winner, loser=loser, reason=reason)
        results.append(_action_dict(action))
        if apply:
            _delete_doc(store, loser)
            _audit_collapse(action)
        if loser.key == new_doc.key:
            break  # the new document itself was collapsed; stop comparing
        new_doc_won = True
    return results
