"""Gateway-owned waiting-on-you asks: the persisted store and every change to it.

An ask is what an agent declared it needs from the person (``waiting_on_you``), and
the dashboard pins it above the composer of the slot that declared it. The store is
the only source of that state: the slot payload, the owner routes, the agent routes
and the per-turn block all read it here, and nothing is read back from transcript
rows. One record per dashboard slot holds:

* ``asks``: at most :data:`~kiro_crew.waiting_on_you_contract.MAX_ASKS` live asks in
  stack order, oldest declaration first. Replacing one keeps its place;
* ``retired``: asks the person retired (answered, wrote in the chat, dismissed) or
  that expired, kept whole until the agent's next turn hears of them once.

Memory first, disk before success: every check-and-mutate runs under one lock and is
written to ``waiting_on_you.json`` under the data home (off the event loop, behind a
file lock) before its caller reports success or anything is broadcast. A write that
fails puts the record back and raises, so nothing changed.

The file is agent-writable, so each slot record carries a keyed MAC under a vault
subkey. A record the gateway did not write, or one that was edited, is dropped on
load. An unreadable or oversized file loads as empty and is left alone until the next
successful write.

An incognito or temporary session's record is never written: a restart drops it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from kiro_crew.dashboard import signed_json_store
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.waiting_on_you_contract import (
    ASK_ID_RE,
    CODE_CLEARED,
    CODE_SUPERSEDED,
    CODE_TOO_MANY_ASKS,
    MAX_ASKS,
    AskError,
)

logger = logging.getLogger(__name__)

STORE_FILENAME = "waiting_on_you.json"
STORE_MAC_PURPOSE = "waiting-on-you-store"
#: The largest file read or written; a bigger one loads as empty, unread.
STORE_FILE_MAX = 4 * 1024 * 1024
#: Slots with any record; past it the least recently changed slot's records go.
MAX_SLOTS = 2000
MAX_RETIRED = 4
#: An ask expires this long after its last change.
ASK_TTL_SECONDS = 7 * 24 * 60 * 60
RETIRED_TTL_SECONDS = 7 * 24 * 60 * 60
#: How often the expiry sweep runs.
SWEEP_INTERVAL_SECONDS = 10 * 60

#: Why an ask left the stack, as a retired record names it.
REASON_ANSWERED = "answered"
REASON_MESSAGE = "message"
REASON_DISMISSED = "dismissed"
REASON_EXPIRED = "expired"
#: Removed by the agent itself; it keeps no retired record.
REASON_CLEARED = "cleared"

_SLOT_KEY_MAX = 512


class StoreUnavailable(AskError):
    def __init__(self, message: str = "the pinned asks could not be saved; try again") -> None:
        super().__init__(503, "store_unavailable", message)


def mint_ask_id() -> str:
    return "wy-" + secrets.token_hex(8)


def _read_capped(path: Path) -> str | None:
    """The store file's text, ``None`` when absent; raises ``OSError`` when unusable.

    The read itself is bounded, not only a size the file reports: the data home is
    writable by the agents, and a device or a pipe put in the file's place reports
    a size of zero, so anything but a regular file is refused.
    """
    from kiro_crew.bounded_read import bounded_read_regular_file

    try:
        data = bounded_read_regular_file(str(path), STORE_FILE_MAX + 1)
    except FileNotFoundError:
        return None
    if len(data) > STORE_FILE_MAX:
        raise OSError("waiting-on-you store exceeds its size limit")
    return data.decode("utf-8")


def public_ask(ask: dict[str, Any]) -> dict[str, Any]:
    """The ask as the slot payload and the routes show it."""
    return {
        "key": ask["key"],
        "ask_id": ask["ask_id"],
        "title": ask["title"],
        "detail": ask["detail"],
        "options": [dict(o) for o in ask["options"]],
        "ts": ask["ts"],
    }


def _empty_record(slot_key: str, persist: bool, now: float) -> dict[str, Any]:
    return {
        "slot": slot_key,
        "persist": persist,
        "updated": now,
        "asks": [],
        "retired": [],
    }


class WaitingStore:
    """Every slot's pinned asks, bounded and persisted."""

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
        self._slots: dict[str, dict[str, Any]] = {}
        self._loaded = path is None
        self._lock = LoopBoundLock()

    # ── persistence ──

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _store_key(self) -> bytes:
        if self._mac_key is None:
            self._mac_key = signed_json_store.store_key(
                self._path, STORE_MAC_PURPOSE, self._key_source, "waiting-on-you store"
            )
        return self._mac_key

    async def warm(self) -> None:
        """Load the file once, off the event loop. Never raises.

        An unreadable, oversized or unverifiable file loads as empty: the gateway
        keeps serving, and the file is not rewritten until a change succeeds.
        """
        if self._loaded:
            return
        path = self._path
        assert path is not None

        def _read() -> tuple[str | None, bytes | None]:
            text = _read_capped(path)
            return text, (self._store_key() if text else None)

        try:
            text, key = await asyncio.to_thread(_read)
        except Exception:
            logger.warning("waiting-on-you store unreadable; starting empty", exc_info=True)
            text, key = None, None
        if self._loaded:
            return
        if text and key is not None:
            try:
                self._load_text(text, key)
            except RecursionError:
                # Nesting deeper than the parser's recursion limit: the store's own
                # writer never makes it, so the file is foreign, and it loads as empty.
                logger.warning("waiting-on-you store is nested too deeply; starting empty")
                self._slots = {}
        self._loaded = True

    def _load_text(self, text: str, key: bytes) -> None:
        try:
            raw = json.loads(text)
        except ValueError:
            logger.warning("waiting-on-you store is not JSON; starting empty")
            return
        records = raw.get("slots") if isinstance(raw, dict) else None
        if not isinstance(records, list):
            logger.warning("waiting-on-you store has no records list; starting empty")
            return
        dropped = 0
        for item in records[:MAX_SLOTS]:
            rec = self._authentic(key, item)
            if rec is None:
                dropped += 1
                continue
            self._slots[rec["slot"]] = rec
        dropped += max(0, len(records) - MAX_SLOTS)
        if dropped:
            logger.warning("waiting-on-you store: dropped %d unverifiable record(s)", dropped)

    def _authentic(self, key: bytes, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        rec = dict(item)
        if not signed_json_store.mac_matches(key, rec, rec.pop(signed_json_store.MAC_FIELD, None)):
            return None
        slot = rec.get("slot")
        if not isinstance(slot, str) or not slot or len(slot) > _SLOT_KEY_MAX:
            return None
        if not all(isinstance(rec.get(k), list) for k in ("asks", "retired")):
            return None
        if not all(
            isinstance(a, dict) and ASK_ID_RE.fullmatch(str(a.get("ask_id"))) for a in rec["asks"]
        ):
            return None
        rec["persist"] = True
        return rec

    def _serialization(
        self, key: bytes, slots: dict[str, dict[str, Any]] | None = None
    ) -> tuple[str, list[str]]:
        """Return the file text and slot keys omitted to stay within the size cap."""
        source = self._slots if slots is None else slots
        records = sorted(
            (r for r in source.values() if r.get("persist")),
            key=lambda r: r["updated"],
            reverse=True,
        )
        signed = [
            {**r, signed_json_store.MAC_FIELD: signed_json_store.record_mac(key, r)}
            for r in records
        ]
        evicted: list[str] = []
        while True:
            payload = json.dumps({"version": 1, "slots": signed}, ensure_ascii=False)
            if len(payload.encode("utf-8")) <= STORE_FILE_MAX or not signed:
                return payload, evicted
            evicted.append(str(signed.pop()["slot"]))

    def serialize(self, key: bytes) -> str:
        """The file's text: every persisted record that fits within the size cap."""
        payload, _evicted = self._serialization(key)
        return payload

    async def _persist(self, slots: dict[str, dict[str, Any]]) -> None:
        if self._path is None:
            return
        path = self._path
        try:
            key = await asyncio.to_thread(self._store_key)
            payload, evicted = self._serialization(key, slots)
            await asyncio.to_thread(signed_json_store.write_locked, path, payload)
        except Exception:
            logger.warning("could not persist the waiting-on-you asks", exc_info=True)
            raise StoreUnavailable() from None
        for slot_key in evicted:
            logger.warning("waiting-on-you store full: dropping the asks of %s", slot_key)
            slots.pop(slot_key, None)

    async def _commit(self, slot_key: str, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Run *mutate* on *slot_key*'s record and persist it; on failure, change nothing.

        *mutate* may raise :class:`AskError` to refuse, leaving the record as it was.
        A record left empty is removed. Nothing is written when *mutate* reports no
        change (it returns a ``(result, False)`` pair).
        """
        await self.warm()
        async with self._lock:
            existing = self._slots.get(slot_key)
            rec = (
                copy.deepcopy(existing)
                if existing is not None
                else _empty_record(slot_key, True, 0.0)
            )
            result, changed = mutate(rec)
            if not changed:
                return result
            rec["updated"] = self._clock()
            candidate = dict(self._slots)
            candidate[slot_key] = rec
            if not (rec["asks"] or rec["retired"]):
                del candidate[slot_key]
            evicted = self._evict(candidate)
            was_written = bool(existing and existing.get("persist"))
            if rec.get("persist") or was_written or any(r.get("persist") for r in evicted):
                await self._persist(candidate)
            self._slots = candidate
            return result

    @staticmethod
    def _evict(slots: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the least recently changed slots past :data:`MAX_SLOTS`; returns them."""
        gone: list[dict[str, Any]] = []
        while len(slots) > MAX_SLOTS:
            oldest = min(slots, key=lambda k: slots[k]["updated"])
            gone.append(slots.pop(oldest))
        return gone

    # ── reads ──

    def asks(self, slot_key: str) -> list[dict[str, Any]]:
        """*slot_key*'s live asks in stack order, as the slot payload shows them. No I/O."""
        rec = self._slots.get(slot_key)
        if rec is None:
            return []
        now = self._clock()
        return [public_ask(a) for a in rec["asks"] if now - a["ts"] < ASK_TTL_SECONDS]

    def has_any(self, slot_key: str) -> bool:
        rec = self._slots.get(slot_key)
        return bool(rec and (rec["asks"] or rec["retired"]))

    def slots_with_asks(self) -> list[str]:
        """Slot keys that currently have at least one unexpired ask."""
        return sorted(slot_key for slot_key in self._slots if self.asks(slot_key))

    def snapshot_for_turn(self, slot_key: str) -> dict[str, Any]:
        """A copy of *slot_key*'s live asks and retired records."""
        rec = self._slots.get(slot_key)
        return copy.deepcopy(rec) if rec is not None else _empty_record(slot_key, True, 0.0)

    # ── agent changes ──

    async def declare(
        self, slot_key: str, ask: dict[str, Any], *, persist: bool
    ) -> tuple[dict[str, Any], bool, list[str]]:
        """Pin *ask* (validated, redacted) on *slot_key*: ``(public, changed, keys)``.

        A pinned key is replaced in place with a new ``ask_id``. An identical
        declaration changes nothing and keeps its ``ask_id``. A fifth new key is
        refused with the pinned keys listed.
        """

        def _mutate(rec: dict[str, Any]) -> tuple[tuple[dict[str, Any], bool, list[str]], bool]:
            rec["persist"] = persist
            now = self._clock()
            for i, live in enumerate(rec["asks"]):
                if live["key"] != ask["key"]:
                    continue
                same = all(live[f] == ask[f] for f in ("title", "detail", "options"))
                if same and now - live["ts"] < ASK_TTL_SECONDS:
                    return (public_ask(live), False, _keys(rec)), False
                rec["asks"][i] = _new_ask(ask, now)
                return (public_ask(rec["asks"][i]), True, _keys(rec)), True
            if len(rec["asks"]) >= MAX_ASKS:
                keys = _keys(rec)
                raise AskError(
                    409,
                    CODE_TOO_MANY_ASKS,
                    f"{MAX_ASKS} asks are already pinned ({', '.join(keys)}): clear one "
                    "with waiting_on_you_clear or replace one by its key",
                    keys=keys,
                )
            rec["asks"].append(_new_ask(ask, now))
            return (public_ask(rec["asks"][-1]), True, _keys(rec)), True

        result: tuple[dict[str, Any], bool, list[str]] = await self._commit(slot_key, _mutate)
        return result

    async def clear(self, slot_key: str, keys: list[str] | None) -> list[dict[str, Any]]:
        """Remove *keys* (every ask when ``None``) from *slot_key*; returns those removed.

        The agent's own clear keeps no retired record: the agent already knows.
        """

        def _mutate(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
            gone = [a for a in rec["asks"] if keys is None or a["key"] in keys]
            if not gone:
                return [], False
            rec["asks"] = [a for a in rec["asks"] if a not in gone]
            return [dict(a, reason=REASON_CLEARED) for a in gone], True

        result: list[dict[str, Any]] = await self._commit(slot_key, _mutate)
        return result

    async def retire_all(
        self,
        slot_key: str,
        reason: str,
        *,
        keep: frozenset[str] = frozenset(),
        only: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Retire every ask of *slot_key* with *reason*, keeping each as a retired record.

        Asks whose id is in *keep* stay pinned: an answer to them is already on its
        way, and its own row retires them and names them. With *only*, just those
        ids retire: the asks pinned when the owner wrote, so one declared while
        this retirement waited for the store stays pinned.
        """

        def _mutate(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
            gone = [
                a
                for a in rec["asks"]
                if a["ask_id"] not in keep and (only is None or a["ask_id"] in only)
            ]
            if not gone:
                return [], False
            rec["asks"] = [a for a in rec["asks"] if a not in gone]
            for ask in gone:
                _add_retired(rec, ask, reason, self._clock())
            return [dict(a, reason=reason) for a in gone], True

        result: list[dict[str, Any]] = await self._commit(slot_key, _mutate)
        return result

    async def retire(self, slot_key: str, ask_id: str, reason: str) -> dict[str, Any] | None:
        """Retire the live ask *ask_id* with *reason*; ``None`` when it is not pinned.

        Idempotent: the row that answered an ask and the turn it opens both call it.
        """

        def _mutate(rec: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
            live = next((a for a in rec["asks"] if a["ask_id"] == ask_id), None)
            if live is None:
                return None, False
            rec["asks"].remove(live)
            _add_retired(rec, live, reason, self._clock())
            return dict(live, reason=reason), True

        result: dict[str, Any] | None = await self._commit(slot_key, _mutate)
        return result

    async def take_retired(self, slot_key: str) -> list[dict[str, Any]]:
        """Hand over *slot_key*'s retired records once, for the turn that starts now."""

        def _mutate(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
            taken = rec["retired"]
            if not taken:
                return [], False
            rec["retired"] = []
            return taken, True

        result: list[dict[str, Any]] = await self._commit(slot_key, _mutate)
        return result

    # ── owner changes ──

    def _find(self, rec: dict[str, Any], key: str, ask_id: str) -> dict[str, Any]:
        """The live ask *key* if it is still declaration *ask_id*, else the 409 why not."""
        live = next((a for a in rec["asks"] if a["key"] == key), None)
        if live is not None and live["ask_id"] == ask_id:
            return live
        if live is not None:
            raise AskError(409, CODE_SUPERSEDED, "the agent replaced this ask")
        raise AskError(409, CODE_CLEARED, "this ask is no longer pinned")

    async def dismiss(self, slot_key: str, key: str, ask_id: str) -> dict[str, Any]:
        """The owner's ✕: remove the ask, keep a retired record ``dismissed``."""

        def _mutate(rec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            live = self._find(rec, key, ask_id)
            rec["asks"].remove(live)
            _add_retired(rec, live, REASON_DISMISSED, self._clock())
            return live, True

        result: dict[str, Any] = await self._commit(slot_key, _mutate)
        return result

    async def forget(self, slot_key: str) -> bool:
        """Remove every record of a deleted session."""

        def _mutate(rec: dict[str, Any]) -> tuple[bool, bool]:
            had = bool(rec["asks"] or rec["retired"])
            rec["asks"], rec["retired"] = [], []
            return had, had

        # Loaded first: a record still on disk only would come back with the load.
        await self.warm()
        if slot_key not in self._slots:
            return False
        result: bool = await self._commit(slot_key, _mutate)
        return result

    # ── housekeeping ──

    async def expire_due(self, slot_key: str) -> list[dict[str, Any]]:
        """Expire *slot_key*'s asks past their 7 days now, as :meth:`sweep` would.

        A declaration runs this first, so an ask the slot already hides as expired
        never counts toward its limit between sweeps. Returns the expired asks.
        """

        def _mutate(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
            now = self._clock()
            gone = [a for a in rec["asks"] if now - a["ts"] >= ASK_TTL_SECONDS]
            if not gone:
                return [], False
            rec["asks"] = [a for a in rec["asks"] if a not in gone]
            for ask in gone:
                _add_retired(rec, ask, REASON_EXPIRED, now)
            return gone, True

        result: list[dict[str, Any]] = await self._commit(slot_key, _mutate)
        return result

    async def sweep(self) -> dict[str, list[dict[str, Any]]]:
        """Expire asks 7 days after their last change and prune old records.

        Returns ``{slot_key: [expired asks]}``. Expired asks become retired records.
        """
        await self.warm()
        expired: dict[str, list[dict[str, Any]]] = {}
        async with self._lock:
            candidate = copy.deepcopy(self._slots)
            now = self._clock()
            changed = False
            for slot_key, rec in list(candidate.items()):
                gone = [a for a in rec["asks"] if now - a["ts"] >= ASK_TTL_SECONDS]
                for ask in gone:
                    rec["asks"].remove(ask)
                    _add_retired(rec, ask, REASON_EXPIRED, now)
                if gone:
                    expired[slot_key] = gone
                kept_retired = [r for r in rec["retired"] if now - r["ts"] < RETIRED_TTL_SECONDS]
                if gone or kept_retired != rec["retired"]:
                    changed = True
                    rec["retired"] = kept_retired
                    rec["updated"] = now
                if not (rec["asks"] or rec["retired"]):
                    del candidate[slot_key]
            if not changed:
                return {}
            try:
                await self._persist(candidate)
            except StoreUnavailable:
                return {}
            self._slots = candidate
        return expired


def _keys(rec: dict[str, Any]) -> list[str]:
    return [a["key"] for a in rec["asks"]]


def _new_ask(ask: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "key": ask["key"],
        "ask_id": mint_ask_id(),
        "title": ask["title"],
        "detail": ask["detail"],
        "options": [dict(o) for o in ask["options"]],
        "ts": now,
    }


def _add_retired(rec: dict[str, Any], ask: dict[str, Any], reason: str, now: float) -> None:
    rec["retired"].append(
        {
            "key": ask["key"],
            "ask_id": ask["ask_id"],
            "title": ask["title"],
            "detail": ask["detail"],
            "options": [dict(o) for o in ask["options"]],
            "reason": reason,
            "ts": now,
        }
    )
    del rec["retired"][:-MAX_RETIRED]


_STORES: dict[Path, WaitingStore] = {}


def store_for_home() -> WaitingStore:
    """The one store of the data home this process runs under."""
    # A module import, not a from-import: every slot row calls this, and a from-import
    # probes the config facade's ``__path__`` once per call.
    import kiro_crew.config.loader as _config_loader

    path = Path(_config_loader.config_dir()) / STORE_FILENAME
    store = _STORES.get(path)
    if store is None:
        store = _STORES[path] = WaitingStore(path)
    return store


def store_for(state: Any) -> WaitingStore:
    """The store attached to this gateway's dashboard state (the data home's own)."""
    store = getattr(state, "_waiting_on_you_store", None)
    if not isinstance(store, WaitingStore):
        store = store_for_home()
        state._waiting_on_you_store = store
    return store


def slot_payload(slot_key: str) -> list[dict[str, Any]]:
    """The ``waiting_on_you`` field of *slot_key*'s slot row. No I/O.

    Read from the data home's store: a slot row is projected without its state.
    Empty until the store has loaded, which the gateway starts at boot.
    """
    try:
        return store_for_home().asks(slot_key)
    except Exception:
        logger.debug("waiting-on-you payload unavailable for %s", slot_key, exc_info=True)
        return []
