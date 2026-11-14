"""The memory provider contract: the data a provider exchanges with Crew, and its errors.

A provider stores and finds memory items. Crew keeps everything else: who is asking
(``ProviderScope`` is built from the session's execution record, never by the
provider), which memory mode applies, governance, budgets, prompt framing and content
screens. A provider returns DATA -- items with text, ids and timestamps -- and Crew
renders the blocks the model sees.

The first call is ``connect``: Crew says which API versions it speaks and which mode
it wants, and the provider answers with :class:`Capabilities` -- the operations it
serves, the kinds it holds, and the write, history and isolation properties a mode
needs. :meth:`Capabilities.mode_gaps` turns that answer into the list of what a mode
lacks, which the picker shows and the switch refuses on.

Items carry enough to move without loss: the timestamps, the ``origin`` they were
first written under, an optional revision, and reserved ``crew.*`` meta keys for
Crew's provenance and lesson fields. The ``document`` kind holds one whole Markdown
document (Global V1's ``preferences.md`` or ``projects.md``).

The wire form of every type is the JSON object its ``to_wire`` writes and its
``*_from_wire`` reads. Every value that crosses from a provider is validated here:
an answer that does not have the documented shape raises
:class:`ProviderInvalidResponse`, which callers treat as unavailable.
"""

from __future__ import annotations

import asyncio
import builtins
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

PROVIDER_API_VERSION = 1
#: Every contract version this Crew speaks, sent in ``connect``.
API_VERSIONS: tuple[int, ...] = (1,)

Kind = Literal[
    "preference", "fact", "episode", "summary", "rule", "finding", "index", "note", "document"
]
KINDS: frozenset[str] = frozenset(
    ("preference", "fact", "episode", "summary", "rule", "finding", "index", "note", "document")
)
#: The kinds Crew's lesson tools speak: a ``rule`` is a standing correction, a
#: ``finding`` an on-topic experience.
LESSON_KINDS: frozenset[str] = frozenset(("rule", "finding"))
#: The kinds a provider must hold to replace Crew's memory.
REPLACE_KINDS: frozenset[str] = frozenset(("rule", "finding", "preference", "fact"))
#: The Markdown documents a ``document`` item can be, named in ``meta["crew.doc"]``.
DOCUMENT_NAMES: frozenset[str] = frozenset(("preferences", "projects"))
STATUSES: frozenset[str] = frozenset(("active", "draft", "retired"))
#: ``list`` takes one of these; ``all`` is every status, retired included.
LIST_STATUSES: frozenset[str] = frozenset(("active", "draft", "retired", "all"))
#: ``put`` answers one of the first five; ``delete`` answers ``deleted`` or ``unchanged``.
OUTCOMES: frozenset[str] = frozenset(
    ("inserted", "enriched", "unchanged", "deduped", "refused", "deleted")
)
ERROR_CODES: frozenset[str] = frozenset(
    ("unavailable", "refused", "read_only", "not_supported", "conflict", "invalid")
)
#: Every operation a provider may serve. ``connect``, ``health`` and ``context`` are
#: required; the rest are listed in the ``connect`` answer's ``ops``.
OPS: frozenset[str] = frozenset(
    (
        "context",
        "search",
        "get",
        "list",
        "put",
        "delete",
        "put_batch",
        "changes",
        "revisions",
        "ingest",
    )
)
WRITE_OPS: frozenset[str] = frozenset(("put", "delete", "put_batch", "ingest"))
#: What ``replace`` needs on top of ``context``.
REPLACE_OPS: tuple[str, ...] = ("get", "list", "put", "delete", "search")
SEARCH_FILTERS: frozenset[str] = frozenset(("kind", "scope", "status", "updated_after"))
#: Meta keys Crew writes for its own fields. Opaque to a provider, which returns them
#: as written. ``negative`` is a lesson's NOT clause.
RESERVED_META_KEYS: frozenset[str] = frozenset(
    (
        "crew.source",
        "crew.confidence",
        "crew.importance",
        "crew.written_by",
        "crew.session_key",
        "crew.derived_from",
        "crew.category",
        "crew.retired_reason",
        "crew.doc",
        "negative",
    )
)

MAX_ITEM_ID = 256
MAX_ITEM_TEXT = 8_000
MAX_DOCUMENT_TEXT = 64_000
MAX_TITLE = 300
MAX_META_PAIRS = 50
MAX_META_VALUE = 1_000
MAX_SCOPES = 20
MAX_SCOPE_CHARS = 200
MAX_PAGE = 200
MAX_BATCH = 100
MAX_ANSWER_ITEMS = 200
MAX_ANSWER_BYTES = 512 * 1024
MAX_REASON = 500
MAX_REVISIONS = 200
MAX_NAME = 100
TRUNCATED_MARK = " [truncated]"

#: Per-operation deadlines, in seconds. ``context`` takes
#: ``memory.provider.timeout_secs`` instead (default 3, at most 10).
OP_DEADLINES: Mapping[str, float] = {
    "connect": 5.0,
    "health": 5.0,
    "search": 5.0,
    "get": 5.0,
    "list": 5.0,
    "revisions": 5.0,
    "put": 10.0,
    "delete": 10.0,
    "changes": 10.0,
    "put_batch": 30.0,
    "ingest": 60.0,
}
CONTEXT_TIMEOUT_DEFAULT = 3.0
CONTEXT_TIMEOUT_MAX = 10.0


def text_cap(kind: str) -> int:
    """The longest text an item of *kind* may carry."""
    return MAX_DOCUMENT_TEXT if kind == "document" else MAX_ITEM_TEXT


class ProviderError(Exception):
    """A provider call that did not produce an answer Crew can use."""

    code = "unavailable"

    def __init__(self, message: str = "", *, retry_after_secs: float | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.retry_after_secs = retry_after_secs


class ProviderUnavailable(ProviderError):
    """Timeout, crash, an unhealthy backend, or an ``unavailable`` answer."""

    code = "unavailable"


class ProviderRefused(ProviderError):
    """The provider refused: auth, quota or its own policy. ``message`` is the reason."""

    code = "refused"


class ProviderReadOnly(ProviderError):
    """A write was sent to a provider that only reads."""

    code = "read_only"


class ProviderNotSupported(ProviderError):
    """The provider does not implement this operation."""

    code = "not_supported"


class ProviderConflict(ProviderError):
    """The write conflicts with the provider's current state (a moved id, a stale revision)."""

    code = "conflict"


class ProviderInvalidResponse(ProviderUnavailable):
    """The answer did not have the documented shape. Treated as unavailable."""

    code = "invalid"


_ERRORS_BY_CODE: Mapping[str, type[ProviderError]] = {
    "unavailable": ProviderUnavailable,
    "refused": ProviderRefused,
    "read_only": ProviderReadOnly,
    "not_supported": ProviderNotSupported,
    "conflict": ProviderConflict,
    "invalid": ProviderInvalidResponse,
}


def error_from_wire(error: object) -> ProviderError:
    """The exception a wire ``{"ok": false, "error": ...}`` names."""
    if not isinstance(error, dict):
        return ProviderInvalidResponse("error is not an object")
    code = error.get("code")
    message = error.get("message")
    retry = error.get("retry_after_secs")
    cls = _ERRORS_BY_CODE.get(code) if isinstance(code, str) else None
    if cls is None:
        return ProviderInvalidResponse("unknown error code")
    text = message[:MAX_REASON] if isinstance(message, str) else ""
    retry_after = (
        float(retry)
        if isinstance(retry, (int, float)) and not isinstance(retry, bool) and 0 <= retry <= 3600
        else None
    )
    return cls(text, retry_after_secs=retry_after)


@dataclass(frozen=True)
class ProviderScope:
    """Who a call is for. Built by Crew from the session's execution record."""

    store_id: str
    member_id: str | None
    memory_mode: Literal["persistent", "incognito"]
    session_id: str
    project: str | None = None
    #: Writer attribution in a Shared Crew; never an authorization claim.
    principal: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "member_id": self.member_id,
            "memory_mode": self.memory_mode,
            "session_id": self.session_id,
            "project": self.project,
            "principal": self.principal,
        }


@dataclass(frozen=True)
class Origin:
    """Where an item was first written: the provider, store and id it had there.

    A move writes it on every item, so a second move or a switch back can tell an
    item that came from the destination from one that did not.
    """

    provider: str
    store: str
    id: str

    def to_wire(self) -> dict[str, str]:
        return {"provider": self.provider, "store": self.store, "id": self.id}


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: str
    text: str
    title: str | None = None
    scopes: tuple[str, ...] = ()
    source: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    status: str = "active"
    score: float | None = None
    meta: Mapping[str, str] = field(default_factory=dict)
    origin: Origin | None = None
    revision: int | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "text": self.text}
        for name in ("title", "source", "created_at", "updated_at", "score", "revision"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.scopes:
            out["scopes"] = list(self.scopes)
        if self.status != "active":
            out["status"] = self.status
        if self.meta:
            out["meta"] = dict(self.meta)
        if self.origin is not None:
            out["origin"] = self.origin.to_wire()
        return out


@dataclass(frozen=True)
class NewItem:
    """An item to write. ``put`` with an ``item_id`` replaces that item.

    ``created_at`` and ``updated_at`` are honoured by a provider that says
    ``preserves_timestamps``; ``origin`` is kept by one that says ``keeps_origin``.
    """

    kind: str
    text: str
    title: str | None = None
    scopes: tuple[str, ...] = ()
    source: str | None = None
    status: str = "active"
    meta: Mapping[str, str] = field(default_factory=dict)
    created_at: float | None = None
    updated_at: float | None = None
    origin: Origin | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "text": self.text}
        for name in ("title", "source", "created_at", "updated_at"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.scopes:
            out["scopes"] = list(self.scopes)
        if self.status != "active":
            out["status"] = self.status
        if self.meta:
            out["meta"] = dict(self.meta)
        if self.origin is not None:
            out["origin"] = self.origin.to_wire()
        return out


@dataclass(frozen=True)
class WriteResult:
    """A write's outcome, in the vocabulary Crew's lesson tools already speak."""

    outcome: str
    id: str | None = None
    reason: str | None = None
    superseded: tuple[str, ...] = ()
    winner: str | None = None

    @property
    def stored(self) -> bool:
        return self.outcome != "refused"

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"outcome": self.outcome}
        if self.id is not None:
            out["id"] = self.id
        if self.reason is not None:
            out["reason"] = self.reason
        if self.superseded:
            out["superseded"] = list(self.superseded)
        if self.winner is not None:
            out["winner"] = self.winner
        return out


@dataclass(frozen=True)
class Capabilities:
    """A provider's ``connect`` answer: what it serves and how it writes.

    Nested on the wire (``search``, ``writes``, ``history``, ``isolation``); flat here.
    A ``read_only`` provider's write operations are dropped from :attr:`ops` when the
    answer is read, so nothing downstream has to check both.
    """

    api_version: int
    provider_name: str
    provider_version: str
    egress: str
    read_only: bool
    ops: frozenset[str]
    kinds: frozenset[str]
    search_lexical: bool = False
    search_semantic: bool = False
    search_filters: frozenset[str] = frozenset()
    verbatim: bool = False
    preserves_timestamps: bool = False
    keeps_origin: bool = False
    idempotent: bool = False
    dry_run_put: bool = False
    max_batch: int = 1
    max_text: int = MAX_ITEM_TEXT
    revisions: bool = False
    retired: bool = False
    changes: bool = False
    ingest: str = "none"
    multi_store: bool = False
    principal_auth: bool = False
    documents: str = "none"
    steering: str = "none"
    max_page: int = 50

    def has(self, op: str) -> bool:
        return op in self.ops

    @property
    def supports_search(self) -> bool:
        return "search" in self.ops

    @property
    def supports_list(self) -> bool:
        return "list" in self.ops

    @property
    def supports_delete(self) -> bool:
        return "delete" in self.ops

    @property
    def supports_lessons(self) -> bool:
        """Whether lesson writes reach it: ``put`` plus both lesson kinds."""
        return "put" in self.ops and LESSON_KINDS <= self.kinds

    @property
    def reads(self) -> bool:
        """Whether it can answer a read: ``context`` or ``search``."""
        return "context" in self.ops or "search" in self.ops

    def mode_gaps(self, mode: str) -> list[str]:
        """What *mode* needs that this provider lacks; empty when it has everything.

        ``replace`` here leaves out ``ingest``: a provider with none is still allowed
        once the owner acknowledges that automatic learning stops.
        """
        if mode == "augment":
            return [] if self.reads else ["op:context_or_search"]
        if mode != "replace":
            return []
        gaps: list[str] = []
        if self.read_only:
            gaps.append("read_only")
        gaps.extend(f"op:{op}" for op in REPLACE_OPS if op not in self.ops)
        gaps.extend(f"kind:{kind}" for kind in sorted(REPLACE_KINDS - self.kinds))
        if not self.verbatim:
            gaps.append("writes.verbatim")
        if not self.idempotent:
            gaps.append("writes.idempotent")
        return gaps

    def replace_gaps(self) -> list[str]:
        return self.mode_gaps("replace")

    @property
    def holds_documents(self) -> bool:
        """Whether Global V1's Markdown documents move to it (``documents: readwrite``)."""
        return self.documents == "readwrite" and "document" in self.kinds and "put" in self.ops

    def to_wire(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "provider": {"name": self.provider_name, "version": self.provider_version},
            "egress": self.egress,
            "read_only": self.read_only,
            "ops": sorted(self.ops),
            "kinds": sorted(self.kinds),
            "search": {
                "lexical": self.search_lexical,
                "semantic": self.search_semantic,
                "filters": sorted(self.search_filters),
            },
            "writes": {
                "verbatim": self.verbatim,
                "preserves_timestamps": self.preserves_timestamps,
                "keeps_origin": self.keeps_origin,
                "idempotent": self.idempotent,
                "dry_run_put": self.dry_run_put,
                "max_batch": self.max_batch,
                "max_text": self.max_text,
            },
            "history": {
                "revisions": self.revisions,
                "retired": self.retired,
                "changes": self.changes,
            },
            "ingest": self.ingest,
            "isolation": {"multi_store": self.multi_store, "principal_auth": self.principal_auth},
            "documents": self.documents,
            "steering": self.steering,
            "max_page": self.max_page,
        }


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""
    latency_ms: float | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "latency_ms": self.latency_ms}


@dataclass(frozen=True)
class Page:
    items: tuple[MemoryItem, ...]
    cursor: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"items": [item.to_wire() for item in self.items], "cursor": self.cursor}


@dataclass(frozen=True)
class Changes:
    """What changed since a cursor: items written and ids deleted, and the next cursor."""

    upserts: tuple[MemoryItem, ...]
    deletes: tuple[str, ...]
    cursor: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "upserts": [item.to_wire() for item in self.upserts],
            "deletes": list(self.deletes),
            "cursor": self.cursor,
        }


@dataclass(frozen=True)
class Revision:
    """One entry of an item's history."""

    revision: int
    at: float
    op: str
    source: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"revision": self.revision, "at": self.at, "op": self.op, "source": self.source}


@dataclass(frozen=True)
class TranscriptSpan:
    """The messages a consolidation pass hands to a provider that extracts for itself."""

    messages: tuple[Mapping[str, str], ...]
    start: int
    end: int

    def to_wire(self) -> dict[str, Any]:
        return {"messages": [dict(m) for m in self.messages], "span": [self.start, self.end]}


@dataclass(frozen=True)
class IngestReceipt:
    accepted: bool
    items: int = 0
    detail: str = ""


@runtime_checkable
class MemoryProvider(Protocol):
    """The one memory seam. ``resolver.route_for`` is the only selection point."""

    provider_id: str
    display_name: str

    def capabilities(self) -> Capabilities: ...

    async def health(self) -> Health: ...

    async def context(
        self, scope: ProviderScope, query: str, budget_chars: int
    ) -> list[MemoryItem]: ...

    async def search(
        self, scope: ProviderScope, query: str, limit: int, kinds: frozenset[str] | None
    ) -> list[MemoryItem]: ...

    async def get(self, scope: ProviderScope, item_id: str) -> MemoryItem | None: ...

    async def list(
        self,
        scope: ProviderScope,
        cursor: str | None,
        kinds: frozenset[str] | None,
        status: str | None,
        updated_after: float | None = None,
    ) -> Page: ...

    async def put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
        *,
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> WriteResult: ...

    async def put_batch(
        self, scope: ProviderScope, entries: Sequence[tuple[NewItem, str]]
    ) -> builtins.list[WriteResult]: ...

    async def delete(self, scope: ProviderScope, item_id: str) -> WriteResult: ...

    async def changes(self, scope: ProviderScope, cursor: str | None) -> Changes: ...

    async def revisions(self, scope: ProviderScope, item_id: str) -> builtins.list[Revision]: ...

    async def ingest(
        self, scope: ProviderScope, span: TranscriptSpan, receipt_id: str
    ) -> IngestReceipt: ...


class BlockingProvider:
    """A provider built on blocking operations, with the async contract on top.

    Crew's prompt builders are synchronous and run on worker threads, so each
    implementation writes ``blocking_<op>`` methods and gets the async methods of
    :class:`MemoryProvider` for free (each one runs its blocking twin on a thread).
    ``put_batch`` defaults to one ``put`` per entry, for a provider without it.
    """

    provider_id = ""
    display_name = ""

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def blocking_health(self) -> Health:
        raise NotImplementedError

    def blocking_context(
        self, scope: ProviderScope, query: str, budget_chars: int
    ) -> list[MemoryItem]:
        raise ProviderNotSupported("context")

    def blocking_search(
        self, scope: ProviderScope, query: str, limit: int, kinds: frozenset[str] | None
    ) -> list[MemoryItem]:
        raise ProviderNotSupported("search")

    def blocking_get(self, scope: ProviderScope, item_id: str) -> MemoryItem | None:
        raise ProviderNotSupported("get")

    def blocking_list(
        self,
        scope: ProviderScope,
        cursor: str | None,
        kinds: frozenset[str] | None,
        status: str | None,
        updated_after: float | None = None,
    ) -> Page:
        raise ProviderNotSupported("list")

    def blocking_put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
        *,
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> WriteResult:
        raise ProviderNotSupported("put")

    def blocking_put_batch(
        self, scope: ProviderScope, entries: Sequence[tuple[NewItem, str]]
    ) -> list[WriteResult]:
        return [self.blocking_put(scope, item, key) for item, key in entries]

    def blocking_delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        raise ProviderNotSupported("delete")

    def blocking_changes(self, scope: ProviderScope, cursor: str | None) -> Changes:
        raise ProviderNotSupported("changes")

    def blocking_revisions(self, scope: ProviderScope, item_id: str) -> list[Revision]:
        raise ProviderNotSupported("revisions")

    def blocking_ingest(
        self, scope: ProviderScope, span: TranscriptSpan, receipt_id: str
    ) -> IngestReceipt:
        raise ProviderNotSupported("ingest")

    async def health(self) -> Health:
        return await asyncio.to_thread(self.blocking_health)

    async def context(
        self, scope: ProviderScope, query: str, budget_chars: int
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(self.blocking_context, scope, query, budget_chars)

    async def search(
        self, scope: ProviderScope, query: str, limit: int, kinds: frozenset[str] | None
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(self.blocking_search, scope, query, limit, kinds)

    async def get(self, scope: ProviderScope, item_id: str) -> MemoryItem | None:
        return await asyncio.to_thread(self.blocking_get, scope, item_id)

    async def list(
        self,
        scope: ProviderScope,
        cursor: str | None,
        kinds: frozenset[str] | None,
        status: str | None,
        updated_after: float | None = None,
    ) -> Page:
        return await asyncio.to_thread(
            self.blocking_list, scope, cursor, kinds, status, updated_after
        )

    async def put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
        *,
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> WriteResult:
        return await asyncio.to_thread(
            lambda: self.blocking_put(
                scope,
                item,
                idempotency_key,
                item_id,
                expected_revision=expected_revision,
                dry_run=dry_run,
            )
        )

    async def put_batch(
        self, scope: ProviderScope, entries: Sequence[tuple[NewItem, str]]
    ) -> builtins.list[WriteResult]:
        return await asyncio.to_thread(self.blocking_put_batch, scope, entries)

    async def delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        return await asyncio.to_thread(self.blocking_delete, scope, item_id)

    async def changes(self, scope: ProviderScope, cursor: str | None) -> Changes:
        return await asyncio.to_thread(self.blocking_changes, scope, cursor)

    async def revisions(self, scope: ProviderScope, item_id: str) -> builtins.list[Revision]:
        return await asyncio.to_thread(self.blocking_revisions, scope, item_id)

    async def ingest(
        self, scope: ProviderScope, span: TranscriptSpan, receipt_id: str
    ) -> IngestReceipt:
        return await asyncio.to_thread(self.blocking_ingest, scope, span, receipt_id)


# ── Wire validation ──────────────────────────────────────────────────────────


def _is_one_of(value: object, allowed: frozenset[str]) -> bool:
    """Whether *value* is a string in *allowed*; an unhashable answer is simply not."""
    return isinstance(value, str) and value in allowed


def _bounded_str(value: object, limit: int, name: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise ProviderInvalidResponse(f"{name} must be a string")
    if len(value) > limit:
        raise ProviderInvalidResponse(f"{name} is longer than {limit} characters")
    return value


def _timestamp(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderInvalidResponse(f"{name} must be a number")
    return float(value)


def _count(value: object, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProviderInvalidResponse(f"{name} must be an integer of at least {minimum}")
    return value


def truncate_text(text: str, limit: int = MAX_ITEM_TEXT) -> str:
    """*text* cut to *limit* characters, marked when it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_MARK)] + TRUNCATED_MARK


def _scopes(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SCOPES:
        raise ProviderInvalidResponse("scopes must be a list of at most 20 strings")
    out = []
    for scope in value:
        if not isinstance(scope, str) or not scope or len(scope) > MAX_SCOPE_CHARS:
            raise ProviderInvalidResponse("scopes must be short strings")
        out.append(scope)
    return tuple(out)


def _meta(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_META_PAIRS:
        raise ProviderInvalidResponse("meta must be an object of at most 50 pairs")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or len(key) > 100:
            raise ProviderInvalidResponse("meta pairs must be strings")
        out[key] = item[:MAX_META_VALUE]
    return out


def origin_from_wire(value: object) -> Origin | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProviderInvalidResponse("origin must be an object")
    provider = _bounded_str(value.get("provider"), MAX_ITEM_ID, "origin.provider", required=True)
    store = _bounded_str(value.get("store"), MAX_ITEM_ID, "origin.store", required=True)
    item_id = _bounded_str(value.get("id"), MAX_ITEM_ID, "origin.id", required=True)
    assert provider is not None and store is not None and item_id is not None
    return Origin(provider, store, item_id)


def _status(value: object) -> str:
    status = "active" if value is None else value
    if not _is_one_of(status, STATUSES):
        raise ProviderInvalidResponse("status must be active, draft or retired")
    return str(status)


def item_from_wire(data: object) -> MemoryItem:
    """One validated item. Text past its kind's cap is cut and marked, not refused."""
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("item must be an object")
    item_id = _bounded_str(data.get("id"), MAX_ITEM_ID, "id", required=True)
    kind = data.get("kind")
    if not _is_one_of(kind, KINDS):
        raise ProviderInvalidResponse("item kind is not one of the contract kinds")
    text = data.get("text")
    if not isinstance(text, str):
        raise ProviderInvalidResponse("text must be a string")
    score = data.get("score")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
    ):
        raise ProviderInvalidResponse("score must be a number")
    assert item_id is not None
    return MemoryItem(
        id=item_id,
        kind=str(kind),
        text=truncate_text(text, text_cap(str(kind))),
        title=_bounded_str(data.get("title"), MAX_TITLE, "title"),
        scopes=_scopes(data.get("scopes")),
        source=_bounded_str(data.get("source"), 2_000, "source"),
        created_at=_timestamp(data.get("created_at"), "created_at"),
        updated_at=_timestamp(data.get("updated_at"), "updated_at"),
        status=_status(data.get("status")),
        score=float(score) if score is not None else None,
        meta=_meta(data.get("meta")),
        origin=origin_from_wire(data.get("origin")),
        revision=_count(data.get("revision"), "revision"),
    )


def _item_list(raw: object, name: str, limit: int = MAX_ANSWER_ITEMS) -> list[MemoryItem]:
    if not isinstance(raw, list):
        raise ProviderInvalidResponse(f"{name} must be a list")
    if len(raw) > MAX_ANSWER_ITEMS:
        raise ProviderInvalidResponse(f"more than {MAX_ANSWER_ITEMS} items in one answer")
    return [item_from_wire(entry) for entry in raw[:limit]]


def items_from_wire(data: object, *, limit: int = MAX_ANSWER_ITEMS) -> list[MemoryItem]:
    """``{"items": [...]}`` validated, at most *limit* items."""
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("result must carry an items list")
    return _item_list(data.get("items"), "items", limit)


def page_from_wire(data: object) -> Page:
    items = items_from_wire(data)
    cursor = data.get("cursor") if isinstance(data, dict) else None
    return Page(tuple(items), _bounded_str(cursor, 1_000, "cursor"))


def changes_from_wire(data: object) -> Changes:
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("changes result must be an object")
    deletes = data.get("deletes") or []
    if not isinstance(deletes, list) or len(deletes) > MAX_ANSWER_ITEMS:
        raise ProviderInvalidResponse("deletes must be a list")
    return Changes(
        upserts=tuple(_item_list(data.get("upserts") or [], "upserts")),
        deletes=tuple(
            _bounded_str(d, MAX_ITEM_ID, "deletes", required=True) or "" for d in deletes
        ),
        cursor=_bounded_str(data.get("cursor"), 1_000, "cursor"),
    )


def revisions_from_wire(data: object) -> list[Revision]:
    if not isinstance(data, dict) or not isinstance(data.get("revisions"), list):
        raise ProviderInvalidResponse("revisions result must carry a revisions list")
    raw = data["revisions"]
    if len(raw) > MAX_REVISIONS:
        raise ProviderInvalidResponse("more than 200 revisions in one answer")
    out: list[Revision] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProviderInvalidResponse("a revision must be an object")
        revision = _count(entry.get("revision"), "revision")
        at = _timestamp(entry.get("at"), "at")
        op = _bounded_str(entry.get("op"), MAX_NAME, "op", required=True)
        if revision is None or at is None or op is None:
            raise ProviderInvalidResponse("a revision needs revision, at and op")
        source = _bounded_str(entry.get("source"), MAX_NAME, "source") or ""
        out.append(Revision(revision, at, op, source))
    return out


def new_item_from_wire(data: object) -> NewItem:
    """A validated write body (the host routes and the import read this).

    Raises ``ValueError`` with a reason the caller can show.
    """
    if not isinstance(data, dict):
        raise ValueError("item must be an object")
    kind = data.get("kind")
    text = data.get("text")
    if not _is_one_of(kind, KINDS):
        raise ValueError("kind must be one of: " + ", ".join(sorted(KINDS)))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    cap = text_cap(str(kind))
    if len(text) > cap:
        raise ValueError(f"text is longer than {cap} characters")
    try:
        status = _status(data.get("status"))
        title = _bounded_str(data.get("title"), MAX_TITLE, "title")
        scopes = _scopes(data.get("scopes"))
        source = _bounded_str(data.get("source"), 2_000, "source")
        meta = _meta(data.get("meta"))
        created_at = _timestamp(data.get("created_at"), "created_at")
        updated_at = _timestamp(data.get("updated_at"), "updated_at")
        origin = origin_from_wire(data.get("origin"))
    except ProviderInvalidResponse as exc:
        raise ValueError(exc.message) from None
    if kind == "document" and meta.get("crew.doc") not in DOCUMENT_NAMES:
        raise ValueError('a document item needs meta["crew.doc"] of preferences or projects')
    return NewItem(
        kind=str(kind),
        text=text,
        title=title,
        scopes=scopes,
        source=source,
        status=status,
        meta=meta,
        created_at=created_at,
        updated_at=updated_at,
        origin=origin,
    )


def new_item_of(item: MemoryItem) -> NewItem:
    """*item* as a write that keeps every field the envelope carries."""
    return NewItem(
        kind=item.kind,
        text=item.text,
        title=item.title,
        scopes=item.scopes,
        source=item.source,
        status=item.status,
        meta=dict(item.meta),
        created_at=item.created_at,
        updated_at=item.updated_at,
        origin=item.origin,
    )


def write_result_from_wire(data: object) -> WriteResult:
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("write result must be an object")
    outcome = data.get("outcome")
    if not _is_one_of(outcome, OUTCOMES):
        raise ProviderInvalidResponse("outcome is not one of the lesson outcomes")
    superseded = data.get("superseded") or []
    if not isinstance(superseded, list) or len(superseded) > MAX_ANSWER_ITEMS:
        raise ProviderInvalidResponse("superseded must be a list")
    return WriteResult(
        outcome=str(outcome),
        id=_bounded_str(data.get("id"), MAX_ITEM_ID, "id"),
        reason=(data.get("reason") or None) if isinstance(data.get("reason"), str) else None,
        superseded=tuple(
            _bounded_str(s, MAX_ITEM_TEXT, "superseded", required=True) or "" for s in superseded
        ),
        winner=_bounded_str(data.get("winner"), MAX_ITEM_TEXT, "winner"),
    )


def write_results_from_wire(data: object, expected: int) -> list[WriteResult]:
    """``put_batch``'s answer: one result per entry sent, in order."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ProviderInvalidResponse("put_batch result must carry a results list")
    raw = data["results"]
    if len(raw) != expected:
        raise ProviderInvalidResponse("put_batch must answer once per entry")
    return [write_result_from_wire(entry) for entry in raw]


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ProviderInvalidResponse(f"{name} must be an object")
    return value


def _flag(data: dict, name: str, label: str) -> bool:
    value = data.get(name, False)
    if not isinstance(value, bool):
        raise ProviderInvalidResponse(f"{label} must be a boolean")
    return value


def _positive(data: dict, name: str, default: int, cap: int, label: str) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProviderInvalidResponse(f"{label} must be a positive integer")
    return min(value, cap)


def capabilities_from_wire(data: object) -> Capabilities:
    """A validated ``connect`` answer."""
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("connect result must be an object")
    api_version = data.get("api_version")
    if isinstance(api_version, bool) or api_version not in API_VERSIONS:
        raise ProviderInvalidResponse("unsupported provider api_version")
    provider = _section(data, "provider")
    name = _bounded_str(provider.get("name"), MAX_NAME, "provider.name") or ""
    version = _bounded_str(provider.get("version"), MAX_NAME, "provider.version") or ""
    egress = data.get("egress")
    if egress not in ("local", "remote"):
        raise ProviderInvalidResponse("egress must be local or remote")
    read_only = _flag(data, "read_only", "read_only")
    ops = data.get("ops")
    if not isinstance(ops, list) or any(not _is_one_of(op, OPS) for op in ops):
        raise ProviderInvalidResponse("ops must list contract operations")
    effective_ops = frozenset(ops) - (WRITE_OPS if read_only else frozenset())
    if "context" not in effective_ops:
        raise ProviderInvalidResponse("context is a required operation")
    kinds = data.get("kinds")
    if not isinstance(kinds, list) or any(not _is_one_of(k, KINDS) for k in kinds):
        raise ProviderInvalidResponse("kinds must list contract kinds")
    search = _section(data, "search")
    filters = search.get("filters", [])
    if not isinstance(filters, list) or any(not _is_one_of(f, SEARCH_FILTERS) for f in filters):
        raise ProviderInvalidResponse("search.filters must list contract filters")
    writes = _section(data, "writes")
    history = _section(data, "history")
    isolation = _section(data, "isolation")
    ingest = data.get("ingest", "none")
    if ingest not in ("none", "raw", "items"):
        raise ProviderInvalidResponse("ingest must be none, raw or items")
    documents = data.get("documents", "none")
    if documents not in ("none", "read", "readwrite"):
        raise ProviderInvalidResponse("documents must be none, read or readwrite")
    if data.get("steering", "none") != "none":
        # Steering stays Crew's and the harness's in contract v1.
        raise ProviderInvalidResponse('steering must be "none" in api version 1')
    history_revisions = _flag(history, "revisions", "history.revisions")
    history_changes = _flag(history, "changes", "history.changes")
    return Capabilities(
        api_version=int(api_version),
        provider_name=name,
        provider_version=version,
        egress=str(egress),
        read_only=read_only,
        ops=effective_ops
        - (frozenset() if history_changes else frozenset(("changes",)))
        - (frozenset() if history_revisions else frozenset(("revisions",))),
        kinds=frozenset(kinds),
        search_lexical=_flag(search, "lexical", "search.lexical"),
        search_semantic=_flag(search, "semantic", "search.semantic"),
        search_filters=frozenset(filters),
        verbatim=_flag(writes, "verbatim", "writes.verbatim") and not read_only,
        preserves_timestamps=_flag(writes, "preserves_timestamps", "writes.preserves_timestamps"),
        keeps_origin=_flag(writes, "keeps_origin", "writes.keeps_origin"),
        idempotent=_flag(writes, "idempotent", "writes.idempotent") and not read_only,
        dry_run_put=_flag(writes, "dry_run_put", "writes.dry_run_put") and not read_only,
        max_batch=_positive(writes, "max_batch", 1, MAX_BATCH, "writes.max_batch"),
        max_text=_positive(writes, "max_text", MAX_ITEM_TEXT, MAX_DOCUMENT_TEXT, "writes.max_text"),
        revisions=history_revisions,
        retired=_flag(history, "retired", "history.retired"),
        changes=history_changes,
        ingest="none" if read_only else str(ingest),
        multi_store=_flag(isolation, "multi_store", "isolation.multi_store"),
        principal_auth=_flag(isolation, "principal_auth", "isolation.principal_auth"),
        documents="read" if read_only and documents == "readwrite" else str(documents),
        steering="none",
        max_page=_positive(data, "max_page", 50, MAX_PAGE, "max_page"),
    )


def health_from_wire(data: object) -> Health:
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise ProviderInvalidResponse("health result must carry ok")
    detail = data.get("detail")
    return Health(ok=data["ok"], detail=detail[:MAX_REASON] if isinstance(detail, str) else "")


def ingest_receipt_from_wire(data: object) -> IngestReceipt:
    if not isinstance(data, dict) or not isinstance(data.get("accepted"), bool):
        raise ProviderInvalidResponse("ingest result must carry accepted")
    items = data.get("items", 0)
    detail = data.get("detail")
    return IngestReceipt(
        accepted=data["accepted"],
        items=items if isinstance(items, int) and not isinstance(items, bool) else 0,
        detail=detail[:MAX_REASON] if isinstance(detail, str) else "",
    )


def connect_request(want_mode: str, scope: ProviderScope) -> dict[str, Any]:
    """The body Crew sends with ``connect``."""
    return {
        "crew": {
            "api_versions": list(API_VERSIONS),
            "want_mode": want_mode,
            "kinds": sorted(KINDS),
        },
        "scope": scope.to_wire(),
    }
