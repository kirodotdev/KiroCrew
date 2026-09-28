"""Durable Windows pod run identity; missing evidence never certifies retirement.

The CLI reserves before scheduling. A separate short claim lock serializes
supervisor admission without taking the name mutex held by `pod up` while it
waits for health. Ready records are immutable until a terminal drain receipt;
only a retired publisher or the publisher's final action may write that receipt.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat as pc
from kiro_crew.atomic_write import atomic_write
from kiro_crew.pod import _windows_job as jobs
from kiro_crew.pod.config import PodConfig

_VERSION = 1
_MAX_RECORD_BYTES = 8192
_STATES = {"reserved", "cancelled", "preparing", "ready", "drained"}


def path(cfg: PodConfig, name: str) -> Path:
    return cfg.pods_dir / f"{cfg.unit_prefix}.{name}.winrun"


def _plane(cfg: PodConfig) -> list[str]:
    return [cfg.unit_prefix, str(cfg.pods_dir.resolve()), str(cfg.pod_root.resolve())]


@contextlib.contextmanager
def claim_lock(cfg: PodConfig, name: str) -> Iterator[None]:
    """Short nonblocking admission lock; contention refuses instead of deadlocking."""
    with pc.open_lock_file(path(cfg, name).with_suffix(".winrun.lock")) as fd:
        with pc.file_lock(fd, exclusive=True, required=True, wait=False):
            yield


def _valid_identity(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and type(value[0]) is int
        and 1 < value[0] <= 0xFFFFFFFF
        and isinstance(value[1], str)
        and value[1].isascii()
        and value[1].isdigit()
        and int(value[1]) > 0
    )


def read(cfg: PodConfig, name: str) -> dict[str, Any] | None:
    """Validate a bounded record. Only ENOENT returns None; all other faults raise."""
    try:
        with path(cfg, name).open("rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > _MAX_RECORD_BYTES:
        raise OSError("Windows pod run record is oversized")
    try:
        record = json.loads(raw)
        valid = (
            isinstance(record, dict)
            and type(record.get("version")) is int
            and record["version"] == _VERSION
            and record.get("plane") == _plane(cfg)
            and record.get("name") == name
            and isinstance(record.get("generation"), str)
            and uuid.UUID(hex=record["generation"]).hex == record["generation"]
            and record.get("state") in _STATES
        )
        if not valid:
            raise ValueError("invalid run identity")
        if record["state"] in {"reserved", "cancelled"}:
            if any(key in record for key in ("publisher", "root", "job")):
                raise ValueError("unclaimed run carries runtime evidence")
        elif not _valid_identity(record.get("publisher")):
            raise ValueError("invalid publisher identity")
        if record["state"] in {"ready", "drained"}:
            if not _valid_identity(record.get("root")):
                raise ValueError("invalid initial process identity")
            job_name = record.get("job")
            if not isinstance(job_name, str) or jobs._NAME_RE.fullmatch(job_name) is None:
                raise ValueError("invalid contained job name")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise OSError("Windows pod run record is malformed or belongs to another run") from exc
    return record


def publish(cfg: PodConfig, name: str, record: dict[str, Any]) -> None:
    atomic_write(path(cfg, name), json.dumps(record), fsync=True, restrict_to_owner=True)


def reserve(cfg: PodConfig, name: str) -> dict[str, Any]:
    """Reserve a fresh invocation; never overwrite unresolved runtime evidence."""
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    with claim_lock(cfg, name):
        if read(cfg, name) is not None:
            raise OSError("an unresolved Windows pod run must be retired before another start")
        record = {
            "version": _VERSION,
            "plane": _plane(cfg),
            "name": name,
            "generation": uuid.uuid4().hex,
            "state": "reserved",
        }
        publish(cfg, name, record)
        return record


@contextlib.contextmanager
def cancel_reserved(cfg: PodConfig, name: str, record: dict[str, Any]) -> Iterator[None]:
    """Revoke only this unclaimed start, with admission locked through cleanup.

    Caller must not have attempted /Run. Persist cancellation before cleanup so
    an I/O failure is retryable without admitting a supervisor to this generation.
    A plain reservation is never enough evidence to recover a different start.
    """
    with claim_lock(cfg, name):
        if record["state"] not in {"reserved", "cancelled"} or read(cfg, name) != record:
            raise OSError("Windows pod reservation changed; startup rollback refused")
        if record["state"] == "reserved":
            publish(cfg, name, {**record, "state": "cancelled"})
        yield
        path(cfg, name).unlink()


def claim(cfg: PodConfig, name: str) -> dict[str, Any]:
    with claim_lock(cfg, name):
        record = read(cfg, name)
        if record is None or record["state"] != "reserved":
            raise OSError("Windows pod boot requires an unclaimed run reservation")
        token = pc.process_start_time(os.getpid())
        if not token:
            raise OSError("Windows pod publisher identity is unavailable")
        record.update(state="preparing", publisher=[os.getpid(), token])
        publish(cfg, name, record)
        return record


def ready(
    cfg: PodConfig, name: str, record: dict[str, Any], job: str, root: tuple[int, str]
) -> dict[str, Any]:
    """Publish only after suspended assignment, preserving the reserved generation."""
    current = read(cfg, name)
    if current != record or record["state"] != "preparing":
        raise OSError("Windows pod run changed during suspended boot")
    result = {**record, "state": "ready", "job": job, "root": list(root)}
    publish(cfg, name, result)
    return result


def drained(cfg: PodConfig, name: str, record: dict[str, Any]) -> None:
    """Retain a generation-bound kernel-zero receipt until HOME reclamation ends."""
    current = read(cfg, name)
    if current is None or {**current, "state": "ready"} != {**record, "state": "ready"}:
        raise OSError("Windows pod run changed before drain receipt publication")
    publish(cfg, name, {**record, "state": "drained"})


def finish(cfg: PodConfig, name: str) -> None:
    """Consume the receipt only after all HOME sweeps succeed under the name lock."""
    record = read(cfg, name)
    if record is None:
        return
    if record["state"] != "drained" or cfg.home_dir(name).exists():
        raise OSError("Windows pod reclamation has no completed drain receipt")
    path(cfg, name).unlink()
