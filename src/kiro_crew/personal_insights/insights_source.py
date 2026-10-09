from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterator

from kiro_crew.personal_insights.insights_platform import reject_client_platform

CATALOG_MAX: Final[int] = 200
_REUSE: Final[str] = "reuse"
_EXPORT: Final[str] = "export_and_compare_digest"
_ORIGIN_MAP: Final[dict[str, str]] = {
    "dashboard": "dashboard",
    "cli": "cli",
    "automation": "automation",
    "api": "api",
}
_SESSION_KIND_MAP: Final[dict[str, str]] = {
    "user": "user",
    "subagent": "subagent",
    "cron": "cron",
    "workflow": "workflow",
    "task_runner": "task_runner",
    "webhook": "webhook",
}
_COMPLETENESS: Final[frozenset[str]] = frozenset({"complete", "prefix_unavailable", "unknown"})
_ADMITTED_ACTORS: Final[frozenset[str]] = frozenset({"owner_user", "direct_assistant"})
_FORBIDDEN_BODY_CLASSES: Final[frozenset[str]] = frozenset(
    {"tool", "injected", "attachment", "imported", "child_prose"}
)
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@" r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])"
)
_MENTION_RE: Final[re.Pattern[str]] = re.compile(r"<@[A-Za-z0-9._:-]+>")
_PRIVATE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w/])/(?:local/home|home|Users)/[^/\s]+"
)
_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CREDENTIAL_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:ssh-rsa\s+\S+|bearer\s+\S+|-----BEGIN[^\n]+PRIVATE KEY-----|"
    r"(?:aws|secret|access|api)[_-]?(?:key|token)\s*[:=]\s*\S+)"
)


class SourceError(Exception):
    pass


class AuthorityChangeError(SourceError):
    pass


class OversizedSourceError(SourceError):
    pass


class CatalogCursorError(SourceError):
    pass


@dataclass(frozen=True)
class CatalogPin:
    authenticated_principal: str
    workspace_id: str
    execution_platform: str
    window_start: str
    window_end: str


@dataclass(frozen=True)
class CatalogEntry:
    session_key: str
    owner_id: str
    workspace_id: str
    origin: str
    session_kind: str
    safe_title: str
    created_at: str
    updated_at: str
    message_count: int
    content_revision: None
    history_completeness_hint: str


@dataclass(frozen=True)
class SourceExcluded:
    session_key: str
    reason: str


@dataclass(frozen=True)
class CatalogSelection:
    included: tuple[CatalogEntry, ...]
    excluded: tuple[SourceExcluded, ...]
    count: int


@dataclass(frozen=True)
class CatalogPage:
    entries: tuple[CatalogEntry, ...]
    excluded: tuple[SourceExcluded, ...]
    exact_count: int
    next_cursor: str | None


@dataclass(frozen=True)
class RawEvent:
    source_event_id: str
    sequence: int
    actor_class: str
    visibility_class: str
    event_class: str
    content_state: str
    content_digest: str
    content: str | None
    structural_payload: dict[str, Any]


@dataclass(frozen=True)
class MinimizationReceipt:
    classes: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class AdmissionResult:
    admitted_raw_ids: tuple[str, ...]
    sidecar_text: dict[str, str]
    minimization: dict[str, MinimizationReceipt]


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise SourceError("workspace identity lock is not a regular file")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise SourceError("workspace identity lock is not owner-held")
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SourceError("workspace identity lock permissions are unsafe")
        if os.name == "nt":
            msvcrt: Any = __import__("msvcrt")

            if status.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            msvcrt_unlock: Any = __import__("msvcrt")

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt_unlock.locking(descriptor, msvcrt_unlock.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise SourceError("workspace identity write did not advance")
        offset += written


class WorkspaceIdentityMap:
    def __init__(self, home: Path) -> None:
        self._directory = Path(home) / "personal_insights"
        self._path = self._directory / "workspace_identity.json"
        self._lock_path = self._directory / ".workspace_identity.lock"

    def _secure_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._directory.chmod(0o700)
        status = self._directory.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise SourceError("workspace identity directory is unsafe")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise SourceError("workspace identity directory is not owner-held")
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SourceError("workspace identity directory permissions are unsafe")

    def _load_locked(self) -> dict[str, str]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags)
        except FileNotFoundError:
            return {}
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise SourceError("workspace identity map is not a regular file")
            if hasattr(os, "getuid") and status.st_uid != os.getuid():
                raise SourceError("workspace identity map is not owner-held")
            if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SourceError("workspace identity map permissions are unsafe")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        try:
            parsed = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SourceError("workspace identity map is corrupt") from error
        if not isinstance(parsed, dict):
            raise SourceError("workspace identity map is not an object")
        result: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SourceError("workspace identity map has invalid entries")
            try:
                identity = uuid.UUID(value)
            except ValueError as error:
                raise SourceError("workspace identity is not a UUID") from error
            if identity.version != 4:
                raise SourceError("workspace identity is not opaque")
            result[key] = value
        return result

    def _store_locked(self, mapping: dict[str, str]) -> None:
        payload = json.dumps(
            mapping, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace_identity.", dir=self._directory)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary_path, self._path)
            directory_descriptor = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def workspace_id_for(self, canonical_project: str) -> str:
        canonical = _canonical_project(canonical_project)
        self._secure_directory()
        with _exclusive_file_lock(self._lock_path):
            mapping = self._load_locked()
            existing = mapping.get(canonical)
            if existing is not None:
                return existing
            assigned = str(uuid.uuid4())
            mapping[canonical] = assigned
            self._store_locked(mapping)
            return assigned


def _canonical_project(project: str) -> str:
    if not isinstance(project, str) or not project or not os.path.isabs(project):
        raise SourceError("slot project must be an absolute path")
    return os.path.realpath(os.path.normpath(project))


def canonical_project_from_slot(slot: Any) -> str:
    project = getattr(slot, "project", None)
    if not isinstance(project, str) or not project:
        raise SourceError("slot has no canonical project")
    return _canonical_project(project)


def pin_scope(
    authenticated_principal: str,
    canonical_project_path: str,
    server_platform: str,
    identity_map: WorkspaceIdentityMap,
    request_fields: dict[str, object],
    window_start: str,
    window_end: str,
) -> CatalogPin:
    reject_client_platform(request_fields)
    if not authenticated_principal:
        raise SourceError("authenticated principal must be non-empty")
    if not server_platform:
        raise SourceError("server execution platform must be non-null")
    _parse_timestamp(window_start)
    _parse_timestamp(window_end)
    if _parse_timestamp(window_start) > _parse_timestamp(window_end):
        raise SourceError("catalog window is reversed")
    return CatalogPin(
        authenticated_principal=authenticated_principal,
        workspace_id=identity_map.workspace_id_for(canonical_project_path),
        execution_platform=server_platform,
        window_start=window_start,
        window_end=window_end,
    )


def pin_scope_from_slot(
    authenticated_principal: str,
    slot: Any,
    server_platform: str,
    identity_map: WorkspaceIdentityMap,
    request_fields: dict[str, object],
    window_start: str,
    window_end: str,
) -> CatalogPin:
    return pin_scope(
        authenticated_principal,
        canonical_project_from_slot(slot),
        server_platform,
        identity_map,
        request_fields,
        window_start,
        window_end,
    )


def _default_credential_redactor(text: str) -> str:
    from kiro_crew.platform.context import redact_via_context

    return redact_via_context(text)


def _minimize_text(text: str) -> tuple[str, MinimizationReceipt]:
    counts: dict[str, int] = {}

    def substitute(pattern: re.Pattern[str], marker: str, key: str, value: str) -> str:
        replaced, count = pattern.subn(marker, value)
        if count:
            counts[key] = counts.get(key, 0) + count
        return replaced

    minimized = _CONTROL_RE.sub("", text)
    minimized = substitute(_MENTION_RE, "[identity]", "identity", minimized)
    minimized = substitute(_EMAIL_RE, "[email]", "email", minimized)
    minimized = substitute(_PRIVATE_PATH_RE, "[private_path]", "private_path", minimized)
    minimized = substitute(_CREDENTIAL_HINT_RE, "[credential]", "credential", minimized)
    credential_redacted = _default_credential_redactor(minimized)
    if credential_redacted != minimized:
        counts["credential"] = counts.get("credential", 0) + 1
        minimized = credential_redacted
    return minimized, MinimizationReceipt(classes=tuple(sorted(counts.items())))


def safe_title(raw_title: str | None, session_kind: str, session_key: str) -> str:
    fallback = f"{session_kind}:{session_key[:8]}"
    if raw_title is None:
        return fallback
    if _CREDENTIAL_HINT_RE.search(raw_title):
        return fallback
    minimized, _ = _minimize_text(raw_title)
    if not minimized:
        return fallback
    return html.escape(minimized, quote=True)


def content_revision_for_catalog(row: dict[str, Any]) -> str | None:
    return None


def warm_reuse_decision(
    content_revision: str | None,
    cached_revision: str | None,
    message_count: int,
    cached_message_count: int,
    event_ceiling: int,
    cached_event_ceiling: int,
) -> str:
    if content_revision is None or cached_revision is None:
        return _EXPORT
    if content_revision != cached_revision:
        return _EXPORT
    if message_count != cached_message_count or event_ceiling != cached_event_ceiling:
        return _EXPORT
    return _REUSE


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise SourceError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise SourceError("timestamp lacks timezone")
    return parsed


def _enforce_authority(row: dict[str, Any], pin: CatalogPin) -> None:
    if str(row.get("owner_id")) != pin.authenticated_principal:
        raise AuthorityChangeError("authenticated principal changed between pages")
    if str(row.get("workspace_id")) != pin.workspace_id:
        raise AuthorityChangeError("workspace identity changed between pages")


def catalog_entry_from_host(row: dict[str, Any], pin: CatalogPin) -> CatalogEntry:
    _enforce_authority(row, pin)
    origin = _ORIGIN_MAP.get(str(row.get("origin")), "unknown")
    session_kind = _SESSION_KIND_MAP.get(str(row.get("session_kind")), "unknown")
    completeness = str(row.get("history_completeness_hint", "unknown"))
    if completeness not in _COMPLETENESS:
        completeness = "unknown"
    return CatalogEntry(
        session_key=str(row["session_key"]),
        owner_id=pin.authenticated_principal,
        workspace_id=pin.workspace_id,
        origin=origin,
        session_kind=session_kind,
        safe_title=safe_title(row.get("title"), session_kind, str(row["session_key"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        message_count=int(row["message_count"]),
        content_revision=None,
        history_completeness_hint=completeness,
    )


def _exclusion_reason(row: dict[str, Any], excluded_keys: set[str]) -> str | None:
    key = str(row["session_key"])
    if key in excluded_keys:
        return "user_excluded"
    if bool(row.get("temporary")):
        return "temporary"
    if bool(row.get("incognito")):
        return "incognito"
    if bool(row.get("deleted")):
        return "deleted"
    origin = _ORIGIN_MAP.get(str(row.get("origin")))
    if origin is None:
        return "origin_unknown"
    if origin != "dashboard":
        return "origin_not_dashboard"
    session_kind = _SESSION_KIND_MAP.get(str(row.get("session_kind")))
    if session_kind is None:
        return "session_kind_unknown"
    if session_kind != "user":
        return "session_kind_not_user"
    if not bool(row.get("accessible")):
        return "inaccessible"
    return None


def _cursor_fingerprint(pin: CatalogPin) -> str:
    payload = "\x1f".join(
        (
            pin.authenticated_principal,
            pin.workspace_id,
            pin.window_start,
            pin.window_end,
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _catalog_digest(entries: list[CatalogEntry]) -> str:
    payload = json.dumps(
        [(entry.session_key, entry.updated_at) for entry in entries],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_cursor(offset: int, pin: CatalogPin, catalog_digest: str) -> str:
    payload = json.dumps(
        {
            "catalog": catalog_digest,
            "offset": offset,
            "pin": _cursor_fingerprint(pin),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, pin: CatalogPin, catalog_digest: str) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor:
        raise CatalogCursorError("catalog cursor is invalid")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded).decode("ascii"))
    except (ValueError, UnicodeDecodeError) as error:
        raise CatalogCursorError("catalog cursor is invalid") from error
    if set(parsed) != {"catalog", "offset", "pin"}:
        raise CatalogCursorError("catalog cursor shape is invalid")
    offset = parsed["offset"]
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise CatalogCursorError("catalog cursor offset is invalid")
    if parsed["pin"] != _cursor_fingerprint(pin):
        raise CatalogCursorError("catalog cursor belongs to another scope")
    if parsed["catalog"] != catalog_digest:
        raise CatalogCursorError("catalog changed between pages")
    return offset


def _catalog_material(
    rows: list[dict[str, Any]], pin: CatalogPin, excluded_keys: set[str]
) -> tuple[list[CatalogEntry], list[SourceExcluded]]:
    if len(rows) > CATALOG_MAX:
        raise OversizedSourceError("catalog exceeds the maximum candidate count")
    start = _parse_timestamp(pin.window_start)
    end = _parse_timestamp(pin.window_end)
    included: list[CatalogEntry] = []
    excluded: list[SourceExcluded] = []
    for row in rows:
        _enforce_authority(row, pin)
        key = str(row["session_key"])
        updated = _parse_timestamp(str(row["updated_at"]))
        if updated < start or updated > end:
            excluded.append(SourceExcluded(key, "outside_window"))
            continue
        reason = _exclusion_reason(row, excluded_keys)
        if reason is not None:
            excluded.append(SourceExcluded(key, reason))
            continue
        included.append(catalog_entry_from_host(row, pin))
    included.sort(
        key=lambda value: (_parse_timestamp(value.updated_at), value.session_key),
        reverse=True,
    )
    excluded.sort(key=lambda value: (value.session_key, value.reason))
    return included, excluded


def catalog_page(
    rows: list[dict[str, Any]],
    pin: CatalogPin,
    excluded_keys: set[str],
    cursor: str | None,
    page_size: int,
) -> CatalogPage:
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise SourceError("catalog page size is invalid")
    if page_size < 1 or page_size > CATALOG_MAX:
        raise SourceError("catalog page size is outside the manifest bound")
    included, excluded = _catalog_material(rows, pin, excluded_keys)
    catalog_digest = _catalog_digest(included)
    offset = _decode_cursor(cursor, pin, catalog_digest)
    if offset > len(included):
        raise CatalogCursorError("catalog cursor exceeds the result set")
    end = min(offset + page_size, len(included))
    next_cursor = _encode_cursor(end, pin, catalog_digest) if end < len(included) else None
    return CatalogPage(
        entries=tuple(included[offset:end]),
        excluded=tuple(excluded),
        exact_count=len(included),
        next_cursor=next_cursor,
    )


def select_catalog_entries(
    rows: list[dict[str, Any]], pin: CatalogPin, excluded_keys: set[str]
) -> CatalogSelection:
    included, excluded = _catalog_material(rows, pin, excluded_keys)
    return CatalogSelection(tuple(included), tuple(excluded), len(included))


def _is_admissible_text(raw: RawEvent) -> bool:
    body_class = str(raw.structural_payload.get("body_class", "message"))
    return (
        raw.actor_class in _ADMITTED_ACTORS
        and raw.visibility_class == "user_visible"
        and raw.event_class == "message"
        and raw.content_state == "available"
        and raw.content is not None
        and body_class not in _FORBIDDEN_BODY_CLASSES
    )


def admit_raw_events(raws: list[RawEvent], max_events: int | None = None) -> AdmissionResult:
    if max_events is not None:
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise SourceError("raw event ceiling is invalid")
        if len(raws) > max_events:
            raise OversizedSourceError("source exceeds the pinned raw event ceiling")
    ordered = sorted(raws, key=lambda event: event.sequence)
    admitted: list[str] = []
    sidecar: dict[str, str] = {}
    receipts: dict[str, MinimizationReceipt] = {}
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for raw in ordered:
        if not raw.source_event_id or raw.source_event_id in seen_ids:
            raise SourceError("raw event id is missing or duplicated")
        if raw.sequence < 1 or raw.sequence in seen_orders:
            raise SourceError("raw event sequence is invalid or duplicated")
        seen_ids.add(raw.source_event_id)
        seen_orders.add(raw.sequence)
        admitted.append(raw.source_event_id)
        if _is_admissible_text(raw) and raw.content is not None:
            minimized, receipt = _minimize_text(raw.content)
            sidecar[raw.source_event_id] = minimized
            receipts[raw.source_event_id] = receipt
    return AdmissionResult(tuple(admitted), sidecar, receipts)
