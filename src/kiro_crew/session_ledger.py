"""Per-session work ledger — durable loop state that lives on disk, not in context.

Long-horizon sessions (monitor loops, goal loops) accumulate their working
state as prior transcript turns, which the harness-owned compaction then
summarizes lossily. This module gives every session one small on-disk store
for that state instead: a mutable **state record** (goal, phase, next intent,
tried approaches, artifact pointers) carrying a bounded **event tail**. The
context window becomes a cache; the ledger is the authority.

Layout (see docs/system-specs/features/session-work-ledger.md):

    <data_home>/ledger/<store-name>/
        slot_key        # breadcrumb: the exact ledger key this dir belongs to
        state.json      # the whole record, replaced atomically on every write
        .lock           # cross-process mutex inode (never replaced by writes)

Design notes, each earned by a review finding:

- **One document, one atomic write.** State and its event land in the same
  ``atomic_write`` (temp file + rename), so a crash between "phase moved" and
  "event logged" cannot exist — the phase-requires-event invariant is
  crash-atomic by construction, not by ordering. The event tail is bounded
  (``_MAX_EVENTS``), so the file cannot grow without limit and every read is
  O(record), never O(history).
- **Exact-key identity.** :func:`ledger_key` only strips the dashboard
  prefixes (the same strip the permanent-delete funnel applies); it never
  folds the key's charset. Uniqueness comes from a digest over the exact key
  inside :func:`_store_name` — a lossy charset fold as the identity would map
  distinct channel session keys (colon-structured) onto one ledger.
- **Bounded lock.** The per-ledger lock acquire is a bounded poll and fails
  closed with ``OSError`` — a *live cross-process holder* of the flock costs
  one refused write, not an executor thread parked on it forever. The bound
  covers only what the deadline can reach: contention for an already-created
  lock inode. It does NOT cover a wedged filesystem/mount — the pre-lock
  ``mkdir``/``os.open`` are ordinary path/inode syscalls that a dead NFS mount
  or dying disk can stall unboundedly, and no in-process deadline can
  interrupt a stalled syscall. That mount-level failure is a distinct,
  out-of-scope failure mode, not a bound this code promises.
- **Size ceiling before parse.** A state file past ``_MAX_STATE_BYTES`` is
  treated as corrupt/absent rather than parsed, so a hostile or damaged file
  cannot make every nudge fire allocate its size.

Callers pass keys through :func:`ledger_key`; this module never imports
dashboard state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import release_lock, try_acquire_lock

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Phases that end a workstream. ``finished_at`` is stamped when the record
#: enters one of these; anything else is an in-flight phase.
TERMINAL_PHASES = frozenset({"done", "abandoned"})

#: Vocabulary for event lines. A phase change REQUIRES one of these; an event
#: without a phase change coerces an unrecognized kind to ``note`` (the text
#: is the payload there, the kind only a filter).
EVENT_KINDS = frozenset({"progress", "decision", "tried", "blocked", "unblocked", "phase", "note"})

# Bounds. The ledger is injected into nudge turns and read back every cycle,
# so every field is capped at write time; a runaway writer degrades to a
# clamped record instead of an unbounded file.
_MAX_TEXT = 2000
_MAX_TRIED = 50
_MAX_ARTIFACTS = 32
_MAX_EVENTS = 100
_MAX_EVENT_TAIL = 20
#: Refuse to parse a state file past this size: with every field clamped the
#: legitimate maximum is well under it, so anything bigger is damage or
#: tampering, and parsing it would cost what the clamps exist to prevent.
_MAX_STATE_BYTES = 1_000_000

#: Lock acquire budget. Every in-tree critical section is a sub-millisecond
#: read + atomic rename, so this is a ceiling against a live cross-process
#: holder, not a normal wait. On expiry the write FAILS CLOSED with OSError.
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

_STATE_FILE = "state.json"
_KEY_FILE = "slot_key"
_LOCK_FILE = ".lock"

#: Identical fold to ``crew_chat._store_name`` — kept in lockstep so a slot
#: key and its stores share one spelling family. Reimplemented rather than
#: imported: ``crew_chat`` drags the whole crew orchestrator import graph into
#: what must stay a leaf module usable from the gateway boot path. The fold
#: shapes only the READABLE half of a directory name; identity is the digest
#: over the exact key.
_STORE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STORE_NAME_READABLE_MAX = 80


def ledger_key(session_key: str) -> str:
    """Fold a session/slot key to the ledger's identity spelling — LOSSLESSLY.

    Only the dashboard prefixes are stripped, because one dashboard session is
    legitimately spelled both ``dashboard_chat-X`` (history/API) and
    ``chat-X`` (live slot, nudge loop) and both must reach one ledger. Nothing
    else is rewritten: a charset fold here would be lossy, and two distinct
    channel session keys that fold to the same string would share a ledger —
    one session reading and overwriting another's state.
    """
    key = session_key or ""
    if key.startswith("dashboard:"):
        key = key[len("dashboard:") :]
    while key.startswith("dashboard_"):
        key = key[len("dashboard_") :]
    return key


def _store_name(slot_key: str) -> str:
    """Directory name for *slot_key*'s ledger — unique per EXACT key.

    Readable fold capped for filesystem name limits; uniqueness comes from the
    digest over the FULL key, so ``Foo``/``foo`` and long shared prefixes all
    get distinct directories on every filesystem. The name is not decodable
    back to the key — the key is persisted inside the store instead.
    """
    readable = _STORE_NAME_UNSAFE.sub("_", slot_key)[:_STORE_NAME_READABLE_MAX]
    digest = hashlib.sha256(slot_key.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _ledger_root() -> Path:
    """Ledger root, resolved against the live data home per call.

    Never captured at import: an import-time binding freezes the data home and
    defeats pod isolation and test isolation (same rule as
    ``subagent_persistence._subagents_dir``).
    """
    return data_home() / "ledger"


def ledger_dir(slot_key: str) -> Path:
    """Validated per-session ledger directory for *slot_key*.

    Raises ``ValueError`` on an empty or path-hostile key. The fold in
    :func:`_store_name` already removes separators, but the raw key is checked
    too so a hostile key is refused loudly instead of silently folded, and the
    resolved path is required to stay inside the ledger root (symlink-safe).
    """
    if not slot_key or "\0" in slot_key or "/" in slot_key or "\\" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    base = _ledger_root()
    resolved = (base / _store_name(slot_key)).resolve()
    parent = base.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
        raise ValueError(f"Path traversal blocked for slot key: {slot_key!r}")
    return resolved


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clamp(value: Any, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


@contextmanager
def _locked(dir_path: Path) -> Iterator[None]:
    """Bounded-against-a-holder exclusive lock over one ledger directory.

    The lock file is a dedicated inode that writes never replace (replacing
    the locked inode would let a second writer lock the NEW inode and
    interleave). The acquire is a bounded poll over
    :func:`platform_compat.try_acquire_lock` — the repo's one non-blocking
    acquire primitive, covering POSIX and Windows alike — and FAILS CLOSED
    with ``OSError`` rather than entering the critical section unserialized.

    Scope of the bound (be precise — the docstring must not out-promise the
    code). ``_LOCK_TIMEOUT_SECS`` bounds ONLY the ``try_acquire_lock`` poll
    loop below: against a *live cross-process holder* of the flock, this
    refuses with ``OSError`` instead of waiting forever. The deadline is
    checked between sleeps rather than during one, so the refusal lands within
    the budget plus at most one ``_LOCK_POLL_SECS`` interval — a bound, not an
    exact wall-clock cap.

    The pre-lock ``mkdir``/``os.open`` are ordinary path/inode syscalls; on a
    wedged filesystem (hard NFS mount, dying disk) they can stall unboundedly,
    and NO in-process deadline can interrupt them — SIGALRM is main-thread +
    POSIX-only (this runs on an ``asyncio.to_thread`` worker), a bounded
    dedicated-thread offload leaks an unkillable thread and a held fd on a
    hard hang, and ``O_NONBLOCK`` does not cover path-resolution/inode stalls
    (only FIFO/device opens). A wedged mount is therefore explicitly OUT OF
    SCOPE of this lock's deadline, not a bound this contextmanager promises.

    The deadline is established immediately before the poll it governs and is
    NOT hoisted above ``mkdir``/``os.open`` — hoisting would spend the budget
    on the pre-lock syscalls and leave a near-zero retry window for genuine
    contention, which is the inversion that must not recur.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    lock_path = dir_path / _LOCK_FILE
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Bound only the acquire poll: set the deadline adjacent to the loop
        # it governs, after the pre-lock syscalls (which it cannot bound).
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise OSError("ledger lock is held by another process; try again")
            time.sleep(_LOCK_POLL_SECS)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "goal": "",
        "phase": "",
        "next": "",
        "tried": [],
        "artifacts": {},
        "events": [],
        "created_at": "",
        "last_progress_at": "",
        "finished_at": "",
    }


def _coerce_state(raw: Any) -> dict[str, Any]:
    """Fold whatever is on disk into a well-typed state record.

    Unknown fields are preserved (forward compatibility for a newer writer);
    known fields with the wrong type are reset to their defaults. Never raises.
    """
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    extra = {k: v for k, v in raw.items() if k not in state}
    for key in ("goal", "phase", "next", "created_at", "last_progress_at", "finished_at"):
        if isinstance(raw.get(key), str):
            state[key] = _clamp(raw[key])
    if isinstance(raw.get("tried"), list):
        tried: list[dict[str, str]] = []
        for item in raw["tried"]:
            if isinstance(item, dict) and isinstance(item.get("approach"), str):
                tried.append(
                    {
                        "approach": _clamp(item["approach"]),
                        "rejected_because": _clamp(item.get("rejected_because", "")),
                        "at": _clamp(item.get("at", ""), 64),
                    }
                )
        state["tried"] = tried[-_MAX_TRIED:]
    if isinstance(raw.get("artifacts"), dict):
        arts: dict[str, str] = {}
        for k, v in raw["artifacts"].items():
            if isinstance(k, str) and isinstance(v, str) and len(arts) < _MAX_ARTIFACTS:
                arts[_clamp(k, 128)] = _clamp(v)
        state["artifacts"] = arts
    if isinstance(raw.get("events"), list):
        events: list[dict[str, str]] = []
        for item in raw["events"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                events.append(
                    {
                        "ts": _clamp(item.get("ts", ""), 64),
                        "kind": _clamp(item.get("kind", ""), 32),
                        "text": _clamp(item["text"]),
                    }
                )
        state["events"] = events[-_MAX_EVENTS:]
    schema = raw.get("schema")
    if isinstance(schema, int) and not isinstance(schema, bool) and schema > 0:
        state["schema"] = schema
    state.update(extra)
    return state


def _read_state_unlocked(dir_path: Path) -> dict[str, Any]:
    path = dir_path / _STATE_FILE
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            logger.warning("ledger state file over size ceiling; treating as absent")
            return _empty_state()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty_state()
    return _coerce_state(raw)


def read_state(slot_key: str) -> dict[str, Any]:
    """Best-effort read of the state record. Absent/malformed reads as empty.

    Lock-free by design: ``atomic_write`` means a reader sees the old or the
    new document, never a torn one, and the events ride the same document so
    state and event tail are always from one transaction.
    """
    try:
        dir_path = ledger_dir(slot_key)
    except ValueError:
        return _empty_state()
    return _read_state_unlocked(dir_path)


def has_ledger(slot_key: str) -> bool:
    """Whether *slot_key* has ever recorded anything (cheap existence probe)."""
    try:
        return (ledger_dir(slot_key) / _STATE_FILE).exists()
    except ValueError:
        return False


def record(
    slot_key: str,
    *,
    goal: str | None = None,
    phase: str | None = None,
    next_step: str | None = None,
    tried_approach: str | None = None,
    tried_rejected_because: str | None = None,
    artifacts: dict[str, str] | None = None,
    event: str | None = None,
    event_kind: str | None = None,
) -> dict[str, Any]:
    """Apply one partial update to the state record, in one locked transaction.

    Enforces the crew-ledger discipline: passing *phase* without *event* and a
    recognized *event_kind* is refused with ``ValueError`` — a phase must
    never move without a logged, classified reason. State and event land in
    the SAME atomic write, so the invariant holds across crashes too.

    Returns the resulting state record.
    """
    if phase is not None:
        if not (event and event.strip()):
            raise ValueError("phase change requires an event: pass event + event_kind")
        if (event_kind or "").strip() not in EVENT_KINDS:
            kinds = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"phase change requires event_kind (one of: {kinds})")
    dir_path = ledger_dir(slot_key)
    with _locked(dir_path):
        state = _read_state_unlocked(dir_path)
        now = _now_iso()
        created = not state["created_at"]
        if created:
            state["created_at"] = now
            # Breadcrumb so the folded directory name can be mapped back to
            # its key by a human (the fold itself is not decodable).
            try:
                atomic_write(dir_path / _KEY_FILE, slot_key + "\n", mode=0o600)
            except OSError:
                logger.debug("ledger: slot_key breadcrumb write failed", exc_info=True)
        if goal is not None:
            state["goal"] = _clamp(goal)
        if phase is not None:
            state["phase"] = _clamp(phase, 128)
            state["finished_at"] = now if state["phase"] in TERMINAL_PHASES else ""
        if next_step is not None:
            state["next"] = _clamp(next_step)
        if tried_approach:
            tried = list(state["tried"])
            tried.append(
                {
                    "approach": _clamp(tried_approach),
                    "rejected_because": _clamp(tried_rejected_because or ""),
                    "at": now,
                }
            )
            state["tried"] = tried[-_MAX_TRIED:]
        if artifacts:
            merged = dict(state["artifacts"])
            for k, v in artifacts.items():
                if isinstance(k, str) and isinstance(v, str):
                    ck = _clamp(k, 128)
                    # Pop before reassigning: a dict update keeps the key's
                    # ORIGINAL insertion position, so updating the oldest
                    # pointer on a full map would leave it first in line for
                    # the age-out below — deleting the very artifact this call
                    # just wrote. Re-inserting moves it to newest.
                    merged.pop(ck, None)
                    merged[ck] = _clamp(v)
            # Cap by insertion order: oldest pointers age out first.
            if len(merged) > _MAX_ARTIFACTS:
                merged = dict(list(merged.items())[-_MAX_ARTIFACTS:])
            state["artifacts"] = merged
        if event and event.strip():
            kind = (event_kind or "").strip()
            if kind not in EVENT_KINDS:
                kind = "note"
            events = list(state["events"])
            events.append({"ts": now, "kind": kind, "text": _clamp(event.strip())})
            state["events"] = events[-_MAX_EVENTS:]
        state["last_progress_at"] = now
        state["schema"] = SCHEMA_VERSION
        blob = _serialize_bounded(state, source=dir_path.name)
        atomic_write(dir_path / _STATE_FILE, blob + "\n", mode=0o600)
        # ``_serialize_bounded`` evicted from THIS dict, so the caller's
        # post-write view is the document that just landed on disk. Do not
        # serialize a copy here: that would return the pre-eviction lists
        # while disk held the evicted ones (#6290).
        return state


def _serialize_bounded(state: dict[str, Any], source: str = "") -> str:
    """Serialize the state document, guaranteeing it fits the read ceiling.

    The reader treats a file past ``_MAX_STATE_BYTES`` as damage and reads it
    as absent — so the WRITER must guarantee no accepted record ever crosses
    it, or a large-but-legitimate document would be silently zeroed by its own
    next read. Two measures: ``ensure_ascii=False`` stores text as UTF-8
    (4 bytes/char worst case) instead of ``\\uXXXX`` escapes (12 bytes/char
    for astral-plane characters, which alone could triple the size of a
    clamped-but-full record past the ceiling); and if the document still does
    not fit — every field is clamped, but events + tried + artifacts at their
    joint maxima can exceed 1 MB in 4-byte script — the oldest events, then
    the oldest tried entries, are evicted until it fits. History ages out;
    the current state (goal/phase/next/artifacts) is never dropped and cannot
    exceed the ceiling on its own.

    Every one of those evictions DISCARDS data that never reaches disk, so
    unlike the read side there is no original file left to recover it from —
    the loss is permanent the moment the write lands. The reader already WARNs
    when the ceiling makes it discard a whole file; discarding history to stay
    under that same ceiling is the same loss in a smaller quantity and is
    reported the same way: one line per over-budget serialization, naming what
    went and how much, and nothing at all when the document fits. The line
    describes the document this call built, not a durable write — the caller's
    ``atomic_write`` runs afterwards and may still fail.

    Mutates *state* in place while evicting, and ``record`` returns that same
    dict — deliberately, so a caller's post-write view is the document on
    disk. See the note at the ``return`` in ``record``.
    """
    budget = _MAX_STATE_BYTES - 4096  # headroom so the reader's check never ties
    evicted_events = 0
    evicted_tried = 0
    dropped_extras: list[str] = []
    try:
        while True:
            blob = json.dumps(state, ensure_ascii=False)
            if len(blob.encode("utf-8")) <= budget:
                return blob
            if state["events"]:
                state["events"] = state["events"][1:]
                evicted_events += 1
            elif state["tried"]:
                state["tried"] = state["tried"][1:]
                evicted_tried += 1
            else:
                # History is gone and the document still does not fit. The known
                # fields are all clamped well under the budget, so the excess can
                # only live in UNKNOWN fields carried forward by ``_coerce_state``
                # (a newer writer's data, or a corrupt/hostile file that parsed).
                # Preserving unknown fields is best-effort forward compatibility;
                # preserving THE LEDGER is the contract — a document past the
                # reader's ceiling is discarded wholesale on the next read, which
                # loses the known state too. Drop the extras and retry once.
                extras = [k for k in state if k not in _empty_state()]
                if extras:
                    for k in extras:
                        state.pop(k, None)
                    dropped_extras.extend(extras)
                    continue
                # Unreachable with the field clamps; refuse rather than write a
                # document the reader is guaranteed to discard.
                raise ValueError("record too large to store")
    finally:
        # In ``finally`` so the refusal path reports too: it raises with the
        # in-memory record already gutted, which is exactly when an operator
        # most needs to know what this call threw away.
        losses: list[str] = []
        if evicted_events:
            losses.append(f"oldest events[] evicted x{evicted_events}")
        if evicted_tried:
            losses.append(f"oldest tried[] evicted x{evicted_tried}")
        if dropped_extras:
            losses.append("unknown fields dropped: " + ", ".join(sorted(dropped_extras)))
        if losses:
            # Describes the DOCUMENT being serialized, not a completed write:
            # ``atomic_write`` runs after this and can still fail (ENOSPC),
            # leaving the previous file intact, and the refusal path above
            # never writes at all. Either way this call's in-memory record has
            # already lost the entries named here.
            logger.warning(
                "ledger state exceeded the %d-byte serialization budget%s; discarded: %s",
                budget,
                f" ({source})" if source else "",
                "; ".join(losses),
            )


def purge(slot_key: str) -> None:
    """Delete *slot_key*'s ledger directory. Best-effort, never raises.

    Ledger content is disposable intermediate state — nothing reconstructs
    from it — so this runs unconditionally on permanent session deletion.
    A write racing the delete can at worst recreate an orphan directory that
    the next delete sweeps; it can never touch another session's ledger, so
    the funnel narrows the window (purge after the slot's turn is torn down)
    instead of buying a tombstone protocol for disposable state.
    """
    try:
        dir_path = ledger_dir(slot_key)
    except ValueError:
        return
    shutil.rmtree(dir_path, ignore_errors=True)


def purge_matching(exact_keys: set[str], folded_keys: set[str], fold: Any) -> int:
    """Purge every ledger whose breadcrumb key matches a delete candidate.

    The delete funnel names a session by whatever spellings it has on hand
    (history key, slot key, folded transcript spelling), but a channel
    session's ledger is keyed by its EXACT session key — a spelling the
    funnel may not hold once the slot is gone. This sweep closes that gap:
    it walks the ledger root, reads each directory's ``slot_key`` breadcrumb,
    and removes the ledger when the breadcrumb matches a candidate exactly or
    under the caller-supplied *fold* (the transcript-filename fold, so the
    folded history spelling the funnel does hold reaches the exact-key
    ledger it names). The fold is used only to MATCH deletion targets, never
    as storage identity; in the rare case two exact keys share a folded
    spelling, both ledgers are removed — acceptable for disposable state,
    where the alternative is one of them silently surviving its session.

    Best-effort, never raises. Returns the number of ledgers removed. The
    root holds one directory per session that ever recorded, so the walk is
    small; a breadcrumbless directory (breadcrumb write is best-effort) is
    still covered by the direct :func:`purge` calls the funnel makes first.
    """
    removed = 0
    try:
        root = _ledger_root()
        if not root.is_dir():
            return 0
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if not child.is_dir():
                continue
            key = (child / _KEY_FILE).read_text(encoding="utf-8").strip()
            if not key:
                continue
            if key in exact_keys or fold(key) in folded_keys:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except Exception:
            continue
    return removed


#: Ceiling for the injected snapshot block. A nudge turn carries this every
#: cycle, so it must stay small even against a clamped-but-full record.
_SNAPSHOT_MAX_CHARS = 1600
_SNAPSHOT_FIELD_MAX = 300
_SNAPSHOT_TRIED_TAIL = 3


def render_snapshot(slot_key: str) -> str:
    """Compact ``[work ledger]`` block for per-cycle injection, or ``""``.

    Empty when the session has no ledger, the record is empty, or the
    workstream is finished — a terminal ledger has nothing to steer. Reads
    are lock-free (see :func:`read_state`); callers on an event loop must
    dispatch this to a worker thread — it does filesystem I/O.
    """
    if not has_ledger(slot_key):
        return ""
    state = read_state(slot_key)
    if not any((state["goal"], state["phase"], state["next"], state["tried"], state["artifacts"])):
        return ""
    if state["phase"] in TERMINAL_PHASES:
        return ""

    def _field(v: str) -> str:
        v = " ".join(v.split())
        return v[:_SNAPSHOT_FIELD_MAX]

    lines = [
        "[work ledger — durable state for this session; authoritative over memory of prior cycles]"
    ]
    if state["goal"]:
        lines.append(f"goal: {_field(state['goal'])}")
    if state["phase"]:
        lines.append(f"phase: {_field(state['phase'])}")
    if state["next"]:
        lines.append(f"next: {_field(state['next'])}")
    for item in state["tried"][-_SNAPSHOT_TRIED_TAIL:]:
        why = f" (rejected: {_field(item['rejected_because'])})" if item["rejected_because"] else ""
        lines.append(f"tried: {_field(item['approach'])}{why}")
    for k, v in state["artifacts"].items():
        lines.append(f"artifact {k}: {_field(v)}")
    block = "\n".join(lines)
    if len(block) > _SNAPSHOT_MAX_CHARS:
        block = block[: _SNAPSHOT_MAX_CHARS - 1] + "…"
    return block
