"""LoCoMo → the neutral corpus contract.

Why this file is longer than "read JSON, build dataclasses": LoCoMo's on-disk
shape disagrees with its own paper and with every intuition about how a
multi-session dialogue dataset would be laid out, and three of those
disagreements silently corrupt the measurement rather than raising.

* ``conversation`` is a FLAT DICT. Sessions are string-keyed siblings
  (``session_1``, ``session_1_date_time``, ``session_2``, …) of the two speaker
  names, not a list. So session discovery is key-pattern matching, and ordering
  has to be numeric — a lexical sort puts ``session_10`` before ``session_2``
  and scrambles the only temporal signal the haystack has.
* ``conv-26`` carries ``session_20_date_time`` … ``session_35_date_time`` with no
  matching ``session_N`` list. Deriving the session list from the timestamp keys
  yields 16 phantom empty sessions on that one conversation, which inflates
  session-level denominators for a reason that has nothing to do with the
  memory layer. Discovery therefore keys off ``session_N`` presence and looks the
  date up with ``.get()``.
* ``answer`` is ABSENT on the 444 adversarial items (category 5), so
  ``q['answer']`` raises on 22% of the corpus. Two items carry both ``answer``
  and ``adversarial_answer``; both are kept.

Session membership is derived from *which session list a turn appeared in*, never
from parsing the ``dia_id``. The id happens to look like ``D<session>:<turn>``,
but the file is the authority on membership and the string is treated as opaque —
the same discipline ``corpus.BenchTurn`` documents for LongMemEval's
``answer_``-prefixed session ids, and for the same reason: structure parsed out
of an id is structure that can be wrong without anyone noticing.

Dangling evidence refs (7 of 2 815 name a ``dia_id`` present in no conversation)
are passed straight through and dropped by ``BenchInstance.resolve_gold()``,
which this loader calls before returning. Filtering them here would hide from the
report the fact that the dataset shipped them; ``resolve_gold`` is the one place
that owns "gold that names nothing in this haystack".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..corpus import (
    CAT_ADVERSARIAL,
    CAT_COMMONSENSE,
    CAT_MULTI_HOP,
    CAT_SINGLE_HOP,
    CAT_TEMPORAL,
    CAT_UNKNOWN,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from ..safepath import guard_read_path

# Only a key of exactly this shape carries turns. Anchored on both ends so
# `session_20_date_time` cannot match — that orphan-key family is trap #1.
_SESSION_KEY = re.compile(r"^session_(\d+)$")

# LoCoMo's `category` is a bare int with no legend in the data.
#   5 = adversarial  — confirmed in upstream hf_llm_utils.py:251
#   2 = temporal     — confirmed in upstream hf_llm_utils.py:251
#   1 = multi-hop    — confirmed in upstream evaluation.py:213 (comment)
#   4 = single-hop   } INFERRED, NOT verified upstream. The basis is the count
#   3 = commonsense  } profile (4→841, 3→96) read against the display order
#                      keys=[4,1,2,3,5] in upstream evaluation_stats.py:98: the
#                      largest bucket leading a list that otherwise runs
#                      easiest→hardest reads as single-hop, and the 96-item tail
#                      as commonsense. Treat the 3-vs-4 assignment as a
#                      hypothesis; it affects per-category reporting only, never
#                      retrieval scoring.
_CATEGORY_BY_INT = {
    1: CAT_MULTI_HOP,
    2: CAT_TEMPORAL,
    3: CAT_COMMONSENSE,
    4: CAT_SINGLE_HOP,
    5: CAT_ADVERSARIAL,
}

# Adversarial items are the ones whose correct behavior is refusal.
_ADVERSARIAL_CATEGORY = 5

#: Prefix that marks image-derived text inside a turn. Deliberately a visible
#: marker rather than a bare concatenation so a transcript read by a human (or
#: an LLM judge) can tell which words the speaker actually said.
IMAGE_MARKER = "[image]"

_NOTE_CATEGORY_INFERENCE = (
    "category 3=commonsense / 4=single_hop is INFERRED from count profile and "
    "upstream display order, not confirmed in upstream code"
)
_NOTE_ORPHAN_DATES = (
    "sessions discovered by session_N presence, not by *_date_time keys: conv-26 "
    "ships session_20_date_time..session_35_date_time with no matching turn list"
)
_NOTE_MEMBERSHIP = (
    "session membership derived from the containing session list, never parsed "
    "out of the dia_id string"
)
_NOTE_DANGLING = (
    "dangling evidence dia_ids are passed through and dropped by "
    "BenchInstance.resolve_gold(), leaving those queries scorable_retrieval=False"
)
_NOTE_NO_ASK_DATE = "LoCoMo has no per-question ask date; BenchQuery.ask_date is left empty"


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def _as_str(value: object) -> str:
    """Coerce to text without inventing content: non-strings become empty.

    ``str(value)`` would be worse than useless here — it would push ``"None"``
    or a repr of a list into a turn's text and into the corpus fingerprint.
    """
    return value if isinstance(value, str) else ""


def _opt_str(value: object) -> str | None:
    """``None`` for a missing OR non-string value, never a coerced repr.

    ``corpus.BenchQuery`` is explicit that ``gold_answer`` may be ``None`` and
    must never be treated as a string to match against; this keeps the ``None``
    honest instead of turning an absent key into the text ``"None"``.
    """
    return value if isinstance(value, str) else None


def _turn_text(turn: dict[str, object], *, include_blip_captions: bool) -> str:
    """Assemble the text a memory store will actually ingest.

    Image turns carry a BLIP caption (1 226 turns do, 27 of them with no
    ``img_url`` at all), and dropping it makes the image's content invisible to
    a text memory system — questions whose evidence is a photo become
    unanswerable for a reason the harness introduced rather than measured. So the
    caption is folded in by default and the behavior is an explicit knob:
    ingestion granularity and content are among the dominant score drivers in
    memory benchmarks, so this must be a recorded parameter, never a hidden
    default. It is part of the corpus fingerprint by construction, because the
    fingerprint hashes turn text.

    ``img_url`` itself is deliberately NOT included: a URL is not content a text
    memory can reason over, and it would add per-run noise to the fingerprint.
    """
    text = _as_str(turn.get("text"))
    if not include_blip_captions:
        return text
    # `blip_caption` can appear without `img_url`, so key off the caption alone.
    caption = _as_str(turn.get("blip_caption")).strip()
    if not caption:
        return text
    marked = f"{IMAGE_MARKER} {caption}"
    return f"{text}\n{marked}" if text else marked


def _category(raw_value: object) -> tuple[str, str]:
    """Return ``(normalized_category, raw_category_as_string)``.

    An unexpected value maps to ``CAT_UNKNOWN`` rather than raising: a new
    category appearing upstream should degrade one report bucket, not make the
    whole corpus unloadable.
    """
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        return CAT_UNKNOWN, _as_str(raw_value)
    return _CATEGORY_BY_INT.get(raw_value, CAT_UNKNOWN), str(raw_value)


def _sessions_for(
    conversation: dict[str, object],
    *,
    sample_id: str,
    evidence_ids: frozenset[str],
    include_blip_captions: bool,
) -> tuple[tuple[BenchSession, ...], dict[str, str], list[str]]:
    """Build one conversation's sessions plus a ``dia_id → session_id`` map.

    The map is the authoritative membership record every gold session set is
    resolved through, which is why it is returned rather than recomputed.
    """
    notes: list[str] = []
    numbered: list[tuple[int, str]] = []
    for key, value in conversation.items():
        match = _SESSION_KEY.match(key)
        if match is not None and isinstance(value, list):
            numbered.append((int(match.group(1)), key))
    # Numeric sort: `session_10` must land after `session_9`.
    numbered.sort(key=lambda pair: pair[0])

    sessions: list[BenchSession] = []
    turn_session: dict[str, str] = {}
    for number, key in numbered:
        session_id = f"{sample_id}#session_{number}"
        # Orphan-tolerant: conv-26 has date keys with no list, and any session
        # list may equally have no date key.
        timestamp = _as_str(conversation.get(f"{key}_date_time"))
        turns: list[BenchTurn] = []
        for index, entry in enumerate(_as_list(conversation.get(key)) or []):
            turn = _as_dict(entry)
            if turn is None:
                notes.append(f"{sample_id}: skipped a non-dict turn in {key}")
                continue
            turn_id = _as_str(turn.get("dia_id")).strip()
            if not turn_id:
                # Synthesized ids are namespaced so they can never collide with
                # a real dia_id and be matched by an evidence ref by accident.
                turn_id = f"_synth:{session_id}:{index}"
                notes.append(f"{sample_id}: synthesized a turn id for a turn with no dia_id")
            if turn_id in turn_session:
                notes.append(f"{sample_id}: duplicate dia_id {turn_id}; first occurrence wins")
            else:
                turn_session[turn_id] = session_id
            turns.append(
                BenchTurn(
                    turn_id=turn_id,
                    session_id=session_id,
                    # A NAME ("Caroline"), not a role. corpus.BenchTurn types
                    # speaker as a free string exactly for this.
                    speaker=_as_str(turn.get("speaker")),
                    text=_turn_text(turn, include_blip_captions=include_blip_captions),
                    timestamp=timestamp,
                    is_evidence=turn_id in evidence_ids,
                )
            )
        sessions.append(
            BenchSession(session_id=session_id, turns=tuple(turns), timestamp=timestamp)
        )
    return tuple(sessions), turn_session, notes


def _queries_for(
    qa_items: list[dict[str, object]],
    *,
    sample_id: str,
    turn_session: dict[str, str],
) -> tuple[BenchQuery, ...]:
    queries: list[BenchQuery] = []
    for index, item in enumerate(qa_items):
        category, raw_category = _category(item.get("category"))
        evidence = tuple(
            ref for ref in (_as_str(e) for e in (_as_list(item.get("evidence")) or [])) if ref
        )
        gold_sessions: list[str] = []
        for ref in evidence:
            session_id = turn_session.get(ref)
            if session_id is not None and session_id not in gold_sessions:
                gold_sessions.append(session_id)
        queries.append(
            BenchQuery(
                # The dataset ships no QA id of its own, so one is synthesized
                # from position — stable across loads of the same bytes, and
                # unique corpus-wide because sample_id is.
                query_id=f"{sample_id}#q{index}",
                question=_as_str(item.get("question")),
                category=category,
                # .get(), never [...]: `answer` is absent on adversarial items.
                gold_answer=_opt_str(item.get("answer")),
                adversarial_answer=_opt_str(item.get("adversarial_answer")),
                gold_session_ids=tuple(gold_sessions),
                gold_turn_ids=evidence,
                # Refusal is the correct behavior for exactly category 5.
                unanswerable=raw_category == str(_ADVERSARIAL_CATEGORY),
                ask_date="",
                raw_category=raw_category,
            )
        )
    return tuple(queries)


def load_locomo(
    raw: object,
    *,
    source_path: str = "",
    include_blip_captions: bool = True,
) -> Corpus:
    """Normalize already-parsed ``locomo10.json`` into a :class:`Corpus`.

    Takes parsed JSON rather than a path so every trap in this module is
    testable from a small inline fixture, with no file and no network.

    One instance per conversation, which is also one memory store: LoCoMo's 10
    conversations each carry ~199 questions against one shared haystack, and
    merging them would put unrelated speakers' facts in each other's way.

    ``include_blip_captions`` controls whether image captions are folded into
    turn text — see :func:`_turn_text` for why that is a knob and not a default.
    """
    conversations = _as_list(raw)
    if conversations is None:
        raise ValueError(
            "LoCoMo's top level is a JSON array of conversations; got "
            f"{type(raw).__name__}. Pass the parsed file, not a wrapper object."
        )

    notes: list[str] = [
        _NOTE_ORPHAN_DATES,
        _NOTE_MEMBERSHIP,
        _NOTE_CATEGORY_INFERENCE,
        _NOTE_DANGLING,
        _NOTE_NO_ASK_DATE,
        f"blip_caption folded into turn text: {include_blip_captions} "
        f"(marker {IMAGE_MARKER!r}); img_url never included",
    ]

    instances: list[BenchInstance] = []
    for conv_index, conv_raw in enumerate(conversations):
        conv = _as_dict(conv_raw)
        if conv is None:
            notes.append(f"skipped non-dict conversation at index {conv_index}")
            continue
        sample_id = _as_str(conv.get("sample_id")).strip() or f"conv-{conv_index}"
        conversation = _as_dict(conv.get("conversation")) or {}
        qa_items = [d for d in (_as_list(conv.get("qa")) or []) if isinstance(d, dict)]

        # Turn-level ground truth is the union of every question's evidence for
        # THIS conversation. corpus.BenchTurn is explicit that is_evidence is a
        # diagnostic, not the thing scoring joins on.
        evidence_ids = frozenset(
            ref
            for item in qa_items
            for ref in (_as_str(e) for e in (_as_list(item.get("evidence")) or []))
            if ref
        )

        sessions, turn_session, session_notes = _sessions_for(
            conversation,
            sample_id=sample_id,
            evidence_ids=evidence_ids,
            include_blip_captions=include_blip_captions,
        )
        notes.extend(session_notes)
        queries = _queries_for(qa_items, sample_id=sample_id, turn_session=turn_session)
        # resolve_gold() is what actually drops the dangling refs.
        instances.append(BenchInstance(sample_id, sessions, queries).resolve_gold())

    # dict.fromkeys, not set(): notes are reported, so their order must be
    # stable across runs.
    return Corpus(
        name="locomo",
        variant="locomo10",
        instances=tuple(instances),
        source_path=source_path,
        notes=tuple(dict.fromkeys(notes)),
    )


def load_locomo_file(path: str | Path, *, include_blip_captions: bool = True) -> Corpus:
    """Read ``locomo10.json`` from disk and delegate to :func:`load_locomo`.

    Records the path in ``Corpus.source_path`` so a report can say which bytes
    it read alongside the fingerprint of what they normalized into.
    """
    file_path = Path(path)
    file_path = guard_read_path(file_path, what="corpus file")
    with file_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return load_locomo(
        raw,
        source_path=str(file_path),
        include_blip_captions=include_blip_captions,
    )
