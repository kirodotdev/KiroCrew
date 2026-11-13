"""The memory provider contract: the data a provider exchanges with Crew, and its errors.

A provider stores and finds memory items. Crew keeps everything else: who is asking
(``ProviderScope`` is built from the session's execution record, never by the
provider), which memory mode applies, governance, budgets, prompt framing and content
screens. A provider returns DATA -- items with text, ids and timestamps -- and Crew
renders the blocks the model sees.

The wire form of every type is the JSON object its ``to_wire`` writes and its
``*_from_wire`` reads. Every value that crosses from a provider is validated here:
an answer that does not have the documented shape raises
:class:`ProviderInvalidResponse`, which callers treat as unavailable.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

PROVIDER_API_VERSION = 1

Kind = Literal["preference", "fact", "episode", "summary", "rule", "finding", "index", "note"]
KINDS: frozenset[str] = frozenset(
    ("preference", "fact", "episode", "summary", "rule", "finding", "index", "note")
)
#: The kinds Crew's lesson tools speak: a ``rule`` is a standing correction, a
#: ``finding`` an on-topic experience.
LESSON_KINDS: frozenset[str] = frozenset(("rule", "finding"))
STATUSES: frozenset[str] = frozenset(("active", "draft", "retired"))
#: ``put`` answers one of the first five; ``delete`` answers ``deleted`` or ``unchanged``.
OUTCOMES: frozenset[str] = frozenset(
    ("inserted", "enriched", "unchanged", "deduped", "refused", "deleted")
)
ERROR_CODES: frozenset[str] = frozenset(
    ("unavailable", "refused", "read_only", "not_supported", "conflict", "invalid")
)

MAX_ITEM_ID = 256
MAX_ITEM_TEXT = 8_000
MAX_TITLE = 300
MAX_META_PAIRS = 50
MAX_META_VALUE = 1_000
MAX_SCOPES = 20
MAX_SCOPE_CHARS = 200
MAX_PAGE = 200
MAX_ANSWER_ITEMS = 200
MAX_ANSWER_BYTES = 512 * 1024
MAX_REASON = 500
TRUNCATED_MARK = " [truncated]"

#: Per-operation deadlines, in seconds. ``context`` takes
#: ``memory.provider.timeout_secs`` instead (default 3, at most 10).
OP_DEADLINES: Mapping[str, float] = {
    "describe": 5.0,
    "health": 5.0,
    "search": 5.0,
    "get": 5.0,
    "list": 5.0,
    "put": 10.0,
    "delete": 10.0,
    "ingest": 60.0,
}
CONTEXT_TIMEOUT_DEFAULT = 3.0
CONTEXT_TIMEOUT_MAX = 10.0


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
    """The write conflicts with the provider's current state (for example a moved id)."""

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

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "text": self.text}
        for name in ("title", "source", "created_at", "updated_at", "score"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.scopes:
            out["scopes"] = list(self.scopes)
        if self.status != "active":
            out["status"] = self.status
        if self.meta:
            out["meta"] = dict(self.meta)
        return out


@dataclass(frozen=True)
class NewItem:
    """An item to write. ``put`` with an ``item_id`` replaces that item."""

    kind: str
    text: str
    title: str | None = None
    scopes: tuple[str, ...] = ()
    source: str | None = None
    status: str = "active"
    meta: Mapping[str, str] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.title is not None:
            out["title"] = self.title
        if self.scopes:
            out["scopes"] = list(self.scopes)
        if self.source is not None:
            out["source"] = self.source
        if self.status != "active":
            out["status"] = self.status
        if self.meta:
            out["meta"] = dict(self.meta)
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
    api_version: int
    read_only: bool
    supports_search: bool
    supports_vectors: bool
    supports_lessons: bool
    supports_list: bool
    supports_delete: bool
    ingest: str
    multi_store: bool
    egress: str
    kinds: frozenset[str]
    max_page: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "read_only": self.read_only,
            "supports_search": self.supports_search,
            "supports_vectors": self.supports_vectors,
            "supports_lessons": self.supports_lessons,
            "supports_list": self.supports_list,
            "supports_delete": self.supports_delete,
            "ingest": self.ingest,
            "multi_store": self.multi_store,
            "egress": self.egress,
            "kinds": sorted(self.kinds),
            "max_page": self.max_page,
        }

    def replace_gaps(self) -> list[str]:
        """What ``replace`` mode needs that this provider lacks (``ingest`` aside)."""
        gaps = []
        if self.read_only:
            gaps.append("read_only")
        for name in ("supports_lessons", "supports_search", "supports_list", "supports_delete"):
            if not getattr(self, name):
                gaps.append(name)
        return gaps


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

    def describe(self) -> Capabilities: ...

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
    ) -> Page: ...

    async def put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
    ) -> WriteResult: ...

    async def delete(self, scope: ProviderScope, item_id: str) -> WriteResult: ...

    async def ingest(
        self, scope: ProviderScope, span: TranscriptSpan, receipt_id: str
    ) -> IngestReceipt: ...


class BlockingProvider:
    """A provider built on blocking operations, with the async contract on top.

    Crew's prompt builders are synchronous and run on worker threads, so each
    implementation writes ``blocking_<op>`` methods and gets the async methods of
    :class:`MemoryProvider` for free (each one runs its blocking twin on a thread).
    """

    provider_id = ""
    display_name = ""

    def describe(self) -> Capabilities:
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
    ) -> Page:
        raise ProviderNotSupported("list")

    def blocking_put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
    ) -> WriteResult:
        raise ProviderNotSupported("put")

    def blocking_delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        raise ProviderNotSupported("delete")

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
    ) -> Page:
        return await asyncio.to_thread(self.blocking_list, scope, cursor, kinds, status)

    async def put(
        self,
        scope: ProviderScope,
        item: NewItem,
        idempotency_key: str,
        item_id: str | None = None,
    ) -> WriteResult:
        return await asyncio.to_thread(self.blocking_put, scope, item, idempotency_key, item_id)

    async def delete(self, scope: ProviderScope, item_id: str) -> WriteResult:
        return await asyncio.to_thread(self.blocking_delete, scope, item_id)

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


def item_from_wire(data: object) -> MemoryItem:
    """One validated item. Text past ``MAX_ITEM_TEXT`` is cut and marked, not refused."""
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("item must be an object")
    item_id = _bounded_str(data.get("id"), MAX_ITEM_ID, "id", required=True)
    kind = data.get("kind")
    if not _is_one_of(kind, KINDS):
        raise ProviderInvalidResponse("item kind is not one of the contract kinds")
    text = data.get("text")
    if not isinstance(text, str):
        raise ProviderInvalidResponse("text must be a string")
    status = data.get("status", "active")
    if not _is_one_of(status, STATUSES):
        raise ProviderInvalidResponse("status must be active, draft or retired")
    score = data.get("score")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
    ):
        raise ProviderInvalidResponse("score must be a number")
    assert item_id is not None
    return MemoryItem(
        id=item_id,
        kind=str(kind),
        text=truncate_text(text),
        title=_bounded_str(data.get("title"), MAX_TITLE, "title"),
        scopes=_scopes(data.get("scopes")),
        source=_bounded_str(data.get("source"), 2_000, "source"),
        created_at=_timestamp(data.get("created_at"), "created_at"),
        updated_at=_timestamp(data.get("updated_at"), "updated_at"),
        status=str(status),
        score=float(score) if score is not None else None,
        meta=_meta(data.get("meta")),
    )


def items_from_wire(data: object, *, limit: int = MAX_ANSWER_ITEMS) -> list[MemoryItem]:
    """``{"items": [...]}`` validated, at most *limit* items."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ProviderInvalidResponse("result must carry an items list")
    raw = data["items"]
    if len(raw) > MAX_ANSWER_ITEMS:
        raise ProviderInvalidResponse("more than 200 items in one answer")
    return [item_from_wire(entry) for entry in raw[:limit]]


def page_from_wire(data: object) -> Page:
    items = items_from_wire(data)
    cursor = data.get("cursor") if isinstance(data, dict) else None
    return Page(tuple(items), _bounded_str(cursor, 1_000, "cursor"))


def new_item_from_wire(data: object) -> NewItem:
    """A validated write body (the host routes and the import read this)."""
    if not isinstance(data, dict):
        raise ValueError("item must be an object")
    kind = data.get("kind")
    text = data.get("text")
    if not _is_one_of(kind, KINDS):
        raise ValueError("kind must be one of: " + ", ".join(sorted(KINDS)))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    if len(text) > MAX_ITEM_TEXT:
        raise ValueError(f"text is longer than {MAX_ITEM_TEXT} characters")
    status = data.get("status", "active")
    if not _is_one_of(status, STATUSES):
        raise ValueError("status must be active, draft or retired")
    try:
        title = _bounded_str(data.get("title"), MAX_TITLE, "title")
        scopes = _scopes(data.get("scopes"))
        source = _bounded_str(data.get("source"), 2_000, "source")
        meta = _meta(data.get("meta"))
    except ProviderInvalidResponse as exc:
        raise ValueError(exc.message) from None
    return NewItem(
        kind=str(kind),
        text=text,
        title=title,
        scopes=scopes,
        source=source,
        status=str(status),
        meta=meta,
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


def capabilities_from_wire(data: object) -> Capabilities:
    if not isinstance(data, dict):
        raise ProviderInvalidResponse("describe result must be an object")
    api_version = data.get("api_version")
    if api_version != PROVIDER_API_VERSION:
        raise ProviderInvalidResponse("unsupported provider api_version")

    def flag(name: str) -> bool:
        value = data.get(name, False)
        if not isinstance(value, bool):
            raise ProviderInvalidResponse(f"{name} must be a boolean")
        return value

    ingest = data.get("ingest", "none")
    if ingest not in ("none", "raw", "items"):
        raise ProviderInvalidResponse("ingest must be none, raw or items")
    egress = data.get("egress")
    if egress not in ("local", "remote"):
        raise ProviderInvalidResponse("egress must be local or remote")
    kinds = data.get("kinds", sorted(KINDS))
    if not isinstance(kinds, list) or any(not _is_one_of(k, KINDS) for k in kinds):
        raise ProviderInvalidResponse("kinds must list contract kinds")
    max_page = data.get("max_page", 50)
    if isinstance(max_page, bool) or not isinstance(max_page, int) or max_page < 1:
        raise ProviderInvalidResponse("max_page must be a positive integer")
    read_only = flag("read_only")
    return Capabilities(
        api_version=PROVIDER_API_VERSION,
        read_only=read_only,
        supports_search=flag("supports_search"),
        supports_vectors=flag("supports_vectors"),
        supports_lessons=flag("supports_lessons") and not read_only,
        supports_list=flag("supports_list"),
        supports_delete=flag("supports_delete") and not read_only,
        ingest="none" if read_only else str(ingest),
        multi_store=flag("multi_store"),
        egress=str(egress),
        kinds=frozenset(kinds),
        max_page=min(max_page, MAX_PAGE),
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
