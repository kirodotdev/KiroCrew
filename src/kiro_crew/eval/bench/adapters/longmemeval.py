"""LongMemEval → neutral corpus.

Shape, verified empirically against the cleaned ``longmemeval_oracle.json``
rather than against the upstream README (which is wrong on ``has_answer``, see
below): the file is a JSON **array** of 500 objects, each carrying its own
haystack and exactly one question. That one-question-per-instance shape is the
mirror image of LoCoMo's ~199-questions-per-instance, and it is why LongMemEval's
ingest cost per scored question is two orders of magnitude higher — every
question pays for its own store.

Three traps are handled here explicitly because each one silently produces a
*plausible* number if you get it wrong:

1. **``has_answer`` is always present.** The README says it appears only on
   evidence turns; in the cleaned file all 10 960 turns carry it as an explicit
   bool (896 true / 10 064 false). Presence therefore means nothing — only the
   value does. Read defensively with ``.get`` so an older or uncleaned dump still
   loads, but never infer evidence from the key existing.

2. **Abstention is not a ``question_type``.** The six types are
   ``temporal-reasoning`` (133), ``multi-session`` (133), ``knowledge-update``
   (78), ``single-session-user`` (70), ``single-session-assistant`` (56),
   ``single-session-preference`` (30). The 30 abstention items are marked by an
   ``_abs`` suffix on ``question_id`` and *also* carry one of those six types, so
   a reader that looks for an abstention category finds none and scores refusal
   questions as ordinary recall questions.

3. **Evidence sessions are named ``answer_…``.** That prefix is a label leak: any
   ranking, chunking or gold-resolution step that keys off it would score the
   dataset's naming convention instead of the memory layer. Gold comes from
   ``answer_session_ids`` and from ``has_answer`` — never from the id's spelling.
   Nothing in this module inspects the prefix.

``answer`` is not a string to match against. It has three unrelated families:
a factual answer for most types, a *prose rubric* for
``single-session-preference`` (the official judge feeds it under a ``Rubric:``
header), and an *explanation of why the question is unanswerable* for ``_abs``
items (fed under an ``Explanation:`` header). The adapter only carries it in
``gold_answer``; deciding what to do with it is the scorer's job, and anyone
tempted to write ``==`` against it should read this paragraph first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kiro_crew.eval.bench.corpus import (
    CAT_KNOWLEDGE_UPDATE,
    CAT_MULTI_HOP,
    CAT_PREFERENCE,
    CAT_SINGLE_HOP,
    CAT_TEMPORAL,
    CAT_UNKNOWN,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from kiro_crew.eval.bench.safepath import guard_read_path

DATASET_NAME = "longmemeval"

#: Marks abstention items. Not a ``question_type`` — see module docstring trap 2.
ABSTENTION_SUFFIX = "_abs"

# ``single-session-user`` and ``single-session-assistant`` both collapse to
# CAT_SINGLE_HOP: for cross-dataset reporting the interesting axis is how many
# sessions the answer spans, and both of these span exactly one. The distinction
# they do carry (whose utterance holds the evidence) survives in
# ``raw_category``, which is the field to group by when that matters — a report
# that only shows the normalized bucket has merged two upstream types, and would
# be merging types the official judge already prompts identically anyway.
_CATEGORY_BY_QUESTION_TYPE = {
    "temporal-reasoning": CAT_TEMPORAL,
    "multi-session": CAT_MULTI_HOP,
    "knowledge-update": CAT_KNOWLEDGE_UPDATE,
    "single-session-user": CAT_SINGLE_HOP,
    "single-session-assistant": CAT_SINGLE_HOP,
    "single-session-preference": CAT_PREFERENCE,
}

_NOTES = (
    "Synthesized turn ids: LongMemEval has no turn identifier, so turn_id is "
    "'<haystack_session_id>#<0-based index within that session>'.",
    "Timestamps are the raw dataset strings ('2023/04/10 (Mon) 23:07'), "
    "unparsed — both BenchSession.timestamp and every BenchTurn.timestamp in a "
    "session carry that session's haystack_dates entry, because the dataset "
    "dates sessions and not turns.",
    "unanswerable is derived from the '_abs' suffix on question_id, not from "
    "question_type, which never names abstention.",
    "gold_turn_ids come from has_answer == True; gold_session_ids come from "
    "answer_session_ids. The 'answer_' prefix on evidence session ids is never "
    "read — it is a label leak.",
    "gold_answer is a factual answer, a prose rubric (single-session-preference) "
    "or an unanswerability explanation (_abs items). Not comparable as a string.",
)


class LongMemEvalSchemaError(ValueError):
    """Raised when the input contradicts the documented LongMemEval schema.

    A dedicated type rather than a bare ``ValueError`` so a caller sweeping a
    directory of dumps can tell "this file is not LongMemEval" apart from a bug
    in the corpus contract, and so the message can name the offending instance —
    a stack trace pointing at ``zip`` is useless against a 500-instance file.
    """


def _as_list(value: object, *, where: str) -> list[Any]:
    if value is None:
        raise LongMemEvalSchemaError(f"{where}: missing (expected a JSON array)")
    if not isinstance(value, list):
        raise LongMemEvalSchemaError(
            f"{where}: expected a JSON array, got {type(value).__name__}"
        )
    return value


def _as_str(value: object, *, where: str) -> str:
    if value is None:
        raise LongMemEvalSchemaError(f"{where}: missing (expected a string)")
    if not isinstance(value, str):
        raise LongMemEvalSchemaError(
            f"{where}: expected a string, got {type(value).__name__}"
        )
    return value


def _coerce_answer(value: object, *, where: str) -> str | None:
    """Normalize the ``answer`` field, which is NOT always a string in real data.

    32 of the 500 instances in ``longmemeval_oracle.json`` carry an ``int`` here —
    they are counting questions ("how many …") whose gold answer was serialized as
    a bare number. Aborting a 500-instance load on that would make the corpus
    unusable, and the value is semantically a string answer either way, so scalars
    are coerced.

    A ``dict`` or ``list`` is still refused: that would mean the file's shape
    genuinely changed, and silently stringifying a container would feed the judge
    a Python repr as if it were the gold answer.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Before the int check: bool is a subclass of int, and "True" is not an
        # answer any upstream prompt would produce.
        raise LongMemEvalSchemaError(f"{where}: unexpected bool answer")
    if isinstance(value, (int, float)):
        return str(value)
    raise LongMemEvalSchemaError(
        f"{where}: expected a string or a number, got {type(value).__name__}"
    )


def _as_str_list(value: object, *, where: str) -> list[str]:
    return [
        _as_str(item, where=f"{where}[{i}]") for i, item in enumerate(_as_list(value, where=where))
    ]


def _as_dict(value: object, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LongMemEvalSchemaError(
            f"{where}: expected a JSON object, got {type(value).__name__}"
        )
    return value


def normalize_question_type(question_type: str) -> str:
    """Map a dataset ``question_type`` onto the corpus category vocabulary.

    Unknown types return :data:`CAT_UNKNOWN` instead of raising: a future
    LongMemEval revision adding a seventh type should degrade to an honest
    "unknown" bucket in the report, not abort a run that is otherwise valid.
    """
    return _CATEGORY_BY_QUESTION_TYPE.get(question_type, CAT_UNKNOWN)


def _build_session(
    raw_turns: object,
    *,
    session_id: str,
    timestamp: str,
    where: str,
) -> tuple[BenchSession, list[str]]:
    """Return one session plus the ids of its evidence turns."""
    turns: list[BenchTurn] = []
    evidence: list[str] = []
    for index, raw_turn in enumerate(_as_list(raw_turns, where=where)):
        turn = _as_dict(raw_turn, where=f"{where}[{index}]")
        # ``has_answer`` is present on every turn of the cleaned file, so its
        # VALUE is the ground truth and its presence is not. Default False keeps
        # older dumps loadable.
        is_evidence = bool(turn.get("has_answer", False))
        turn_id = f"{session_id}#{index}"
        turns.append(
            BenchTurn(
                turn_id=turn_id,
                session_id=session_id,
                speaker=_as_str(turn.get("role"), where=f"{where}[{index}].role"),
                text=_as_str(turn.get("content"), where=f"{where}[{index}].content"),
                timestamp=timestamp,
                is_evidence=is_evidence,
            )
        )
        if is_evidence:
            evidence.append(turn_id)
    return BenchSession(session_id=session_id, turns=tuple(turns), timestamp=timestamp), evidence


def _build_instance(entry: object, *, position: int) -> BenchInstance:
    where = f"instance[{position}]"
    obj = _as_dict(entry, where=where)
    question_id = _as_str(obj.get("question_id"), where=f"{where}.question_id")
    where = f"instance[{position}] ({question_id})"

    dates = _as_str_list(obj.get("haystack_dates", []), where=f"{where}.haystack_dates")
    session_ids = _as_str_list(
        obj.get("haystack_session_ids", []), where=f"{where}.haystack_session_ids"
    )
    raw_sessions = _as_list(obj.get("haystack_sessions", []), where=f"{where}.haystack_sessions")
    # These three are documented as parallel and are parallel in 500/500 oracle
    # instances, but zip() truncates to the shortest without a word, which would
    # drop haystack sessions — and therefore gold evidence — and report the loss
    # as a retrieval miss. Check instead of trusting.
    if not len(dates) == len(session_ids) == len(raw_sessions):
        raise LongMemEvalSchemaError(
            f"{where}: haystack arrays must be parallel but disagree in length — "
            f"haystack_dates={len(dates)}, haystack_session_ids={len(session_ids)}, "
            f"haystack_sessions={len(raw_sessions)}"
        )

    sessions: list[BenchSession] = []
    gold_turn_ids: list[str] = []
    seen_session_ids: set[str] = set()
    for index, (session_id, timestamp) in enumerate(zip(session_ids, dates)):
        if session_id in seen_session_ids:
            # Turn ids are synthesized from the session id, so a repeat inside
            # one haystack collides them and makes a gold turn ref ambiguous
            # between two sessions. Session ids repeat freely ACROSS instances
            # (each instance is its own store, so that is harmless); within one
            # instance uniqueness is load-bearing, so refuse rather than
            # invent a disambiguator the gold refs know nothing about.
            raise LongMemEvalSchemaError(
                f"{where}: haystack_session_ids repeats {session_id!r} at position {index}; "
                "turn ids are synthesized as '<session_id>#<index>' and would collide"
            )
        seen_session_ids.add(session_id)
        session, evidence = _build_session(
            raw_sessions[index],
            session_id=session_id,
            timestamp=timestamp,
            where=f"{where}.haystack_sessions[{index}]",
        )
        sessions.append(session)
        gold_turn_ids.extend(evidence)

    question_type = _as_str(obj.get("question_type", ""), where=f"{where}.question_type")
    answer = _coerce_answer(obj.get("answer"), where=f"{where}.answer")

    query = BenchQuery(
        query_id=question_id,
        question=_as_str(obj.get("question", ""), where=f"{where}.question"),
        category=normalize_question_type(question_type),
        gold_answer=answer,
        gold_session_ids=tuple(
            _as_str_list(obj.get("answer_session_ids", []), where=f"{where}.answer_session_ids")
        ),
        gold_turn_ids=tuple(gold_turn_ids),
        unanswerable=question_id.endswith(ABSTENTION_SUFFIX),
        ask_date=_as_str(obj.get("question_date", ""), where=f"{where}.question_date"),
        raw_category=question_type,
    )
    # One instance, exactly one query — the whole haystack exists to answer it.
    instance = BenchInstance(
        instance_id=question_id, sessions=tuple(sessions), queries=(query,)
    )
    return instance.resolve_gold()


def load_longmemeval(raw: object, *, variant: str, source_path: str = "") -> Corpus:
    """Normalize already-parsed LongMemEval JSON into a :class:`Corpus`.

    Takes the parsed object rather than a path so the mapping is testable
    without a 15 MB (oracle) or 277 MB (``s``) download; :func:`load_longmemeval_file`
    is the thin file-reading wrapper.

    ``variant`` is the caller's label for which file this is — ``"oracle"`` or
    ``"s_cleaned"``. It is recorded verbatim in the corpus and reaches the report
    through the fingerprint, because the two variants differ by ~20x in haystack
    size and the oracle one cannot measure retrieval at all
    (see :func:`is_evidence_only`).
    """
    entries = _as_list(raw, where="top level")
    instances = tuple(
        _build_instance(entry, position=position) for position, entry in enumerate(entries)
    )
    return Corpus(
        name=DATASET_NAME,
        variant=variant,
        instances=instances,
        source_path=source_path,
        notes=_NOTES,
    )


def load_longmemeval_file(path: str | Path, *, variant: str) -> Corpus:
    """Read *path* and normalize it. ``source_path`` is recorded on the corpus."""
    file_path = Path(path)
    file_path = guard_read_path(file_path, what="corpus file")
    with file_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return load_longmemeval(raw, variant=variant, source_path=str(file_path))


def is_evidence_only(corpus: Corpus) -> bool:
    """True when no instance in *corpus* contains a single distractor session.

    This is the programmatic form of the oracle-variant trap. In
    ``longmemeval_oracle.json`` the gold session set equals the entire haystack
    for 500/500 instances: every session present is an evidence session, so any
    retriever that returns anything at all scores perfect session recall. A
    recall number measured against such a corpus describes the file, not the
    memory layer, so the retrieval ruler calls this and refuses the run.

    The check is deliberately whole-corpus and conjunctive — one instance with a
    distractor makes the corpus worth running, so False is the permissive answer.
    An empty corpus returns True (vacuously): there is no instance offering a
    distractor, and a run over nothing should be refused for the same reason.
    """
    for instance in corpus.instances:
        haystack = {session.session_id for session in instance.sessions}
        gold = {sid for query in instance.queries for sid in query.gold_session_ids}
        if gold != haystack:
            return False
    return True
