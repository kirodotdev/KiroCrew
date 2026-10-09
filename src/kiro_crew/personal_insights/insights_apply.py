from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from kiro_crew.config import KiroCrewConfig
from kiro_crew.history import ConversationLog
from kiro_crew.lesson_validation import LESSON_APPLIES_ALWAYS
from kiro_crew.personal_insights.insights_canonical import canonical_lesson_digest
from kiro_crew.personal_insights.insights_runs import RunRepository
from kiro_crew.vector_memory import VectorMemoryStore

MIN_SUPPORTING_SESSIONS = 2
MIN_INDEPENDENT_LINEAGES = 2
MIN_DISTINCT_DAYS = 2
MAX_SINGLE_LINEAGE_SHARE = 0.75
DUPLICATE_OVERLAP = 0.6
SIMILAR_LOW = 0.55
DUPLICATE_SIMILARITY = 0.80
EMBEDDER_READY_SECONDS = 120
LESSON_CATEGORY = "preference"
LESSON_SOURCE = "user_explicit"

_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")
_STOP = frozenset(
    "the and for with that this from into before after when then than your you are not "
    "any all one two each every been have has had was were will would should could about "
    "what which while where there their them they its do does did done doing out over "
    "under also just only more most very can may might must need needs rather instead "
    "report reporting task complete based without actual result last".split()
)


def open_lesson_store() -> VectorMemoryStore:
    cfg = KiroCrewConfig.load()
    store = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)
    store.init()
    try:
        from kiro_crew.embeddings import (
            activate_shared_embedder,
            get_shared_embedder,
            make_sync_embed_fn,
            model_file_present,
        )

        if model_file_present():
            activate_shared_embedder()
            wait_ready = getattr(get_shared_embedder(), "wait_ready", None)
            ready = wait_ready(EMBEDDER_READY_SECONDS) if callable(wait_ready) else True
            if ready:
                store.embed_fn = make_sync_embed_fn()
    except Exception:
        store.embed_fn = None
    return store


@dataclass
class OverfitCheck:
    passed: bool
    supporting_sessions: int
    independent_lineages: int
    distinct_days: int
    largest_lineage_share: float
    guidance_overlap: float
    duplicate_method: str = "none"
    overlapping_guidance: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyResult:
    action_id: str
    state: str
    message: str
    overfit: OverfitCheck | None = None
    lesson_rule: str | None = None
    verified: bool = False
    undo_available: bool = False
    superseded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lineage(log: ConversationLog, key: str) -> str:
    title = ""
    try:
        title = str((log._read_metadata(key) or {}).get("title") or "")
    except Exception:
        title = ""
    base = title.strip()
    while base.startswith("↳ Fork of "):
        base = base.removeprefix("↳ Fork of ").strip()
    return base.lower() or key


def _day(log: ConversationLog, key: str) -> str | None:
    for row in log.read_messages(key):
        if row.get("ts"):
            return str(row["ts"])[:10]
    return None


def _stem(token: str) -> str:
    for suffix in ("ication", "ations", "ation", "ing", "ies", "ied", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _WORD_RE.findall(text.lower()) if t not in _STOP}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _lesson_value(row: Any) -> dict[str, Any] | str | None:
    if not isinstance(row, dict):
        return None
    if "value" in row:
        return row["value"]
    raw = row.get("value_json")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)


def _lesson_rule_text(value: dict[str, Any] | str | None) -> tuple[str, str]:
    if isinstance(value, dict):
        return str(value.get("rule") or ""), str(value.get("negative") or "")
    if isinstance(value, str):
        rule, _, negative = value.partition(" NOT: ")
        return rule, negative
    return "", ""


def _lesson_texts(store: VectorMemoryStore) -> list[str]:
    texts: list[str] = []
    for row in store.get_lessons():
        rule, negative = _lesson_rule_text(_lesson_value(row))
        if rule:
            texts.append(f"{rule} {negative}".strip())
    return texts


def check_overfit(
    *,
    log: ConversationLog,
    store: VectorMemoryStore,
    session_keys: list[str],
    rule: str,
    negative: str,
) -> OverfitCheck:
    keys = list(dict.fromkeys(session_keys))
    lineages: dict[str, int] = {}
    days: set[str] = set()
    for key in keys:
        lineage = _lineage(log, key)
        lineages[lineage] = lineages.get(lineage, 0) + 1
        day = _day(log, key)
        if day:
            days.add(day)
    largest = max(lineages.values(), default=0) / max(len(keys), 1)
    proposed = f"{rule} {negative}".strip()
    overlaps: list[tuple[float, str]] = []
    rule_emb = None
    try:
        rule_emb = store.embed_lesson(rule)
    except Exception:
        rule_emb = None
    if rule_emb is not None:
        try:
            for cand in store.find_contradiction_candidates(
                rule, threshold_low=SIMILAR_LOW, threshold_high=1.01, rule_emb=rule_emb
            ):
                score = float(cand.get("similarity") or cand.get("score") or 0.0)
                text, neg_text = _lesson_rule_text(_lesson_value(cand) or cand.get("rule"))
                overlaps.append((score, f"{text} {neg_text}".strip() or str(cand)[:200]))
        except Exception:
            rule_emb = None
    if rule_emb is None:
        overlaps = [
            (score, text)
            for text in _lesson_texts(store)
            if (score := _overlap(proposed, text)) >= 0.35
        ]
    overlaps.sort(reverse=True)
    top = overlaps[0][0] if overlaps else 0.0
    duplicate_threshold = DUPLICATE_SIMILARITY if rule_emb is not None else DUPLICATE_OVERLAP
    reasons: list[str] = []
    if len(keys) < MIN_SUPPORTING_SESSIONS:
        reasons.append(f"only {len(keys)} supporting session(s)")
    if len(lineages) < MIN_INDEPENDENT_LINEAGES:
        reasons.append(
            f"only {len(lineages)} independent session lineage(s); forks of one thread count once"
        )
    if len(days) < MIN_DISTINCT_DAYS:
        reasons.append(f"evidence spans {len(days)} day(s)")
    if largest > MAX_SINGLE_LINEAGE_SHARE and len(keys) > 1:
        reasons.append(f"{largest:.0%} of evidence comes from one lineage")
    if top >= duplicate_threshold:
        reasons.append(f"existing guidance already covers this (similarity {top:.2f})")
    return OverfitCheck(
        passed=not reasons,
        supporting_sessions=len(keys),
        independent_lineages=len(lineages),
        distinct_days=len(days),
        largest_lineage_share=round(largest, 2),
        guidance_overlap=round(top, 2),
        duplicate_method="embedding" if rule_emb is not None else "keyword",
        overlapping_guidance=[t[:200] for _, t in overlaps[:5]],
        reasons=reasons,
    )


def _find_stored(store: VectorMemoryStore, rule: str) -> dict[str, Any] | None:
    wanted = rule.lower().strip()
    for row in store.get_lessons():
        stored_rule, _ = _lesson_rule_text(_lesson_value(row))
        if stored_rule.lower().strip() == wanted:
            return row
    return None


def do_it(
    action_id: str,
    *,
    repo: RunRepository,
    store: VectorMemoryStore | None = None,
    log: ConversationLog | None = None,
    force: bool = False,
) -> ApplyResult:
    store = store or open_lesson_store()
    log = log or ConversationLog()
    record = repo.get_action(action_id)
    if record is None:
        return ApplyResult(action_id, "unknown", "No such action in the run repository.")
    if record["state"] in ("applied_verified", "changed_unverified"):
        return ApplyResult(
            action_id, record["state"], "Already applied.", verified=True, undo_available=True
        )
    payload = record["payload"]
    if payload["action_class"] != "lesson_proposal":
        return ApplyResult(
            action_id,
            record["state"],
            f"{payload['action_class']} is copy-first in this slice; Do it is wired for lessons only.",
        )
    fields = payload["artifact"]["structured_parameters"]
    rule = fields["rule"].strip()
    negative = (fields.get("negative") or "").strip()
    claims = repo.claims_for_action(record["run_id"], payload.get("claim_ids", []))
    session_keys = [k for c in claims for k in c["session_keys"]]
    overfit = check_overfit(
        log=log, store=store, session_keys=session_keys, rule=rule, negative=negative
    )
    if not overfit.passed and not force:
        repo.set_action_state(record["run_id"], action_id, "held_overfit")
        return ApplyResult(
            action_id,
            "held_overfit",
            "Not applied. The evidence or existing guidance does not justify a new standing rule: "
            + "; ".join(overfit.reasons)
            + ".",
            overfit=overfit,
        )
    result = store.write_lesson(
        rule, LESSON_CATEGORY, negative or None, LESSON_SOURCE, applies=LESSON_APPLIES_ALWAYS
    )
    outcome = str(getattr(result.outcome, "value", result.outcome))
    superseded = list(result.superseded)
    if outcome == "refused":
        repo.set_action_state(record["run_id"], action_id, "failed")
        return ApplyResult(
            action_id,
            "failed",
            f"The lesson store refused the write ({result.reason}).",
            overfit=overfit,
        )
    if outcome == "deduped":
        repo.set_action_state(record["run_id"], action_id, "held_duplicate")
        return ApplyResult(
            action_id,
            "held_duplicate",
            f"Not applied. The lesson store judged this a duplicate of stored guidance ({result.reason}).",
            overfit=overfit,
        )
    stored = _find_stored(store, rule)
    verified = stored is not None
    expected = canonical_lesson_digest({"rule": rule, "negative": negative, "scope": "global"})
    state = "applied_verified" if verified else "changed_unverified"
    repo.set_action_state(
        record["run_id"],
        action_id,
        state,
        target_identity=f"lesson:{expected}",
        applied=True,
        verified=verified,
    )
    message = (
        f"Done and verified ({outcome}). The lesson is stored and reads back."
        if verified
        else f"Changed, not verified ({outcome}). The store accepted the write but readback did not find it."
    )
    return ApplyResult(
        action_id,
        state,
        message,
        overfit=overfit,
        lesson_rule=rule,
        verified=verified,
        undo_available=True,
        superseded=superseded,
    )


def undo(
    action_id: str,
    *,
    repo: RunRepository,
    store: VectorMemoryStore | None = None,
) -> ApplyResult:
    store = store or open_lesson_store()
    record = repo.get_action(action_id)
    if record is None:
        return ApplyResult(action_id, "unknown", "No such action.")
    if record["state"] not in ("applied_verified", "changed_unverified", "undo_failed"):
        return ApplyResult(
            action_id, record["state"], "Nothing to undo; the action was not applied."
        )
    rule = record["payload"]["artifact"]["structured_parameters"]["rule"].strip()
    row = _find_stored(store, rule)
    if row is None:
        repo.set_action_state(record["run_id"], action_id, "undone", undone=True)
        return ApplyResult(
            action_id, "undone", "Nothing to remove; the lesson is already gone.", verified=True
        )
    from kiro_crew.vector_memory_runtime.lessons import _lesson_display_text

    rendered = _lesson_display_text(_lesson_value(row)) or rule
    removed = store.delete_lesson(rendered, None, exact=True)
    if not removed:
        removed = store.delete_lesson(rule, None, exact=True)
    gone = _find_stored(store, rule) is None
    if removed and gone:
        repo.set_action_state(record["run_id"], action_id, "undone", undone=True)
        return ApplyResult(
            action_id, "undone", "Undone and verified. The lesson is gone.", verified=True
        )
    repo.set_action_state(record["run_id"], action_id, "undo_failed")
    return ApplyResult(
        action_id,
        "undo_failed",
        f"Undo did not remove the lesson (removed={removed}, gone={gone}).",
    )


def describe(result: ApplyResult) -> str:
    return json.dumps(result.as_dict(), indent=1, sort_keys=True)
