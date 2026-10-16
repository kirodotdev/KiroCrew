"""Durable gateway attestations for provider-generated SharePoint locators.

The store contains only SHA-256 digests of ``lowercase-host + path-through-id``.
It never persists a URL. A locator is admitted only after the gateway proves the
MCP identity, an exact companion-declared result path, trusted-host membership,
and a complete same-call input scan.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry, refuse_linked_parent
from kiro_crew.config.paths import data_home
from kiro_crew.security.redaction import (
    _SHAREPOINT_LOCATOR_RE,
    _sharepoint_locator_digest,
)

STORE_MASK_ROOT = "vouched-executions"
STORE_DIR_NAME = f"{STORE_MASK_ROOT}/locator-attestations"
STORE_FILE_NAME = "sharepoint.json"
STORE_VERSION = 1
MAX_FILE_BYTES = 1024 * 1024
MAX_ATTESTATIONS = 8192
MAX_DECLARED_PATHS = 64
MAX_PATH_SEGMENTS = 12
MAX_COLLECTION_ENTRIES = 512
MAX_INPUT_NODES = 4096
MAX_RESULT_TEXT_BYTES = 1024 * 1024
REVALIDATE_INTERVAL_SECONDS = 1.0

_FIELD_PATH_RE = re.compile(
    rf"[A-Za-z][A-Za-z0-9_-]{{0,63}}(?:\.[A-Za-z][A-Za-z0-9_-]{{0,63}})"
    rf"{{0,{MAX_PATH_SEGMENTS - 1}}}"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

_LOCK = threading.RLock()
_path_override: Path | None = None
_FileStamp = tuple[int, int, int, int]
_Snapshot = tuple[Path, int, frozenset[str], _FileStamp | None]
_snapshot: _Snapshot | None = None
_loading = False
_load_thread: threading.Thread | None = None
_next_validation_ns = 0


class _AttestationRefused(Exception):
    """A bounded parser or registry check could not prove admission safe."""


def _path() -> Path:
    return _path_override or data_home() / STORE_DIR_NAME / STORE_FILE_NAME


def _digest_match(match: re.Match[str]) -> str:
    return _sharepoint_locator_digest(match)


def _locator_digests(text: str, trusted_hosts: frozenset[str] | None = None) -> set[str]:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_RESULT_TEXT_BYTES:
        raise _AttestationRefused
    hosts = None if trusted_hosts is None else {host.lower() for host in trusted_hosts}
    out: set[str] = set()
    for match in _SHAREPOINT_LOCATOR_RE.finditer(text):
        if hosts is not None and match.group("host").lower() not in hosts:
            continue
        out.add(_digest_match(match))
        if len(out) > MAX_COLLECTION_ENTRIES:
            raise _AttestationRefused
    return out


def _empty_state() -> dict[str, Any]:
    return {"version": STORE_VERSION, "generation": 0, "clock": 0, "entries": []}


def _decode_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
        raise _AttestationRefused
    generation = raw.get("generation")
    clock = raw.get("clock")
    entries = raw.get("entries")
    if (
        type(generation) is not int
        or generation < 0
        or type(clock) is not int
        or clock < 0
        or not isinstance(entries, list)
        or len(entries) > MAX_ATTESTATIONS
    ):
        raise _AttestationRefused
    normalized: list[dict[str, Any]] = []
    seen_digests: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _AttestationRefused
        digest = entry.get("digest")
        seen = entry.get("seen")
        if (
            not isinstance(digest, str)
            or not _DIGEST_RE.fullmatch(digest)
            or digest in seen_digests
            or type(seen) is not int
            or seen < 0
            or seen > clock
        ):
            raise _AttestationRefused
        seen_digests.add(digest)
        normalized.append({"digest": digest, "seen": seen})
    normalized.sort(key=lambda entry: (-entry["seen"], entry["digest"]))
    state = {
        "version": STORE_VERSION,
        "generation": generation,
        "clock": clock,
        "entries": normalized,
    }
    if raw != state:
        raise _AttestationRefused
    return state


def _refuse_or_empty(strict: bool, exc: BaseException | None = None) -> dict[str, Any]:
    if strict:
        if exc is None:
            raise _AttestationRefused
        raise _AttestationRefused from exc
    return _empty_state()


def _file_stamp(value: os.stat_result) -> _FileStamp:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _stat_file(path: Path) -> _FileStamp:
    """Return a validated path stamp. Call only outside the event loop."""
    refuse_linked_parent(path)
    value = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise OSError("attestation registry is not a private regular file")
    return _file_stamp(value)


def _read_file(*, strict: bool = False) -> tuple[dict[str, Any], _FileStamp | None]:
    path = _path()
    stamp: _FileStamp | None = None
    try:
        refuse_linked_parent(path)
        if platform_compat.IS_WINDOWS:
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return _refuse_or_empty(strict), None
            raw = read_bytes_with_retry(path, max_bytes=MAX_FILE_BYTES + 1)
            after = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            ):
                return _refuse_or_empty(strict), None
            stamp = _file_stamp(after)
        else:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    return _refuse_or_empty(strict), None
                chunks: list[bytes] = []
                remaining = MAX_FILE_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(fd)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    return _refuse_or_empty(strict), None
                raw = b"".join(chunks)
                stamp = _file_stamp(after)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return _empty_state(), None
    except (OSError, RuntimeError) as exc:
        return _refuse_or_empty(strict, exc), None
    if len(raw) > MAX_FILE_BYTES:
        return _refuse_or_empty(strict), stamp
    try:
        return _decode_state(json.loads(raw.decode("utf-8"))), stamp
    except (ValueError, UnicodeError, RecursionError, _AttestationRefused) as exc:
        return _refuse_or_empty(strict, exc), stamp


def _snapshot_for_path() -> _Snapshot | None:
    current = _snapshot
    return current if current is not None and current[0] == _path() else None


def _make_snapshot(path: Path, state: dict[str, Any], stamp: _FileStamp | None) -> _Snapshot:
    return (
        path,
        state["generation"],
        frozenset(entry["digest"] for entry in state["entries"]),
        stamp,
    )


def _schedule_next_validation() -> None:
    global _next_validation_ns
    _next_validation_ns = time.monotonic_ns() + int(REVALIDATE_INTERVAL_SECONDS * 1e9)


def _validated_refresh_state(current: _Snapshot | None, state: dict[str, Any]) -> dict[str, Any]:
    """Reject a replaced file whose generation does not describe its contents."""
    if current is None:
        return state
    digests = frozenset(entry["digest"] for entry in state["entries"])
    if state["generation"] < current[1] or (
        state["generation"] == current[1] and digests != current[2]
    ):
        return _empty_state()
    return state


def _refresh_from_disk(*, force: bool) -> None:
    global _snapshot
    with _LOCK:
        path = _path()
        current = _snapshot_for_path()
        if not force and current is not None:
            try:
                stamp = _stat_file(path)
            except (FileNotFoundError, OSError, RuntimeError):
                stamp = None
            if stamp is not None and current[3] == stamp:
                _schedule_next_validation()
                return
        state, stamp = _read_file()
        _snapshot = _make_snapshot(path, _validated_refresh_state(current, state), stamp)
        _schedule_next_validation()


def preload() -> None:
    """Load the durable registry. Call off the gateway event loop."""
    _refresh_from_disk(force=True)


def _background_preload() -> None:
    global _loading
    try:
        _refresh_from_disk(force=False)
    finally:
        with _LOCK:
            _loading = False


def _start_background_load() -> None:
    global _loading, _load_thread
    cold = _snapshot_for_path() is None
    if _loading or (not cold and time.monotonic_ns() < _next_validation_ns):
        return
    if not _LOCK.acquire(blocking=False):
        return
    try:
        cold = _snapshot_for_path() is None
        if _loading or (not cold and time.monotonic_ns() < _next_validation_ns):
            return
        _loading = True
        _load_thread = threading.Thread(
            target=_background_preload, name="locator-attestation-load", daemon=True
        )
        _load_thread.start()
    finally:
        _LOCK.release()


def snapshot() -> tuple[int, frozenset[str]]:
    """Return the current memory view and schedule off-loop file validation."""
    current = _snapshot_for_path()
    _start_background_load()
    if current is None:
        return 0, frozenset()
    return current[1], current[2]


def digests_for_text(text: str) -> frozenset[str]:
    """Return the immutable digest view only for text that can contain a locator."""
    if "sharepoint.com" not in text.lower():
        return frozenset()
    return snapshot()[1]


def _declared_values(value: Any, segments: tuple[str, ...], *, depth: int = 0) -> Iterable[str]:
    if depth > MAX_PATH_SEGMENTS:
        raise _AttestationRefused
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ENTRIES:
            raise _AttestationRefused
        for child in value:
            yield from _declared_values(child, segments, depth=depth + 1)
        return
    if not segments or not isinstance(value, Mapping):
        return
    if len(value) > MAX_COLLECTION_ENTRIES:
        raise _AttestationRefused
    child = value.get(segments[0])
    if len(segments) == 1:
        if isinstance(child, str):
            yield child
        return
    yield from _declared_values(child, segments[1:], depth=depth + 1)


def _input_digests(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> set[str]:
    if budget is None:
        budget = [MAX_INPUT_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_PATH_SEGMENTS:
        raise _AttestationRefused
    if isinstance(value, str):
        return _locator_digests(value)
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ENTRIES:
            raise _AttestationRefused
        out: set[str] = set()
        for key, child in value.items():
            if not isinstance(key, str):
                raise _AttestationRefused
            out.update(_locator_digests(key))
            out.update(_input_digests(child, depth=depth + 1, budget=budget))
            if len(out) > MAX_COLLECTION_ENTRIES:
                raise _AttestationRefused
        return out
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ENTRIES:
            raise _AttestationRefused
        out = set()
        for child in value:
            out.update(_input_digests(child, depth=depth + 1, budget=budget))
            if len(out) > MAX_COLLECTION_ENTRIES:
                raise _AttestationRefused
        return out
    if value is None or isinstance(value, (bool, int, float)):
        return set()
    raise _AttestationRefused


def _write_digests(digests: set[str]) -> bool:
    global _snapshot
    if not digests:
        return False
    with _LOCK:
        state, _ = _read_file(strict=True)
        state["clock"] += 1
        clock = state["clock"]
        existing = {entry["digest"]: entry for entry in state["entries"]}
        for digest in sorted(digests):
            existing[digest] = {"digest": digest, "seen": clock}
        state["entries"] = sorted(
            existing.values(), key=lambda entry: (-entry["seen"], entry["digest"])
        )[:MAX_ATTESTATIONS]
        state["generation"] += 1
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True)
        if len(payload.encode("utf-8")) > MAX_FILE_BYTES:
            return False
        path = _path()
        current = _snapshot_for_path()
        disk_digests = frozenset(entry["digest"] for entry in state["entries"])
        if current is not None and (
            state["generation"] < current[1]
            or (state["generation"] == current[1] and disk_digests != current[2])
        ):
            return False
        try:
            atomic_write(path, payload, fsync=True, mode=0o600, restrict_to_owner=True)
            try:
                stamp = _stat_file(path)
            except (FileNotFoundError, OSError, RuntimeError):
                stamp = None
        except (OSError, RuntimeError, ValueError):
            return False
        _snapshot = _make_snapshot(path, state, stamp)
        _schedule_next_validation()
        return True


def attest_provider_result(
    *,
    update: Mapping[str, Any],
    raw_input: Any,
    declared_fields: Iterable[str],
    trusted_hosts: Iterable[str],
) -> int:
    """Persist safe locators from exact declared scalar paths, or attest none."""
    try:
        if not isinstance(raw_input, Mapping):
            return 0
        fields = frozenset(itertools.islice(declared_fields, MAX_DECLARED_PATHS + 1))
        if (
            not fields
            or len(fields) > MAX_DECLARED_PATHS
            or any(
                not isinstance(field, str) or not _FIELD_PATH_RE.fullmatch(field)
                for field in fields
            )
        ):
            return 0
        host_members = tuple(itertools.islice(trusted_hosts, MAX_DECLARED_PATHS + 1))
        if len(host_members) > MAX_DECLARED_PATHS or any(
            not isinstance(host, str) for host in host_members
        ):
            return 0
        hosts = frozenset(host.lower() for host in host_members)
        if not hosts:
            return 0
        echoed = _input_digests(raw_input)
        candidates: set[str] = set()
        roots = (update.get("rawOutput"), update.get("content"))
        for field in fields:
            segments = tuple(field.split("."))
            for root in roots:
                for value in _declared_values(root, segments):
                    candidates.update(_locator_digests(value, hosts))
                    if len(candidates) > MAX_COLLECTION_ENTRIES:
                        raise _AttestationRefused
        candidates.difference_update(echoed)
        if not candidates or not _write_digests(candidates):
            return 0
        return len(candidates)
    except Exception:
        return 0
