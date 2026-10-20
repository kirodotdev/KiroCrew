"""What one change card record is: its limits, statuses and error codes, and its checks.

The card lifecycle's statuses and limits, the store's MAC purpose and error codes,
:class:`CardError`, the text checks a proposal's reason and parameters pass, the derived
fields a record carries and :func:`rebuild_record`, which rebuilds them from a record's
``kind`` and ``params`` before the store trusts it, plus the JSON-safe copy every reader
makes of a value. A new status, error code or record check goes here.

Served through :mod:`kiro_crew.dashboard.change_cards`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kiro_crew import change_card_catalog as catalog

CARD_TTL_SECONDS = 24 * 60 * 60
FINISHED_RETAIN_SECONDS = 7 * 24 * 60 * 60
INFLIGHT_STALE_SECONDS = 10 * 60
MAX_STORED_CARDS = 512
#: Largest store file read; bigger is refused unread, like an unreadable one.
STORE_FILE_MAX = 32 * 1024 * 1024
MAX_LIVE_CARDS_PER_SLOT = 16
STORE_FILENAME = "change_cards.json"

STATUS_PENDING = "pending"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_UNDONE = "undone"

FINISHED_STATUSES = frozenset(
    {STATUS_APPLIED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_UNDONE}
)
#: Outcomes the proposing agent hears about on its next turn.
REPORTED_STATUSES = FINISHED_STATUSES
_ALL_STATUSES = FINISHED_STATUSES | {STATUS_PENDING, STATUS_APPLYING}
#: Statuses whose ``undo_unavailable_reason`` is still the preview's own.
_PRE_APPLY_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_APPLYING, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}
)

#: The vault subkey purpose the persisted records are MACed under.
STORE_MAC_PURPOSE = "change-card-store"
_MAC_FIELD = "mac"
_MAC_RE = re.compile(r"[0-9a-f]{64}")
#: ``error.code`` of a card whose step was cut off before it reported back.
CODE_INTERRUPTED = "interrupted"
#: ``error.code`` of a card whose step ran but whose result could not be saved.
CODE_CHECKPOINT_FAILED = "checkpoint_failed"
CODE_STORE_UNAVAILABLE = "store_unavailable"
_INTERRUPTED_MESSAGE = (
    "Kiro Crew stopped while this change was running, so it may or may not have "
    "taken effect. Check it before asking for it again."
)
#: The fields :func:`kiro_crew.change_card_catalog.build_preview` derives, as
#: :meth:`CardStore._apply_preview` stores them.
_DERIVED_KEYS = (
    "title",
    "risk",
    "widen",
    "changes",
    "scope",
    "undo_label",
    "next_run_at",
    "timezone",
    "once",
    "run_at_local",
)

_CARD_ID_RE = re.compile(r"^cc_[A-Za-z0-9_-]{8,40}$")
_MESSAGE_MAX = 300
#: Serialized size cap on one card record (params, snapshot, context, plan).
CARD_RECORD_MAX = 64 * 1024

#: Internal record keys that never leave the gateway.
_PRIVATE_KEYS = frozenset(
    {
        "session_key",
        "before",
        "after",
        "evidence",
        "undo_evidence",
        "inflight",
        "reported_status",
        "dismissed",
        "finished_at",
        "context",
    }
)


class CardError(Exception):
    """A refused card operation, carrying its HTTP status and stable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


CardError.__module__ = "kiro_crew.dashboard.change_cards"


def canonical(value: Any) -> str:
    """The one serialization two bodies or snapshots are compared through."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def clean_reason(reason: Any) -> str:
    if reason is None:
        return ""
    if not isinstance(reason, str):
        raise CardError(400, "invalid_reason", "reason must be a string")
    text = "".join(ch if ch >= " " or ch in "\n" else " " for ch in reason).strip()
    if len(text) > catalog.REASON_MAX_CHARS:
        raise CardError(400, "invalid_reason", "reason must be at most 500 characters")
    from kiro_crew.guide_catalog import needs_redaction

    if needs_redaction(text):
        # Shown on the card and sent to every owner tab as written, so a reason
        # the output redactors would change is refused rather than stored.
        raise CardError(
            400,
            "invalid_text",
            "reason: remove the credential or link; the card shows it as written",
        )
    return text


def check_param_text(params: Any, *, _path: str = "params") -> None:
    """Refuse agent-supplied card parameters any output redactor would change.

    A credential or exfiltration-shaped link in any nested string or key is
    refused with ``invalid_text`` before the card is stored, because the stored
    plan is what runs. An ordinary server URL passes.
    """
    from kiro_crew.guide_catalog import needs_redaction

    if isinstance(params, dict):
        for key, value in params.items():
            where = f"{_path}.{str(key)[:64]}"
            if isinstance(key, str) and needs_redaction(key):
                raise _param_text_refusal(where)
            check_param_text(value, _path=where)
    elif isinstance(params, list):
        for i, value in enumerate(params):
            check_param_text(value, _path=f"{_path}[{i}]")
    elif isinstance(params, str) and needs_redaction(params):
        raise _param_text_refusal(_path)


def _param_text_refusal(where: str) -> CardError:
    return CardError(
        400,
        "invalid_text",
        f"{where}: remove the credential or link; a card shows and sends it as written",
    )


def valid_card_id(card_id: object) -> bool:
    return isinstance(card_id, str) and bool(_CARD_ID_RE.fullmatch(card_id))


def derived_fields(kind: str, preview: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """The record fields a preview determines (everything in :data:`_DERIVED_KEYS`)."""
    return {
        "title": preview["title"],
        "risk": preview["risk"],
        "widen": bool(preview.get("widen")),
        "changes": preview.get("changes") or [],
        "scope": preview.get("scope"),
        "undo_label": catalog.undo_label(kind),
        "next_run_at": context.get("next_run_at"),
        "timezone": context.get("timezone"),
        # A one-shot schedule: the dashboard shows "Runs once" with this local time.
        "once": bool(context.get("once")),
        "run_at_local": context.get("run_at_local"),
    }


def _same(a: Any, b: Any) -> bool:
    return canonical(a) == canonical(b)


def rebuild_record(rec: dict[str, Any]) -> dict[str, Any] | None:
    """*rec* with every displayed and executed field rebuilt, or ``None`` to drop it.

    Kind and params are re-validated and the preview and undo plan rebuilt the
    way ``propose_change`` builds them; any stored field that differs drops the
    record. Execution reads only the rebuilt plan.
    """
    kind = rec.get("kind")
    if not isinstance(kind, str) or kind not in catalog.KINDS:
        return None
    if rec.get("status") not in _ALL_STATUSES or not isinstance(rec.get("revision"), int):
        return None
    before, context = rec.get("before"), rec.get("context")
    if not isinstance(before, dict) or not isinstance(context, dict):
        return None
    try:
        params = catalog.validate_params(kind, rec.get("params"))
        preview = catalog.build_preview(kind, params, before, context)
    except Exception:
        return None
    if not _same(params, rec.get("params")):
        return None
    fields = derived_fields(kind, preview, context)
    # ``risk`` is re-derived rather than compared: a build that reclassifies a
    # setting would otherwise drop every authentic card on it, with its history
    # and Undo, and the record then carries the risk this build derives.
    if any(
        not _same(fields[k], rec.get(k))
        for k in _DERIVED_KEYS
        if k != "risk" and (k != "widen" or "widen" in rec)
    ):
        return None
    if not _same(list(catalog.KINDS[kind].editable), rec.get("editable")):
        return None
    plan = rec.get("plan")
    if not isinstance(plan, dict):
        return None
    status = rec["status"]
    apply_plan = preview["apply"]
    if not _same(apply_plan, plan.get("apply")):
        # A crewmate.create card stored before goal records: the same card with
        # the goal in description. Still unrun, it runs the current plan; once
        # applied, its steps and evidence keep the shape they ran with.
        if kind != catalog.KIND_CREWMATE_CREATE or not any(
            _same(catalog.legacy_create_apply(params, first_greeting=g), plan.get("apply"))
            for g in (True, False)
        ):
            return None
        if status == STATUS_APPLYING or status not in _PRE_APPLY_STATUSES:
            apply_plan = plan["apply"]
    if status in _PRE_APPLY_STATUSES and not _same(
        preview.get("undo_unavailable_reason"), rec.get("undo_unavailable_reason")
    ):
        return None
    undo = plan.get("undo")
    if undo is not None:
        if status not in (STATUS_APPLIED, STATUS_PARTIAL, STATUS_UNDONE):
            return None
        evidence = rec.get("evidence")
        after = rec.get("after") or {}
        if not isinstance(evidence, list) or not isinstance(after, dict):
            return None
        try:
            rebuilt_undo, _reason = catalog.build_undo(
                kind,
                params,
                before,
                evidence,
                catalog.applied_write_count(apply_plan, len(evidence)),
                {"rule_id": after.get("id")},
            )
        except Exception:
            return None
        if rebuilt_undo is None or not _same(rebuilt_undo, undo):
            return None
        undo = rebuilt_undo
    return {**rec, "params": params, **fields, "plan": {"apply": apply_plan, "undo": undo}}


def _jsonable(value: Any) -> Any:
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
