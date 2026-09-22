"""Fold savepoints: where a resumed fold starts, instead of replaying the whole log.

A fold costs one step per entry with no upper bound, so what it costs to serve a
view grows with how long its store has run. A savepoint is that cost removed: each
fold's state written down with the seq it had consumed, so the next read folds only
the tail.

A SAVEPOINT IS A SHORTCUT, AND THAT IS THE PROPERTY TO KEEP WHILE CHANGING THIS
FILE. It may be STALE -- resuming from an older one replays more tail and reaches
the same value -- but it may never be WRONG, because a wrong one is served as
though it were computed. So every failure here (no file, a truncated one, a payload
this build does not understand, a state shape it does not recognise) is answered by
returning ``None`` and letting the caller fold from the start. :meth:`load` and
:meth:`save` never raise, and nothing a caller is served depends on a savepoint
existing or being current.

WHAT MAKES A SAVEPOINT SAFE TO RESUME. An append-only prefix never invalidates one:
the entries a savepoint consumed cannot change, so folding the entries after it
reaches what a cold fold reaches. Three things break that, and each is checked
before a payload is used:

* the STATE SHAPE changed. ``state_version`` belongs to the definition and
  describes what its ``init`` and ``apply`` store. A build whose fold keeps
  different bookkeeping must not resume the old build's state onto new logic --
  that serves pre-change numbers for the life of the store. A mismatch discards.
* the payload FORMAT changed. ``v`` is this module's own envelope version,
  independent of any definition's ``state_version``, so a field added here retires
  old files without every definition having to bump.
* the savepoint describes a DIFFERENT LOG than the one being folded. That is what
  the identity block is for, below.

THE IDENTITY BLOCK, and why it is opaque. The kernel cannot enumerate the ways a
client's log can stop being the log a savepoint came from, so it does not try. A
client hands over a mapping of whatever facts identify its log, the kernel stores it
beside the state and compares it VERBATIM on load, and any difference discards. The
kernel never interprets a key, which is what lets a second client add a condition
without changing this file. dsh's ``checkpointIdentity`` (``formatVersion``,
``createdAt``, ``cwd``, ``isSeeded``, ``inheritedEventCount``) is the same shape and
exists for the same reason: an id names a SLOT, not a lifecycle, so a unit deleted
and recreated under the same id would otherwise pass every watermark check and seed
state folded from an unrelated log.

Equality covers a fact known before the fold and fixed afterwards. It cannot cover a
condition that must be evaluated against live log state, so :meth:`load` takes an
optional ``admit`` predicate for those. It is called with the stored identity block
only after every equality check has passed, and returning ``False`` discards.

HOW THIS MAPS ONTO THE CREW LOG (read this before re-hosting that client -- it is
why the shape above is what it is, and it is recorded here so the mapping is not
rediscovered):

    | this module      | crew_log                        | member eventlog |
    |------------------|---------------------------------|-----------------|
    | store            | ``unit`` = (kind, unit_id)      | slug            |
    | key              | fold ``name``                   | projection key  |
    | state_version    | ``v`` (one constant, all folds) | per definition  |
    | watermark        | ``last_seq``                    | ``observed_seq``|
    | state            | ``state``                       | cell state      |
    | identity block   | ``origin``, ``first_seq``       | (client's own)  |
    | ``admit``        | ``prefix_sha``/``prefix_records`` | (see below)   |

``crew_log.checkpoint`` enforces four admission conditions beyond the three-field
key, and they split cleanly along that line. ``origin`` (a unit recreated under the
same id restarts its seqs) and ``first_seq`` (retention dropped whole segments off
the front) are both known before the fold and are pure equality, so they are
identity-block entries. The prefix digest is not: it is recomputed against the live
file, because a line damaged AFTER it was folded is skipped by a cold fold while a
savepoint keeps the value that line contributed -- so it is an ``admit`` predicate.
Its fourth, ``seq > handle.last_seq`` (the log is shorter than the savepoint), is
also live state and also belongs in ``admit``.

The member log needs the prefix condition too, for the same reason and not a
borrowed one: ``MemberLog.last_seq`` documents that a damaged committed line is
skipped on load, "so a reader loses that line and not the file". The moment its fold
is persisted, a resumed fold and a cold fold can disagree about a damaged log.
``raw_prefix_digest`` and ``raw_records_through`` are on ``CrewLog`` itself rather
than being kind-specific, so one mechanism serves both clients.

The kernel owns no path. A concrete store is handed its directory by the client, so
where savepoints live stays the client's decision -- and for both clients above that
directory is already fenced from the agent's own tools, which is why nothing here
adds a fence entry of its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Envelope version this module writes and is willing to read. THE RULE: any change
#: to what this file STORES bumps it, including one that keeps the same keys. It is
#: independent of a definition's ``state_version``, which describes that fold's own
#: state, so a field added here retires stale files without every definition moving.
PAYLOAD_VERSION: Final[int] = 1

#: Largest savepoint file this module reads or writes. Every fold's state is already
#: bounded by its own definition, so this is a BACKSTOP on those bounds rather than
#: the bound itself: a fold that grew unbounded state loses its savepoint here
#: instead of writing an unbounded file on every read. Exceeding it costs
#: performance and nothing else.
MAX_PAYLOAD_BYTES: Final[int] = 2 * 1024 * 1024

#: Watermark meaning "this fold has consumed nothing", matching the registry's own
#: empty-cell value so a restored cell and a fresh one are the same kind of thing.
EMPTY_WATERMARK: Final[int] = -1

#: Called with the STORED identity block once every equality check has passed.
#: Returning False discards the savepoint. For conditions that must be evaluated
#: against live log state rather than compared to a stored constant.
Admit = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class Savepoint:
    """One fold's resumable position: what it consumed, what it holds, whose log.

    ``state`` must be JSON-serializable and is the fold's own bookkeeping, NOT its
    rendered view: a fold keeps things a reader has no use for, and keeping exactly
    what the fold needs to continue is what lets the view stay the surface a client
    reads.
    """

    key: str
    state_version: int
    watermark: int
    state: Any
    identity: Mapping[str, Any]


class CheckpointStore(Protocol):
    """Where savepoints live. A client supplies the implementation and the path."""

    def load(
        self,
        store: str,
        key: str,
        *,
        state_version: int,
        identity: Mapping[str, Any],
        admit: Admit | None = None,
    ) -> Savepoint | None: ...

    def save(self, store: str, savepoint: Savepoint) -> bool: ...

    def discard(self, store: str, key: str) -> None: ...


class DirectoryCheckpointStore:
    """Savepoints as one JSON file per fold, under a directory the client names.

    One file per fold rather than one file for all of them, so a payload this build
    cannot read costs that fold its savepoint instead of costing every fold, and so
    a client asking for one projection writes one file.

    The file name is derived from the store and key, and both are checked against a
    conservative character set first. A key reaching here is a projection key its
    own definition declared, and a store is a client's unit id -- but this module
    turns them into a PATH, so it refuses anything that could leave the directory
    rather than trusting its caller to have validated them.
    """

    #: Characters a store or key may contain to be usable as a file name. Anything
    #: else is refused rather than escaped: every present caller uses names well
    #: inside this set, so a name outside it is a mistake worth surfacing as "no
    #: savepoint" instead of silently mapping two distinct names onto one file.
    _SAFE: Final[frozenset[str]] = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def path_for(self, store: str, key: str) -> Path | None:
        """The file holding one fold's savepoint, or None if either name is unusable."""
        if not self._is_safe(store) or not self._is_safe(key):
            return None
        return self._root / store / f"{key}.json"

    def _is_safe(self, name: str) -> bool:
        if not name or name in {".", ".."}:
            return False
        return all(char in self._SAFE for char in name)

    # ---- read -------------------------------------------------------------
    def load(
        self,
        store: str,
        key: str,
        *,
        state_version: int,
        identity: Mapping[str, Any],
        admit: Admit | None = None,
    ) -> Savepoint | None:
        """The savepoint for one fold, or None -- meaning "fold from the start".

        Never raises. Every rejection reason collapses to the same answer because a
        caller has the same remedy for all of them, and a savepoint that cannot be
        trusted is not an error a reader should see.
        """
        path = self.path_for(store, key)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAX_PAYLOAD_BYTES:
                logger.debug("savepoint over %d bytes, ignoring: %r", MAX_PAYLOAD_BYTES, path)
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return self._admit(raw, key, state_version, identity, admit)

    @staticmethod
    def _admit(
        raw: dict,
        key: str,
        state_version: int,
        identity: Mapping[str, Any],
        admit: Admit | None,
    ) -> Savepoint | None:
        """*raw* as a Savepoint if every admission condition holds, else None."""
        if raw.get("v") != PAYLOAD_VERSION:
            return None
        if raw.get("key") != key:
            # The file names the fold too, so a payload that moved or was copied
            # under another name is discarded rather than folded as that fold's.
            return None
        if raw.get("state_version") != state_version:
            # The definition's stored shape changed: resume would put old state
            # under new logic, which serves pre-change values for the store's life.
            return None
        watermark = raw.get("watermark")
        if not isinstance(watermark, int) or isinstance(watermark, bool):
            return None
        if watermark < EMPTY_WATERMARK:
            return None
        stored_identity = raw.get("identity")
        if not isinstance(stored_identity, dict):
            return None
        # Compared VERBATIM and never interpreted: the kernel does not know what a
        # client's identity facts mean, only whether they are the same ones.
        if stored_identity != dict(identity):
            return None
        if "state" not in raw:
            return None
        if admit is not None and not admit(stored_identity):
            return None
        return Savepoint(
            key=key,
            state_version=state_version,
            watermark=watermark,
            state=raw["state"],
            identity=stored_identity,
        )

    # ---- write ------------------------------------------------------------
    def save(self, store: str, savepoint: Savepoint) -> bool:
        """Write *savepoint*; True if it landed. Never raises.

        A failed write leaves the caller's IN-MEMORY state authoritative and leaves
        any previous file untouched, because the write goes through
        :func:`~kiro_crew.atomic_write.atomic_write`: the reader either sees the old
        complete payload or the new one, never a half-written file. Losing a
        savepoint costs one cold fold, so it is reported by the return value rather
        than raised -- a caller that has just folded correctly must not be made to
        handle an exception about its cache.
        """
        path = self.path_for(store, savepoint.key)
        if path is None:
            return False
        payload = {
            "v": PAYLOAD_VERSION,
            "key": savepoint.key,
            "state_version": savepoint.state_version,
            "watermark": savepoint.watermark,
            "identity": dict(savepoint.identity),
            "state": savepoint.state,
        }
        try:
            text = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            # A fold whose state is not JSON-serializable cannot be saved. That is
            # the definition's bug, but it must not break the read that folded fine.
            logger.debug("savepoint state is not JSON-serializable, skipping: %r", path)
            return False
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            logger.debug("savepoint would exceed %d bytes, skipping: %r", MAX_PAYLOAD_BYTES, path)
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # fsync=False deliberately: a savepoint lost to a crash costs one cold
            # fold, so paying an fsync on every read-side write would buy nothing the
            # cold fold does not already give for free. The RENAME is still atomic,
            # which is the property that matters -- a reader sees the old complete
            # payload or the new one. newline="" keeps the bytes exactly as written,
            # so a payload is byte-identical across platforms.
            atomic_write(path, text, fsync=False, newline="")
        except OSError:
            return False
        return True

    def discard(self, store: str, key: str) -> None:
        """Remove one fold's savepoint if present. Never raises."""
        path = self.path_for(store, key)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
