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

from kiro_crew.pinned_fs import read_file_pinned
from kiro_crew.security import redact_and_truncate
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

logger = logging.getLogger(__name__)

#: Serialized size cap for one event's ``data`` (contract §4).
MAX_EVENT_DATA_BYTES = 64 * 1024

#: How large ONE unit's contribution file may be before it is read as unreadable.
#: The caps above bound what a contributor may PUBLISH, which bounds what this
#: file should ever hold -- but the file is a cache on disk, not a value in hand,
#: so nothing stops something else from writing it larger. This is the only bound
#: that can run before the read allocates.
MAX_STORE_FILE_BYTES = 8 * 1024 * 1024

#: Serialized size cap for one published projection ``value``. Not in the
#: contract's §5 prose, but a projection is a WHOLE value pushed to every
#: dashboard socket, so leaving it unbounded would let a contributor make the
#: Members page unloadable. Ten times the event cap: a folded view legitimately
#: summarises many events.
MAX_PROJECTION_VALUE_BYTES = 640 * 1024

#: Default per-app, per-unit, per-day event budget (contract §4).
DEFAULT_EVENT_BUDGET_PER_DAY = 10_000

#: Most distinct projection keys one app may hold on one unit.
#:
#: The event budget above bounds a RATE and resets daily; this bounds STANDING
#: state and so cannot reset, because every stored key is written to the unit's
#: contrib file and shipped in every roster response for that member. A grant is
#: a namespace prefix (`<app>/*`), so without a cap one contributor publishing a
#: fresh key per fold grows the roster payload and the file without limit, and
#: the daily event budget does not bound it: one append can publish one new key,
#: and the keys persist after the day rolls over.
#:
#: Generous on purpose -- a contributor legitimately renders a few dozen rows --
#: so hitting it means a key is being MINTED rather than updated.
MAX_PROJECTION_KEYS_PER_UNIT = 200

#: Characters in one render-schema selector. Bounded because a selector is
#: RETAINED in the stored schema and shipped to the browser on every frame, and
#: capping only the selector COUNT leaves 32 slots that can each hold megabytes.
#: Over-long selectors are REFUSED rather than truncated: a selector names a path
#: into the published value, so a shortened one silently names a different path.
MAX_SELECTOR_CHARS = 200

#: Selectors one render schema may name. Was a bare ``[:32]`` slice in three
#: places, which silently DROPPED the rest -- and a dropped selector changes what
#: the card renders just as a shortened one does, so it is refused instead.
MAX_SCHEMA_SELECTORS = 32

#: Bytes a normalized render schema can reach. DERIVED, not chosen: every field
#: the schema retains carries its own cap, so their composition is the bound --
#: 32 selectors of MAX_SELECTOR_CHARS, a 120-character title, and one enum kind,
#: plus JSON punctuation. Stated so a field added later has to be counted here.
MAX_SCHEMA_BYTES = MAX_SCHEMA_SELECTORS * (MAX_SELECTOR_CHARS + 4) + 120 + 64

#: Characters in one contributed event TYPE. The type is retained verbatim in
#: every envelope the append writes, and the namespace pattern it must match
#: bounds its shape but not its length.
MAX_EVENT_TYPE_CHARS = 120

#: Characters in a projection's IDENTITY fields -- its key and its owning app.
#:
#: The sibling caps above bound what a row CONTAINS; these two are what a row
#: IS, and they are retained just as long: the key is a JSON object key in the
#: unit's contrib file and is shipped in every roster response, and the app name
#: rides beside it. Capping the value and the key COUNT leaves 200 slots whose
#: names can each hold megabytes.
#:
#: REFUSED rather than truncated, for the reason :data:`MAX_SELECTOR_CHARS`
#: gives and one worse: a shortened selector names a different path, and a
#: shortened KEY can land on a key another app already holds, so truncating
#: would turn an over-long name into a silent overwrite of someone else's row.
MAX_PROJECTION_IDENTITY_CHARS = 200

#: Rows one unit's contrib FILE may hold in total, across every app in it.
#:
#: :data:`MAX_PROJECTION_KEYS_PER_UNIT` is charged per app, which is a true bound
#: at publish time because an authenticated contributor cannot mint a second app
#: identity. On the LOAD path the app name is read from the file, so the writer
#: picks the bucket key and a per-app cap is not an aggregate cap at all. This is
#: the ceiling that holds whoever writes the file.
#:
#: Deliberately far above the per-app cap: a unit legitimately carries rows from
#: several contributors, so this bounds the FILE without constraining honest use.
MAX_PROJECTION_KEYS_PER_FILE = 2_000

#: Largest ``seq`` or ``stateVersion`` a publish may carry.
#:
#: Higher wins, and a Python int has no width, so a single publish carrying 10**60
#: pins the row at a value no honest fold can ever advance past -- the contributor
#: freezes that key for good, and a refold or a teardown-and-republish cannot take
#: it back. The number is also retained: it is written to the contrib file and
#: shipped in every roster response, so an arbitrary-precision integer is an
#: unbounded retained field like any other.
#:
#: 2**53-1 is the ceiling because these values cross to a browser as JSON, where
#: they land in a double; anything past it is not representable there anyway.
MAX_PROJECTION_SEQ = 2**53 - 1

#: How deeply a published value or an event's ``data`` may nest.
#:
#: A BYTE cap does not bound depth: a thousand nested empty lists is about two
#: kilobytes, far inside the 64 KiB event cap, and the network-boundary redactor
#: that scrubs this payload before it reaches a browser RECURSES over it. Past
#: the interpreter's own recursion limit that redactor raises instead of
#: returning, so unbounded depth is what turns a size-legal payload into a
#: failure of the one control standing between agent-authored text and the
#: dashboard. Bounded HERE, at the point of acceptance, so the depth that
#: defeats the redactor can never be persisted and replayed on every read.
#:
#: Far above any real fold: a rendered card is a handful of levels deep.
MAX_JSON_DEPTH = 32

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
    # Same class as the line above and so the same status, but a separate code
    # because the remedy differs: that one says shrink THIS value, this one says
    # the unit's store is full and another key has to go first.
    "projection_store_too_large": 413,
    "quota_exceeded": 429,
    "stale_seq": 409,
    "invalid_after": 400,
    "invalid_limit": 400,
    "invalid_event_type": 400,
    "invalid_projection_value": 400,
    "invalid_projection_key": 400,
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
            # Size asked of the filesystem BEFORE the read. Every other bound in this
            # module applies to a published value, which is the right place for a
            # bound on what is retained -- but this file is read whole in one call,
            # and it is agent-writable by the same reasoning that makes it merely a
            # cache, so a bound that runs after the read has already spent the
            # memory. Over the ceiling reads as unreadable, which is the answer this
            # loader already gives a hand-edited file: the rows are re-published by
            # their contributor on its own cadence, so the store self-heals.
            # The read is PINNED for the same reason the write half is: this root is
            # agent-writable without going through ``publish``, so a name here is a
            # disclosure primitive pointed at whatever it currently resolves to. A
            # planted link would be followed out of the store, and a planted FIFO
            # would stat within the ceiling and then block this thread forever while
            # it holds the per-unit lock -- a hang no ``except`` arm below can catch,
            # because a blocking read raises nothing. ``read_file_pinned`` refuses a
            # non-regular target through the descriptor it reads and bounds the read
            # itself, so the ceiling is enforced before the bytes are spent.
            text = read_file_pinned(
                path,
                what=f"contributed {kind} projection rows",
                max_bytes=MAX_STORE_FILE_BYTES + 1,
            )
            if len(text) > MAX_STORE_FILE_BYTES:
                logger.warning(
                    "contrib projections at %s exceed %d bytes; starting empty",
                    path,
                    MAX_STORE_FILE_BYTES,
                )
                raw = {}
            else:
                raw = json.loads(text)
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            # A hand-edited or truncated file is not worth failing a request
            # over: these rows are re-published by their contributor on its own
            # cadence, so an unreadable file self-heals.
            logger.warning("contrib projections unreadable at %s; starting empty", path)
            raw = {}
        if isinstance(raw, dict):
            held: dict[str, int] = {}
            dropped = 0
            for row_key, row_data in raw.items():
                if not isinstance(row_key, str):
                    continue
                row = ExternalRow.from_dict(row_data)
                if row is None:
                    continue
                # The bounds the publish path enforces are re-applied HERE. The
                # contrib root is not fenced from an agent's file tools, so this
                # file is writable without going through `publish`, and a row
                # that never passed a bound is retained in memory AND shipped in
                # every roster response for the unit. Bounding only on the way in
                # leaves the invariant resting on the file never being edited.
                #
                # An over-cap row is DROPPED rather than failing the whole read,
                # for the reason the unreadable-file arm above gives: rows are
                # re-published by their contributor on its own cadence, so a drop
                # self-heals, while a refusal takes the Members page down until
                # someone edits the file by hand.
                try:
                    check_projection_identity(row_key, row.app)
                    check_projection_authority(row_key, row.app)
                    check_projection_counters(row.seq, row.state_version)
                    check_projection_value(row.value)
                except ContribError:
                    dropped += 1
                    continue
                if row.schema is not None:
                    try:
                        row.schema = normalize_schema(row.schema)
                    except ContribError:
                        # A row whose value is fine but whose schema is not still
                        # renders -- without a schema the card falls back to a
                        # key-value dump -- so drop the schema, not the row.
                        row.schema = None
                # TOTAL first, then per-app. The per-app cap is the real bound at
                # PUBLISH time, where `app` comes from an authenticated token and a
                # contributor cannot choose a second identity. Here `app` is read
                # from the FILE, and this file is writable without going through
                # publish -- so the writer chooses how many buckets exist, and a
                # per-app cap alone bounds nothing in aggregate: 10,000 fabricated
                # app names each get their own allowance. The total is the only
                # ceiling a writer who picks the bucket key cannot widen.
                if len(rows) >= MAX_PROJECTION_KEYS_PER_FILE:
                    dropped += 1
                    continue
                if held.get(row.app, 0) >= MAX_PROJECTION_KEYS_PER_UNIT:
                    dropped += 1
                    continue
                held[row.app] = held.get(row.app, 0) + 1
                rows[row_key] = row
            if dropped:
                logger.warning(
                    "contrib projections at %s: dropped %d row(s) the published rules "
                    "refuse (a key naming a gateway projection or outside its app's "
                    "namespace, a key or value carrying a credential, a value past %d "
                    "bytes, or more than %d keys for one app)",
                    path,
                    dropped,
                    MAX_PROJECTION_VALUE_BYTES,
                    MAX_PROJECTION_KEYS_PER_UNIT,
                )
        self._rows[key] = rows
        self._loaded.add(key)
        return rows

    def _flush(self, kind: str, id_: str, rows: dict[str, ExternalRow]) -> None:
        """Caller holds the unit lock.

        Raises on a persistence failure so a write path that reports success to
        its caller does not acknowledge a durable write that did not land. Every
        caller undoes its own in-memory mutation and reports the failure; there is
        no best-effort mode, because the one caller that had it -- the teardown
        sweep -- is the case where an unwritten delete does NOT self-heal: the
        app's grant is revoked in the same flow, so no contributor remains to
        republish, and the next cold load restores the row from disk for good.
        """
        from kiro_crew.pinned_fs import unlink_pinned, write_file_pinned

        path = self._path(kind, id_)
        what = f"contributed {kind} projection rows"
        if not rows:
            # No rows left: remove the file rather than leaving an empty
            # object behind, so an uninstalled app leaves no residue.
            unlink_pinned(path, what=what)
            return
        payload = json.dumps({k: r.to_dict() for k, r in rows.items()}, ensure_ascii=False)
        # Refused HERE, against the same ceiling the loader applies to the file it
        # reads, because the per-value and per-key bounds multiply past it: a value
        # may be MAX_PROJECTION_VALUE_BYTES and an app may hold
        # MAX_PROJECTION_KEYS_PER_UNIT of them, so thirteen near-limit values --
        # far inside the key bound -- serialize past MAX_STORE_FILE_BYTES. Each
        # such publish would succeed, and the next cold load would then refuse the
        # whole file and start empty, losing every durable row in it rather than
        # only the one that crossed the line. Measured on the UTF-8 encoding, since
        # the ceiling is compared against a byte size on disk and
        # ``ensure_ascii=False`` leaves multi-byte characters in the payload.
        #
        # Raising is what rolls the publish back: every caller of this method
        # already undoes its own in-memory mutation on an exception, so a refused
        # publish leaves the rows as they were rather than holding a value the file
        # on disk does not carry.
        size = len(payload.encode("utf-8"))
        if size > MAX_STORE_FILE_BYTES:
            raise ContribError(
                "projection_store_too_large",
                f"{what} would be {size} bytes, over the {MAX_STORE_FILE_BYTES} byte "
                f"limit for one unit's store; delete a key before publishing another",
            )
        # This store's own tree root, created before the pinned publish because
        # ``create_and_open_dir_pinned`` states the division in its own docstring:
        # it pins the parent chain and creates ONLY the final component, so "the
        # parent must therefore already exist -- callers create their own tree
        # roots". ``contrib_root()`` is two levels under the data home and no
        # other writer visits it, so without this the first publish on a fresh
        # home fails on the missing ``eventlog`` component rather than on
        # anything about the payload.
        #
        # Private mode, and by name for the root only: the same trade the sibling
        # crew-log store makes for its own root. A by-name ``parents=True``
        # follows a link already sitting at an ancestor, which is the residual
        # ``pin_parent`` documents and accepts because refusing every symlinked
        # ancestor breaks a destination under ``/tmp`` on macOS. Matching the
        # sibling keeps one answer for tree roots instead of a second, stricter
        # spelling here that would still share the same residual.
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Published through the PINNED primitives, not by name. A by-name write
        # is a truncation aimed at whatever the path resolves to at that
        # instant, so a link planted at a parent component sends this publish
        # at a file outside the contribution store -- and the agent that can
        # plant it is not the contributor whose rows these are. ``pinned_fs``
        # documents itself as the single no-follow publish path for exactly
        # this, and pins the parent through one descriptor so no ancestor is
        # re-resolved between the check and the write.
        write_file_pinned(path, payload, what=what)

    # ---- read -------------------------------------------------------------
    def values(self, kind: str, id_: str) -> dict[str, ExternalRow]:
        """Every published row for one unit, keyed ``<app>/<key>``."""
        with self._lock(kind, id_):
            return dict(self._ensure_loaded(kind, id_))

    def get(self, kind: str, id_: str, key: str) -> ExternalRow | None:
        with self._lock(kind, id_):
            return self._ensure_loaded(kind, id_).get(key)

    # ---- write ------------------------------------------------------------
    def _refuse_a_minted_key(
        self, rows: dict[str, ExternalRow], key: str, *, app: str, kind: str, id_: str
    ) -> None:
        """Refuse a NEW key once *app* holds ``MAX_PROJECTION_KEYS_PER_UNIT`` here.

        Charged on creation only: a key the app already holds is always writable,
        so a contributor at the cap can still refold every row it owns and is only
        stopped from minting another. Counted per app, so one contributor cannot
        spend another's headroom on a unit they both publish to.

        Called by every path that can create a row -- ``publish`` and
        ``put_schema`` -- rather than by ``publish`` alone, because a schema
        publish creates a value-less row and would otherwise be the way around
        the cap.
        """
        if key in rows:
            return
        held = sum(1 for row in rows.values() if row.app == app)
        if held >= MAX_PROJECTION_KEYS_PER_UNIT:
            raise ContribError(
                "quota_exceeded",
                f"{app} already holds {held} projection keys on {kind}/{id_}, the "
                f"limit of {MAX_PROJECTION_KEYS_PER_UNIT}; update an existing key "
                f"instead of publishing a new one",
            )

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
        still_granted: Callable[[], bool] | None = None,
    ) -> PublishResult:
        """Store a published value, or refuse it as stale.

        Higher ``seq`` wins. A publish whose ``stateVersion`` is HIGHER than the
        stored row's replaces it regardless of ``seq``, which is how a
        contributor that changed its fold re-publishes from zero. Equal
        ``stateVersion`` and a ``seq`` that did not advance is a replay or a
        slower contributor: ``409 stale_seq``.

        ``still_granted`` is re-asked HERE, under the lock that writes. The route
        checked the grant before handing this work off, and the handoff is an await:
        an app revoked while its publish is in flight had already passed that check,
        so without re-asking, the write lands for an app whose grant is already gone.
        Asking under the write lock is what makes the decision and the write one
        step -- a check anywhere earlier is a check that can go stale.
        """
        with self._lock(kind, id_):
            if still_granted is not None and not still_granted():
                raise ContribError(
                    "unit_kind_not_granted",
                    f"{app} does not hold a contribution grant for {kind}",
                )
            rows = self._ensure_loaded(kind, id_)
            check_projection_identity(key, app)
            self._refuse_a_minted_key(rows, key, app=app, kind=kind, id_=id_)
            existing = rows.get(key)
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
        self,
        kind: str,
        id_: str,
        key: str,
        *,
        app: str,
        schema: dict[str, Any],
        still_granted: Callable[[], bool] | None = None,
    ) -> ExternalRow:
        """Attach a render schema to a key, creating a value-less row if needed.

        A contributor may publish the schema before its first fold completes, so
        this does not require an existing row. Such a row carries ``value:
        None`` at ``seq: -1``, which the first real publish then advances past.

        ``still_granted`` is re-asked HERE, under the lock that writes, for the same
        reason :meth:`publish` re-asks it: the route checked the grant and then handed
        this work off across an await, so an app revoked mid-flight had already passed
        that check. A schema is not a lesser write -- it is what the dashboard renders
        the key WITH -- so a revoked app must not be able to leave one behind.
        """
        with self._lock(kind, id_):
            if still_granted is not None and not still_granted():
                raise ContribError(
                    "projection_key_not_owned",
                    f"{app} does not hold a contribution grant for {key!r}",
                )
            rows = self._ensure_loaded(kind, id_)
            check_projection_identity(key, app)
            self._refuse_a_minted_key(rows, key, app=app, kind=kind, id_=id_)
            existing = rows.get(key)
            if existing is None:
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

        A unit whose delete cannot be PERSISTED is rolled back and left out of the
        answer, the same shape ``publish`` and ``put_schema`` use. Reporting it
        would be worse than reporting nothing: the caller would broadcast the card
        as gone while the row survives on disk, and the usual self-healing does not
        apply here because this runs inside a flow that has already revoked the
        app's grant -- no contributor remains to republish, so the next cold load
        restores the row and the card returns with nothing able to correct it.
        Excluding the unit keeps the stale card on screen, which is the truthful
        state and the one the operator can act on.
        """
        removed: list[tuple[str, str, str]] = []
        for kind, id_ in self._known_units():
            with self._lock(kind, id_):
                rows = self._ensure_loaded(kind, id_)
                doomed = [k for k, r in rows.items() if r.app == app]
                if not doomed:
                    continue
                kept = {k: rows[k] for k in doomed}
                for k in doomed:
                    del rows[k]
                try:
                    self._flush(kind, id_, rows)
                except Exception:
                    rows.update(kept)
                    logger.warning(
                        "contrib projections for %s/%s could not be deleted for app %s; "
                        "the rows are unchanged",
                        kind,
                        id_,
                        app,
                        exc_info=True,
                    )
                    continue
                removed.extend((kind, id_, k) for k in doomed)
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

        Charged BEFORE the append so that going over budget is refused rather than
        queued. A charge is therefore a RESERVATION, not a record of a committed
        event: a caller whose append then fails or is refused hands it back with
        :meth:`release`, passing the same ``now``, or the contributor pays for an
        event the log does not hold.
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
        """Hand back one :meth:`charge` whose append did not land.

        Pass the ``now`` the charge used. The counter is keyed by UTC day, so a
        release that re-derives the day from the clock would decrement a fresh
        day's bucket for a charge taken seconds earlier on the previous one --
        handing the contributor a free event instead of its own back.

        A key that is absent or already at zero is left alone rather than driven
        negative, so a release the pruning above has outlived, or a second release
        for one charge, cannot mint budget.
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
    from kiro_crew.config.paths import data_home

    return data_home() / "eventlog" / "contrib"


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

    A unit whose log exists but holds no events is a legitimate append target,
    so existence is asked of the service's own roster rather than inferred from
    a cursor. The member service does answer ``-1`` only for a MISSING log and
    ``0`` for an empty one, which would make this redundant for that kind alone
    -- but ``resolve_unit`` is generic over registered kinds, and a kind is free
    to spell its empty cursor differently.
    """
    try:
        return id_ in set(service.slugs())
    except Exception:
        return False


def check_json_depth(value: Any, what: str) -> None:
    """Refuse a payload nested past :data:`MAX_JSON_DEPTH`.

    Walks an explicit stack rather than recursing: a recursive depth check would
    hit the very interpreter limit it exists to keep the redactor away from, and
    would raise RecursionError instead of this module's coded refusal.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ContribError(
                "invalid_projection_value",
                f"{what} nests deeper than {MAX_JSON_DEPTH} levels",
            )
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend((child, depth + 1) for child in node)


def check_event_data(data: Any) -> str:
    """Serialize an event's ``data`` and enforce the 64 KiB cap."""
    if not isinstance(data, dict):
        raise ContribError("invalid_projection_value", "event data must be an object")
    check_json_depth(data, "event data")
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


def check_projection_counters(seq: int, state_version: int) -> None:
    """Enforce the counter ceiling on a row, wherever it entered from.

    The route checks a caller's numbers, but the contrib file is writable without
    going through it, and HIGHER WINS: a hand-written row carrying an over-limit
    ``seq`` is retained on load and then every valid publish for that key is
    refused as stale, for good. A bound applied only where a value ENTERS is not a
    bound on what LOAD retains -- the same reason the value and identity checks
    above are re-applied here.
    """
    for label, value in (("seq", seq), ("stateVersion", state_version)):
        if value > MAX_PROJECTION_SEQ:
            raise ContribError(
                "invalid_projection_value",
                f"{label} is {value}, over the {MAX_PROJECTION_SEQ} limit",
            )


def check_projection_identity(key: str, app: str) -> None:
    """Enforce what a projection's key and owning app may BE.

    Separate from :func:`check_projection_value` because these two are what a row
    IS rather than what it holds, and they must be checked by every path that can
    create a row -- the same rule, and for the same reason, that
    ``_refuse_a_minted_key`` states for the key COUNT.

    Two bounds, and the second is not a size. The key is app-authored text that
    travels verbatim: it is placed in the WebSocket frame beside the redacted
    value, written into the SEL audit ``resources`` string and persisted in this
    store, and no path redacts it. A key is therefore REFUSED when its own text
    carries a credential or an exfiltration URL, rather than rewritten -- it is an
    identifier, so the grant patterns match on it and a rewritten key would stop
    matching the pattern its contributor declared and would address a different
    row than the one published. Detection is the same chain the projection egress
    paths run over the value, so the two boundaries cannot disagree about what a
    credential looks like.
    """
    for label, value in (("key", key), ("app", app)):
        if len(value) > MAX_PROJECTION_IDENTITY_CHARS:
            raise ContribError(
                "invalid_projection_key",
                f"{label} is {len(value)} characters, over the "
                f"{MAX_PROJECTION_IDENTITY_CHARS} character limit",
            )
    if _identifier_carries_a_credential(key):
        raise ContribError(
            "invalid_projection_key",
            "key carries a credential or an exfiltration URL; publish under a key "
            "whose own text holds neither",
        )


def check_projection_authority(key: str, app: str) -> None:
    """Enforce WHOSE a projection row is, which no size bound can express.

    Two rules, and the publish path applies both through ``grants``: a key may not
    name one of the gateway's own folds, and it must sit under its app's own
    ``<app>/`` prefix. They belong on every path that admits a row, not only the
    one this protocol writes through, because the store file is reachable by an
    agent's file tools without passing ``publish`` -- so a publish-time check
    bounds what this protocol writes, not what the file holds.

    Each rule answers a different impersonation. A row keyed like a gateway fold
    answers in place of the value the gateway computed, because the roster
    response merges contributed rows over its own folds. A row outside its app's
    namespace outlives the only app that can retract it, because teardown matches
    on ``app`` while the row claims someone else's prefix.

    The first rule is currently unreachable on its own: every key in
    ``ALL_PROJECTION_KEYS`` is a bare name, so the prefix rule refuses each of them
    anyway. It is stated separately because that is a property of today's key names
    rather than of the rules, and ``TestProjectionAuthority`` pins it -- a gateway
    key that ever carried a ``/`` would be admissible under the prefix rule to an
    app holding the matching name, and the test fails the day one is added.
    """
    from kiro_crew.eventlog import types

    if key in types.ALL_PROJECTION_KEYS:
        raise ContribError(
            "invalid_projection_key",
            f"key {key!r} names a projection the gateway owns",
        )
    if not key.startswith(f"{app}/"):
        raise ContribError(
            "invalid_projection_key",
            f"key {key!r} is outside the {app!r} namespace it is attributed to",
        )


def _identifier_carries_a_credential(identifier: str) -> bool:
    """True when the identifier's own text carries a credential or exfil URL.

    The chain is the one :func:`check_projection_identity` documents: an
    identifier whose text the redactor would alter is one that must not travel
    verbatim, and every surface this store feeds carries it verbatim.
    """
    text, _ = redact_exfiltration_urls(identifier)
    text, _ = redact_credentials(text)
    return text != identifier


def check_projection_value(value: Any) -> None:
    """Enforce that a published value is JSON and within the size cap."""
    check_json_depth(value, "value")
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
        # Redact the WHOLE title, then truncate -- ``redact_and_truncate`` is
        # exactly that composition and the established spelling of it here. The
        # redactors match a credential as a whole token, so a secret severed at
        # character 120 leaves a stump their patterns do not match, and the
        # surviving prefix renders in the dashboard. Slicing a redaction
        # placeholder costs nothing; slicing a secret publishes it. The 120 cap
        # still holds afterwards, so the derived ceiling below is unaffected.
        out["title"] = redact_and_truncate(title, 120)
    path = raw.get("path")
    if isinstance(path, list):
        selectors = [str(p) for p in path if isinstance(p, str) and p]
        # REFUSED before slicing, for the same reason the length check below gives
        # and with the same consequence: a DROPPED selector makes the card render a
        # different shape than the contributor published, exactly as a shortened one
        # would. Slicing first also capped the length check at the first 32, so a
        # 33rd over-long selector was never examined at all.
        if len(selectors) > MAX_SCHEMA_SELECTORS:
            raise ContribError(
                "invalid_projection_value",
                f"schema names {len(selectors)} selectors, over the "
                f"{MAX_SCHEMA_SELECTORS} selector limit",
            )
        for selector in selectors:
            # REFUSED, not truncated: a selector names a path into the published
            # value, so a shortened one names a different path and the card would
            # render something the contributor never published. The count cap
            # below says nothing about how long each one is.
            if len(selector) > MAX_SELECTOR_CHARS:
                raise ContribError(
                    "invalid_projection_value",
                    f"schema selector is {len(selector)} characters, over the "
                    f"{MAX_SELECTOR_CHARS} character limit",
                )
        if selectors:
            out["path"] = selectors
    # The serialized schema needs no ceiling of its own: every field it RETAINS
    # carries one, so their composition is the bound. ``kind`` is one of a fixed
    # set, ``title`` is truncated at 120, and ``path`` is at most 32 selectors of
    # at most MAX_SELECTOR_CHARS each -- so a normalized schema cannot exceed
    # MAX_SCHEMA_BYTES, which is derived from those numbers rather than chosen.
    # A separate runtime size check would be a branch that can never fire.
    return out
