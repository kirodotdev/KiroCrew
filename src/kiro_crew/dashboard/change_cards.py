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
import hashlib
import hmac
import importlib as _importlib
import json
import logging
import os
import secrets
import sys as _sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import _facade_mirror
from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard.change_cards_records import (
    _INTERRUPTED_MESSAGE,
    _MAC_FIELD,
    _MAC_RE,
    _MESSAGE_MAX,
    _PRIVATE_KEYS,
    CARD_RECORD_MAX,
    CARD_TTL_SECONDS,
    CODE_CHECKPOINT_FAILED,
    CODE_INTERRUPTED,
    CODE_STORE_UNAVAILABLE,
    FINISHED_RETAIN_SECONDS,
    FINISHED_STATUSES,
    INFLIGHT_STALE_SECONDS,
    MAX_LIVE_CARDS_PER_SLOT,
    MAX_STORED_CARDS,
    REPORTED_STATUSES,
    STATUS_APPLIED,
    STATUS_APPLYING,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_UNDONE,
    STORE_FILE_MAX,
    STORE_FILENAME,
    STORE_MAC_PURPOSE,
    CardError,
    _jsonable,
    canonical,
    derived_fields,
    rebuild_record,
    valid_card_id,
)

logger = logging.getLogger("kiro_crew.dashboard.change_cards")


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

    def last_finished_at(self, slot_key: str, statuses: "frozenset[str]") -> float:
        """When a card of *slot_key* last finished in one of *statuses*, else ``0.0``.

        Read over every stored card, a dismissed one and one past its display TTL
        included: :meth:`pending` hides both, but a press still answered the question
        it was raised for.
        """
        self._ensure_loaded()
        newest = 0.0
        for rec in self._cards.values():
            if rec["slot_key"] != slot_key or rec.get("status") not in statuses:
                continue
            finished = rec.get("finished_at")
            if isinstance(finished, (int, float)):
                newest = max(newest, float(finished))
        return newest

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
        error = rec.get("error") or {}
        rec["result"] = {
            "summary": catalog.result_summary(
                rec["kind"],
                rec["params"],
                status,
                rec["title"],
                failed_step=error.get("step") if error.get("op") == catalog.OP_APPLY else None,
            ),
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
        same = self._open_duplicate(slot_key, kind, params)
        if same is not None:
            raise CardError(
                409,
                "duplicate_card",
                f"the same change is already on card {same['id']}, waiting for the person: "
                "this one would only repeat it",
            )
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

    def _open_duplicate(
        self, slot_key: str, kind: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A card in *slot_key* still waiting on the person that proposes exactly this.

        A second identical card only doubles what the person has to press, and the
        two then race each other's apply. An edited, finished or expired card is not
        one: the same proposal after any of those is a new question.
        """
        for rec in self._cards.values():
            if (
                rec["slot_key"] == slot_key
                and rec["kind"] == kind
                and rec["status"] in (STATUS_PENDING, STATUS_APPLYING)
                and rec["expires_at"] > self._clock()
                and rec["params"] == params
            ):
                return rec
        return None

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


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.change_cards_readers import (  # noqa: F401
        _CARD_CAPABILITY_SECTIONS,
        _CREWMATE_SPEC_FIELDS,
        _CRON_CONFIG_FIELDS,
        _MCP_SCOPE_UNREADABLE,
        KR_RECORDS_KEY,
        _capability_service,
        _capability_state,
        _connection_grant_fingerprint,
        _crewmate_identity,
        _crewmate_key,
        _crewmate_label,
        _crewmate_revision,
        _cron_job_fields,
        _cron_job_revision,
        _folder,
        _goal_state,
        _import_after_is_written,
        _import_rules_digest,
        _import_state,
        _mcp_detail,
        _mcp_exists,
        _mcp_global_entry,
        _mcp_scopes,
        _mcp_undo_names,
        _mcp_value_key,
        _page_offer_context,
        _page_offer_snapshot,
        _revision_digest,
        _secret_revision,
        _server_reach,
        _sync_read_state,
        _template_state_with_skills,
        _valid_refs,
        after_shows_the_write,
        build,
        capability_ref_hint,
        check_preview_text,
        comparable_state,
        cron_job_revision,
        member_capabilities_view,
        next_run_at,
        one_shot_context,
        read_context,
        read_state,
        resolve_params,
        resumed_undo_is_stale,
        template_auto_approved_servers,
        undo_snapshot_changed,
        undo_survivors,
    )
    from kiro_crew.dashboard.change_cards_records import (  # noqa: F401
        _ALL_STATUSES,
        _CARD_ID_RE,
        _DERIVED_KEYS,
        _PRE_APPLY_STATUSES,
        _param_text_refusal,
        _same,
        check_param_text,
        clean_reason,
    )
    from kiro_crew.dashboard.change_cards_settings import (  # noqa: F401
        _DASHBOARD_LINE,
        _DASHBOARD_PREFIX,
        _DIAGNOSE_HISTORY_DAYS,
        _DIAGNOSE_VALUE_MAX,
        _FIND_LIMIT,
        _FIND_LIST_ITEMS,
        _FINDING_RANK,
        _KIROCREW_STORE,
        _REGISTRY_PATH,
        _SENSITIVE_SEGMENTS,
        _TOPIC_STOPWORDS,
        DIAGNOSE_OUTPUT_MAX,
        DIAGNOSE_RECENT_MAX,
        _bounded,
        _config_value,
        _dashboard_allowed,
        _dashboard_list_target,
        _dashboard_target,
        _editable_config,
        _finding_hits,
        _flatten_like,
        _help_text,
        _key_withheld,
        _kirocrew_target,
        _ranked,
        _registry_by_config_key,
        _registry_entry,
        _row_hits,
        _scrub,
        _stem,
        _token_hits,
        _topic_tokens,
        _value_withheld,
        check_setting_value,
        check_target_params,
        check_target_value,
        diagnose_settings,
        find_settings,
        localize_setting_rows,
        non_default_settings,
        read_setting_value,
        recent_dashboard_changes,
        resolve_setting,
        setting_spec,
        settings_registry,
        spec_allowed_values,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.config.schema import (  # noqa: F401
        requires_restart,
    )

# This module is the facade of the change cards: every name is reached through it as
# ``kiro_crew.dashboard.change_cards.<name>``. A read is served by the owner that binds the name,
# and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and they log under
# its name. The persisted store above stays here. New work goes to the owner of its
# responsibility:
#
#   ``change_cards_records`` -- statuses, limits, error codes and the record checks
#   ``change_cards_settings`` -- the settings a card targets: lookup, search, diagnosis
#   ``change_cards_readers`` -- the current-state readers and building a card
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.change_cards_records",
    "kiro_crew.dashboard.change_cards_settings",
    "kiro_crew.dashboard.change_cards_readers",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
