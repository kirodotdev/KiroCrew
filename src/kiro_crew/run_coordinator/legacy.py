"""Read-only import of pre-coordinator subagent run folders."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    open_dir_chain_nofollow,
    supports_pinned_tree_walk,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

from .models import (
    CoordinatorDecision,
    LegacyRunImport,
    ObservedState,
    RunCoordinator,
    RunOutcome,
)

logger = logging.getLogger(__name__)

_SOURCE_VERSION = "legacy-state-v1"
_MAX_AGENT_CHARS = 200
_MAX_CONVERSATION_KEY_CHARS = 500
_MAX_TASK_CHARS = 1000
_MAX_ERROR_CHARS = 2000
_MAX_LEGACY_JSON_BYTES = 1024 * 1024
_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class LegacyImportReport:
    imported: int = 0
    existing: int = 0
    corrupt: int = 0


def _redact(value: str) -> str:
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


def _number(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("legacy timestamp is out of range") from exc
        if not math.isfinite(number):
            raise ValueError("legacy timestamp must be finite")
        return number
    return default


class LegacyRunImporter:
    """Import known legacy fields while leaving source folders byte-identical."""

    def __init__(
        self,
        coordinator: RunCoordinator,
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._coordinator = coordinator
        self._root = root
        self._clock = clock

    def _base(self) -> Path:
        if self._root is not None:
            return self._root
        # The configured data home is operator-owned and may intentionally be
        # a symlink. Resolve that trusted prefix before the no-follow walk so
        # the untrusted ``subagents`` tree itself remains link-resistant.
        return data_home().resolve(strict=False) / "subagents"

    def _open_base(self) -> int | None:
        if not supports_pinned_tree_walk():
            return None
        try:
            return open_dir_chain_nofollow(self._base(), what="legacy subagent root")
        except (OSError, PinnedPathRefusal):
            return None

    @staticmethod
    def _is_directory(base_fd: int, name: str) -> bool:
        try:
            entry = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISDIR(entry.st_mode) and not getattr(entry, "st_reparse_tag", False)

    @classmethod
    def _folders(cls, base_fd: int) -> list[str]:
        folders: list[str] = []
        try:
            names = sorted(os.listdir(base_fd))
        except OSError:
            return folders
        for name in names:
            if cls._is_directory(base_fd, name):
                folders.append(name)
        return folders

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if (
            not run_id
            or run_id in (".", "..")
            or ".." in run_id
            or "/" in run_id
            or "\\" in run_id
            or "\0" in run_id
        ):
            raise ValueError("invalid legacy run id")
        return run_id

    @staticmethod
    def _valid_id(run_id: object, folder_name: str) -> str:
        if not isinstance(run_id, str) or run_id != folder_name:
            raise ValueError("state id does not match its folder")
        if _redact(run_id) != run_id:
            raise ValueError("legacy run id contains sensitive data")
        return run_id

    @staticmethod
    def _read_object(
        folder_fd: int, name: str, *, missing_ok: bool = False
    ) -> dict[str, object] | None:
        try:
            fd = os.open(name, _FILE_OPEN_FLAGS, dir_fd=folder_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ValueError("legacy record is missing") from None
        except OSError:
            raise ValueError("legacy record is not a safe regular file") from None
        try:
            opened = os.fstat(fd)
            if (
                opened.st_nlink > 1
                or not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_reparse_tag", False)
            ):
                raise ValueError("legacy record is not a safe regular file")
            opened_fd = fd
            fd = -1
            with os.fdopen(opened_fd, "rb") as stream:
                raw = stream.read(_MAX_LEGACY_JSON_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > _MAX_LEGACY_JSON_BYTES:
            raise FileTooLargeError("legacy record exceeds its safety cap")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("legacy record must be an object")
        return value

    @staticmethod
    def _has_result(folder_fd: int) -> bool:
        try:
            fd = os.open("result.txt", _FILE_OPEN_FLAGS, dir_fd=folder_fd)
        except FileNotFoundError:
            return False
        except OSError:
            raise ValueError("legacy result is not a safe regular file") from None
        try:
            opened = os.fstat(fd)
            if (
                opened.st_nlink > 1
                or not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_reparse_tag", False)
            ):
                raise ValueError("legacy result is not a safe regular file")
            return True
        finally:
            os.close(fd)

    def _request(self, base_fd: int, folder_name: str) -> LegacyRunImport:
        run_id = self._safe_run_id(folder_name)
        try:
            folder_fd = os.open(run_id, _DIR_OPEN_FLAGS, dir_fd=base_fd)
        except OSError:
            raise ValueError("legacy run folder is not a safe directory") from None
        try:
            state = self._read_object(folder_fd, "state.json")
            assert state is not None
            run_id = self._valid_id(state.get("id"), folder_name)
            tombstone = self._read_object(folder_fd, "tombstone.json", missing_ok=True)
            result_path = (
                str(self._base() / run_id / "result.txt") if self._has_result(folder_fd) else ""
            )
        finally:
            os.close(folder_fd)
        started = _number(state.get("started"), self._clock())
        updated = _number(state.get("updated_at"), started)
        task = _redact(str(state.get("task") or ""))[:_MAX_TASK_CHARS]
        agent = _redact(str(state.get("agent") or ""))[:_MAX_AGENT_CHARS]
        conversation = _redact(str(state.get("conversation_key") or ""))[
            :_MAX_CONVERSATION_KEY_CHARS
        ]
        if tombstone is None:
            return LegacyRunImport(
                run_id=run_id,
                parent_session="",
                agent=agent,
                task=task,
                conversation_key=conversation,
                observed_state=ObservedState.RUNNING,
                outcome=None,
                result_path=result_path,
                error="",
                created_at=started,
                updated_at=updated,
                terminal_at=None,
                source_version=_SOURCE_VERSION,
            )

        died = _number(tombstone.get("died"), updated)
        raw_outcome = str(tombstone.get("outcome") or "")
        try:
            outcome = RunOutcome(raw_outcome)
        except ValueError:
            outcome = (
                RunOutcome.COMPLETED
                if tombstone.get("cause") == "delivered"
                else RunOutcome.INTERRUPTED
            )
        error = _redact(
            str(tombstone.get("detail") or tombstone.get("cause") or "gateway restart")
        )[:_MAX_ERROR_CHARS]
        return LegacyRunImport(
            run_id=run_id,
            parent_session="",
            agent=agent,
            task=task,
            conversation_key=conversation,
            observed_state=ObservedState.TERMINAL,
            outcome=outcome,
            result_path=result_path,
            error=error,
            created_at=started,
            updated_at=died,
            terminal_at=died,
            source_version=_SOURCE_VERSION,
            # Legacy routing fields are agent-writable evidence, not authority.
            # Import terminal history without manufacturing a pending delivery
            # that could inject into an attacker-selected session.
            event_type="",
            destination="",
            payload_json="",
            delivery_state=None,
        )

    async def import_all(
        self,
        *,
        exclude_run_ids: frozenset[str] = frozenset(),
    ) -> LegacyImportReport:
        imported = 0
        existing = 0
        corrupt = 0
        base_fd = await asyncio.to_thread(self._open_base)
        if base_fd is None:
            return LegacyImportReport()
        try:
            folders = await asyncio.to_thread(self._folders, base_fd)
            requests: list[LegacyRunImport] = []
            for folder_name in folders:
                if folder_name in exclude_run_ids:
                    continue
                try:
                    request = await asyncio.to_thread(self._request, base_fd, folder_name)
                    requests.append(request)
                except (
                    FileTooLargeError,
                    OSError,
                    RecursionError,
                    TypeError,
                    UnicodeDecodeError,
                    ValueError,
                ):
                    corrupt += 1
                    logger.warning("legacy subagent import skipped for %s", _redact(folder_name))
            results = await self._coordinator.import_legacy_batch(tuple(requests))
            for request, result in zip(requests, results):
                if result.decision is CoordinatorDecision.APPLIED:
                    imported += 1
                elif result.decision is CoordinatorDecision.UNCHANGED:
                    existing += 1
                else:
                    corrupt += 1
                    logger.warning("legacy subagent import skipped for %s", _redact(request.run_id))
        finally:
            await asyncio.to_thread(os.close, base_fd)
        return LegacyImportReport(imported=imported, existing=existing, corrupt=corrupt)
