"""Durable channel-keyed shadow of Discord resume bindings.

All public methods offload filesystem work and serialize mutation with one async lock.
The state and durability contract lives in ``docs/system-specs/modules/messaging.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Agent-blocked ``trust`` keeps binding evidence separate from agent-reachable map rows.
#: The file is owner-restricted because titles contain conversation text.
_TRUST_SUBDIR = "trust"
_FILENAME = "discord_resume_expectations.json"


@dataclass(frozen=True)
class ResumeExpectation:
    """Expected session/title; ``version`` protects settlement from newer records."""

    key: str
    title: str
    version: int
    retired: bool = False


def _read(path: Path) -> dict[str, ResumeExpectation]:
    """Load the store; only an absent file means no expectations."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ExpectationStoreError(f"could not read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ExpectationStoreError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExpectationStoreError(f"{path} is not a JSON object")
    records: dict[str, ResumeExpectation] = {}
    for channel_id, value in raw.items():
        if not isinstance(value, dict) or not str(value.get("key") or ""):
            raise ExpectationStoreError(f"{path} holds a malformed row for {channel_id}")
        version = value.get("version")
        if type(version) is not int:
            raise ExpectationStoreError(f"{path} holds an unusable version for {channel_id}")
        retired = value.get("retired", False)
        if type(retired) is not bool:
            raise ExpectationStoreError(f"{path} holds an unusable retired flag for {channel_id}")
        records[str(channel_id)] = ResumeExpectation(
            str(value["key"]), str(value.get("title") or ""), version, retired)
    return records


def _resolve(previous: Path | None) -> tuple[Path, dict[str, ResumeExpectation] | None]:
    """Resolve the live store and reload only when its data-home path changes."""
    path = config_dir() / _TRUST_SUBDIR / _FILENAME
    return (path, None) if path == previous else (path, _read(path))


class ExpectationStoreError(RuntimeError):
    """Durability/read failure; unlike a CAS miss, callers must refuse routing."""


def _write(path: Path, records: dict[str, ResumeExpectation]) -> None:
    """Persist the whole store, or raise. Never swallow: see the error above."""
    payload = {channel_id: {"key": record.key, "title": record.title, "version": record.version,
                            "retired": record.retired} for channel_id, record in records.items()}
    try:
        # Owner-only dir, re-asserted per write; the FILE needs restrict_to_owner,
        # since a mode is a no-op on Windows and would leave a title readable.
        path.parent.mkdir(parents=True, exist_ok=True)
        platform_compat.chmod_safe(path.parent, 0o700)
        atomic_write(path, json.dumps(payload, indent=2), restrict_to_owner=True)
    except OSError as exc:
        raise ExpectationStoreError(f"could not persist {path}: {exc}") from exc


class ResumeExpectations:
    """Channel id → :class:`ResumeExpectation`, one small owner-only JSON file, written
    only on attach, rebind or detach. Single-writer: a pod resolves a different home."""

    def __init__(self) -> None:
        self._loaded_from: Path | None = None
        self._records: dict[str, ResumeExpectation] = {}
        self._lock = asyncio.Lock()

    async def get(self, channel_id: str) -> ResumeExpectation | None:
        async with self._lock:
            await self._synced()
            return self._records.get(channel_id)

    async def record(self, channel_id: str, key: str, title: str) -> ResumeExpectation:
        """Replace one record; establishment paths use this, settlement uses CAS."""
        async with self._lock:
            return await self._put(channel_id, key, title)

    async def record_if(self, channel_id: str, version: int, key: str, title: str) -> None:
        """Replace only the still-current version; a newer record wins."""
        async with self._lock:
            await self._synced()
            current = self._records.get(channel_id)
            if current is not None and current.version == version:
                await self._put(channel_id, key, title)

    async def retire_if(self, channel_id: str, version: int) -> bool:
        """Acknowledge a detach without deleting the evidence a racing bind needs."""
        async with self._lock:
            path = await self._synced()
            current = self._records.get(channel_id)
            if current is None or current.version != version:
                return False
            retired = ResumeExpectation(
                current.key, current.title, current.version + 1, retired=True)
            await self._publish(path, {**self._records, channel_id: retired})
            return True

    async def _put(self, channel_id: str, key: str, title: str) -> ResumeExpectation:
        """Write a successor record for *channel_id*. Caller holds the lock."""
        path = await self._synced()
        current = self._records.get(channel_id)
        version = (current.version + 1) if current is not None else 1
        record = ResumeExpectation(key, title, version)
        await self._publish(path, {**self._records, channel_id: record})
        return record

    async def _publish(self, path: Path, candidate: dict[str, ResumeExpectation]) -> None:
        """Persist *candidate*, then publish: mutating first makes a failed write report
        success and leaves memory no restart can recover."""
        await asyncio.to_thread(_write, path, candidate)
        self._records = candidate

    async def _synced(self) -> Path:
        """The store path, having reloaded if the data home moved under us."""
        path, records = await asyncio.to_thread(_resolve, self._loaded_from)
        if records is not None:
            self._records = records
            self._loaded_from = path
        return path
