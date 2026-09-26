"""``options.rank`` -- SHADOW scoring of the ``[OPTIONS:]`` chips a reply offers.

When an owner-dashboard reply that ends in an options marker finalizes, this point
asks Jev the registry's questions about the chips in ONE ``decide`` request, combines
the answers into a recommendation, and writes an outcome row. The owner's next
message is then recorded as the label for that row. Nothing the owner or the agent
sees changes: the reply, its chips and their order stay exactly as the agent wrote
them, and nothing reads the recommendation back.

Authorized by the keystone's ``options_text`` scope (``consent.consented_options_text``).
An install that has not granted it refuses in :func:`rank_options`'s first step,
before any request, row or file read beyond the keystone.

The question registry
    Three built-ins (:data:`BUILTINS`) plus owner-authored definitions, one JSON
    object per ``*.json`` file in ``config.loader.decisions_questions_dir()``. The
    directory sits under the decision-log directory and inherits its seals: the OS
    sandbox mounts it read-only and the file-edit gate write-protects it. Kiro Crew
    never writes it. A file that fails :func:`parse_question` is skipped with a
    warning, a file naming a built-in's id replaces that built-in, and the total is
    capped at :data:`MAX_QUESTIONS`.

Inputs
    A question names what the request may carry, from :data:`INPUTS`. The state sent
    is the chip labels plus the union of the active questions' inputs; a question
    whose input is empty (no ledger goal) is skipped rather than asked blind.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.constants import OPTIONS_RE_LINE
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import as_text
from kiro_crew.decisions.types import Answer, Answers, Choice, Question

logger = logging.getLogger(__name__)

POINT = "options.rank"

#: The reserved "none of these" option. A chip label can never contain ``|``,
#: because ``|`` separates labels in the marker, so no label can equal this id.
NONE_ID = "|none of these|"

#: Per-chip answer domain.
YES, NO = "yes", "no"

SHAPE_MENU = "menu_choice"
SHAPE_PER_CHIP = "per_chip_yes_no"
SHAPES = (SHAPE_MENU, SHAPE_PER_CHIP)

ROLE_SCORE, ROLE_FLAG, ROLE_VETO = "score", "flag", "veto"
ROLES = (ROLE_SCORE, ROLE_FLAG, ROLE_VETO)

HIGHER, LOWER = "higher_is_better", "lower_is_better"
DIRECTIONS = (HIGHER, LOWER)

#: What a question may name as request content. Every entry rides the
#: ``options_text`` scope. ``preferences`` is not offered: it needs the
#: ``memory_text`` scope as well and is not part of this point.
INPUTS = ("reply_text", "past_picks", "goal")

ID_RE = re.compile(r"\A[a-z0-9_]{1,40}\Z")
MAX_PROMPT_CHARS = 300
MAX_STRIP_LABEL_CHARS = 40
MAX_WEIGHT = 10.0
#: Definitions asked per reply, built-ins included: every question shares one
#: request and one provider timeout.
MAX_QUESTIONS = 6
#: Chips scored per reply. A marker with more is left unscored.
MAX_LABELS = 8
MAX_LABEL_CHARS = 120
MAX_REPLY_CHARS = 2000
MAX_GOAL_CHARS = 300
MAX_PAST_PICKS = 5
#: Registry files larger than this are not read.
MAX_FILE_BYTES = 16 * 1024

#: Pending-label bounds: sessions kept, and how long an unanswered record waits.
MAX_PENDING = 256
PENDING_TTL_SECS = 24 * 3600.0

#: The run's outer budget, clamped around the provider timeout.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0
#: How long a row write may hold the run.
LOG_BUDGET_SECS = 0.05

#: ``kind`` on the row that records the owner's pick.
KIND_PICK = "option_pick"

_FIELDS = frozenset(
    {
        "id",
        "version",
        "prompt",
        "shape",
        "none_option",
        "inputs",
        "role",
        "weight",
        "direction",
        "threshold",
        "strip_label",
    }
)


class QuestionError(ValueError):
    """A question definition that does not validate."""


@dataclass(frozen=True)
class QuestionDef:
    """One validated registry entry. Build through :func:`parse_question`."""

    id: str
    version: int
    prompt: str
    shape: str
    role: str
    inputs: tuple[str, ...]
    none_option: bool = False
    weight: float = 1.0
    direction: str = HIGHER
    threshold: float = 0.5
    strip_label: str = ""

    @property
    def key(self) -> str:
        """``id@version``: shadow statistics never mix across an edit."""
        return f"{self.id}@{self.version}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_question(raw: object) -> QuestionDef:
    """*raw* as a :class:`QuestionDef`, or raise :class:`QuestionError`.

    Strict by design: an unknown key, a wrong type or a field that does not belong to
    the question's role is an error rather than a silent default, because a typo in an
    owner's file must not turn into a different question than the one they wrote.
    """
    if not isinstance(raw, dict):
        raise QuestionError("a question is a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise QuestionError(f"unknown field(s): {', '.join(sorted(map(str, unknown)))}")
    qid = raw.get("id")
    if not isinstance(qid, str) or not ID_RE.fullmatch(qid):
        raise QuestionError("id must match [a-z0-9_]{1,40}")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise QuestionError("version must be a whole number of at least 1")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise QuestionError("prompt must be non-empty text")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise QuestionError(f"prompt is longer than {MAX_PROMPT_CHARS} characters")
    shape = raw.get("shape")
    if shape not in SHAPES:
        raise QuestionError(f"shape must be one of {', '.join(SHAPES)}")
    none_option = raw.get("none_option", False)
    if not isinstance(none_option, bool):
        raise QuestionError("none_option must be true or false")
    if none_option and shape != SHAPE_MENU:
        raise QuestionError("none_option applies to menu_choice only")
    inputs = raw.get("inputs")
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        raise QuestionError("inputs must be a list of names")
    if len(set(inputs)) != len(inputs):
        raise QuestionError("inputs must not repeat")
    if "preferences" in inputs:
        raise QuestionError("the preferences input is not available to this point")
    outside = [i for i in inputs if i not in INPUTS]
    if outside:
        raise QuestionError(f"inputs outside the allowlist: {', '.join(outside)}")
    role = raw.get("role")
    if role not in ROLES:
        raise QuestionError(f"role must be one of {', '.join(ROLES)}")
    weight, direction, threshold = 1.0, HIGHER, 0.5
    if role == ROLE_SCORE:
        if "threshold" in raw:
            raise QuestionError("threshold applies to flag and veto questions only")
        if "weight" in raw:
            number = _number(raw["weight"])
            if number is None or not 0.0 < number <= MAX_WEIGHT:
                raise QuestionError(f"weight must be a number in (0, {MAX_WEIGHT:g}]")
            weight = number
        direction = raw.get("direction", HIGHER)
        if direction not in DIRECTIONS:
            raise QuestionError(f"direction must be one of {', '.join(DIRECTIONS)}")
    else:
        if "weight" in raw or "direction" in raw:
            raise QuestionError("weight and direction apply to score questions only")
        if "threshold" in raw:
            number = _number(raw["threshold"])
            if number is None or not 0.0 < number <= 1.0:
                raise QuestionError("threshold must be a number in (0, 1]")
            threshold = number
    strip_label = raw.get("strip_label", "")
    if not isinstance(strip_label, str) or len(strip_label) > MAX_STRIP_LABEL_CHARS:
        raise QuestionError(f"strip_label must be text of at most {MAX_STRIP_LABEL_CHARS}")
    return QuestionDef(
        id=qid,
        version=version,
        prompt=prompt,
        shape=shape,
        role=role,
        inputs=tuple(inputs),
        none_option=none_option,
        weight=weight,
        direction=direction,
        threshold=threshold,
        strip_label=strip_label,
    )


#: The starter set, validated by the same parser as an owner's file.
BUILTINS: tuple[QuestionDef, ...] = tuple(
    parse_question(raw)
    for raw in (
        {
            "id": "owner_pick",
            "version": 1,
            "prompt": "Which of these options will the owner pick next?",
            "shape": SHAPE_MENU,
            "inputs": ["reply_text", "past_picks"],
            "role": ROLE_SCORE,
            "strip_label": "you usually pick",
        },
        {
            "id": "goal_progress",
            "version": 1,
            "prompt": "Which of these options most advances the session's goal?",
            "shape": SHAPE_MENU,
            "none_option": True,
            "inputs": ["reply_text", "goal"],
            "role": ROLE_SCORE,
            "strip_label": "advances goal",
        },
        {
            "id": "risk",
            "version": 1,
            "prompt": "Which of these options is irreversible or touches shared systems?",
            "shape": SHAPE_MENU,
            "none_option": True,
            "inputs": ["reply_text"],
            "role": ROLE_FLAG,
            "threshold": 0.5,
            "strip_label": "risky",
        },
    )
)


def _registry_files(directory: Path) -> list[Path]:
    """Regular ``*.json`` files in *directory*, by name. Links are not followed."""
    try:
        entries = sorted(
            (e for e in directory.iterdir() if e.name.endswith(".json")), key=lambda e: e.name
        )
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("options.rank: question registry unreadable (%s)", type(exc).__name__)
        return []
    files = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                logger.warning("options.rank: skipping %s: not a regular file", entry.name)
                continue
        except OSError:
            continue
        files.append(entry)
    return files


def load_registry(directory: Path | None = None) -> tuple[QuestionDef, ...]:
    """The built-ins plus every valid registry file, capped at :data:`MAX_QUESTIONS`.

    Read-only. A file that is too large, is not JSON or fails :func:`parse_question`
    is skipped with a warning naming the file and the reason. Never raises.
    """
    if directory is None:
        from kiro_crew.config import loader as config_loader

        directory = config_loader.decisions_questions_dir()
    by_id: dict[str, QuestionDef] = {q.id: q for q in BUILTINS}
    for path in _registry_files(directory):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise QuestionError(f"larger than {MAX_FILE_BYTES} bytes")
            question = parse_question(json.loads(path.read_text(encoding="utf-8")))
        except (QuestionError, ValueError, OSError) as exc:
            logger.warning("options.rank: skipping question file %s: %s", path.name, exc)
            continue
        by_id[question.id] = question
    questions = tuple(by_id.values())
    if len(questions) > MAX_QUESTIONS:
        logger.warning(
            "options.rank: %d questions registered; asking the first %d",
            len(questions),
            MAX_QUESTIONS,
        )
    return questions[:MAX_QUESTIONS]


# ── State ──


def labels_of(text: str) -> list[str]:
    """The chip labels of the LAST options marker in *text*, deduplicated, in order."""
    matches = list(OPTIONS_RE_LINE.finditer(as_text(text)))
    if not matches:
        return []
    seen: dict[str, None] = {}
    for part in matches[-1].group("labels").split("|"):
        label = part.strip()
        if label:
            seen.setdefault(label, None)
    return list(seen)


def reply_text(text: str) -> str:
    """*text* with every options marker removed, clipped to :data:`MAX_REPLY_CHARS`."""
    return OPTIONS_RE_LINE.sub("", as_text(text)).strip()[:MAX_REPLY_CHARS]


def past_picks(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """This session's earlier offered-then-picked pairs, newest last.

    A pair is an assistant message carrying an options marker followed by a ``user``
    message whose whole text equals one of its labels. Any other user message ends
    the offer without a pair. Only the last :data:`MAX_PAST_PICKS` pairs are kept.
    """
    pairs: list[dict[str, Any]] = []
    offered: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            offered = labels_of(content) if isinstance(content, str) else []
        elif role == "user":
            picked = content.strip() if isinstance(content, str) else ""
            if offered and picked in offered:
                pairs.append(
                    {
                        "offered": [label[:MAX_LABEL_CHARS] for label in offered[:MAX_LABELS]],
                        "picked": picked[:MAX_LABEL_CHARS],
                    }
                )
            offered = []
    return pairs[-MAX_PAST_PICKS:]


def active_questions(
    registry: Sequence[QuestionDef], *, goal: str
) -> tuple[list[QuestionDef], list[QuestionDef]]:
    """``(asked, skipped)``: a question that needs the goal is skipped without one."""
    asked: list[QuestionDef] = []
    skipped: list[QuestionDef] = []
    for question in registry:
        (skipped if "goal" in question.inputs and not goal else asked).append(question)
    return asked, skipped


def build_state(
    asked: Sequence[QuestionDef],
    *,
    labels: Sequence[str],
    reply: str,
    picks: Sequence[Mapping[str, Any]],
    goal: str,
) -> dict[str, Any]:
    """The request state: the labels plus the union of the asked questions' inputs."""
    needed = {name for question in asked for name in question.inputs}
    state: dict[str, Any] = {"options": list(labels)}
    if "reply_text" in needed:
        state["reply"] = reply
    if "past_picks" in needed:
        state["past_picks"] = [dict(pair) for pair in picks]
    if "goal" in needed and goal:
        state["goal"] = goal[:MAX_GOAL_CHARS]
    return state


# ── Questions ──


def build_questions(asked: Sequence[QuestionDef], labels: Sequence[str]) -> list[Question]:
    """One ``Choice`` per menu question, one yes/no ``Choice`` per chip otherwise."""
    questions: list[Question] = []
    for question in asked:
        if question.shape == SHAPE_MENU:
            options = list(labels) + ([NONE_ID] if question.none_option else [])
            questions.append(Choice(id=question.id, prompt=question.prompt, options=options))
            continue
        for index, label in enumerate(labels):
            questions.append(
                Choice(
                    id=f"{question.id}.{index}",
                    prompt=f"{question.prompt}\nOption: {label}",
                    options=[YES, NO],
                )
            )
    return questions


def _menu_distribution(answer: Answer, options: Sequence[str]) -> dict[str, float]:
    """A probability per option, from Jev's map only when it covers every option.

    The Jev lane drops an entry it cannot read, so a map missing an option is
    partial, not a zero for that option. A covering map is normalized to sum to 1.
    Otherwise the chosen option keeps ``p`` and the rest share ``1 - p`` evenly.
    """
    probabilities = answer.probabilities or {}
    if all(option in probabilities for option in options):
        total = sum(float(probabilities[option]) for option in options)
        if total > 0:
            return {option: float(probabilities[option]) / total for option in options}
    rest = [option for option in options if option != answer.value]
    share = (1.0 - answer.p) / len(rest) if rest else 0.0
    return {option: (answer.p if option == answer.value else share) for option in options}


def _yes_probability(answer: Answer) -> float:
    if answer.probabilities and YES in answer.probabilities:
        return float(answer.probabilities[YES])
    return answer.p if answer.value == YES else 1.0 - answer.p


@dataclass
class Reading:
    """One asked question's answer, reduced to a probability per label."""

    question: QuestionDef
    per_label: list[float]
    none_p: float | None = None
    picked_none: bool = False


def read_answers(
    asked: Sequence[QuestionDef], labels: Sequence[str], answers: Answers
) -> list[Reading] | None:
    """Each asked question's per-label probabilities, or ``None`` on a missing answer."""
    readings: list[Reading] = []
    for question in asked:
        if question.shape == SHAPE_MENU:
            answer = answers.get(question.id)
            if answer is None:
                return None
            options = list(labels) + ([NONE_ID] if question.none_option else [])
            dist = _menu_distribution(answer, options)
            readings.append(
                Reading(
                    question=question,
                    per_label=[dist[label] for label in labels],
                    none_p=dist.get(NONE_ID) if question.none_option else None,
                    picked_none=answer.value == NONE_ID,
                )
            )
            continue
        per_label = []
        for index in range(len(labels)):
            answer = answers.get(f"{question.id}.{index}")
            if answer is None:
                return None
            per_label.append(_yes_probability(answer))
        readings.append(Reading(question=question, per_label=per_label))
    return readings


# ── Combiner ──


@dataclass
class Combined:
    """The combiner's verdict over one menu."""

    scores: list[float | None]
    recommendation: int | None
    vetoed: list[int]
    flags: list[tuple[str, int]]
    disagree: bool
    none_signal: list[str]
    top1: dict[str, int]


def _argmax(values: Sequence[float], allowed: Iterable[int]) -> int | None:
    best: int | None = None
    for index in allowed:
        if best is None or values[index] > values[best]:
            best = index
    return best


def combine(readings: Sequence[Reading], n_labels: int) -> Combined:
    """Normalized product of the score questions, with vetoes removed and flags noted.

    Each score question contributes ``p ** weight`` (``(1 - p) ** weight`` when lower
    is better). A veto removes every chip at or above its threshold, a flag only
    records it. ``disagree`` is whether the score questions' own top chips differ,
    and ``none_signal`` names the score questions that answered "none of these".
    Ties go to the chip the agent listed first.
    """
    vetoed = sorted(
        {
            index
            for reading in readings
            if reading.question.role == ROLE_VETO
            for index, p in enumerate(reading.per_label)
            if p >= reading.question.threshold
        }
    )
    flags = [
        (reading.question.key, index)
        for reading in readings
        if reading.question.role == ROLE_FLAG
        for index, p in enumerate(reading.per_label)
        if p >= reading.question.threshold
    ]
    surviving = [index for index in range(n_labels) if index not in vetoed]
    scoring = [reading for reading in readings if reading.question.role == ROLE_SCORE]
    raw = [1.0] * n_labels
    top1: dict[str, int] = {}
    for reading in scoring:
        question = reading.question
        for index, p in enumerate(reading.per_label):
            base = 1.0 - p if question.direction == LOWER else p
            raw[index] *= max(base, 0.0) ** question.weight
        oriented = [(1.0 - p if question.direction == LOWER else p) for p in reading.per_label]
        best = _argmax(oriented, range(n_labels))
        if best is not None:
            top1[question.key] = best
    total = sum(raw[index] for index in surviving)
    scores: list[float | None] = [
        (raw[index] / total if total > 0 else None) if index in surviving else None
        for index in range(n_labels)
    ]
    recommendation = (
        _argmax([s or 0.0 for s in scores], surviving) if scoring and total > 0 else None
    )
    return Combined(
        scores=scores,
        recommendation=recommendation,
        vetoed=vetoed,
        flags=flags,
        disagree=len(set(top1.values())) > 1,
        none_signal=[r.question.key for r in scoring if r.picked_none],
        top1=top1,
    )


def build_outcome(
    *,
    turn_id: str,
    labels: Sequence[str],
    readings: Sequence[Reading],
    skipped: Sequence[QuestionDef],
    combined: Combined,
) -> dict[str, Any]:
    """The outcome row's own fields, flat and inside the log's bounds.

    ``dist.<id@version>`` holds one probability per label, in label order, and a
    menu with a "none of these" option carries that option's probability last. Lists
    hold numbers only, because the log renders a ``None`` list item as text: a vetoed
    chip scores ``0.0`` in ``combined`` and is named in ``vetoed``.
    """
    outcome: dict[str, Any] = {
        "turn_id": turn_id,
        "labels": [label[:MAX_LABEL_CHARS] for label in labels],
        "asked": [reading.question.key for reading in readings],
        "skipped": [question.key for question in skipped],
    }
    for reading in readings:
        dist = list(reading.per_label) + ([] if reading.none_p is None else [reading.none_p])
        outcome[f"dist.{reading.question.key}"] = [round(p, 4) for p in dist]
    outcome["combined"] = [0.0 if s is None else round(s, 4) for s in combined.scores]
    outcome["recommendation"] = combined.recommendation
    outcome["vetoed"] = list(combined.vetoed)
    outcome["flags"] = [f"{key}:{index}" for key, index in combined.flags]
    outcome["disagree"] = combined.disagree
    outcome["none_signal"] = list(combined.none_signal)
    return outcome


# ── Owner turns and pending labels ──

_lock = threading.Lock()
_owner_turn: "collections.OrderedDict[str, bool]" = collections.OrderedDict()
_send_epoch: "collections.OrderedDict[str, int]" = collections.OrderedDict()
_pending: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_seq = itertools.count(1)
_detached: set[asyncio.Task] = set()


def _bounded_set(store: collections.OrderedDict, key: str, value: Any) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > MAX_PENDING:
        store.popitem(last=False)


def reset_state() -> None:
    """Forget every session's owner-turn flag, send epoch and pending record."""
    with _lock:
        _owner_turn.clear()
        _send_epoch.clear()
        _pending.clear()


def note_turn(session_key: str, *, user_turn: bool) -> None:
    """Record that a turn started. A turn nobody typed ends the owner's claim on it."""
    if not user_turn:
        with _lock:
            _bounded_set(_owner_turn, session_key, False)


def is_owner_turn(session_key: str) -> bool:
    """Whether the session's latest dashboard send was the owner's."""
    with _lock:
        return _owner_turn.get(session_key, False)


def note_send(session_key: str, text: str, *, owner: bool) -> dict[str, Any] | None:
    """Record a dashboard send. The owner's is the label for the pending record.

    A send by anyone else discards the pending record unlabelled: it is not the
    owner's pick. Returns the label row when one was built.
    """
    with _lock:
        _bounded_set(_owner_turn, session_key, owner)
        _bounded_set(_send_epoch, session_key, _send_epoch.get(session_key, 0) + 1)
        if not owner:
            _pending.pop(session_key, None)
            return None
    return record_pick(session_key, text)


def record_pick(session_key: str, text: str) -> dict[str, Any] | None:
    """Write the label row for *session_key*'s pending record and clear it.

    ``picked`` is the label the whole message equals, or ``None`` for typed text that
    matches no chip. ``agree_top1`` compares it with the recommendation, and
    ``agree`` with each score question's own top chip. Returns the row, or ``None``
    when nothing was pending.
    """
    with _lock:
        record = _pending.pop(session_key, None)
    if record is None or time.monotonic() - record["created"] > PENDING_TTL_SECS:
        return None
    labels: list[str] = record["labels"]
    message = as_text(text).strip()
    index = labels.index(message) if message in labels else None
    recommendation = record["recommendation"]
    row = _log.build_row(
        point=POINT,
        session_key=session_key,
        latency_ms=0,
        extra={
            "kind": KIND_PICK,
            "turn_id": record["turn_id"],
            "picked": None if index is None else labels[index][:MAX_LABEL_CHARS],
            "picked_index": index,
            "agree_top1": (
                None if index is None or recommendation is None else index == recommendation
            ),
            "agree": (
                []
                if index is None
                else [record["top1"].get(key) == index for key in record["scored"]]
            ),
            "scored": list(record["scored"]),
            "age_s": round(time.monotonic() - record["created"], 1),
        },
    )
    _append_detached(row)
    return row


def _append_detached(row: dict[str, Any]) -> None:
    """Append *row* off the event loop without waiting; inline when no loop runs."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.append(row)
        return
    task = loop.create_task(asyncio.to_thread(_log.append, row))
    _detached.add(task)
    task.add_done_callback(_detached.discard)


def _store_pending(session_key: str, record: dict[str, Any], epoch: int) -> bool:
    """Keep *record* unless the owner already answered, or a newer one is kept."""
    with _lock:
        if _send_epoch.get(session_key, 0) != epoch:
            return False
        current = _pending.get(session_key)
        if current is not None and current["seq"] > record["seq"]:
            return False
        _bounded_set(_pending, session_key, record)
        return True


# ── The run ──


def wait_budget() -> float:
    """How long the whole run may take, clamped into a sane window. Never raises."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def _read_goal(session_key: str) -> str:
    """The session ledger's goal, or ``""``. Never raises."""
    try:
        from kiro_crew import session_ledger

        goal = session_ledger.read_state(session_ledger.ledger_key(session_key)).get("goal")
    except Exception:
        return ""
    return goal.strip() if isinstance(goal, str) else ""


async def rank_options(
    session_key: str,
    text: str,
    labels: Sequence[str],
    history: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Score *labels* in the shadow and record the outcome. Returns the outcome row.

    *text* is the finalized reply, *labels* its chips and *history* the messages
    before it. An unconsented scope, an unsampled session, fewer than two chips,
    more than ``MAX_LABELS`` chips or a chip longer than ``MAX_LABEL_CHARS`` return
    ``None`` before any request or row. Scoring a cut-down menu would record the
    owner's pick of a dropped or shortened chip as typed text. Never raises except
    :class:`asyncio.CancelledError`.
    """
    labels = list(dict.fromkeys(labels))
    if not 2 <= len(labels) <= MAX_LABELS:
        return None
    if any(len(label) > MAX_LABEL_CHARS for label in labels):
        return None
    try:
        if not await asyncio.to_thread(core.is_enabled, POINT, session_key=session_key):
            return None
        with _lock:
            epoch = _send_epoch.get(session_key, 0)
        return await asyncio.wait_for(
            _run(session_key, text, labels, list(history), epoch=epoch), timeout=wait_budget()
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.debug("options.rank: the run outlived its budget")
        return None
    except Exception:
        logger.debug("options.rank: leaving this reply unscored", exc_info=True)
        return None


async def _run(
    session_key: str,
    text: str,
    labels: list[str],
    history: list[Mapping[str, Any]],
    *,
    epoch: int,
) -> dict[str, Any] | None:
    started = time.monotonic()
    seq = next(_seq)
    turn_id = uuid.uuid4().hex[:16]
    registry = await asyncio.to_thread(load_registry)
    goal = await asyncio.to_thread(_read_goal, session_key)
    asked, skipped = active_questions(registry, goal=goal)
    if not asked:
        return None
    picks = await asyncio.to_thread(past_picks, history)
    state = build_state(asked, labels=labels, reply=reply_text(text), picks=picks, goal=goal)
    questions = build_questions(asked, labels)
    answers = await core.decide(
        POINT, state, questions, session_key=session_key, extra={"turn_id": turn_id}
    )
    if answers is None:
        return None
    readings = read_answers(asked, labels, answers)
    if readings is None:
        return None
    combined = combine(readings, len(labels))
    outcome = build_outcome(
        turn_id=turn_id, labels=labels, readings=readings, skipped=skipped, combined=combined
    )
    row = _log.build_row(
        point=POINT,
        session_key=session_key,
        latency_ms=int((time.monotonic() - started) * 1000),
        extra=outcome,
    )
    try:
        written = await asyncio.wait_for(asyncio.to_thread(_log.append, row), LOG_BUDGET_SECS)
    except asyncio.TimeoutError:
        written = False
    if not written:
        return None
    _store_pending(
        session_key,
        {
            "seq": seq,
            "turn_id": turn_id,
            "labels": labels,
            "recommendation": combined.recommendation,
            "top1": dict(combined.top1),
            "scored": [r.question.key for r in readings if r.question.role == ROLE_SCORE],
            "created": time.monotonic(),
        },
        epoch,
    )
    return row
