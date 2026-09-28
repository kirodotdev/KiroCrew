"""Durable receipts for ACP adapter cleanup that has not been confirmed."""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import ACP_CLEANUP_RECEIPTS_DIR_NAME, config_dir

logger = logging.getLogger(__name__)

_KIND_MCP_CLEAR = "mcp_clear"
_KIND_SLOT_DELETE = "slot_delete"
_ALLOWED_KINDS = frozenset({_KIND_MCP_CLEAR, _KIND_SLOT_DELETE})
_MAX_RECEIPT_BYTES = 4096
_MAX_SESSION_ID_CHARS = 256
_MAX_GATEWAY_ORIGIN_CHARS = 512
_MAX_OWNER_CHARS = 128
_MAX_OWNER_START_CHARS = 256
_MAX_FINGERPRINT_CHARS = 64


@dataclass(frozen=True)
class CleanupReceipt:
    """One idempotent adapter cleanup operation awaiting confirmation."""

    receipt_id: str
    kind: str
    gateway_origin: str
    session_id: str
    owner: str = ""
    owner_pid: int = 0
    owner_started: str = ""
    project_fingerprint: str = ""
    mutation_id: str = ""


class CleanupReceiptStore:
    """Persist cleanup intent before remote state can become unreachable."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root or config_dir() / ACP_CLEANUP_RECEIPTS_DIR_NAME

    def add_mcp_clear(self, gateway_origin: str, session_id: str, owner: str) -> CleanupReceipt:
        owner_pid = os.getpid()
        return self._add(
            kind=_KIND_MCP_CLEAR,
            gateway_origin=gateway_origin,
            session_id=session_id,
            owner=owner,
            owner_pid=owner_pid,
            owner_started=platform_compat.get_process_start_id(owner_pid) or "",
            mutation_id=uuid.uuid4().hex,
        )

    def add_slot_delete(
        self,
        gateway_origin: str,
        session_id: str,
        *,
        project_fingerprint: str = "",
    ) -> CleanupReceipt:
        return self._add(
            kind=_KIND_SLOT_DELETE,
            gateway_origin=gateway_origin,
            session_id=session_id,
            project_fingerprint=project_fingerprint,
        )

    def pending(self) -> list[CleanupReceipt]:
        if not self.root.is_dir():
            return []
        if platform_compat.is_link_or_junction(self.root):
            logger.warning("ignoring linked ACP cleanup receipt directory %s", self.root)
            return []
        receipts: list[CleanupReceipt] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_size > _MAX_RECEIPT_BYTES
                    ):
                        raise ValueError("unsafe cleanup receipt inode or size")
                    raw = os.read(fd, _MAX_RECEIPT_BYTES + 1)
                finally:
                    os.close(fd)
                if len(raw) > _MAX_RECEIPT_BYTES:
                    raise ValueError("cleanup receipt exceeds size limit")
                payload = json.loads(raw.decode("utf-8"))
                receipt = _receipt_from_payload(payload)
                if path.name != f"{receipt.receipt_id}.json":
                    raise ValueError("cleanup receipt identity mismatch")
            except (OSError, UnicodeError, ValueError, TypeError):
                logger.warning("ignoring unreadable ACP cleanup receipt %s", path)
                continue
            receipts.append(receipt)
        return receipts

    def mcp_owner_is_live(self, receipt: CleanupReceipt) -> bool:
        """Fail closed while the adapter process named by an MCP receipt may be live."""
        if receipt.kind != _KIND_MCP_CLEAR or receipt.owner_pid <= 0:
            return False
        if not receipt.owner_started:
            return platform_compat.pid_exists(receipt.owner_pid)
        current = platform_compat.get_process_start_id(receipt.owner_pid)
        if current is None:
            return platform_compat.pid_exists(receipt.owner_pid)
        return current == receipt.owner_started and platform_compat.pid_exists(receipt.owner_pid)

    def discard(self, receipt: CleanupReceipt) -> None:
        path = self.root / f"{receipt.receipt_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to retire ACP cleanup receipt %s", path, exc_info=True)
            return
        fsync_dir(self.root)

    def _add(
        self,
        *,
        kind: str,
        gateway_origin: str,
        session_id: str,
        owner: str = "",
        owner_pid: int = 0,
        owner_started: str = "",
        project_fingerprint: str = "",
        mutation_id: str = "",
    ) -> CleanupReceipt:
        receipt = CleanupReceipt(
            receipt_id=uuid.uuid4().hex,
            kind=kind,
            gateway_origin=gateway_origin,
            session_id=session_id,
            owner=owner,
            owner_pid=owner_pid,
            owner_started=owner_started,
            project_fingerprint=project_fingerprint,
            mutation_id=mutation_id,
        )
        _validate_receipt(receipt)
        if platform_compat.is_link_or_junction(self.root):
            raise OSError(f"refusing linked ACP cleanup receipt directory: {self.root}")
        platform_compat.make_owner_only_dir(self.root)
        platform_compat.restrict_dir_to_owner(self.root)
        path = self.root / f"{receipt.receipt_id}.json"
        atomic_write(
            path,
            json.dumps(_receipt_payload(receipt), sort_keys=True) + "\n",
            fsync=True,
            restrict_to_owner=True,
        )
        fsync_dir(self.root)
        return receipt


def _receipt_payload(receipt: CleanupReceipt) -> dict[str, str | int]:
    return {
        "id": receipt.receipt_id,
        "kind": receipt.kind,
        "gateway_origin": receipt.gateway_origin,
        "session_id": receipt.session_id,
        "owner": receipt.owner,
        "owner_pid": receipt.owner_pid,
        "owner_started": receipt.owner_started,
        "project_fingerprint": receipt.project_fingerprint,
        "mutation_id": receipt.mutation_id,
    }


def _receipt_from_payload(payload: Any) -> CleanupReceipt:
    if not isinstance(payload, dict):
        raise ValueError("cleanup receipt must be an object")
    receipt = CleanupReceipt(
        receipt_id=payload.get("id", ""),
        kind=payload.get("kind", ""),
        gateway_origin=payload.get("gateway_origin", ""),
        session_id=payload.get("session_id", ""),
        owner=payload.get("owner", ""),
        owner_pid=payload.get("owner_pid", 0),
        owner_started=payload.get("owner_started", ""),
        project_fingerprint=payload.get("project_fingerprint", ""),
        mutation_id=payload.get("mutation_id", ""),
    )
    _validate_receipt(receipt)
    return receipt


def _validate_receipt(receipt: CleanupReceipt) -> None:
    fields = (
        receipt.receipt_id,
        receipt.kind,
        receipt.gateway_origin,
        receipt.session_id,
        receipt.owner,
        receipt.owner_started,
        receipt.project_fingerprint,
        receipt.mutation_id,
    )
    if not all(isinstance(field, str) for field in fields):
        raise ValueError("cleanup receipt fields must be strings")
    if (
        not isinstance(receipt.owner_pid, int)
        or isinstance(receipt.owner_pid, bool)
        or receipt.owner_pid < 0
    ):
        raise ValueError("invalid cleanup receipt owner pid")
    if receipt.kind not in _ALLOWED_KINDS:
        raise ValueError("unsupported cleanup receipt kind")
    if not receipt.receipt_id or len(receipt.receipt_id) > 64:
        raise ValueError("invalid cleanup receipt id")
    if not receipt.gateway_origin or len(receipt.gateway_origin) > _MAX_GATEWAY_ORIGIN_CHARS:
        raise ValueError("invalid cleanup receipt gateway")
    if not receipt.session_id or len(receipt.session_id) > _MAX_SESSION_ID_CHARS:
        raise ValueError("invalid cleanup receipt session")
    if len(receipt.owner) > _MAX_OWNER_CHARS:
        raise ValueError("invalid cleanup receipt owner")
    if len(receipt.owner_started) > _MAX_OWNER_START_CHARS:
        raise ValueError("invalid cleanup receipt owner start identity")
    if receipt.project_fingerprint and (
        len(receipt.project_fingerprint) != _MAX_FINGERPRINT_CHARS
        or any(ch not in "0123456789abcdef" for ch in receipt.project_fingerprint)
    ):
        raise ValueError("invalid cleanup receipt project fingerprint")
    if len(receipt.mutation_id) > 64:
        raise ValueError("invalid cleanup receipt mutation id")
    if receipt.kind == _KIND_MCP_CLEAR and (not receipt.owner or not receipt.mutation_id):
        raise ValueError("MCP cleanup receipt requires owner and mutation id")
    if receipt.kind == _KIND_SLOT_DELETE and (
        receipt.owner or receipt.owner_pid or receipt.owner_started or receipt.mutation_id
    ):
        raise ValueError("slot cleanup receipt has unexpected owner state")


__all__ = ["CleanupReceipt", "CleanupReceiptStore"]
