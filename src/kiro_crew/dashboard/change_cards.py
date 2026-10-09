"""Gateway-owned change cards: the persisted store and the current-state readers.

A card is one proposal from :mod:`kiro_crew.change_card_catalog`, owned by the
dashboard slot of the session that proposed it. The gateway owns all of its
state; the MCP shim holds none, and the browser reads it back from
``GET /api/cards/pending`` and ``card_update`` frames. Where the card is DRAWN is
the conversation: a ``card`` transcript row at the point it was proposed
(:mod:`kiro_crew.dashboard.chat_cards`), which references the card by id.

Lifecycle::

    pending --apply step 0--> applying --last step 2xx--> applied --undo--> undone
       |  ^                      |  \\--later step fails--> partial --undo--> undone
       |  +--step 0 fails-- failed
       +--cancel--> cancelled          pending/failed --24h--> expired

* Only the browser's request to the real route, carrying ``X-Card-*`` headers,
  moves a card past ``pending``; the hook in
  :mod:`kiro_crew.dashboard.handlers.change_cards` checks it against the plan and
  records each step only from that route's own 2xx response.
* ``revision`` changes only when the proposal's parameters change (a re-preview),
  so a request carrying an older revision is refused rather than applied.
* A card applies at most once: a repeat of a finished apply is answered with the
  recorded result, not a second write.

The store keeps everything in memory and writes ``change_cards.json`` under the
data home after each change (off the event loop, behind a file lock), so cards
survive a gateway restart. Finished cards are pruned after seven days.

That file is a ``VISIBLE`` crew-home leaf: the agent can write it. So nothing
read back from it is trusted as written:

* every record carries a keyed MAC under a vault subkey (the vault is ``HIDDEN``
  in every sandbox mode), so a record the gateway did not write -- or one it
  wrote and the agent edited -- is dropped with a warning on load;
* every derived field the browser displays or executes (title, risk, changes,
  scope, the apply plan, the undo plan) is rebuilt from the record's ``kind`` and
  ``params`` through the same validator and builder ``propose_change`` uses, and a
  record whose stored copy differs is dropped too. Execution uses the rebuilt plan.

Execution is persist-before-you-publish: a step's admission is written to disk
(:meth:`CardStore.flush` with ``strict=True``) before its route runs, and a step
that cannot be admitted durably runs nothing. A record loaded with a step still in
flight never reported back, so it may or may not have taken effect: it is marked
for review (``partial`` with ``error.code == "interrupted"`` and no undo) and is
never re-admitted.

The readers (:func:`read_state`, :func:`read_context`) are the gateway's own read
of what a card replaces. They return names, flags and settings values only:
never a secret value, an env value or a token.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from kiro_crew import change_card_catalog as catalog
from kiro_crew.config.schema import requires_restart

logger = logging.getLogger(__name__)

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
_CLOSED_STATUSES = frozenset({STATUS_CANCELLED, STATUS_EXPIRED, STATUS_UNDONE})
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
    if any(
        not _same(fields[k], rec.get(k)) for k in _DERIVED_KEYS if k != "widen" or "widen" in rec
    ):
        return None
    if not _same(list(catalog.KINDS[kind].editable), rec.get("editable")):
        return None
    plan = rec.get("plan")
    if not isinstance(plan, dict) or not _same(preview["apply"], plan.get("apply")):
        return None
    status = rec["status"]
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
                catalog.applied_write_count(preview["apply"], len(evidence)),
                {"rule_id": after.get("id")},
            )
        except Exception:
            return None
        if rebuilt_undo is None or not _same(rebuilt_undo, undo):
            return None
        undo = rebuilt_undo
    return {**rec, "params": params, **fields, "plan": {"apply": preview["apply"], "undo": undo}}


def _store_unavailable() -> "CardError":
    return CardError(
        503, CODE_STORE_UNAVAILABLE, "the change cards cannot be read right now; try again"
    )


def _read_store(path: Path) -> str:
    if path.stat().st_size > STORE_FILE_MAX:
        raise OSError("change cards store exceeds its size limit")
    return path.read_text(encoding="utf-8")


class CardStore:
    """Every change card this gateway holds, bounded and persisted."""

    def __init__(
        self,
        path: Path | None,
        clock: Callable[[], float] = time.time,
        *,
        key: Callable[[], bytes] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._key_source = key
        self._mac_key: bytes | None = None
        self._cards: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._loaded = path is None
        self._seq = 0
        self._written_seq = 0
        self._write_lock = asyncio.Lock()

    # ── persistence ──

    def _store_key(self) -> bytes:
        """The MAC key for the persisted records (a vault subkey). Raises on failure."""
        if self._mac_key is None:
            if self._key_source is not None:
                key = self._key_source()
            else:
                from kiro_crew.secrets import SecretVault

                assert self._path is not None
                key = SecretVault(self._path.parent).derive_subkey(STORE_MAC_PURPOSE)
            if not isinstance(key, bytes) or len(key) < 16:
                raise ValueError("change card store key unavailable")
            self._mac_key = key
        return self._mac_key

    @staticmethod
    def _mac(key: bytes, rec: dict[str, Any]) -> str:
        return hmac.new(key, canonical(rec).encode("utf-8"), hashlib.sha256).hexdigest()

    def _ensure_loaded(self) -> None:
        """Load the persisted cards once; ``503 store_unavailable`` while it cannot.

        An unreadable store stays unloaded, so nothing is flushed over it.
        """
        if self._loaded:
            return
        assert self._path is not None
        try:
            text = _read_store(self._path)
        except FileNotFoundError:
            self._loaded = True
            return
        except OSError:
            logger.warning("change cards store unreadable; will retry", exc_info=True)
            raise _store_unavailable() from None
        self._load_text(text)
        self._loaded = True

    def _dirty(self) -> None:
        self._seq += 1

    async def warm(self) -> None:
        """Load the persisted cards off the event loop (a no-op once loaded)."""
        if self._loaded or self._path is None:
            return
        path = self._path

        def _read() -> str | None:
            try:
                text = _read_store(path)
            except FileNotFoundError:
                return None
            if text:
                self._store_key()  # the vault read stays off the event loop too
            return text

        try:
            text = await asyncio.to_thread(_read)
        except Exception:
            # Stay unloaded and refuse: callers must not retry disk I/O on the loop.
            logger.warning("change cards store unavailable; will retry", exc_info=True)
            raise _store_unavailable() from None
        if self._loaded:
            return
        if text:
            self._load_text(text)
        self._loaded = True

    def _load_text(self, text: str) -> None:
        try:
            raw = json.loads(text)
        except ValueError:
            logger.warning("change cards store unreadable; starting empty")
            return
        cards = raw.get("cards") if isinstance(raw, dict) else None
        if not isinstance(cards, list):
            return
        try:
            key = self._store_key()
        except Exception:
            # The cards cannot be verified yet, which is not the same as forged:
            # they stay on disk, unloaded, until the key can be read.
            logger.warning("change cards store key unavailable; will retry", exc_info=True)
            raise _store_unavailable() from None
        dropped = max(0, len(cards) - MAX_STORED_CARDS)
        for item in cards[:MAX_STORED_CARDS]:
            rec = self._authentic(key, item)
            if rec is None:
                dropped += 1
                continue
            inflight = rec.get("inflight")
            if inflight:
                if self._idempotent_step(rec, inflight):
                    # A poll (a GET) cut off mid-answer changes nothing: admit it again.
                    rec["inflight"] = None
                else:
                    # A write that never reported back may or may not have
                    # happened: never replayed, it waits for a person to check.
                    self.mark_needs_review(rec, CODE_INTERRUPTED, _INTERRUPTED_MESSAGE)
            self._cards[rec["id"]] = rec
        if dropped:
            # The gateway wrote neither these records nor their edits.
            logger.warning("change cards store: dropped %d unverifiable card(s)", dropped)
            self._dirty()

    def _authentic(self, key: bytes, item: Any) -> dict[str, Any] | None:
        """*item* as the gateway wrote it with its derived fields rebuilt, or ``None``."""
        if not isinstance(item, dict) or not valid_card_id(item.get("id")):
            return None
        rec = dict(item)
        mac = rec.pop(_MAC_FIELD, None)
        # Only a lowercase hex SHA-256 digest can be a MAC this store wrote;
        # anything else (a non-ASCII string makes ``compare_digest`` raise) is
        # one unverifiable record, never a reason to stop loading the rest.
        if not isinstance(mac, str) or not _MAC_RE.fullmatch(mac):
            return None
        try:
            if not hmac.compare_digest(mac, self._mac(key, rec)):
                return None
            return rebuild_record(rec)
        except Exception:
            logger.debug("change card record could not be verified", exc_info=True)
            return None

    @staticmethod
    def _idempotent_step(rec: dict[str, Any], inflight: Any) -> bool:
        if not isinstance(inflight, dict) or inflight.get("op") != catalog.OP_APPLY:
            return False
        idx = inflight.get("step")
        plan = (rec.get("plan") or {}).get("apply") or []
        if not isinstance(idx, int) or not 0 <= idx < len(plan):
            return False
        step = plan[idx]
        return bool(step.get("repeat")) and str(step.get("method", "")).upper() == "GET"

    def serialize(self, key: bytes | None = None) -> str:
        """The store as written to disk: every record carries its MAC."""
        self._ensure_loaded()
        key = key if key is not None else self._store_key()
        signed = [{**rec, _MAC_FIELD: self._mac(key, rec)} for rec in self._cards.values()]
        return json.dumps({"version": 1, "cards": signed}, ensure_ascii=False)

    async def flush(self, *, strict: bool = False) -> None:
        """Write the current state if it changed.

        By default a failed write is logged and swallowed. With ``strict`` it
        raises :class:`CardError` (``503 checkpoint_failed``): the caller is
        about to act on this state being durable and must not.
        """
        if self._path is None:
            return
        target = self._seq
        if target <= self._written_seq:
            return
        async with self._write_lock:
            if target <= self._written_seq:
                return
            seq = self._seq
            try:
                key = await asyncio.to_thread(self._store_key)
                payload = self.serialize(key)
                await asyncio.to_thread(_write_locked, self._path, payload)
            except Exception:
                logger.warning("could not persist change cards", exc_info=True)
                if strict:
                    raise CardError(
                        503, CODE_CHECKPOINT_FAILED, "the change card could not be saved"
                    ) from None
                return
            self._written_seq = max(self._written_seq, seq)

    # ── reads ──

    def get(self, card_id: object) -> dict[str, Any]:
        self._ensure_loaded()
        if not valid_card_id(card_id):
            raise CardError(400, "invalid_card_id", "card_id is required")
        rec = self._cards.get(str(card_id))
        if rec is None:
            raise CardError(404, "not_found", "no such card")
        self._refresh(rec)
        return rec

    def public(self, rec: dict[str, Any]) -> dict[str, Any]:
        out = {k: copy.deepcopy(v) for k, v in rec.items() if k not in _PRIVATE_KEYS}
        if rec["status"] == STATUS_APPLYING:
            # A plan interrupted between two writes (a reload, a dropped
            # connection) continues from the step the gateway expects, with
            # the identity fields earlier steps returned for later fills. A
            # plan waiting on an approval poll resumes that same poll step.
            progress = rec.get("progress") or {}
            step = len(rec["evidence"])
            if progress.get("waiting") and isinstance(progress.get("done"), int):
                step = progress["done"]
            out["resume"] = {
                "step": step,
                "responses": copy.deepcopy(rec["evidence"][:step]),
            }
        undo_plan = (rec.get("plan") or {}).get("undo") or []
        undo_done = rec.get("undo_evidence") or []
        if rec["status"] in (STATUS_APPLIED, STATUS_PARTIAL) and 0 < len(undo_done) < len(
            undo_plan
        ):
            # The same for an Undo that stopped between two of its steps.
            out["undo_resume"] = {"step": len(undo_done), "responses": copy.deepcopy(undo_done)}
        return out

    def pending(self, slot_key: str | None) -> list[dict[str, Any]]:
        self._ensure_loaded()
        now = self._clock()
        out = []
        for rec in self._cards.values():
            self._refresh(rec)
            if slot_key is not None and rec["slot_key"] != slot_key:
                continue
            if rec.get("dismissed"):
                continue
            finished = rec.get("finished_at")
            if (
                rec["status"] in FINISHED_STATUSES
                and finished
                and now - finished > CARD_TTL_SECONDS
            ):
                continue
            out.append(self.public(rec))
        return out

    def status_for_caller(self, slot_key: str, card_id: str | None) -> dict[str, Any]:
        self._ensure_loaded()
        if card_id:
            rec = self.get(card_id)
            if rec["slot_key"] != slot_key:
                raise CardError(404, "not_found", "no such card in this conversation")
            return rec
        for rec in reversed(self._cards.values()):
            if rec["slot_key"] == slot_key:
                self._refresh(rec)
                return rec
        raise CardError(404, "not_found", "this conversation has no change card")

    # ── housekeeping ──

    def _finish(self, rec: dict[str, Any], status: str) -> None:
        rec["status"] = status
        rec["finished_at"] = self._clock()
        rec["inflight"] = None
        rec["progress"] = None
        rec["result"] = {
            "summary": catalog.result_summary(rec["kind"], rec["params"], status, rec["title"]),
        }
        self._dirty()

    def _refresh(self, rec: dict[str, Any]) -> bool:
        now = self._clock()
        changed = False
        inflight = rec.get("inflight")
        if inflight and now - float(inflight.get("started_at", 0)) > INFLIGHT_STALE_SECONDS:
            if not self._idempotent_step(rec, inflight):
                # A write that has not reported back for this long may or may
                # not have happened: never re-admitted, the same verdict a
                # restart gives it (:meth:`_load`).
                self.mark_needs_review(rec, CODE_INTERRUPTED, _INTERRUPTED_MESSAGE)
                return True
            # A stale poll (a repeat GET) changed nothing: admit it again.
            rec["inflight"] = None
            changed = True
        if rec["status"] in (STATUS_PENDING, STATUS_FAILED) and now >= rec["expires_at"]:
            if not rec.get("inflight"):
                self._finish(rec, STATUS_EXPIRED)
                return True
        if (
            rec["status"] == STATUS_APPLYING
            and not rec.get("inflight")
            and now >= rec["expires_at"]
        ):
            # An approval the person never finished (an OAuth poll) grants
            # nothing, so it fails retryably. A multi-step plan whose browser
            # vanished between two writes stays ``applying``: the next load
            # continues from the step the gateway expects.
            if any(s.get("repeat") for s in rec["plan"]["apply"]):
                self._fail_now(
                    rec,
                    catalog.OP_APPLY,
                    len(rec["evidence"]),
                    "abandoned",
                    "the approval was not finished",
                    nothing_applied=True,
                )
                return True
            if not rec["evidence"]:
                self._finish(rec, STATUS_EXPIRED)
                return True
        if changed:
            self._dirty()
        return changed

    def sweep(self) -> list[dict[str, Any]]:
        """Expire and prune. Returns the public form of every card that changed."""
        self._ensure_loaded()
        changed = []
        now = self._clock()
        for rec in list(self._cards.values()):
            status_before = rec["status"]
            self._refresh(rec)
            if rec["status"] != status_before:
                changed.append(self.public(rec))
        for cid, rec in list(self._cards.items()):
            finished = rec.get("finished_at")
            if (
                rec["status"] in FINISHED_STATUSES
                and finished
                and now - finished > FINISHED_RETAIN_SECONDS
            ):
                del self._cards[cid]
                self._dirty()
        while len(self._cards) > MAX_STORED_CARDS:
            oldest = next((cid for cid, r in self._cards.items() if self._evictable(r)), None)
            if oldest is None:
                # Every stored card still has a step to run or undo: keep them
                # all. ``propose`` refuses a new card while none can go.
                break
            del self._cards[oldest]
            self._dirty()
        return changed

    @staticmethod
    def _evictable(rec: dict[str, Any]) -> bool:
        """Whether *rec* may be pruned for capacity: settled, nothing left to run.

        A card that is applying, mid-undo, or holding a step in flight carries
        the evidence its next step (Continue, the confirmed schedule, Undo)
        needs, so it is never dropped to make room.
        """
        return (
            rec["status"] in FINISHED_STATUSES
            and not rec.get("inflight")
            and not rec.get("progress")
        )

    def retire_closed_slots(self, slot_is_open: Callable[[str], bool]) -> list[dict[str, Any]]:
        """Cancel live proposals whose conversation is gone: nobody can confirm them."""
        self._ensure_loaded()
        out = []
        for rec in self._cards.values():
            if rec["status"] == STATUS_PENDING and not rec.get("inflight"):
                if not slot_is_open(rec["slot_key"]):
                    self._finish(rec, STATUS_CANCELLED)
                    out.append(self.public(rec))
        return out

    def housekeep(
        self, slot_is_open: Callable[[str], bool]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """:meth:`retire_closed_slots` then :meth:`sweep`, undoably.

        Returns the public form of every card whose status changed, and what
        :meth:`undo_housekeeping` needs to put the store back as it was: the
        housekeeping is derived from the clock and the open conversations, so a
        caller whose checkpoint then fails restores it and the next settlement
        derives (and announces) the same transitions again.
        """
        self._ensure_loaded()
        order = list(self._cards)
        # Every record is snapshotted by value: "finished" is not "immutable"
        # here -- a retryable ``failed`` card is still expired by ``_refresh``,
        # and an aliased snapshot would compare equal to its own mutation and
        # be left out of the rollback.
        before = {cid: copy.deepcopy(rec) for cid, rec in self._cards.items()}
        changed = self.retire_closed_slots(slot_is_open) + self.sweep()
        touched = {
            cid: snap
            for cid, snap in before.items()
            if cid not in self._cards or self._cards[cid] != snap
        }
        return changed, {"order": order, "touched": touched}

    def undo_housekeeping(self, undo: dict[str, Any]) -> None:
        """Put back what :meth:`housekeep` changed or pruned (its checkpoint failed)."""
        touched: dict[str, dict[str, Any]] = undo["touched"]
        if not touched:
            return
        for cid, snap in touched.items():
            live = self._cards.get(cid)
            if live is not None:
                live.clear()  # in place: a handler may hold this record
                live.update(copy.deepcopy(snap))
        restored: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for cid in undo["order"]:
            rec = self._cards.get(cid)
            if rec is None and cid in touched:
                rec = copy.deepcopy(touched[cid])  # pruned: put it back in its place
            if rec is not None:
                restored[cid] = rec
        for cid, rec in self._cards.items():
            restored.setdefault(cid, rec)  # proposed meanwhile
        self._cards.clear()
        self._cards.update(restored)
        self._dirty()

    # ── proposal lifecycle ──

    def propose(
        self,
        *,
        slot_key: str,
        session_key: str,
        kind: str,
        params: dict[str, Any],
        reason: str,
        preview: dict[str, Any],
        before: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_loaded()
        live = sum(
            1
            for r in self._cards.values()
            if r["slot_key"] == slot_key and r["status"] in (STATUS_PENDING, STATUS_APPLYING)
        )
        if live >= MAX_LIVE_CARDS_PER_SLOT:
            raise CardError(429, "too_many_cards", "this conversation has too many open cards")
        if len(self._cards) >= MAX_STORED_CARDS and not any(
            self._evictable(r) for r in self._cards.values()
        ):
            raise CardError(429, "too_many_cards", "too many cards are still open or running")
        now = self._clock()
        rec: dict[str, Any] = {
            "id": "cc_" + secrets.token_urlsafe(12),
            "slot_key": slot_key,
            "session_key": session_key,
            "kind": kind,
            "revision": 1,
            "status": STATUS_PENDING,
            "reason": reason,
            "editable": list(catalog.KINDS[kind].editable),
            "params": copy.deepcopy(params),
            "created_at": now,
            "expires_at": now + CARD_TTL_SECONDS,
            "error": None,
            "result": None,
            "progress": None,
            "inflight": None,
            "evidence": [],
            "undo_evidence": [],
            "after": None,
            "reported_status": None,
            "dismissed": False,
            "finished_at": None,
        }
        self._apply_preview(rec, preview, before, context)
        self._cards[rec["id"]] = rec
        self._dirty()
        self.sweep()
        return rec

    def _apply_preview(
        self,
        rec: dict[str, Any],
        preview: dict[str, Any],
        before: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        rec.update(derived_fields(rec["kind"], preview, context))
        rec["plan"] = {"apply": preview["apply"], "undo": None}
        rec["undo_unavailable_reason"] = preview.get("undo_unavailable_reason")
        rec["before"] = copy.deepcopy(before)
        # Every preview input is kept (private), so a reload can rebuild the
        # displayed and executed fields from kind + params alone.
        rec["context"] = _jsonable(copy.deepcopy(context))
        # One bound over everything a card retains, nothing truncated: a value
        # Undo would restore is kept whole or the card is refused.
        if len(json.dumps(rec, default=str)) > CARD_RECORD_MAX:
            raise CardError(413, "card_too_large", "this change is too large to show as a card")

    def revise(
        self,
        rec: dict[str, Any],
        *,
        revision: object,
        params: dict[str, Any],
        preview: dict[str, Any],
        before: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_open(rec, revision)
        catalog.check_editable(rec["kind"], rec["params"], params)
        prior = copy.deepcopy(rec)
        rec["params"] = copy.deepcopy(params)
        rec["revision"] += 1
        rec["status"] = STATUS_PENDING
        rec["error"] = None
        try:
            self._apply_preview(rec, preview, before, context)
        except CardError:
            rec.clear()
            rec.update(prior)
            raise
        self._dirty()
        return rec

    def _require_open(self, rec: dict[str, Any], revision: object) -> None:
        self._refresh(rec)
        if rec["status"] not in (STATUS_PENDING, STATUS_FAILED):
            raise CardError(409, f"card_{rec['status']}", f"this card is {rec['status']}")
        if rec.get("inflight"):
            raise CardError(409, "card_busy", "this card is being applied")
        if _coerce_int(revision) != rec["revision"]:
            raise CardError(409, "stale_revision", "the card changed; reload it")

    def cancel(self, rec: dict[str, Any], revision: object) -> dict[str, Any]:
        self._require_open(rec, revision)
        self._finish(rec, STATUS_CANCELLED)
        return rec

    def dismiss(self, rec: dict[str, Any]) -> dict[str, Any]:
        if rec["status"] not in FINISHED_STATUSES:
            raise CardError(409, "card_open", "only a finished card can be dismissed")
        rec["dismissed"] = True
        self._dirty()
        return rec

    def snapshot(self, rec: dict[str, Any]) -> dict[str, Any]:
        """A copy of *rec* that :meth:`restore` can put back."""
        return copy.deepcopy(rec)

    def restore(self, rec: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Put *rec* back to *snapshot* in place: a transition that could not be saved."""
        rec.clear()
        rec.update(copy.deepcopy(snapshot))
        self._dirty()

    def discard(self, card_id: str) -> None:
        """Forget a card that was never saved (its proposal's checkpoint failed)."""
        if self._cards.pop(card_id, None) is not None:
            self._dirty()

    # ── step execution (called by the route hook) ──

    def begin_step(self, rec: dict[str, Any], *, revision: object, op: str, index: object) -> dict:
        """Admit one plan step. Returns ``{"step", "replay"}``; raises :class:`CardError`.

        ``replay`` is True when the operation already finished: the hook answers
        with the recorded result instead of running the route again.
        """
        self._refresh(rec)
        if _coerce_int(revision) != rec["revision"]:
            raise CardError(409, "stale_revision", "the card changed since it was shown")
        idx = _coerce_int(index)
        if not isinstance(idx, int) or idx < 0:
            raise CardError(400, "invalid_step", "X-Card-Step must be a step index")
        if op == catalog.OP_APPLY:
            if rec["status"] in (STATUS_APPLIED, STATUS_PARTIAL, STATUS_UNDONE):
                return {"step": None, "replay": True}
            plan = rec["plan"]["apply"]
            if rec["status"] in (STATUS_PENDING, STATUS_FAILED):
                expected = 0
            elif rec["status"] == STATUS_APPLYING:
                expected = len(rec["evidence"])
            else:
                raise CardError(409, f"card_{rec['status']}", f"this card is {rec['status']}")
        elif op == catalog.OP_UNDO:
            if rec["status"] == STATUS_UNDONE:
                return {"step": None, "replay": True}
            if rec["status"] not in (STATUS_APPLIED, STATUS_PARTIAL):
                raise CardError(409, "not_applied", "only an applied card can be undone")
            plan = rec["plan"].get("undo") or []
            if not plan:
                raise CardError(
                    409,
                    "undo_unavailable",
                    rec.get("undo_unavailable_reason") or "this change cannot be undone",
                )
            expected = len(rec["undo_evidence"])
        else:
            raise CardError(400, "invalid_op", "X-Card-Op must be apply or undo")
        if idx >= len(plan):
            raise CardError(409, "invalid_step", "no such step")
        repeat_ok = plan[idx].get("repeat") and idx == expected - 1 and op == catalog.OP_APPLY
        if idx != expected and not repeat_ok:
            raise CardError(409, "step_out_of_order", f"expected step {expected}")
        if rec.get("inflight"):
            raise CardError(409, "card_busy", "another step of this card is running")
        rec["inflight"] = {"op": op, "step": idx, "started_at": self._clock()}
        if op == catalog.OP_APPLY and idx == 0 and rec["status"] != STATUS_APPLYING:
            rec["status"] = STATUS_APPLYING
            rec["error"] = None
            rec["evidence"] = []
        rec["progress"] = {"op": op, "done": expected, "total": len(plan)}
        if repeat_ok:
            # Another poll of the same approval step: the checkpoint stays on
            # that step, so an interrupted poll resumes it rather than the next.
            rec["progress"] = {"op": op, "done": idx, "total": len(plan), "waiting": True}
        self._dirty()
        return {"step": plan[idx], "replay": False}

    def abort_step(self, rec: dict[str, Any]) -> None:
        """The request never reached a verdict (it raised): admit it again."""
        inflight = rec.get("inflight") or {}
        rec["inflight"] = None
        if inflight.get("op") == catalog.OP_APPLY and inflight.get("step") == 0:
            if rec["status"] == STATUS_APPLYING and not rec["evidence"]:
                rec["status"] = STATUS_PENDING
        self._dirty()

    def step_evidence(self, rec: dict[str, Any], op: str) -> list[dict[str, Any]]:
        return rec["evidence"] if op == catalog.OP_APPLY else rec["undo_evidence"]

    def record_success(
        self, rec: dict[str, Any], *, op: str, index: int, evidence: dict[str, Any]
    ) -> str:
        """Record one step's 2xx. Returns ``"more"``, ``"poll"`` or ``"done"``."""
        rec["inflight"] = None
        plan = rec["plan"]["apply"] if op == catalog.OP_APPLY else rec["plan"]["undo"]
        bucket = self.step_evidence(rec, op)
        repeat = bool(plan[index].get("repeat"))
        if repeat and index < len(bucket):
            bucket[index] = evidence
        else:
            bucket.append(evidence)
        if repeat:
            outcome = catalog.poll_outcome(rec["kind"], evidence)
            if outcome is None:
                rec["progress"] = {"op": op, "done": index, "total": len(plan), "waiting": True}
                self._dirty()
                return "poll"
            if outcome == "failed":
                self._fail_now(
                    rec,
                    op,
                    index,
                    "authorization_not_completed",
                    "not completed",
                    nothing_applied=True,
                )
                return "done"
        rec["progress"] = {"op": op, "done": len(bucket), "total": len(plan)}
        self._dirty()
        return "done" if len(bucket) >= len(plan) else "more"

    def record_failure(
        self, rec: dict[str, Any], *, op: str, index: int, status: int, body: Any
    ) -> None:
        code = f"http_{status}"
        message = f"the request failed ({status})"
        if isinstance(body, dict):
            if isinstance(body.get("code"), str):
                code = body["code"][:64]
            if isinstance(body.get("error"), str):
                message = body["error"][:_MESSAGE_MAX]
        self._fail_now(rec, op, index, code, message)

    def _fail_now(
        self,
        rec: dict[str, Any],
        op: str,
        index: int,
        code: str,
        message: str,
        *,
        nothing_applied: bool = False,
    ) -> None:
        rec["inflight"] = None
        rec["error"] = {"code": code, "message": message, "step": index, "op": op}
        if op == catalog.OP_UNDO:
            rec["progress"] = None
            self._dirty()
            return
        applied = catalog.applied_write_count(
            (rec.get("plan") or {}).get("apply") or [], len(rec["evidence"])
        )
        if nothing_applied or applied == 0 or index == 0:
            rec["evidence"] = []
            rec["status"] = STATUS_FAILED
            rec["progress"] = None
            rec["finished_at"] = self._clock()
            rec["result"] = {
                "summary": catalog.result_summary(
                    rec["kind"], rec["params"], STATUS_FAILED, rec["title"]
                )
            }
            self._dirty()
            return
        self._finish(rec, STATUS_PARTIAL)
        rec["error"] = {"code": code, "message": message, "step": index, "op": op}

    def complete_apply(
        self,
        rec: dict[str, Any],
        *,
        after: dict[str, Any],
        undo: list[dict[str, Any]] | None,
        undo_reason: str | None,
        status: str = STATUS_APPLIED,
    ) -> None:
        if status == STATUS_APPLIED:
            self._finish(rec, STATUS_APPLIED)
        rec["after"] = copy.deepcopy(after)
        rec["plan"]["undo"] = undo
        rec["undo_unavailable_reason"] = undo_reason if undo is None else None
        rec["undo_evidence"] = []
        self._dirty()

    def complete_undo(self, rec: dict[str, Any]) -> None:
        self._finish(rec, STATUS_UNDONE)
        rec["error"] = None

    def mark_needs_review(self, rec: dict[str, Any], code: str, message: str) -> None:
        """A step whose outcome is unknown or unrecorded: settle it for a person to check.

        ``partial`` is terminal for apply (a repeat is answered with the record,
        never run again) and the undo plan is withdrawn, because neither can be
        built on a result nobody recorded. The card says why.
        """
        self._finish(rec, STATUS_PARTIAL)
        rec["plan"] = {**(rec.get("plan") or {}), "undo": None}
        rec["undo_unavailable_reason"] = code
        rec["error"] = {"code": code, "message": message[:_MESSAGE_MAX], "op": None, "step": None}

    # ── results back to the proposing agent ──

    def take_unreported(self, slot_key: str) -> list[dict[str, Any]]:
        """Outcomes this slot's agent has not heard yet; marks them heard."""
        self._ensure_loaded()
        out = []
        for rec in self._cards.values():
            if rec["slot_key"] != slot_key or rec["status"] not in REPORTED_STATUSES:
                continue
            if rec.get("reported_status") == rec["status"]:
                continue
            rec["reported_status"] = rec["status"]
            out.append(
                {
                    "id": rec["id"],
                    "kind": rec["kind"],
                    "title": rec["title"],
                    "status": rec["status"],
                    "summary": (rec.get("result") or {}).get("summary", ""),
                    "error": (rec.get("error") or {}).get("message", ""),
                }
            )
        if out:
            self._dirty()
        return out


def _coerce_int(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 12:
        return int(value)
    return value


def _write_locked(path: Path, payload: str) -> None:
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.platform_compat import file_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            atomic_write(path, payload, restrict_to_owner=True)
    finally:
        os.close(fd)


def card_store_for(state: Any) -> CardStore:
    """The one store attached to this gateway's dashboard state."""
    store = getattr(state, "_change_card_store", None)
    if not isinstance(store, CardStore):
        from kiro_crew.config.loader import config_dir

        store = CardStore(Path(config_dir()) / STORE_FILENAME)
        state._change_card_store = store
    return store


# ── current-state readers ──


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


def _config_value(path: str) -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    obj: Any = KiroCrewConfig.load()
    for segment in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(segment)
        else:
            obj = getattr(obj, segment, None)
        if obj is None:
            return None
    return _jsonable(obj)


_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "docs" / "settings-registry.generated.json"
_FIND_LIMIT = 10
#: Most entries of a string-list setting find_setting echoes as its current value.
_FIND_LIST_ITEMS = 50
#: Id / config-key segments whose CURRENT VALUE is never echoed to the agent.
_SENSITIVE_SEGMENTS = frozenset(
    {"token", "secret", "password", "key", "credential", "credentials", "client", "id"}
)


@functools.lru_cache(maxsize=1)
def settings_registry() -> tuple[dict[str, Any], ...]:
    """The packaged Settings registry (id, label, tab, description, configKey)."""
    try:
        data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("settings registry unreadable", exc_info=True)
        return ()
    entries = data.get("settings") if isinstance(data, dict) else None
    return tuple(e for e in entries or () if isinstance(e, dict) and isinstance(e.get("id"), str))


def _registry_entry(setting_id: str) -> dict[str, Any] | None:
    return next((e for e in settings_registry() if e["id"] == setting_id), None)


def _editable_config() -> dict[str, dict[str, Any]]:
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    return _EDITABLE_CONFIG


def _dashboard_allowed(key: str) -> tuple[Any, ...]:
    if key == "default_memory_mode":
        from kiro_crew.dashboard.state import VALID_MEMORY_MODES

        return tuple(VALID_MEMORY_MODES)
    return catalog.DASHBOARD_KEYS[key]


def _dashboard_target(key: str, label: str) -> dict[str, Any]:
    return {
        "store": catalog.STORE_DASHBOARD,
        "key": key,
        "label": label,
        "allowed": list(_dashboard_allowed(key)),
    }


def spec_allowed_values(spec: dict[str, Any]) -> list[Any] | None:
    """The values a config-route setting accepts, when it is a closed set."""
    if spec.get("type") == "enum":
        return list(spec["values_fn"]()) if "values_fn" in spec else list(spec.get("values", []))
    if spec.get("type") == "bool":
        return [True, False]
    if spec.get("type") == "str" and "values" in spec:
        return list(spec["values"])
    return None


def _dashboard_list_target(key: str, label: str) -> dict[str, Any]:
    """A string-list dashboard setting, edited one item at a time."""
    return {"store": catalog.STORE_DASHBOARD, "key": key, "label": label, "list": True}


def _kirocrew_target(path: str, label: str, spec: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {"store": catalog.STORE_KIROCREW, "key": path, "label": label}
    allowed = spec_allowed_values(spec)
    if allowed is not None:
        target["allowed"] = allowed
    target["spec"] = spec
    return target


def resolve_setting(params: dict[str, Any]) -> dict[str, Any]:
    """Where a ``setting.change`` writes: ``{store, key, label, allowed?, spec?}``.

    A registry id resolves from data: a dashboard-config control the Settings page
    writes through ``PUT /api/dashboard/config``
    (``change_card_catalog.DASHBOARD_SETTINGS``), else its ``configKey`` when the
    config route accepts that key. Anything else has no write path a card may
    use. Raises :class:`kiro_crew.change_card_catalog.CardCatalogError`.
    """
    editable = _editable_config()
    err = catalog.CardCatalogError
    if "setting_id" in params:
        sid = params["setting_id"]
        entry = _registry_entry(sid)
        if entry is None:
            raise err("unknown_setting", f"no setting '{sid[:80]}'; search with find_setting")
        label = str(entry.get("label") or sid)
        if sid in catalog.DASHBOARD_SETTINGS:
            return _dashboard_target(catalog.DASHBOARD_SETTINGS[sid][0], label)
        if sid in catalog.DASHBOARD_LIST_SETTINGS:
            return _dashboard_list_target(catalog.DASHBOARD_LIST_SETTINGS[sid][0], label)
        key = entry.get("configKey")
        if isinstance(key, str) and key in editable:
            return _kirocrew_target(key, label, editable[key])
        if isinstance(key, str) and key.startswith("dashboard."):
            suffix = key.split(".", 1)[1]
            if suffix in catalog.DASHBOARD_KEYS:
                return _dashboard_target(suffix, label)
        raise err(
            "no_write_path",
            f"'{label}' cannot be changed by a card; offer the guide's settings.show "
            f"action ('{sid}') so the user changes it on the Settings page",
        )
    path = params["path"]
    if path in editable:
        return _kirocrew_target(path, path, editable[path])
    if path.startswith("dashboard.") and path.split(".", 1)[1] in catalog.DASHBOARD_KEYS:
        return _dashboard_target(path.split(".", 1)[1], path)
    if path.startswith("dashboard.") and path.split(".", 1)[1] in catalog.DASHBOARD_LIST_KEYS:
        return _dashboard_list_target(path.split(".", 1)[1], path)
    raise err("setting_not_editable", f"'{path}' is not a dashboard setting")


def read_setting_value(target: dict[str, Any]) -> Any:
    """The current value from the same store the card writes."""
    if target["store"] == catalog.STORE_DASHBOARD:
        return _config_value(f"dashboard.{target['key']}")
    return _config_value(target["key"])


def check_target_params(target: dict[str, Any], params: dict[str, Any]) -> None:
    """Refuse a ``value`` on a string-list setting and an ``op`` on any other."""
    err = catalog.CardCatalogError
    if target.get("list"):
        if "op" not in params:
            raise err(
                "list_setting",
                f"'{target['label']}' is a list; pass 'op' ('add' or 'remove') and one 'item'",
            )
        return
    if "op" in params:
        raise err("not_a_list", f"'{target['label']}' is not a list; pass 'value' instead")
    check_target_value(target, params["value"])


def check_target_value(target: dict[str, Any], value: Any) -> None:
    allowed = target.get("allowed")
    if allowed is not None and not catalog.value_allowed(value, allowed):
        raise catalog.CardCatalogError("invalid_value", f"value must be one of {allowed}")
    if "spec" in target:
        check_setting_value(target["spec"], value)


def _stem(word: str) -> str:
    for suffix, repl in (("ies", "y"), ("ing", ""), ("er", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + repl
    return word


def _value_withheld(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('id', '')}.{entry.get('configKey', '')}".lower()
    return bool(set(re.split(r"[-._%:]+", text)) & _SENSITIVE_SEGMENTS)


def template_auto_approved_servers(template: str) -> list[str]:
    """The user-installed MCP servers *template* runs without asking, sorted.

    A new crewmate is bound to the template, so it inherits each bare
    ``@server`` grant in the template's ``allowedTools`` (already narrowed by the
    governance ceiling when the spec was written). Kiro Crew's own managed
    servers are left out: they are the product's own tools, not a connection
    the user installed. Unreadable specs report none.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    try:
        spec = json.loads((kiro_agents_dir() / f"{template}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    allowed = spec.get("allowedTools") if isinstance(spec, dict) else None
    servers = {
        ref[1:]
        for ref in allowed or ()
        if isinstance(ref, str) and ref.startswith("@") and "/" not in ref and len(ref) > 1
    }
    return sorted(s for s in servers if not s.startswith("kirocrew"))


def find_settings(query: str) -> list[dict[str, Any]]:
    """Up to ten registry matches for *query*, best first. Never a credential value."""
    words = [_stem(w) for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    entries = settings_registry()
    hays = []
    for entry in entries:
        title = f"{entry['id']} {entry.get('label', '')}".lower()
        hay = f"{title} {entry.get('description', '')} {entry.get('configKey', '')}".lower()
        hays.append((title, hay))
    # How many entries each word reaches: a tie goes to the entry whose words are
    # rarer, so "verbosity" outranks a "reply" that a dozen channel settings share.
    reach = {w: sum(1 for _t, hay in hays if w in hay) or 1 for w in words}
    for entry, (title, hay) in zip(entries, hays):
        hit = [w for w in words if w in hay]
        score = sum(3 if w in title else 1 for w in hit)
        if entry["id"] == query.strip():
            score += 100
        if score:
            scored.append((score, sum(1 / reach[w] for w in hit), entry))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]["id"]))
    out = []
    for _score, _rarity, entry in scored[:_FIND_LIMIT]:
        row: dict[str, Any] = {
            "setting_id": entry["id"],
            "label": entry.get("label", ""),
            "description": str(entry.get("description", ""))[:400],
            "tab": entry.get("tab", ""),
            "writable": False,
            "current_value": None,
        }
        try:
            target: dict[str, Any] | None = resolve_setting({"setting_id": entry["id"]})
        except catalog.CardCatalogError:
            target = None
        if target is not None:
            row["writable"] = True
            if target.get("store") == catalog.STORE_KIROCREW and requires_restart(
                str(target.get("key") or "")
            ):
                # Most settings apply live; only a schema-marked one waits for a
                # restart, so the agent names a restart only when this says so.
                row["restart_required"] = True
            if target.get("allowed") is not None:
                row["allowed_values"] = target["allowed"]
            if target.get("list"):
                row["value_type"] = "string_list"
                row["ops"] = list(catalog._LIST_OPS)
            if not _value_withheld(entry):
                value = read_setting_value(target)
                if target.get("list"):
                    row["current_value"] = catalog.string_items(value)[:_FIND_LIST_ITEMS]
                else:
                    row["current_value"] = None if isinstance(value, (dict, list)) else value
        out.append(row)
    return out


def localize_setting_rows(rows: list[dict[str, Any]], ui_lang: str) -> list[dict[str, Any]]:
    """*rows* from :func:`find_settings` with labels as the user's screen shows them.

    The registry is English. Each row the dashboard location index holds gets
    its on-screen ``label`` and Settings ``path`` in *ui_lang* (English when
    that is blank or not shipped), plus ``label_locale``; a row the index does
    not hold keeps its registry label. Never raises.
    """
    from kiro_crew.ui_index import setting_labels

    try:
        shown = setting_labels([str(r.get("setting_id") or "") for r in rows], ui_lang or None)
    except Exception:  # a label lookup must never fail the search
        logger.debug("setting label lookup failed", exc_info=True)
        return rows
    locale = ui_lang or "en"
    out = []
    for row in rows:
        hit = shown.get(str(row.get("setting_id") or ""))
        if hit is None:
            out.append(row)
            continue
        out.append({**row, "label": hit["label"], "path": hit["path"], "label_locale": locale})
    return out


# ── settings diagnosis ──

#: Most ``Dashboard: ...`` history lines one diagnosis returns, newest first.
DIAGNOSE_RECENT_MAX = 50
#: How far back the diagnosis reads Global memory history for those lines.
_DIAGNOSE_HISTORY_DAYS = 90
#: A single value longer than this (as JSON) is summarized instead of echoed.
_DIAGNOSE_VALUE_MAX = 400
#: The whole diagnosis, as JSON, stays under this many characters.
DIAGNOSE_OUTPUT_MAX = 48_000
_DASHBOARD_LINE = "Dashboard: "
_KIROCREW_STORE = "kirocrew"
_DASHBOARD_PREFIX = "dashboard."


def _key_withheld(key: str, setting_id: str = "") -> bool:
    """The :func:`find_settings` credential test, applied to one config key path.

    A config path also splits on ``_`` (``slack.bot_token``), because a dotted
    path has no registry id whose hyphens would otherwise carry the split.
    """
    if _value_withheld({"id": setting_id, "configKey": key}):
        return True
    return _value_withheld({"id": "", "configKey": key.replace("_", ".")})


def _scrub(value: Any) -> Any:
    """*value* with every credential-like nested key reduced to ``{set: bool}``
    and every string run through the credential redactor."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _key_withheld(str(k)):
                out[str(k)] = {"set": v not in (None, "", [], {})}
            else:
                out[str(k)] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and value:
        from kiro_crew.platform import redact_via_context

        return redact_via_context(value)
    return value


def _bounded(value: Any) -> Any:
    """*value*, or a short summary of it when its JSON is longer than the cap."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= _DIAGNOSE_VALUE_MAX:
        return value
    summary: dict[str, Any] = {"truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        summary["size"] = len(value)
        summary["keys"] = sorted(str(k) for k in value)[:20]
    elif isinstance(value, list):
        summary["size"] = len(value)
    else:
        summary["preview"] = text[: _DIAGNOSE_VALUE_MAX // 2]
    return summary


def _flatten_like(default: Any, current: Any, prefix: str, out: dict[str, tuple[Any, Any]]) -> None:
    """Pair leaves of *current* with *default*, descending only where the default
    itself is a non-empty mapping; an empty default (a roster, a free-form map)
    stays one leaf so a whole collection compares as one setting."""
    if isinstance(default, dict) and default:
        cur = current if isinstance(current, dict) else {}
        for key in sorted(set(default) | set(cur), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten_like(default.get(key), cur.get(key), path, out)
        return
    out[prefix] = (default, current)


def _registry_by_config_key() -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for entry in settings_registry():
        key = entry.get("configKey")
        if isinstance(key, str) and key:
            by_key.setdefault(key, entry)
    for sid, (dkey, _allowed) in catalog.DASHBOARD_SETTINGS.items():
        dash_entry = _registry_entry(sid)
        if dash_entry is not None:
            by_key.setdefault(f"{_DASHBOARD_PREFIX}{dkey}", dash_entry)
    return by_key


def non_default_settings() -> list[dict[str, Any]]:
    """Every config and dashboard-config leaf whose value differs from its default.

    The defaults are the dataclass defaults (``KiroCrewConfig()``), the same ones
    ``config.json`` falls back to, compared with the loaded configuration.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    pairs: dict[str, tuple[Any, Any]] = {}
    _flatten_like(
        _jsonable(KiroCrewConfig().to_dict()), _jsonable(KiroCrewConfig.load().to_dict()), "", pairs
    )
    registry = _registry_by_config_key()
    rows = []
    for key, (default, current) in pairs.items():
        if default == current:
            continue
        entry = registry.get(key)
        row: dict[str, Any] = {
            "key": key,
            "store": (
                catalog.STORE_DASHBOARD if key.startswith(_DASHBOARD_PREFIX) else _KIROCREW_STORE
            ),
        }
        if entry is not None:
            row["setting_id"] = entry["id"]
            row["label"] = str(entry.get("label") or "")
        if _key_withheld(key, row.get("setting_id", "")):
            row["current"] = {"set": current not in (None, "", [], {})}
            row["default"] = {"set": default not in (None, "", [], {})}
        else:
            row["current"] = _bounded(_scrub(current))
            row["default"] = _bounded(_scrub(default))
        rows.append(row)
    return rows


def recent_dashboard_changes(limit: int = DIAGNOSE_RECENT_MAX) -> list[dict[str, Any]]:
    """The newest ``Dashboard: ...`` lines in Global memory history, newest first.

    Those are the lines :func:`kiro_crew.dashboard.handlers.change_cards._record_memory`
    writes for every owner mutation through a settings route, by hand or by card.
    """
    from datetime import date, timedelta

    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    since = date.today() - timedelta(days=_DIAGNOSE_HISTORY_DAYS)
    # The operator's own Global history, deliberately: that is where the
    # "Dashboard: ..." lines are written, whichever member is asking.
    memory = ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)
    entries = memory.read_history_entries(since=since)
    found: list[dict[str, Any]] = []
    for entry in reversed(entries or []):
        day = str(entry.get("date") or "")
        lines: list[dict[str, Any]] = []
        time_label = ""
        for raw in str(entry.get("content") or "").splitlines():
            line = raw.strip()
            if line.startswith("#### "):
                time_label = line[5:].strip()
            elif line.startswith(_DASHBOARD_LINE):
                lines.append({"date": day, "time": time_label, "line": line[:_MESSAGE_MAX]})
        found.extend(reversed(lines))
        if len(found) >= limit:
            break
    return found[: max(0, min(limit, DIAGNOSE_RECENT_MAX))]


#: Probe statuses, most severe first (``diagnose_probes`` sorts the same way).
_FINDING_RANK = {"problem": 0, "warn": 1, "unknown": 2, "ok": 3}

#: Words a diagnosis topic carries that name no setting ("why did my ... change").
_TOPIC_STOPWORDS = frozenset(
    "the and for but not you your our are was were has have had did does doing done "
    "why what when where which who how this that these those with from into onto "
    "about after before again just still can cant could would should will wont "
    "any all some its it's mine there their them then than also very much more "
    "now new old get got set setting settings change changed changes changing "
    "option options value values thing things work working broken stop stopped "
    "anymore suddenly someone something happen happened".split()
)


def _topic_tokens(topic: str) -> list[str]:
    """The meaningful, stemmed words of *topic*: lower-cased, split on anything
    that is not a letter or digit, stopwords and words under three letters dropped."""
    words = re.findall(r"[a-z0-9]+", topic.lower())
    out: list[str] = []
    for word in words:
        if len(word) < 3 or word in _TOPIC_STOPWORDS:
            continue
        stem = _stem(word)
        if stem not in out:
            out.append(stem)
    return out


def _token_hits(tokens: list[str], *texts: Any) -> int:
    """How many of *tokens* occur in any of *texts*, case-insensitively."""
    hay = " ".join(str(t) for t in texts if t).lower()
    return sum(1 for tok in tokens if tok in hay)


def _row_hits(tokens: list[str], row: dict[str, Any]) -> int:
    entry = _registry_entry(row["setting_id"]) if row.get("setting_id") else None
    extra = (entry.get("description"), entry.get("tab")) if entry else ()
    return _token_hits(tokens, row["key"], row.get("setting_id"), row.get("label"), *extra)


def _finding_hits(tokens: list[str], item: dict[str, Any]) -> int:
    evidence = item.get("evidence")
    sid = evidence.get("setting_id") if isinstance(evidence, dict) else None
    entry = _registry_entry(sid) if isinstance(sid, str) else None
    label = entry.get("label") if entry else None
    return _token_hits(tokens, item.get("id"), item.get("summary"), sid, label)


def _ranked(items: list[dict[str, Any]], hits: Any) -> list[dict[str, Any]]:
    """The items with at least one token hit, most hits first (stable otherwise)."""
    scored = [(hits(item), i, item) for i, item in enumerate(items)]
    return [item for n, _i, item in sorted(scored, key=lambda s: (-s[0], s[1])) if n]


def diagnose_settings(
    topic: str = "", app: Any = None, *, include_history: bool = True
) -> dict[str, Any]:
    """``{findings, non_default, recent_changes, truncated[, topic_matched]}``.

    ``findings`` come from :mod:`kiro_crew.diagnose_probes`, problems first.
    *topic* keeps rows and ``ok`` findings that match any of its meaningful
    words; a non-``ok`` finding is always kept, and a topic that matches
    nothing returns the unfiltered result with ``topic_matched: false``. With
    ``include_history`` false Global memory history is not read. A
    credential-like setting reports only whether it is set; the JSON is cut to
    :data:`DIAGNOSE_OUTPUT_MAX` characters and ``truncated`` says so.
    """
    from kiro_crew.diagnose_probes import run_probes

    topic = (topic or "").strip()
    findings = run_probes(app, topic)
    rows = non_default_settings()
    changes = recent_dashboard_changes() if include_history else []
    result: dict[str, Any] = {
        "findings": findings,
        "non_default": rows,
        "recent_changes": changes,
        "truncated": False,
    }
    tokens = _topic_tokens(topic) if topic else []
    if topic:
        f_rows = _ranked(rows, lambda r: _row_hits(tokens, r))
        f_changes = _ranked(changes, lambda c: _token_hits(tokens, c["line"]))
        matched_findings = _ranked(findings, lambda f: _finding_hits(tokens, f))
        matched = bool(f_rows or f_changes or matched_findings)
        result["topic_matched"] = matched
        if matched:
            kept = {id(f) for f in matched_findings}
            others = [f for f in findings if id(f) not in kept and f.get("status") != "ok"]
            result["findings"] = sorted(
                matched_findings + others,
                key=lambda f: _FINDING_RANK.get(str(f.get("status")), 9),
            )
            result["non_default"] = f_rows
            result["recent_changes"] = f_changes
    while len(json.dumps(result, ensure_ascii=False, default=str)) > DIAGNOSE_OUTPUT_MAX:
        result["truncated"] = True
        if result["non_default"]:
            result["non_default"].pop()
        elif result["recent_changes"]:
            result["recent_changes"].pop()
        elif result["findings"]:
            result["findings"].pop()
        else:
            break
    return result


def setting_spec(path: str) -> dict[str, Any]:
    """The editable-config entry for *path*, as the settings route enforces it."""
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    spec = _EDITABLE_CONFIG.get(path)
    if not spec:
        raise catalog.CardCatalogError(
            "setting_not_editable", f"'{path}' is not a dashboard setting"
        )
    return spec


def check_setting_value(spec: dict[str, Any], value: Any) -> None:
    """The same shape checks the settings route runs, so a card fails up front."""
    kind = spec.get("type")
    err = catalog.CardCatalogError
    if kind == "enum":
        allowed = list(spec["values_fn"]()) if "values_fn" in spec else spec.get("values", [])
        if value not in allowed:
            raise err("invalid_value", f"value must be one of {allowed}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise err("invalid_value", "value must be true or false")
    elif kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise err("invalid_value", "value must be a number")
        lo, hi = spec.get("min", 0), spec.get("max", 999999)
        if value < lo or value > hi:
            raise err("invalid_value", f"value must be between {lo} and {hi}")
    elif kind == "str":
        if not isinstance(value, str) or len(value) > spec.get("max_len", 256):
            raise err("invalid_value", "value must be a short string")
        if "values" in spec and value not in spec["values"]:
            raise err("invalid_value", f"value must be one of {spec['values']}")
        pattern = spec.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            raise err("invalid_value", "value has an invalid format")
    elif kind == "dict":
        if not isinstance(value, dict) or set(value) != set(spec.get("keys", {})):
            raise err("invalid_value", "value must be a record with every declared key")


def _crewmate_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _cron_job_fields(job: Any) -> dict[str, Any]:
    schedule = getattr(job, "schedule", None)
    return {
        "name": getattr(job, "name", None),
        "message": getattr(job, "message", None),
        "cron_expr": getattr(schedule, "cron_expr", None),
        "timezone": getattr(job, "timezone", "") or "",
    }


#: A schedule's configuration a person sets or edits -- never what running it
#: updates (last result, counters, retry stamps). A change to any of these
#: after a card created the schedule makes that card's Undo stale.
_CRON_CONFIG_FIELDS = (
    "name",
    "message",
    "schedule",
    "channel",
    "thread_ts",
    "user_paused",
    "delete_after_run",
    "context_enabled",
    "agent_id",
    "member_id",
    "memory_store",
    "approval_mode",
    "silent",
    "skip_dates",
    "timezone",
    "persistent_session",
    "minimal_context",
    "hide_in_chat",
    "folder_id",
    "model",
    "chat_folder_id",
    "env",
    "timeout_secs",
    "strict_schedule",
    "script",
    "command",
    "timeout",
    "secret_env",
)


def _cron_job_revision(job: Any) -> str:
    return _revision_digest({k: getattr(job, k, None) for k in _CRON_CONFIG_FIELDS})


def cron_job_revision(job: Any) -> str:
    """Public fingerprint of a cron job's schedule config, matching the digest
    :func:`read_state` records for a ``schedule.create`` card's ``after``.

    The card-Undo compare-and-delete (``api_cron_delete``) passes this to the
    cron store so the live job is fingerprinted the same way the card snapshot
    was; identical digests mean the schedule still holds what the card made.
    """
    return _cron_job_revision(job)


def next_run_at(cron_expr: str, timezone: str | None) -> float | None:
    from kiro_crew.cron_service.model import CronJob, CronSchedule
    from kiro_crew.cron_service.schedule import compute_next_run_ts

    job = CronJob(
        id="card-preview",
        name="preview",
        message="",
        schedule=CronSchedule(kind="cron", cron_expr=cron_expr),
    )
    job.timezone = timezone or ""
    try:
        return compute_next_run_ts(job)
    except Exception:
        return None


def one_shot_context(at: str, timezone: str) -> dict[str, Any]:
    """Resolve a one-shot card's local ``at`` to the instant ``POST /api/crons`` takes.

    The zone is the card's own ``timezone``, or the CONFIGURED one when the card
    names none -- the same default ``parse_time_string`` reads a wall clock in,
    so the card and the route agree on the instant. A time already gone is
    refused here, before a card is shown, exactly as the route would refuse it
    (``at_in_past``). ``once`` and ``run_at_local`` are display facts the card
    record carries so the dashboard can say "Runs once" with the local time.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from kiro_crew.cron_service.schedule import (
        _wall_clock_resolution_error,
        get_local_tz,
        is_valid_timezone,
    )

    tz = timezone or get_local_tz()[0]
    if not is_valid_timezone(tz):
        raise catalog.CardCatalogError("invalid_timezone", f"unknown timezone '{tz[:64]}'")
    zone = ZoneInfo(tz)
    local = datetime.fromisoformat(at).replace(tzinfo=zone)
    skipped = _wall_clock_resolution_error(local, zone, tz)
    if skipped is not None:
        raise catalog.CardCatalogError("at_not_in_zone", skipped.removeprefix("Error: "))
    at_ts = local.timestamp()
    if at_ts <= time.time():
        raise catalog.CardCatalogError("at_in_past", "that time has already passed")
    return {
        "next_run_at": at_ts,
        "timezone": tz,
        "once": True,
        "run_at_local": at,
        "at_ts": at_ts,
    }


def _mcp_global_entry(name: str) -> tuple[bool, dict[str, Any] | None]:
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    try:
        data = json.loads(mcp_handlers._GLOBAL_MCP_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        data = {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    try:
        key = mcp_handlers._config_key_for(servers, name)
    except Exception:
        key = None
    if key is not None and isinstance(servers.get(key), dict):
        return True, servers[key]
    from kiro_crew.mcp_discovery import list_servers

    known = {s.name for s in list_servers()}
    return (name in known), None


def _mcp_exists(name: str) -> bool:
    from kiro_crew.dashboard.handlers.mcp import _find_server_spec_anywhere

    return _find_server_spec_anywhere(name) is not None


#: Recorded for a scope whose file exists but cannot be read: an unknown is
#: never taken as "not there".
_MCP_SCOPE_UNREADABLE = "unreadable"


def _mcp_value_key() -> bytes:
    from kiro_crew.config.loader import config_dir
    from kiro_crew.secrets import SecretVault

    return SecretVault(config_dir()).derive_subkey("change-card-mcp-fingerprint")


def _mcp_scopes(name: str) -> dict[str, str]:
    """Each scope an uninstall would delete *name* from, with a fingerprint of
    the definition there.

    ``env`` and ``headers`` values enter only as keyed digests under a vault
    subkey, so an edited value changes the fingerprint while the stored
    fingerprint cannot be tested against a guessed credential. A scope without
    the name is absent; one that cannot be read is :data:`_MCP_SCOPE_UNREADABLE`.
    """
    import hashlib
    import hmac

    from kiro_crew.dashboard.handlers.mcp import _config_entry_for, _uninstall_scope_files

    key: bytes | None = None
    out: dict[str, str] = {}
    for label, path in _uninstall_scope_files():
        if label == "agent" or label.endswith("Agent"):
            # A rendered agent file is Kiro Crew's own merge output, rewritten
            # on every rebuild; the uninstall strips it with the scopes above.
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            out[label] = _MCP_SCOPE_UNREADABLE
            continue
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            out[label] = _MCP_SCOPE_UNREADABLE
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        entry = _config_entry_for(servers, name)
        if not isinstance(entry, dict):
            continue
        shape: dict[str, Any] = {}
        for k, v in entry.items():
            if k in ("env", "headers") and isinstance(v, dict):
                key = key if key is not None else _mcp_value_key()
                shape[k] = {
                    str(ek): hmac.new(
                        key, canonical(ev).encode("utf-8"), hashlib.sha256
                    ).hexdigest()
                    for ek, ev in v.items()
                }
            else:
                shape[k] = v
        out[label] = _revision_digest(shape)
    return out


def _mcp_undo_names(rec: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for s in (rec.get("plan") or {}).get("undo") or []:
        for change in (s.get("body") or {}).get("changes") or []:
            if isinstance(change, dict) and isinstance(change.get("name"), str):
                names.append(change["name"])
    return names


def after_shows_the_write(
    kind: str,
    params: dict[str, Any],
    after: Any,
    evidence: list[dict[str, Any]] | None = None,
) -> bool:
    """Whether the post-apply read still holds what this card wrote.

    Undo's baseline is the value the card WROTE, never whatever the read found:
    a save made in another tab between the write and the read would otherwise
    become the baseline, and Undo would then pass its check and erase it. For
    the kinds that update values in place, the fields this card set must read
    back as it set them; a create is identified by its own evidence instead.
    Applies only to what Undo writes back: a field the card did not touch is
    never restored, so another edit there is left alone.
    """
    if not isinstance(after, dict):
        return False
    p = params
    ev = evidence or []

    def same(a: Any, b: Any) -> bool:
        return canonical(_jsonable(a)) == canonical(_jsonable(b))

    if kind == catalog.KIND_SETTING_CHANGE:
        value = after.get("value")
        if "value" in p:
            return same(value, p["value"])
        items = value if isinstance(value, list) else []
        present = p.get("item") in items
        return present if p.get("op") == catalog.LIST_OP_ADD else not present
    if kind in (
        catalog.KIND_SCHEDULE_UPDATE,
        catalog.KIND_CREWMATE_UPDATE,
        catalog.KIND_TEMPLATE_UPDATE,
    ):
        fields = after.get("fields")
        if not isinstance(fields, dict):
            return False
        return all(same(fields.get(k), v) for k, v in (p.get("fields") or {}).items())
    if kind == catalog.KIND_CREWMATE_CREATE:
        # Undo deletes the crewmate this card made. The only safe baseline is the
        # IMMUTABLE member_id the card's own create returned (step 0's evidence):
        # a name is reusable, so an owner who deleted and recreated the same name
        # between apply and Undo leaves a different crewmate wearing it. Offer the
        # Undo only while the live member_id still equals the one we created; a
        # replacement reads a different id (or none) and the Undo is withheld so
        # it cannot delete the replacement and its crew log.
        created = ev[0].get("member_id") if ev else None
        if not isinstance(created, str) or not created:
            return False
        return bool(after.get("exists")) and after.get("member_id") == created
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        # The write landed at the revision the PUT returned (step 1's evidence);
        # Undo inverts that revision. If the post-apply read is at a different
        # revision, another tab wrote after us and inverting would erase it, so
        # there is no Undo.
        written = ev[1].get("revision") if len(ev) > 1 else None
        return bool(written) and after.get("revision") == written
    if kind == catalog.KIND_MCP_TOGGLE:
        return after.get("enabled") is bool(p.get("enabled"))
    if kind == catalog.KIND_TRUST_APP:
        return after.get("trusted") is True
    if kind == catalog.KIND_DENIED_COMMAND and p.get("action") == "toggle":
        return after.get("enabled") is bool(p.get("enabled"))
    return True


def undo_snapshot_changed(kind: str, current: Any, after: Any) -> bool:
    """Whether the state an Undo is about to reverse differs from what the card made.

    For an MCP add or install, the comparison is per server and per scope: a
    scope already emptied (an earlier Undo got that far) is done; a scope still
    holding the server must hold what the card left there, and an unreadable or
    newly added scope is a change.
    """
    if kind not in (catalog.KIND_MCP_INSTALL, catalog.KIND_MCP_ADD_CUSTOM):
        return canonical(current) != canonical(after)
    cur, then = current or {}, after or {}

    def changed(now: dict[str, str], was: dict[str, str]) -> bool:
        return any(
            digest == _MCP_SCOPE_UNREADABLE or was.get(label) != digest
            for label, digest in now.items()
        )

    if kind == catalog.KIND_MCP_INSTALL:
        return changed(cur.get("scopes") or {}, then.get("scopes") or {})
    now_all, then_all = cur.get("scopes") or {}, then.get("scopes") or {}
    return any(changed(now_all.get(n) or {}, then_all.get(n) or {}) for n in now_all)


#: The spec fields a person edits after a crewmate exists (goal, prompt,
#: capabilities). A change to any of them makes an earlier card's Undo stale.
_CREWMATE_SPEC_FIELDS = (
    "description",
    "prompt",
    "tools",
    "allowedTools",
    "mcpServers",
    "resources",
    "model",
)


def _revision_digest(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical(_jsonable(value)).encode("utf-8")).hexdigest()[:16]


def _crewmate_revision(agent: Any) -> str:
    """A fingerprint of a crewmate's config entry and its spec's editable fields."""
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    specs = _agent_detail_candidates(getattr(agent, "kiro_agent", "") or "")
    spec = specs[0][1] if len(specs) == 1 and isinstance(specs[0][1], dict) else {}
    return _revision_digest(
        {"config": agent, "spec": {k: spec.get(k) for k in _CREWMATE_SPEC_FIELDS}}
    )


def _secret_revision(vault: Any, name: str) -> str:
    """A fingerprint of the stored entry: it changes when the value is replaced.

    The entry is ciphertext under a fresh nonce, so its digest says nothing
    about the value itself.
    """
    from kiro_crew.secrets.vault import entry_revision

    entry = vault._load_entries().get(name)  # noqa: SLF001 - read-only fingerprint
    return entry_revision(entry) if isinstance(entry, dict) else ""


def _template_state_with_skills(params: dict[str, Any], state: Any) -> dict[str, Any]:
    """A template's snapshot when the card changes its skills.

    ``skills`` is not a stored spec field: the template editor projects it from
    the spec's ``resources``, so the snapshot reads it the same way (the prior
    mapping is what Undo restores) and fingerprints ``resources`` so any later
    skill edit makes the card stale.
    """
    from kiro_crew.dashboard.handlers._shared import agent_skill_views
    from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

    candidates = _agent_detail_candidates(params["template"])
    if len(candidates) != 1:
        return {"exists": False, "ambiguous": len(candidates) > 1}
    path, spec = candidates[0]
    spec = spec if isinstance(spec, dict) else {}
    fields = {k: _jsonable(spec.get(k)) for k in params["fields"]}
    fields["skills"] = agent_skill_views(spec, path, state)[0]
    return {
        "exists": True,
        "fields": fields,
        "resources": _revision_digest(spec.get("resources")),
    }


def _sync_read_state(
    kind: str, params: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    p = params
    ev0 = evidence[0] if evidence else {}
    if kind == catalog.KIND_SETTING_CHANGE:
        target = resolve_setting(p)
        return {
            "store": target["store"],
            "key": target["key"],
            "label": target["label"],
            "value": read_setting_value(target),
        }
    if kind in (catalog.KIND_CREWMATE_CREATE, catalog.KIND_CREWMATE_UPDATE):
        from kiro_crew.config.loader import KiroCrewConfig

        agents = KiroCrewConfig.load().agents
        if kind == catalog.KIND_CREWMATE_CREATE:
            key = ev0.get("name") if isinstance(ev0.get("name"), str) else None
            if key:
                agent = agents.get(key)
                return {
                    "exists": agent is not None,
                    "member_id": getattr(agent, "member_id", "") if agent else "",
                    "revision": _crewmate_revision(agent) if agent else "",
                }
            taken = p["name"] in agents or _crewmate_key(p["name"]) in agents
            taken = taken or any(
                getattr(a, "display_name", "") == p["name"] for a in agents.values()
            )
            return {
                "exists": bool(taken),
                "inherited_auto_approve": template_auto_approved_servers(
                    catalog.DEFAULT_CREWMATE_TEMPLATE
                ),
            }
        agent = agents.get(p["name"])
        if agent is None:
            return {"exists": False}
        return {
            "exists": True,
            # The immutable identity: a crewmate deleted and re-made under the
            # same name reads as a change, so the old card's Undo is refused.
            "member_id": getattr(agent, "member_id", "") or "",
            "fields": {k: _jsonable(getattr(agent, k, None)) for k in p["fields"]},
        }
    if kind == catalog.KIND_TEMPLATE_UPDATE:
        from kiro_crew.dashboard.handlers.agents import _agent_detail_candidates

        candidates = _agent_detail_candidates(p["template"])
        if len(candidates) != 1:
            return {"exists": False, "ambiguous": len(candidates) > 1}
        spec = candidates[0][1] if isinstance(candidates[0][1], dict) else {}
        return {"exists": True, "fields": {k: _jsonable(spec.get(k)) for k in p["fields"]}}
    if kind == catalog.KIND_MCP_INSTALL:
        from kiro_crew.dashboard.handlers.mcp_discover import _derive_install_name

        name = ev0.get("name") or _derive_install_name(p["id"])
        exists = bool(name) and _mcp_exists(name)
        return {"name": name, "exists": exists, "scopes": _mcp_scopes(name) if name else {}}
    if kind == catalog.KIND_MCP_ADD_CUSTOM:
        return {
            "exists": {name: _mcp_exists(name) for name in p["servers"]},
            "scopes": {name: _mcp_scopes(name) for name in p["servers"]},
        }
    if kind == catalog.KIND_MCP_TOGGLE:
        server = p.get("server") or p["name"]
        exists, entry = _mcp_global_entry(server)
        entry = entry or {}
        if "tool" in p:
            raw_disabled = entry.get("disabledTools")
            disabled = raw_disabled if isinstance(raw_disabled, list) else []
            return {"exists": exists, "enabled": p["tool"] not in disabled}
        return {"exists": exists, "enabled": not bool(entry.get("disabled"))}
    if kind == catalog.KIND_SECRET_SAVE:
        from kiro_crew.config.loader import config_dir
        from kiro_crew.secrets import SecretVault

        vault = SecretVault(config_dir())
        if p["name"] not in set(vault.list_names()):
            return {"exists": False}
        return {"exists": True, "revision": _secret_revision(vault, p["name"])}
    if kind == catalog.KIND_TRUST_APP:
        from kiro_crew.dashboard.handlers.security import build_trusted_apps_snapshot

        snap = build_trusted_apps_snapshot()
        stored = set(snap.get("apps") or []) | set(snap.get("ineffective") or [])
        return {"trusted": p["name"] in stored}
    if kind == catalog.KIND_DENIED_COMMAND:
        from kiro_crew.dashboard.handlers.security import build_denied_commands_snapshot

        rules = build_denied_commands_snapshot().get("user_added") or []
        if p["action"] == "add":
            hit = next((r for r in rules if r.get("pattern") == p["pattern"]), None)
            return {"exists": hit is not None, **({"id": hit["id"]} if hit else {})}
        hit = next((r for r in rules if r.get("id") == p["id"]), None)
        if hit is None:
            return {"exists": False}
        return {"exists": True, "enabled": bool(hit.get("enabled")), "pattern": hit.get("pattern")}
    raise catalog.CardCatalogError("unknown_kind", kind)


def _capability_service(app: Any) -> Any:
    from kiro_crew.agent_capabilities import CapabilityService
    from kiro_crew.dashboard.handlers import agent_capabilities as handler

    service = app.get(handler._SERVICE) if app is not None else None
    if service is None:
        service = CapabilityService(handler._catalog, handler._connections)
        if app is not None:
            try:
                app[handler._SERVICE] = service
            except Exception:  # a frozen app keeps its own; a fresh one serves this read
                pass
    return service


#: Capability sections a card may edit, in the order the view lists them.
_CARD_CAPABILITY_SECTIONS = ("tools", "allowedTools", "autoApprove", "mcpServers", "skills")


def member_capabilities_view(app: Any, member: str) -> dict[str, Any]:
    """A member's capability rows in exactly the shape ``crewmate.capabilities`` drafts take.

    Ids only, never an MCP transport (its env can carry credentials). Explains the
    two approval grammars, since mixing them is the common mistake: a built-in
    tool (``fs_read``) is approved through ``allowedTools`` by its bare name, an
    MCP tool through ``autoApprove`` as ``@<server>/<tool>``.
    """
    from kiro_crew.agent_capabilities import CapabilityError

    try:
        view = _capability_service(app).get(member)
    except CapabilityError as exc:
        raise catalog.CardCatalogError(exc.code, f"capabilities unavailable: {exc.code}") from None
    rows = []
    for row in view.get("rows") or []:
        section = row.get("section")
        if section not in _CARD_CAPABILITY_SECTIONS:
            continue
        rid = str(row.get("id") or "")
        locked = bool(row.get("managed")) or (
            section == "autoApprove" and rid.startswith("@kirocrew-computer/")
        )
        rows.append(
            {
                "section": section,
                "id": rid,
                "label": row.get("label") or rid,
                "state": row.get("state"),
                "present": bool(row.get("present")),
                "editable": not locked,
            }
        )
    servers = sorted({r["id"] for r in rows if r["section"] == "mcpServers" and r["present"]})
    template = view.get("template") or {}
    return {
        "member": member,
        "template": template.get("name") or "",
        "revision": view.get("revision"),
        "enroll_required": view.get("mode") != "inherited",
        "rows": rows,
        "mcp_servers": servers,
        "connections": [c.get("id") for c in view.get("connections") or [] if c.get("id")],
        "skills": [s.get("id") for s in view.get("skills") or [] if s.get("id")],
        "how_to": {
            "builtin_tool_approval": {"section": "allowedTools", "id": "<tool name, e.g. fs_read>"},
            "mcp_tool_approval": {"section": "autoApprove", "id": "@<mcp server>/<tool>"},
            "set": {"action": "set", "value": True},
            "enroll": 'add "enroll": true to the draft when enroll_required is true',
        },
    }


def _valid_refs(view: dict[str, Any]) -> list[str]:
    return [f"{r['section']}:{r['id']}" for r in view["rows"]]


def capability_ref_hint(app: Any, params: dict[str, Any]) -> str:
    """For a refused draft: the closest valid ``section:id`` refs for each operation."""
    import difflib

    try:
        view = member_capabilities_view(app, params["member"])
    except Exception:
        return ""
    valid = _valid_refs(view)
    builtins = {
        r["id"] for r in view["rows"] if r["section"] == "tools" and not r["id"].startswith("@")
    }
    hints = []
    for op in params["draft"].get("operations") or []:
        section, rid = str(op.get("section")), str(op.get("id"))
        if f"{section}:{rid}" in valid:
            continue
        if section == "autoApprove" and not rid.startswith("@") and rid in builtins | {"*"}:
            hints.append(f"{rid} is a built-in tool: use section allowedTools id {rid}")
            continue
        close = difflib.get_close_matches(f"{section}:{rid}", valid, n=3, cutoff=0.3)
        close += [v for v in valid if v.endswith(":" + rid) and v not in close][:2]
        if close:
            hints.append(f"{section}:{rid} -> closest valid: {', '.join(close)}")
    if not hints:
        return ""
    return "; " + "; ".join(hints) + " (see get_member_capabilities)"


def _capability_state(app: Any, params: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.agent_capabilities import CapabilityError

    try:
        view = _capability_service(app).get(params["member"])
    except CapabilityError as exc:
        raise catalog.CardCatalogError(exc.code, f"capabilities unavailable: {exc.code}") from None
    touched = {(op.get("section"), op.get("id")) for op in params["draft"]["operations"]}
    rows = [
        {k: row.get(k) for k in ("section", "id", "state", "value")}
        for row in view.get("rows") or []
        if (row.get("section"), row.get("id")) in touched
    ]
    return {"revision": view.get("revision"), "rows": rows}


async def resumed_undo_is_stale(rec: dict[str, Any], index: int, *, state: Any, app: Any) -> bool:
    """Whether what a resumed Undo's step *index* would remove changed since apply.

    The step-0 check compares the whole post-apply snapshot, which stops
    matches once earlier Undo steps removed their part. A resumed step compares
    only what is still there to remove.
    """
    kind, params, after = rec["kind"], rec["params"], rec.get("after") or {}
    if kind == catalog.KIND_CREWMATE_CREATE:
        current = await read_state(kind, params, rec["evidence"][:1], state=state, app=app)
        keys = ("exists", "member_id", "revision")
        return canonical({k: current.get(k) for k in keys}) != canonical(
            {k: after.get(k) for k in keys}
        )
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        # Step 1 applies the preview step 0 just took; its token and revision
        # are what the capability route checks, so nothing else to compare.
        return False
    # Every other kind undoes in one step, so a resumed step is never admitted.
    return True


async def undo_survivors(
    rec: dict[str, Any], *, state: Any, app: Any, response: Any = None
) -> list[str]:
    """What a finished Undo was meant to remove but is still there.

    MCP servers are re-read from their scopes. A Disconnect reports its own
    survivors: credential artifacts it could not unlink (``grantSurviving``).
    """
    kind = rec["kind"]
    if kind == catalog.KIND_CONNECTION_CONNECT:
        surviving = response.get("grantSurviving") if isinstance(response, dict) else None
        return [str(s) for s in surviving] if isinstance(surviving, list) else []
    if kind not in (catalog.KIND_MCP_INSTALL, catalog.KIND_MCP_ADD_CUSTOM):
        return []
    names = _mcp_undo_names(rec)
    return [n for n in names if await asyncio.to_thread(_mcp_scopes, n)]


def _connection_grant_fingerprint(slug: str) -> list[int] | None:
    """``[mtime_ns, size]`` of the provider's token artifact; stats only, never opened."""
    from kiro_crew import mcp_grant
    from kiro_crew.connections.registry import get_visible_providers

    provider = next((p for p in get_visible_providers() if p.get("slug") == slug), None)
    if provider is None:
        return None
    stamp = mcp_grant.grant_fingerprint(str(provider["mcp_url"]))
    return list(stamp) if stamp is not None else None


async def read_state(
    kind: str, params: dict[str, Any], evidence: list[dict[str, Any]], *, state: Any, app: Any
) -> dict[str, Any]:
    """The gateway's read of what *params* replaces (or, with *evidence*, what it made)."""
    if kind in (catalog.KIND_SCHEDULE_CREATE, catalog.KIND_SCHEDULE_UPDATE):
        job_id = params.get("id") if kind == catalog.KIND_SCHEDULE_UPDATE else None
        if kind == catalog.KIND_SCHEDULE_CREATE:
            job_id = (evidence[0] if evidence else {}).get("id")
            if not job_id:
                return {}
        job = await state.crons.get_job_async(str(job_id))
        if job is None:
            return {"exists": False}
        return {
            "exists": True,
            "name": job.name,
            "fields": _cron_job_fields(job),
            "revision": _cron_job_revision(job),
        }
    if kind == catalog.KIND_CONNECTION_CONNECT:
        from kiro_crew.connections.status import collect_connection_statuses

        statuses = await collect_connection_statuses()
        row = next((s for s in statuses if s.get("slug") == params["slug"]), None)
        return {
            "known": row is not None,
            "granted": bool(row and row.get("grantPresent")),
            # Presence says "a grant", the stat fingerprint says "THIS grant": a
            # re-authorization rewrites the token artifact, so an Undo recorded
            # against the earlier grant refuses instead of revoking the new one.
            "grant": await asyncio.to_thread(_connection_grant_fingerprint, params["slug"]),
        }
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        return await asyncio.to_thread(_capability_state, app, params)
    if kind == catalog.KIND_TEMPLATE_UPDATE and "skills" in (params.get("fields") or {}):
        return await asyncio.to_thread(_template_state_with_skills, params, state)
    if kind == catalog.KIND_CREWMATE_CREATE and len(evidence) > 1:
        base = await asyncio.to_thread(_sync_read_state, kind, params, evidence[:1])
        job = await state.crons.get_job_async(str(evidence[1].get("id")))
        schedule = _cron_job_revision(job) if job is not None else None
        return {**base, "schedule_exists": job is not None, "schedule": schedule}
    return await asyncio.to_thread(_sync_read_state, kind, params, evidence)


async def _mcp_detail(params: dict[str, Any]) -> dict[str, Any]:
    """Best-effort display facts for an install; empty when the registry is unreachable."""
    try:
        from kiro_crew.dashboard.handlers.mcp_discover import _get_registry

        registry = await asyncio.to_thread(_get_registry)
        provider = registry.get(params["provider"])
        if provider is None or not provider.is_available():
            return {}
        detail = await asyncio.wait_for(provider.fetch_detail(params["id"]), timeout=8)
    except Exception:
        return {}
    if detail is None:
        raise catalog.CardCatalogError("not_found", f"no MCP server '{params['id']}'")
    out: dict[str, Any] = {}
    plan = getattr(detail, "install_plan", None)
    spec = getattr(plan, "spec", None) if plan is not None else None
    if isinstance(spec, dict):
        if spec.get("url"):
            out["command"] = str(spec["url"])
        elif spec.get("command"):
            out["command"] = " ".join(
                [str(spec["command"])] + [str(a) for a in spec.get("args") or []]
            )
    for key in ("version", "repo_url"):
        value = getattr(detail, key, None)
        if isinstance(value, str) and value:
            out[key] = value[:200]
    return out


async def read_context(
    kind: str, params: dict[str, Any], before: dict[str, Any], *, state: Any, app: Any
) -> dict[str, Any]:
    """Display-only facts and up-front validation a preview needs."""
    p = params
    if kind == catalog.KIND_SETTING_CHANGE:
        target = await asyncio.to_thread(resolve_setting, p)
        check_target_params(target, p)
        return {}
    if kind == catalog.KIND_SCHEDULE_CREATE and "at" in p:
        return one_shot_context(p["at"], p.get("timezone") or "")
    if kind in (
        catalog.KIND_SCHEDULE_CREATE,
        catalog.KIND_SCHEDULE_UPDATE,
        catalog.KIND_CREWMATE_CREATE,
    ):
        source = p
        if kind == catalog.KIND_SCHEDULE_UPDATE:
            fields = dict(before.get("fields") or {})
            fields.update(p["fields"])
            source = fields
        if kind == catalog.KIND_CREWMATE_CREATE:
            source = p.get("schedule") or {}
        expr = source.get("cron_expr")
        if not expr:
            return {}
        from kiro_crew.cron_service.schedule import get_local_tz, is_valid_timezone

        tz = source.get("timezone") or ""
        if tz and not is_valid_timezone(tz):
            raise catalog.CardCatalogError("invalid_timezone", f"unknown timezone '{tz[:64]}'")
        when = next_run_at(expr, tz)
        if when is None:
            raise catalog.CardCatalogError("invalid_cron", "that cron expression never runs")
        # No zone on the card: the scheduler runs it in the configured one, so
        # that is the zone the card names and the next run is shown in.
        return {"next_run_at": when, "timezone": tz or get_local_tz()[0]}
    if kind == catalog.KIND_CREWMATE_CAPABILITIES:
        from kiro_crew.agent_capabilities import CapabilityError

        body = {"revision": before.get("revision"), **p["draft"]}

        def _preview() -> dict[str, Any]:
            return _capability_service(app).preview(p["member"], body)

        try:
            result = await asyncio.to_thread(_preview)
        except CapabilityError as exc:
            hint = await asyncio.to_thread(capability_ref_hint, app, p)
            raise catalog.CardCatalogError(
                exc.code, f"capabilities refused: {exc.code}{hint}"
            ) from None
        impact = result.get("impact") or []
        approvals = [
            f"{op['section']}:{op['id']}"
            for op in p["draft"]["operations"]
            if op.get("action") == "set" and op.get("section") in ("allowedTools", "autoApprove")
        ]
        withheld = [
            ref
            for ref in approvals
            if not any(f"{i.get('section')}:{i.get('id')}" == ref for i in impact)
            and any(
                f"{r.get('section')}:{r.get('id')}" == ref
                # Sanitized to a tombstone (forked members) or kept as an override
                # the ceiling filtered out of the spec (the Assistant singleton).
                and (r.get("state") == "removed" or not r.get("present"))
                for r in result.get("rows") or []
            )
        ]
        if withheld and not impact:
            raise catalog.CardCatalogError(
                "approval_withheld_by_policy",
                "the governance ceiling does not allow auto-approving "
                + ", ".join(withheld)
                + "; the tool keeps asking each time, and no card can change that",
            )
        return {"impact": impact}
    if kind == catalog.KIND_TEMPLATE_UPDATE:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        members = sorted(
            crew for crew, c in cfg.agents.items() if getattr(c, "kiro_agent", "") == p["template"]
        )
        return {"members": members}
    if kind == catalog.KIND_MCP_INSTALL:
        return {"detail": await _mcp_detail(p)}
    return {}


async def build(
    kind: str, params: dict[str, Any], *, state: Any, app: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(preview, before, context)`` for a validated proposal."""
    before = await read_state(kind, params, [], state=state, app=app)
    context = await read_context(kind, params, before, state=state, app=app)
    preview = catalog.build_preview(kind, params, before, context)
    check_preview_text(preview)
    return preview, before, context


def check_preview_text(preview: dict[str, Any]) -> None:
    """Refuse a card whose preview shows a stored value an output redactor would change.

    ``check_param_text`` covers what the agent wrote; this covers what the card
    copied from the live record (a schedule's current message, as ``before``).
    The preview is published to the agent and the page as is, and an undo plan
    writes those same values back, so a credential there is refused rather than
    shown or rewritten.
    """
    try:
        check_param_text(preview, _path="current")
    except CardError as exc:
        raise CardError(
            400,
            "unsafe_current_value",
            "the current value holds text that cannot be shown on a card; "
            "change it on its own page",
        ) from exc
