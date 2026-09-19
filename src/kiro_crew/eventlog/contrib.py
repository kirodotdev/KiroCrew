"""Contribution protocol: the gateway side of an out-of-process contributor.

Implements ``docs/system-specs/modules/contribution-protocol.md``. Three pieces
live here, and the HTTP handlers, the WebSocket hub and app teardown are thin
callers of them:

``UnitRegistry``
    Which unit kinds have a log. A kind is a REGISTRATION -- ``{kind, id_field,
    frame, service}`` -- so adding a second kind is one ``register_unit`` call
    rather than a rewrite of the routes. Today: ``member``.

``ExternalProjectionStore``
    One row per ``(kind, id, key)`` published from outside, with higher-seq-wins
    and a ``stateVersion`` override, plus an optional render schema per key.
    Durable, because a contributor publishes at its own cadence: an in-memory
    table would drop every contributed card on a gateway restart and leave the
    Members page blank until the contributor happened to re-fold.

``EventBudget``
    Per app, per unit, per UTC day. Deliberately process memory: the budget
    bounds one gateway's exposure to a runaway contributor, and a restart is
    already the loudest possible signal that the process is not the one that
    counted. Persisting it would buy a stricter bound on a resource (log bytes)
    that the 64 KiB per-event cap already bounds.

Nothing here executes contributor code. A contributor reads events, folds in its
own process, and publishes whole values; the gateway stays the only writer of
every log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.eventlog import types

logger = logging.getLogger(__name__)

#: Serialized size cap for one event's ``data`` (contract §4).
MAX_EVENT_DATA_BYTES = 64 * 1024

#: Serialized size cap for one published projection ``value``. Not in the
#: contract's §5 prose, but a projection is a WHOLE value pushed to every
#: dashboard socket, so leaving it unbounded would let a contributor make the
#: Members page unloadable. Ten times the event cap: a folded view legitimately
#: summarises many events.
MAX_PROJECTION_VALUE_BYTES = 640 * 1024

#: Default per-app, per-unit, per-day event budget (contract §4).
DEFAULT_EVENT_BUDGET_PER_DAY = 10_000

#: How many projection keys ONE app may retain for one unit. The per-value size
#: cap above bounds each row and nothing bounds their number, so a contributor
#: holding a wildcard ``<app>/*`` grant can publish a new unique key forever and
#: every one of them is retained in memory and rewritten to disk. Each key is a
#: card in the member drawer, so this is far past any honest fold and still
#: leaves the store bounded.
MAX_PROJECTION_KEYS_PER_APP_UNIT = 64

#: Maximum nesting depth of an event ``data`` or a projection ``value``.
#:
#: Re-exported from the vocabulary module, which is where the redaction pass
#: reads the same number from: this door and that pass must agree, or a payload
#: accepted here becomes a ``RecursionError`` at the network boundary.
MAX_VALUE_DEPTH = types.MAX_VALUE_DEPTH

#: Render kinds a published schema may name (contract §7).
SCHEMA_KINDS = frozenset({"badge", "text", "list", "table", "keyvalue"})

#: The HTTP status each contract §9 error code answers with. ONE table, so a
#: raise site names only a code and the wire status is decided here -- which is
#: also what lets the repo's error-code contract test verify statically that
#: every error response carries a ``code`` (it cannot follow a computed status).
STATUS_FOR_CODE: dict[str, int] = {
    "event_type_not_owned": 403,
    "projection_key_not_owned": 403,
    "unit_kind_not_granted": 403,
    "unit_not_found": 404,
    "event_too_large": 413,
    "projection_too_large": 413,
    "projection_limit": 409,
    "quota_exceeded": 429,
    "stale_seq": 409,
    "invalid_after": 400,
    "invalid_limit": 400,
    "invalid_projection_value": 400,
}


# ---------------------------------------------------------------------------
# Unit kinds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitKind:
    """One registered unit kind.

    ``id_field`` is the name this kind's id carries in a WebSocket frame and in
    the ``projections`` block -- ``slug`` for a member -- so the existing
    ``member_projection`` frame shape is reproduced exactly rather than
    approximated by a generic ``id``.

    ``frame`` is the whole-value push frame for this kind. It is
    ``member_projection`` for members, which is why a contributed row reaches
    the Members page with no new client path.
    """

    kind: str
    id_field: str
    frame: str
    #: Returns the kind's log service. A callable rather than the service
    #: itself: ``get_service()`` is a lazy singleton that rebuilds when the
    #: process's data home moves, and every test repoints it.
    service: Callable[[], Any]
    #: Raises for an id that is not well-formed for this kind. Runs BEFORE any
    #: path is built from the id.
    validate_id: Callable[[str], Any]


_kinds: dict[str, UnitKind] = {}
_kinds_lock = threading.Lock()


def register_unit(unit: UnitKind) -> None:
    """Register a unit kind. Re-registering the same kind replaces it."""
    with _kinds_lock:
        _kinds[unit.kind] = unit


def get_unit(kind: str) -> UnitKind | None:
    with _kinds_lock:
        return _kinds.get(kind)


def unit_kinds() -> tuple[str, ...]:
    with _kinds_lock:
        return tuple(sorted(_kinds))


def _register_builtin_kinds() -> None:
    """Register the kinds this repo ships. Idempotent."""
    from kiro_crew.eventlog import types

    def _member_service() -> Any:
        from kiro_crew.eventlog.service import get_service

        return get_service()

    def _member_validate(id_: str) -> Any:
        from kiro_crew.members import validate_slug

        return validate_slug(id_)

    register_unit(
        UnitKind(
            kind="member",
            id_field="slug",
            frame=types.WS_MEMBER_PROJECTION,
            service=_member_service,
            validate_id=_member_validate,
        )
    )


_register_builtin_kinds()


# ---------------------------------------------------------------------------
# Errors, carrying the contract's machine-readable codes (§9)
# ---------------------------------------------------------------------------


class ContribError(Exception):
    """A refusal carrying a contract §9 machine-readable ``code``.

    The HTTP status is DERIVED from the code through :data:`STATUS_FOR_CODE`
    rather than passed in, so one code cannot answer 403 on one path and 404 on
    another -- a contributor switches on the code, and a code whose status drifts
    per call site is a code that says less than it appears to.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    @property
    def status(self) -> int:
        return STATUS_FOR_CODE.get(self.code, 400)


class ProjectionDeleteIncomplete(Exception):
    """Raised when teardown could not persist every projection deletion.

    Carries the rows that DID land durably (``removed``) beside the units that
    did not (``failed``), because the caller has work to do for both: the
    dashboard still needs a teardown frame for each durable delete, and the
    failure has to be reported rather than swallowed. A bare raise would lose
    the first half and a bare return would lose the second.
    """

    def __init__(self, app: str, removed: list[tuple[str, str, str]], failed: list[str]) -> None:
        self.app = app
        self.removed = removed
        self.failed = failed
        super().__init__(f"contributed projections for {app} not deleted from: {', '.join(failed)}")


# ---------------------------------------------------------------------------
# External projections
# ---------------------------------------------------------------------------


@dataclass
class ExternalRow:
    """One published projection row."""

    value: Any
    seq: int
    state_version: int
    app: str
    schema: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "value": self.value,
            "seq": self.seq,
            "stateVersion": self.state_version,
            "app": self.app,
        }
        if self.schema is not None:
            d["schema"] = self.schema
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExternalRow | None":
        if not isinstance(data, dict) or "value" not in data:
            return None
        try:
            seq = int(data.get("seq", -1))
            state_version = int(data.get("stateVersion", 0))
        except (TypeError, ValueError):
            return None
        app = data.get("app")
        if not isinstance(app, str) or not app:
            return None
        schema = data.get("schema")
        return cls(
            value=data["value"],
            seq=seq,
            state_version=state_version,
            app=app,
            schema=schema if isinstance(schema, dict) else None,
        )


#: What a publish did, so the caller knows whether to push a frame.
@dataclass(frozen=True)
class PublishResult:
    row: ExternalRow
    #: True when the stored row was replaced because ``stateVersion`` rose,
    #: rather than because ``seq`` did. The caller pushes either way; the
    #: distinction is what the audit trail records.
    by_state_version: bool = False


class ExternalProjectionStore:
    """Rows published from outside a unit's own fold, one per ``(kind, id, key)``.

    On-disk layout, one file per unit so a busy unit never contends with an
    unrelated one::

        <data_home>/eventlog/contrib/<kind>/<id>.json
        { "<app>/<key>": {"value": ..., "seq": n, "stateVersion": v, "app": "<app>"} }

    Writes are serialized per unit and go through ``atomic_write``, so a reader
    sees either the previous file or the next one. The in-memory map is the
    authority once loaded; the file exists so a gateway restart does not blank
    every contributed card until each contributor happens to re-publish.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._rows: dict[tuple[str, str], dict[str, ExternalRow]] = {}
        self._loaded: set[tuple[str, str]] = set()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._map_lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    # ---- plumbing ---------------------------------------------------------
    def _path(self, kind: str, id_: str) -> Path:
        # Both segments are validated by the caller (the kind against the
        # registry, the id against its kind's validator) before reaching here.
        return self._root / kind / f"{id_}.json"

    def _lock(self, kind: str, id_: str) -> threading.Lock:
        key = (kind, id_)
        with self._map_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _ensure_loaded(self, kind: str, id_: str) -> dict[str, ExternalRow]:
        """Caller holds the unit lock."""
        key = (kind, id_)
        if key in self._loaded:
            return self._rows.setdefault(key, {})
        rows: dict[str, ExternalRow] = {}
        path = self._path(kind, id_)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            # A hand-edited or truncated file is not worth failing a request
            # over: these rows are re-published by their contributor on its own
            # cadence, so an unreadable file self-heals.
            logger.warning("contrib projections unreadable at %s; starting empty", path)
            raw = {}
        if isinstance(raw, dict):
            for row_key, row_data in raw.items():
                if not isinstance(row_key, str):
                    continue
                row = ExternalRow.from_dict(row_data)
                if row is not None:
                    rows[row_key] = row
        self._rows[key] = rows
        self._loaded.add(key)
        return rows

    def _flush(self, kind: str, id_: str, rows: dict[str, ExternalRow]) -> None:
        """Caller holds the unit lock.

        Always raises on a persistence failure, so no write path can report
        success for a durable write that did not land -- including teardown,
        where an in-memory-only delete would have the dashboard told a card is
        gone and the gateway serving it again after the next cold load.
        """
        from kiro_crew.atomic_write import atomic_write

        path = self._path(kind, id_)
        if not rows:
            # No rows left: remove the file rather than leaving an empty object
            # behind, so an uninstalled app leaves no residue.
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({k: r.to_dict() for k, r in rows.items()}, ensure_ascii=False)
        atomic_write(path, payload)

    # ---- read -------------------------------------------------------------
    def values(self, kind: str, id_: str) -> dict[str, ExternalRow]:
        """Every published row for one unit, keyed ``<app>/<key>``."""
        with self._lock(kind, id_):
            return dict(self._ensure_loaded(kind, id_))

    def get(self, kind: str, id_: str, key: str) -> ExternalRow | None:
        with self._lock(kind, id_):
            return self._ensure_loaded(kind, id_).get(key)

    # ---- write ------------------------------------------------------------
    def publish(
        self,
        kind: str,
        id_: str,
        key: str,
        *,
        app: str,
        value: Any,
        seq: int,
        state_version: int,
    ) -> PublishResult:
        """Store a published value, or refuse it as stale.

        Higher ``seq`` wins. A publish whose ``stateVersion`` is HIGHER than the
        stored row's replaces it regardless of ``seq``, which is how a
        contributor that changed its fold re-publishes from zero. Equal
        ``stateVersion`` and a ``seq`` that did not advance is a replay or a
        slower contributor: ``409 stale_seq``.
        """
        with self._lock(kind, id_):
            rows = self._ensure_loaded(kind, id_)
            existing = rows.get(key)
            if existing is None:
                # A NEW key for this app: count what it already retains for this
                # unit. The size cap bounds one row; a wildcard grant would
                # otherwise let a contributor retain unique keys without end,
                # each one a row held in memory and rewritten to disk on every
                # publish. Counted per APP, so one noisy contributor cannot
                # crowd out another's cards.
                held = sum(1 for r in rows.values() if r.app == app)
                if held >= MAX_PROJECTION_KEYS_PER_APP_UNIT:
                    raise ContribError(
                        "projection_limit",
                        f"{app} already holds {held} projection keys for "
                        f"{kind}/{id_}, the limit is "
                        f"{MAX_PROJECTION_KEYS_PER_APP_UNIT}; publish under a "
                        "key it already holds instead of a new one",
                    )
            by_state_version = False
            if existing is not None:
                if state_version > existing.state_version:
                    by_state_version = True
                elif state_version < existing.state_version:
                    raise ContribError(
                        "stale_seq",
                        f"stateVersion {state_version} is older than the stored "
                        f"{existing.state_version}",
                    )
                elif seq <= existing.seq:
                    raise ContribError(
                        "stale_seq",
                        f"seq {seq} does not advance the stored {existing.seq}",
                    )
            row = ExternalRow(
                value=value,
                seq=seq,
                state_version=state_version,
                app=app,
                # A schema is published separately and outlives a value publish:
                # re-folding must not blank the rendering the key already has.
                schema=existing.schema if existing is not None else None,
            )
            rows[key] = row
            try:
                self._flush(kind, id_, rows)
            except Exception:
                # The durable write did not land; undo the in-memory mutation so
                # the caller gets a failure to retry rather than a success over a
                # row that vanishes on the next cold load.
                if existing is not None:
                    rows[key] = existing
                else:
                    rows.pop(key, None)
                raise
            return PublishResult(row=row, by_state_version=by_state_version)

    def put_schema(
        self, kind: str, id_: str, key: str, *, app: str, schema: dict[str, Any]
    ) -> ExternalRow:
        """Attach a render schema to a key, creating a value-less row if needed.

        A contributor may publish the schema before its first fold completes, so
        this does not require an existing row. Such a row carries ``value:
        None`` at ``seq: -1``, which the first real publish then advances past.
        """
        with self._lock(kind, id_):
            rows = self._ensure_loaded(kind, id_)
            existing = rows.get(key)
            if existing is None:
                # Same retention bound as `publish`: a schema-only publish
                # creates a row, so leaving this path uncounted would be a
                # second way to retain keys without end.
                held = sum(1 for r in rows.values() if r.app == app)
                if held >= MAX_PROJECTION_KEYS_PER_APP_UNIT:
                    raise ContribError(
                        "projection_limit",
                        f"{app} already holds {held} projection keys for "
                        f"{kind}/{id_}, the limit is "
                        f"{MAX_PROJECTION_KEYS_PER_APP_UNIT}",
                    )
                row = ExternalRow(value=None, seq=-1, state_version=0, app=app, schema=schema)
            else:
                row = ExternalRow(
                    value=existing.value,
                    seq=existing.seq,
                    state_version=existing.state_version,
                    app=existing.app,
                    schema=schema,
                )
            rows[key] = row
            try:
                self._flush(kind, id_, rows)
            except Exception:
                if existing is not None:
                    rows[key] = existing
                else:
                    rows.pop(key, None)
                raise
            return row

    def delete_app_rows(self, app: str) -> list[tuple[str, str, str]]:
        """Delete every row *app* published. Returns ``(kind, id, key)`` per row.

        The caller pushes a ``value: null`` frame for each, which is how a
        dashboard learns the card is gone (contract §6). Walks the on-disk tree
        rather than only the loaded map: an app disabled before any request
        touched its unit still has rows on disk.

        A unit whose file cannot be rewritten is RESTORED in memory and reported
        through :class:`ProjectionDeleteIncomplete` rather than counted as
        deleted: the rows are still on disk, so they reload on the next cold
        read, and an in-memory-only delete would have the dashboard told the card
        is gone and the gateway serving it again after a restart. The units that
        did land ride on the exception, so the caller can push their frames and
        still learn what failed.
        """
        removed: list[tuple[str, str, str]] = []
        failed: list[str] = []
        for kind, id_ in self._known_units():
            with self._lock(kind, id_):
                rows = self._ensure_loaded(kind, id_)
                doomed = {k: r for k, r in rows.items() if r.app == app}
                if not doomed:
                    continue
                for k in doomed:
                    del rows[k]
                try:
                    self._flush(kind, id_, rows)
                except Exception:
                    # Put them back: disk still holds them, so memory must too.
                    for k, r in doomed.items():
                        rows[k] = r
                    failed.append(f"{kind}/{id_}")
                    logger.warning(
                        "contrib projections for %s could not be deleted from %s/%s",
                        app,
                        kind,
                        id_,
                        exc_info=True,
                    )
                    continue
                removed.extend((kind, id_, k) for k in doomed)
        if failed:
            raise ProjectionDeleteIncomplete(app, removed, failed)
        return removed

    def _known_units(self) -> list[tuple[str, str]]:
        """Every ``(kind, id)`` with rows, from disk and from memory."""
        found: set[tuple[str, str]] = set()
        with self._map_lock:
            found.update(self._rows.keys())
        try:
            for kind_dir in self._root.iterdir():
                if not kind_dir.is_dir():
                    continue
                for child in kind_dir.iterdir():
                    if child.suffix == ".json" and child.is_file():
                        found.add((kind_dir.name, child.stem))
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("contrib projection root walk failed", exc_info=True)
        return sorted(found)


# ---------------------------------------------------------------------------
# Event budget
# ---------------------------------------------------------------------------


@dataclass
class EventBudget:
    """Per app, per unit, per UTC day append counter."""

    limit: int = DEFAULT_EVENT_BUDGET_PER_DAY
    _counts: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _day(now: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))

    def charge(self, app: str, kind: str, id_: str, *, now: float | None = None) -> int:
        """Count one append, or raise ``429 quota_exceeded``.

        Charged BEFORE the append: over budget is refused, never queued, so a
        contributor cannot spend the budget and then fail the write. A charge
        whose append then FAILS must be handed back with :meth:`release`,
        passing the same ``now`` -- otherwise a run of disk failures spends a
        budget nothing was written against and honest retries are refused.
        """
        day = self._day(now)
        key = (app, kind, id_, day)
        with self._lock:
            used = self._counts.get(key, 0)
            if used >= self.limit:
                raise ContribError(
                    "quota_exceeded",
                    f"{app} has spent its {self.limit} events for {kind}/{id_} today",
                )
            self._counts[key] = used + 1
            # Yesterday's rows are dead weight; drop them opportunistically
            # rather than on a timer.
            if len(self._counts) > 4096:
                self._counts = {k: v for k, v in self._counts.items() if k[3] == day}
            return used + 1

    def release(self, app: str, kind: str, id_: str, *, now: float | None = None) -> None:
        """Hand back one charge whose append did not commit.

        ``now`` MUST be the value the matching :meth:`charge` was given, so the
        release lands on the day that was charged: a write that fails across the
        UTC midnight boundary would otherwise credit a day it never spent.
        Floors at zero and ignores an unknown key -- releasing what was never
        charged must not create budget.
        """
        key = (app, kind, id_, self._day(now))
        with self._lock:
            used = self._counts.get(key)
            if not used:
                return
            self._counts[key] = used - 1

    def used(self, app: str, kind: str, id_: str, *, now: float | None = None) -> int:
        with self._lock:
            return self._counts.get((app, kind, id_, self._day(now)), 0)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------

_store: ExternalProjectionStore | None = None
_store_lock = threading.Lock()
_budget = EventBudget()


def contrib_root() -> Path:
    """Where contributed projection rows live: inside the FENCED crew-log tree.

    These rows are authority, not cache: ``store.values()`` trusts what is on
    disk and the Members drawer renders it, so a row an agent could plant with
    its own file tools would be attacker-authored app state on the user's page.
    Under ``<data home>/eventlog/`` nothing stopped that -- the leaf is neither in
    the file-tool gate's list nor masked from a sandboxed subprocess. Rooted here
    it inherits both fences from the one leaf that already carries them, which is
    the same move that put the member log under this tree.

    A sibling of the per-kind roots, never inside one: those are enumerated as
    units, and a foreign directory among them would have to be skipped by every
    walk instead of simply not being there.
    """
    from kiro_crew.crew_log.store import crew_log_tree_root

    return crew_log_tree_root() / "contrib"


def get_store() -> ExternalProjectionStore:
    """Lazy singleton rooted at the process's data home.

    Rebuilt when the data home moves -- never in production, but every test
    repoints it, and a cached row from the old root would answer for a unit that
    lives elsewhere now. Same discipline as ``eventlog.service.get_service``.
    """
    global _store
    with _store_lock:
        root = contrib_root()
        if _store is None or _store.root != root:
            _store = ExternalProjectionStore(root)
        return _store


def set_store(store: ExternalProjectionStore | None) -> None:
    """Test seam."""
    global _store
    with _store_lock:
        _store = store


def get_budget() -> EventBudget:
    return _budget


# ---------------------------------------------------------------------------
# Validation helpers shared by the HTTP handlers
# ---------------------------------------------------------------------------


def require_unit(kind: str) -> UnitKind:
    unit = get_unit(kind)
    if unit is None:
        raise ContribError("unit_not_found", f"unknown unit kind {kind!r}")
    return unit


def resolve_unit(kind: str, id_: str) -> UnitKind:
    """The registered kind, with *id_* checked and proven to have a log."""
    unit = require_unit(kind)
    try:
        unit.validate_id(id_)
    except Exception as exc:
        raise ContribError("unit_not_found", f"invalid {unit.id_field}: {exc}") from exc
    try:
        svc = unit.service()
        if svc.last_seq(id_) < 0 and not _unit_log_exists(svc, id_):
            raise ContribError("unit_not_found", f"no log for {kind}/{id_}")
    except ContribError:
        raise
    except Exception as exc:
        raise ContribError("unit_not_found", f"no log for {kind}/{id_}: {exc}") from exc
    return unit


def _unit_log_exists(service: Any, id_: str) -> bool:
    """Whether the unit has a log at all, distinct from having no events yet.

    ``last_seq`` answers -1 for both a missing log and an empty one, and a unit
    whose log exists but holds no events is a legitimate append target.
    """
    try:
        return id_ in set(service.slugs())
    except Exception:
        return False


def _refuse_deep_value(value: Any, noun: str) -> None:
    """Refuse a container nested deeper than :data:`MAX_VALUE_DEPTH`.

    Iterative on purpose: a recursive depth CHECK would raise the very
    ``RecursionError`` it exists to keep out of the egress redactor. Only JSON
    containers are descended, which is all ``json.dumps`` produced.
    """
    frontier: list[tuple[Any, int]] = [(value, 1)]
    while frontier:
        node, depth = frontier.pop()
        if not isinstance(node, (dict, list)):
            continue
        if depth > MAX_VALUE_DEPTH:
            raise ContribError(
                "invalid_projection_value",
                f"{noun} nests deeper than {MAX_VALUE_DEPTH} levels",
            )
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                frontier.append((child, depth + 1))


def check_event_data(data: Any) -> str:
    """Enforce the depth bound, then serialize and enforce the 64 KiB cap.

    Depth FIRST: ``json.dumps`` is itself recursive, so a pathologically deep
    payload raises ``RecursionError`` inside the serializer -- an exception the
    route does not answer with a coded 400 -- before any size check could run.
    """
    if not isinstance(data, dict):
        raise ContribError("invalid_projection_value", "event data must be an object")
    _refuse_deep_value(data, "event data")
    try:
        payload = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContribError(
            "invalid_projection_value", f"event data is not JSON-serializable: {exc}"
        ) from exc
    size = len(payload.encode("utf-8"))
    if size > MAX_EVENT_DATA_BYTES:
        raise ContribError(
            "event_too_large",
            f"event data is {size} bytes, over the {MAX_EVENT_DATA_BYTES} byte limit",
        )
    return payload


def check_projection_value(value: Any) -> None:
    """Enforce that a published value is shallow, JSON, and within the size cap.

    Depth first, for the same reason as :func:`check_event_data`: the serializer
    recurses, so it has to be handed something already known to be bounded.
    """
    _refuse_deep_value(value, "value")
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContribError(
            "invalid_projection_value", f"value is not JSON-serializable: {exc}"
        ) from exc
    size = len(payload.encode("utf-8"))
    if size > MAX_PROJECTION_VALUE_BYTES:
        raise ContribError(
            "projection_too_large",
            f"value is {size} bytes, over the {MAX_PROJECTION_VALUE_BYTES} byte limit",
        )


def normalize_schema(raw: Any) -> dict[str, Any]:
    """Validate a published render schema (contract §7).

    Fields: ``title`` (string), ``kind`` (one of :data:`SCHEMA_KINDS`), ``path``
    (a list of string selectors). Anything else is dropped rather than stored:
    the browser renders from this, and an unknown field is a rendering the host
    never agreed to.
    """
    if not isinstance(raw, dict):
        raise ContribError("invalid_projection_value", "schema must be an object")
    kind = raw.get("kind", "keyvalue")
    if not isinstance(kind, str) or kind not in SCHEMA_KINDS:
        raise ContribError(
            "invalid_projection_value",
            f"schema kind must be one of {sorted(SCHEMA_KINDS)}",
        )
    out: dict[str, Any] = {"kind": kind}
    title = raw.get("title")
    if isinstance(title, str) and title:
        out["title"] = title[:120]
    path = raw.get("path")
    if isinstance(path, list):
        selectors = [str(p) for p in path if isinstance(p, str) and p]
        if selectors:
            out["path"] = selectors[:32]
    return out
