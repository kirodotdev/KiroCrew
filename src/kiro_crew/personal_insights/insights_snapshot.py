from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Final

_COMPLETE: Final[str] = "complete"
_PREFIX: Final[str] = "prefix_unavailable"
_UNKNOWN: Final[str] = "unknown"
_COMPLETENESS: Final[frozenset[str]] = frozenset({_COMPLETE, _PREFIX, _UNKNOWN})


class SnapshotError(Exception):
    pass


class SnapshotInvalidatedError(SnapshotError):
    def __init__(self, session_key: str) -> None:
        super().__init__(f"snapshot invalidated for {session_key}")
        self.session_key = session_key


class OversizedSourceError(SnapshotError):
    pass


class CompletenessDowngradeError(SnapshotError):
    pass


class AllUnknownProofError(SnapshotError):
    pass


class ExportAuthorityError(SnapshotError):
    pass


@dataclass(frozen=True)
class SnapshotManifest:
    max_events: int
    max_bytes: int

    def __post_init__(self) -> None:
        for value in (self.max_events, self.max_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("snapshot manifest bounds must be positive integers")


@dataclass(frozen=True)
class ExportAuthority:
    authenticated_principal: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.authenticated_principal or not self.workspace_id:
            raise ValueError("export authority must be pinned")


@dataclass(frozen=True)
class ExportPage:
    snapshot_id: str
    event_ceiling: int
    history_completeness: str
    events: tuple[dict[str, Any], ...]
    next_cursor: str | None
    end_of_snapshot: bool
    owner_id: str | None = None
    workspace_id: str | None = None


@dataclass
class ExportBuffer:
    snapshot_id: str
    event_ceiling: int
    history_completeness: str
    events: list[dict[str, Any]] = field(default_factory=list)
    byte_total: int = 0
    complete: bool = False
    appended_event_count: int = 0
    seen_event_ids: set[str] = field(default_factory=set)
    seen_orders: set[int] = field(default_factory=set)


def _event_bytes(event: dict[str, Any]) -> int:
    return len(
        json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    )


def _validate_page(page: ExportPage) -> None:
    if not page.snapshot_id:
        raise SnapshotError("snapshot page lacks a snapshot id")
    if (
        not isinstance(page.event_ceiling, int)
        or isinstance(page.event_ceiling, bool)
        or page.event_ceiling < 0
    ):
        raise SnapshotError("snapshot event ceiling is invalid")
    if page.history_completeness not in _COMPLETENESS:
        raise SnapshotError("history completeness is not a closed value")
    if not isinstance(page.events, tuple):
        raise SnapshotError("snapshot events must be immutable")
    if page.end_of_snapshot and page.next_cursor is not None:
        raise SnapshotError("terminal snapshot page carries a cursor")
    if not page.end_of_snapshot and not page.next_cursor:
        raise SnapshotError("non-terminal snapshot page lacks a cursor")


def accumulate(
    buffer: ExportBuffer | None, page: ExportPage, manifest: SnapshotManifest
) -> ExportBuffer:
    _validate_page(page)
    if buffer is None:
        buffer = ExportBuffer(
            page.snapshot_id,
            page.event_ceiling,
            page.history_completeness,
        )
    if page.snapshot_id != buffer.snapshot_id or page.event_ceiling != buffer.event_ceiling:
        raise SnapshotInvalidatedError(buffer.snapshot_id)
    if page.history_completeness != buffer.history_completeness:
        if buffer.history_completeness == _COMPLETE:
            raise CompletenessDowngradeError("history completeness changed within snapshot")
        raise SnapshotInvalidatedError(buffer.snapshot_id)
    for event in page.events:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise SnapshotError("snapshot event sequence is invalid")
        if sequence > buffer.event_ceiling:
            buffer.appended_event_count += 1
            continue
        raw_id = event.get("source_event_id")
        identity = str(raw_id) if raw_id is not None else f"sequence:{sequence}"
        if identity in buffer.seen_event_ids or sequence in buffer.seen_orders:
            raise SnapshotError("snapshot event identity or order is duplicated")
        buffer.seen_event_ids.add(identity)
        buffer.seen_orders.add(sequence)
        buffer.events.append(event)
        buffer.byte_total += _event_bytes(event)
        if len(buffer.events) > manifest.max_events or buffer.byte_total > manifest.max_bytes:
            raise OversizedSourceError("source exceeds manifest ceilings")
    if page.end_of_snapshot:
        buffer.complete = True
    return buffer


def _legacy_once(
    read_pages: Callable[[str | None], ExportPage], manifest: SnapshotManifest
) -> ExportBuffer:
    buffer: ExportBuffer | None = None
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = read_pages(cursor)
        buffer = accumulate(buffer, page, manifest)
        if page.end_of_snapshot:
            if buffer is None or not buffer.complete:
                raise SnapshotError("snapshot did not complete")
            return buffer
        next_cursor = page.next_cursor
        if next_cursor is None or next_cursor in seen_cursors:
            raise SnapshotError("snapshot cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def export_session(
    session_key: str,
    read_pages: Callable[[str | None], ExportPage],
    manifest: SnapshotManifest,
) -> ExportBuffer:
    for attempt in range(2):
        try:
            return _legacy_once(read_pages, manifest)
        except SnapshotInvalidatedError:
            if attempt == 1:
                raise SnapshotInvalidatedError(session_key)
    raise SnapshotInvalidatedError(session_key)


def _strict_once(
    session_key: str,
    authority: ExportAuthority,
    read_page: Callable[[str, str, str | None, str | None, int], ExportPage],
    manifest: SnapshotManifest,
) -> ExportBuffer:
    buffer: ExportBuffer | None = None
    snapshot_id: str | None = None
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = read_page(
            session_key,
            authority.workspace_id,
            snapshot_id,
            cursor,
            manifest.max_events,
        )
        if page.owner_id != authority.authenticated_principal:
            raise ExportAuthorityError("authenticated principal changed during export")
        if page.workspace_id != authority.workspace_id:
            raise ExportAuthorityError("workspace identity changed during export")
        if snapshot_id is None:
            snapshot_id = page.snapshot_id
        elif page.snapshot_id != snapshot_id:
            raise SnapshotInvalidatedError(session_key)
        buffer = accumulate(buffer, page, manifest)
        if page.end_of_snapshot:
            if buffer is None or not buffer.complete:
                raise SnapshotError("snapshot did not complete")
            return buffer
        next_cursor = page.next_cursor
        if next_cursor is None or next_cursor in seen_cursors:
            raise SnapshotError("snapshot cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def read_snapshot(
    session_key: str,
    authority: ExportAuthority,
    read_page: Callable[[str, str, str | None, str | None, int], ExportPage],
    manifest: SnapshotManifest,
) -> ExportBuffer:
    return _strict_once(session_key, authority, read_page, manifest)


def capture_snapshot(
    session_key: str,
    authority: ExportAuthority,
    read_page_factory: Callable[[], Callable[[str, str, str | None, str | None, int], ExportPage]],
    manifest: SnapshotManifest,
    prior_completeness: str | None = None,
) -> ExportBuffer:
    for attempt in range(2):
        try:
            result = _strict_once(
                session_key,
                authority,
                read_page_factory(),
                manifest,
            )
            if prior_completeness is not None:
                enforce_completeness_downgrade(prior_completeness, result.history_completeness)
            return result
        except SnapshotInvalidatedError:
            if attempt == 1:
                raise SnapshotInvalidatedError(session_key)
    raise SnapshotInvalidatedError(session_key)


def enforce_completeness_downgrade(prior: str, current: str) -> None:
    if prior not in _COMPLETENESS or current not in _COMPLETENESS:
        raise SnapshotError("history completeness is not a closed value")
    if prior == _COMPLETE and current in {_PREFIX, _UNKNOWN}:
        raise CompletenessDowngradeError("completeness downgraded below prior snapshot")


def require_decidable_corpus(completeness_values: list[str]) -> None:
    if not completeness_values or all(value == _UNKNOWN for value in completeness_values):
        raise AllUnknownProofError("every proof-corpus session has unknown completeness")
    if any(value not in _COMPLETENESS for value in completeness_values):
        raise SnapshotError("history completeness is not a closed value")
