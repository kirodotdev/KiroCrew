"""Durable Windows pod run identity; missing evidence never certifies retirement.

The CLI reserves before scheduling. A separate short claim lock serializes
producer admission without taking the name mutex held by `pod up` while it
waits for health. The scheduled producer claims the reservation as its first
act, before any provisioning check, so every later refusal carries the identity
(pid + creation time) of the one process that writes this generation's sidecars.
A producer that refuses without spawning a gateway publishes a `finished`
receipt as its last write to the record; it still logs the boot's audit event
on its way out, so `stop` lets it exit before ending it. Ready records are
immutable until a terminal drain
receipt; only a retired publisher or the publisher's final action may write
that receipt. `stop` publishes `retired` once it has proven an unspawned
generation's producer gone, and `finish` consumes either terminal receipt.

Every record also carries the kernel uptime at which it was reserved and then
claimed. A current uptime below that value proves Windows restarted since, and
a restart ends every process the record can name, so `stop` may publish a
terminal receipt marked `rebooted` for any unfinished state. A record without
an uptime, or one taken in the current boot session, proves nothing about a
restart and keeps every other rule.
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
_STATES = {"reserved", "cancelled", "preparing", "finished", "ready", "drained", "retired"}
#: States of a claimed generation whose producer never published a contained
#: gateway. `stop` retires these by proving the producer gone, not by draining.
UNSPAWNED_STATES = frozenset({"preparing", "finished"})
#: Receipts `finish` consumes once HOME reclamation has succeeded.
TERMINAL_STATES = frozenset({"drained", "retired"})


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


def _valid_uptime(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 0xFFFFFFFFFFFFFFFF


def _uptime() -> int | None:
    """The current kernel uptime, or None when it cannot be read (never a guess)."""
    try:
        value = jobs.uptime_ms()
    except OSError:
        return None
    return value if _valid_uptime(value) else None


def _stamped(record: dict[str, Any]) -> dict[str, Any]:
    """*record* with this boot session's uptime, or with none when unreadable."""
    stamped = {key: value for key, value in record.items() if key != "uptime"}
    uptime = _uptime()
    if uptime is not None:
        stamped["uptime"] = uptime
    return stamped


def earlier_boot(record: dict[str, Any]) -> bool:
    """Whether Windows has restarted since *record* was last stamped.

    True only on positive proof: the record carries an uptime and the current
    uptime is lower. A missing or unreadable uptime is unknown, never a restart.
    """
    recorded = record.get("uptime")
    current = _uptime()
    return recorded is not None and current is not None and current < recorded


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
        if "uptime" in record and not _valid_uptime(record["uptime"]):
            raise ValueError("invalid boot-session uptime")
        if "rebooted" in record and (
            record["state"] not in TERMINAL_STATES
            or not _valid_uptime(record["rebooted"])
            or "uptime" not in record
            or record["rebooted"] >= record["uptime"]
        ):
            raise ValueError("invalid restart receipt")
        # A restart receipt for a never-claimed run has no publisher to name.
        unclaimed = record["state"] in {"reserved", "cancelled"} or (
            record["state"] == "retired" and "rebooted" in record and "publisher" not in record
        )
        if unclaimed:
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
        elif record["state"] in UNSPAWNED_STATES | {"retired"}:
            if any(key in record for key in ("root", "job")):
                raise ValueError("unspawned run carries gateway evidence")
            if record["state"] == "finished" and type(record.get("exit")) is not int:
                raise ValueError("finished run carries no exit code")
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
        record = _stamped(
            {
                "version": _VERSION,
                "plane": _plane(cfg),
                "name": name,
                "generation": uuid.uuid4().hex,
                "state": "reserved",
            }
        )
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
    """Bind the reserved generation to this process before it does anything else."""
    with claim_lock(cfg, name):
        record = read(cfg, name)
        if record is None or record["state"] != "reserved":
            raise OSError("Windows pod boot requires an unclaimed run reservation")
        token = pc.process_start_time(os.getpid())
        if not token:
            raise OSError("Windows pod publisher identity is unavailable")
        # Re-stamped: the claim, not the reservation, starts this boot session's writers.
        record = _stamped({**record, "state": "preparing", "publisher": [os.getpid(), token]})
        publish(cfg, name, record)
        return record


def own_claim(cfg: PodConfig, name: str) -> dict[str, Any]:
    """The `preparing` record this process claimed; anything else refuses."""
    record = read(cfg, name)
    if record is None or record["state"] != "preparing":
        raise OSError("Windows pod boot requires this process's own claimed run")
    token = pc.process_start_time(os.getpid())
    if not token or record["publisher"] != [os.getpid(), token]:
        raise OSError("Windows pod run is claimed by a different publisher")
    return record


def finished(cfg: PodConfig, name: str, record: dict[str, Any], exit_code: int) -> None:
    """The producer's last write to the record: it spawned no gateway and is exiting.

    Only the claiming publisher may write it, and only over its own unchanged
    `preparing` record. A boot that reached `ready` publishes `drained` instead.
    """
    with claim_lock(cfg, name):
        current = read(cfg, name)
        if current != record or record["state"] != "preparing":
            raise OSError("Windows pod run changed before its completion receipt")
        publish(cfg, name, {**record, "state": "finished", "exit": int(exit_code)})


def retired(cfg: PodConfig, name: str, record: dict[str, Any]) -> dict[str, Any]:
    """Record that an unspawned generation's producer is proven gone.

    Written by `stop` after it has verified the publisher identity is retired and
    no gateway evidence exists, so HOME reclamation may proceed and retry.
    """
    with claim_lock(cfg, name):
        current = read(cfg, name)
        if current != record or record["state"] not in UNSPAWNED_STATES:
            raise OSError("Windows pod run changed while retiring its producer")
        result = {**record, "state": "retired"}
        publish(cfg, name, result)
        return result


def rebooted(cfg: PodConfig, name: str, record: dict[str, Any]) -> dict[str, Any]:
    """Retire an unfinished generation whose boot session has ended.

    Written by `stop` once :func:`earlier_boot` holds. A restart ends the
    publisher, the gateway and every descendant either could have started, so no
    process from that session can write again. Ready and drained runs become
    `drained`, the rest `retired`; the current uptime is kept as `rebooted`. A
    `retired` run already names no live writer and is left alone. Under the claim
    lock, so a boot started in this session cannot claim the run afterwards.
    """
    with claim_lock(cfg, name):
        current = read(cfg, name)
        if current != record or record["state"] == "retired" or "rebooted" in record:
            raise OSError("Windows pod run changed while retiring it after a restart")
        now = _uptime()
        if now is None or "uptime" not in record or now >= record["uptime"]:
            raise OSError("Windows pod run is not proven to predate a restart")
        state = "drained" if record["state"] in {"ready", "drained"} else "retired"
        result = {**record, "state": state, "rebooted": now}
        publish(cfg, name, result)
        return result


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
    if record["state"] not in TERMINAL_STATES or cfg.home_dir(name).exists():
        raise OSError("Windows pod reclamation has no completed drain or retirement receipt")
    path(cfg, name).unlink()
