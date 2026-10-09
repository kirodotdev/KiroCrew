from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kiro_crew.platform.interfaces import (
    InsightsProjectionDescriptor,
    InsightsProjectionProvider,
)

RECEIPT_SCHEMA: Final[str] = "agent-session-intelligence.projection-receipt/1.0"
_GRACEFUL_STOP_SECONDS: Final[float] = 2.0
_READ_CHUNK: Final[int] = 65536
_NEUTRAL_RE: Final[re.Pattern[str]] = re.compile(r"ne-[0-9a-f]{64}\Z")
_RAW_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_RECEIPT_INT_FIELDS: Final[tuple[str, ...]] = (
    "event_count",
    "relation_count",
    "group_count",
    "interaction_count",
    "lifecycle_count",
    "multi_turn_count",
    "root_event_count",
    "grouped_event_count",
)
_RECEIPT_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "ingest_persisted", "raw_to_neutral", *_RECEIPT_INT_FIELDS}
)


class ProjectionError(Exception):
    pass


class ProjectionExecutionError(ProjectionError):
    pass


class StagedExecutableError(ProjectionError):
    pass


class ProjectionTimeout(ProjectionError):
    pass


class ProjectionReceiptError(ProjectionError):
    pass


@dataclass(frozen=True)
class ProjectionReceipt:
    schema_version: str
    event_count: int
    relation_count: int
    group_count: int
    interaction_count: int
    lifecycle_count: int
    multi_turn_count: int
    root_event_count: int
    grouped_event_count: int
    ingest_persisted: bool
    raw_to_neutral: dict[str, str]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_count": self.event_count,
            "relation_count": self.relation_count,
            "group_count": self.group_count,
            "interaction_count": self.interaction_count,
            "lifecycle_count": self.lifecycle_count,
            "multi_turn_count": self.multi_turn_count,
            "root_event_count": self.root_event_count,
            "grouped_event_count": self.grouped_event_count,
            "ingest_persisted": self.ingest_persisted,
            "raw_to_neutral": dict(sorted(self.raw_to_neutral.items())),
        }

    def receipt_digest(self) -> str:
        payload = json.dumps(
            self.to_wire(), sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def descriptor_digest(descriptor: InsightsProjectionDescriptor) -> str:
    payload = json.dumps(
        {
            "schema_version": descriptor.schema_version,
            "executable_path": descriptor.executable_path,
            "executable_sha256": descriptor.executable_sha256,
            "argv": list(descriptor.argv),
            "max_stdin_bytes": descriptor.max_stdin_bytes,
            "max_stdout_bytes": descriptor.max_stdout_bytes,
            "timeout_seconds": descriptor.timeout_seconds,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_descriptor(descriptor: int) -> str:
    hasher = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, _READ_CHUNK)
        if not chunk:
            break
        hasher.update(chunk)
    return hasher.hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise StagedExecutableError("staged executable write did not advance")
        offset += written


def _open_source(descriptor: InsightsProjectionDescriptor) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source = os.open(descriptor.executable_path, flags)
    except OSError as error:
        raise StagedExecutableError(f"cannot open source executable: {error}") from error
    status = os.fstat(source)
    if not stat.S_ISREG(status.st_mode):
        os.close(source)
        raise StagedExecutableError("source executable is not a regular file")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        os.close(source)
        raise StagedExecutableError("source executable is not owner-held")
    if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(source)
        raise StagedExecutableError("source executable mode is unsafe")
    if not status.st_mode & stat.S_IXUSR:
        os.close(source)
        raise StagedExecutableError("source executable lacks owner execute mode")
    if _hash_descriptor(source) != descriptor.executable_sha256:
        os.close(source)
        raise StagedExecutableError("source executable digest mismatch")
    os.lseek(source, 0, os.SEEK_SET)
    return source


def _verify_staged(path: Path, expected_digest: str) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise StagedExecutableError("staged executable is not a regular file")
        if stat.S_IMODE(status.st_mode) != 0o700:
            raise StagedExecutableError("staged executable mode is unsafe")
        if _hash_descriptor(descriptor) != expected_digest:
            raise StagedExecutableError("staged executable digest mismatch")
    finally:
        os.close(descriptor)


def _stage_from_source(source: int, descriptor: InsightsProjectionDescriptor) -> Path:
    staging_directory = Path(tempfile.mkdtemp(prefix="p05-projection-"))
    try:
        staging_directory.chmod(0o700)
        status = staging_directory.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise StagedExecutableError("staging directory is unsafe")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise StagedExecutableError("staging directory is not owner-held")
        if stat.S_IMODE(status.st_mode) != 0o700:
            raise StagedExecutableError("staging directory mode is unsafe")
        staged = staging_directory / "asi-projection-probe"
        target = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            while True:
                chunk = os.read(source, _READ_CHUNK)
                if not chunk:
                    break
                _write_all(target, chunk)
            os.fsync(target)
        finally:
            os.close(target)
        _verify_staged(staged, descriptor.executable_sha256)
        return staged
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        process.wait()
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=_GRACEFUL_STOP_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_GRACEFUL_STOP_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ProjectionExecutionError("projection process group did not terminate") from error


def _bounded_run(
    staged: Path,
    descriptor: InsightsProjectionDescriptor,
    stdin_bytes: bytes,
) -> bytes:
    process = subprocess.Popen(
        [str(staged), *descriptor.argv[1:]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=staged.parent,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        bufsize=0,
    )
    if process.stdin is None or process.stdout is None:
        _terminate(process)
        raise ProjectionExecutionError("projection stdio pipes are unavailable")
    stdin_descriptor = process.stdin.fileno()
    stdout_descriptor = process.stdout.fileno()
    os.set_blocking(stdin_descriptor, False)
    os.set_blocking(stdout_descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(stdout_descriptor, selectors.EVENT_READ, "stdout")
    if stdin_bytes:
        selector.register(stdin_descriptor, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    deadline = time.monotonic() + descriptor.timeout_seconds
    input_offset = 0
    output = bytearray()
    stdout_open = True
    try:
        while stdout_open or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                raise ProjectionTimeout("projection executable exceeded its timeout")
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.data == "stdin":
                    try:
                        written = os.write(
                            stdin_descriptor,
                            stdin_bytes[input_offset : input_offset + _READ_CHUNK],
                        )
                    except BrokenPipeError:
                        written = 0
                        input_offset = len(stdin_bytes)
                    input_offset += written
                    if input_offset >= len(stdin_bytes):
                        selector.unregister(stdin_descriptor)
                        process.stdin.close()
                else:
                    allowed = descriptor.max_stdout_bytes - len(output) + 1
                    chunk = os.read(stdout_descriptor, min(_READ_CHUNK, allowed))
                    if chunk:
                        output.extend(chunk)
                        if len(output) > descriptor.max_stdout_bytes:
                            _terminate(process)
                            raise ProjectionExecutionError(
                                "projection receipt exceeds stdout bound"
                            )
                    else:
                        selector.unregister(stdout_descriptor)
                        process.stdout.close()
                        stdout_open = False
            if process.poll() is not None and not stdout_open:
                break
        return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        _terminate(process)
        raise ProjectionTimeout("projection executable exceeded its timeout") from error
    finally:
        selector.close()
        if process.poll() is None:
            _terminate(process)
    if return_code != 0:
        raise ProjectionExecutionError(f"projection executable exited with status {return_code}")
    return bytes(output)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectionReceiptError("projection receipt has a duplicate key")
        result[key] = value
    return result


def _parse_receipt(raw: bytes) -> ProjectionReceipt:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except ProjectionReceiptError:
        raise
    except (UnicodeDecodeError, ValueError) as error:
        raise ProjectionReceiptError("projection receipt is not strict JSON") from error
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_FIELDS:
        raise ProjectionReceiptError("projection receipt fields are not exact")
    if payload["schema_version"] != RECEIPT_SCHEMA:
        raise ProjectionReceiptError("projection receipt schema version is unsupported")
    for field in _RECEIPT_INT_FIELDS:
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProjectionReceiptError(f"projection receipt field {field} is invalid")
    if payload["ingest_persisted"] is not True:
        raise ProjectionReceiptError("projection receipt did not persist ingest")
    event_count = payload["event_count"]
    group_count = payload["group_count"]
    if payload["grouped_event_count"] != event_count:
        raise ProjectionReceiptError("projection receipt does not group every event")
    if group_count != (
        payload["interaction_count"] + payload["lifecycle_count"] + payload["multi_turn_count"]
    ):
        raise ProjectionReceiptError("projection group-kind counts are inconsistent")
    if event_count > 0 and (group_count < 1 or payload["root_event_count"] < 1):
        raise ProjectionReceiptError("non-empty projection lacks groups or roots")
    if payload["root_event_count"] > event_count:
        raise ProjectionReceiptError("projection root count exceeds event count")
    if payload["relation_count"] > event_count * 4:
        raise ProjectionReceiptError("projection relation count exceeds structural maximum")
    raw_to_neutral = payload["raw_to_neutral"]
    if not isinstance(raw_to_neutral, dict) or len(raw_to_neutral) != event_count:
        raise ProjectionReceiptError("projection receipt map is incomplete")
    neutral_ids: set[str] = set()
    for raw_id, neutral_id in raw_to_neutral.items():
        if not isinstance(raw_id, str) or _RAW_RE.fullmatch(raw_id) is None:
            raise ProjectionReceiptError("projection receipt raw id is invalid")
        if not isinstance(neutral_id, str) or _NEUTRAL_RE.fullmatch(neutral_id) is None:
            raise ProjectionReceiptError("projection receipt neutral id is invalid")
        if neutral_id in neutral_ids:
            raise ProjectionReceiptError("projection receipt neutral ids are duplicated")
        neutral_ids.add(neutral_id)
    return ProjectionReceipt(
        schema_version=RECEIPT_SCHEMA,
        event_count=payload["event_count"],
        relation_count=payload["relation_count"],
        group_count=payload["group_count"],
        interaction_count=payload["interaction_count"],
        lifecycle_count=payload["lifecycle_count"],
        multi_turn_count=payload["multi_turn_count"],
        root_event_count=payload["root_event_count"],
        grouped_event_count=payload["grouped_event_count"],
        ingest_persisted=True,
        raw_to_neutral=dict(raw_to_neutral),
    )


class ProjectionRunner:
    def __init__(self, descriptor: InsightsProjectionDescriptor) -> None:
        self.descriptor = descriptor

    def run(self, stdin_bytes: bytes) -> ProjectionReceipt:
        if not isinstance(stdin_bytes, bytes):
            raise ProjectionExecutionError("projection input must be bytes")
        if len(stdin_bytes) > self.descriptor.max_stdin_bytes:
            raise ProjectionExecutionError("projection input exceeds stdin bound")
        source = _open_source(self.descriptor)
        staged: Path | None = None
        try:
            staged = _stage_from_source(source, self.descriptor)
        finally:
            os.close(source)
        try:
            return _parse_receipt(_bounded_run(staged, self.descriptor, stdin_bytes))
        finally:
            shutil.rmtree(staged.parent)


def run_projection(
    descriptor: InsightsProjectionDescriptor,
    stdin_bytes: bytes,
) -> ProjectionReceipt:
    return ProjectionRunner(descriptor).run(stdin_bytes)


def run_with_provider(
    provider: InsightsProjectionProvider,
    stdin_bytes: bytes,
) -> ProjectionReceipt:
    return ProjectionRunner(provider.descriptor()).run(stdin_bytes)


def resolve_projection_descriptor() -> InsightsProjectionDescriptor:
    from kiro_crew.platform.context import current_context

    return current_context().insights_projection.descriptor()
