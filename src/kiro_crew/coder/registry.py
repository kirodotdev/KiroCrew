"""Integrity-sensitive ownership records for managed Coder workspaces.

One row per trusted parent session, minted before any workspace exists, so a
crash before the create leaves a countable record. Mutations run under one file
lock and land by atomic rename; unproven identity fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
from dataclasses import MISSING, asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from kiro_crew.atomic_write import atomic_write
from kiro_crew.constants import CODER_WORKSPACE_PREFIX_MAX_CHARS
from kiro_crew.platform_compat import file_lock

_SCHEMA_VERSION = 1
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CODER_WORKSPACE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_BINDING_BYTES = 5
_OWNER_SLUG_MAX_CHARS = 20
_WORKSPACE_NAME_MAX_CHARS = 32
_IDENTITY_ALLOCATION_ATTEMPTS = 128

#: A session row whose workspace the archived-session sweep is deleting.
DELETING = "deleting"


class WorkspaceRegistryCorrupt(RuntimeError):
    """The ownership registry cannot safely authorize lifecycle mutations."""


@dataclass(frozen=True)
class WorkspaceBinding:
    """One workspace's durable ownership record; unknown keys are dropped on read."""

    binding_id: str
    session_key: str
    workspace_name: str
    workspace_uuid: str
    owner_id: str
    organization_id: str
    deployment_id: str
    template: str
    preset: str
    generation: int
    state: str
    created_at: str
    last_activity_at: str
    deletion_due_at: str
    deleted_at: str
    failure_code: str

    @classmethod
    def from_dict(cls, value: object) -> WorkspaceBinding:
        if not isinstance(value, dict):
            raise WorkspaceRegistryCorrupt("Coder workspace binding is not an object")
        fields = cls.__dataclass_fields__
        present: dict[str, object] = {}
        for name, spec in fields.items():
            if name in value:
                present[name] = value[name]
            elif spec.default is MISSING:
                # A missing original-schema field is corruption, not age.
                raise WorkspaceRegistryCorrupt("Coder workspace binding has an invalid shape")
        try:
            binding = cls(**present)  # type: ignore[arg-type]
        except TypeError as exc:
            raise WorkspaceRegistryCorrupt("Coder workspace binding has an invalid shape") from exc
        if (
            not binding.binding_id
            or not binding.session_key
            or not _SAFE_NAME_RE.fullmatch(binding.workspace_name)
            or not isinstance(binding.generation, int)
            or binding.generation < 1
        ):
            raise WorkspaceRegistryCorrupt("Coder workspace binding has invalid identity fields")
        return binding


class WorkspaceBindingRegistry:
    """Atomic, lock-serialized binding registry; corrupt input fails closed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._cache_lock = threading.Lock()
        self._cache_signature: tuple[int, int, int] | None = None
        self._cache_bindings: dict[str, WorkspaceBinding] = {}
        self._cache_valid = False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _read_unlocked(self) -> dict[str, WorkspaceBinding]:
        if not self.path.exists():
            with self._cache_lock:
                self._cache_signature = None
                self._cache_bindings = {}
                self._cache_valid = True
            return {}
        stat_result = self.path.stat()
        signature = (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)
        with self._cache_lock:
            if self._cache_valid and signature == self._cache_signature:
                return dict(self._cache_bindings)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRegistryCorrupt("Coder workspace registry is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            raise WorkspaceRegistryCorrupt("Coder workspace registry version is invalid")
        values = raw.get("bindings")
        if not isinstance(values, dict):
            raise WorkspaceRegistryCorrupt("Coder workspace registry bindings are invalid")
        parsed: dict[str, WorkspaceBinding] = {}
        sessions: set[str] = set()
        for key, value in values.items():
            binding = WorkspaceBinding.from_dict(value)
            if key != binding.binding_id or binding.session_key in sessions:
                # A mismatched key or a shared session lets two callers own one workspace.
                raise WorkspaceRegistryCorrupt("Coder workspace registry identity is ambiguous")
            parsed[key] = binding
            sessions.add(binding.session_key)
        with self._cache_lock:
            self._cache_signature = signature
            self._cache_bindings = dict(parsed)
            self._cache_valid = True
        return parsed

    def _write_unlocked(self, bindings: dict[str, WorkspaceBinding]) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "bindings": {key: asdict(value) for key, value in sorted(bindings.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            fsync=True,
            restrict_to_owner=True,
        )
        stat_result = self.path.stat()
        with self._cache_lock:
            self._cache_signature = (
                stat_result.st_ino,
                stat_result.st_size,
                stat_result.st_mtime_ns,
            )
            self._cache_bindings = dict(bindings)
            self._cache_valid = True

    def _open_lock(self) -> BinaryIO:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.lock_path, "a+b")
        if os.fstat(fd.fileno()).st_size == 0:
            fd.write(b"0")
            fd.flush()
        return fd

    @staticmethod
    def _new_workspace_identity(
        bindings: dict[str, WorkspaceBinding],
        *,
        prefix: str,
        owner_name: str,
    ) -> tuple[str, str]:
        """Mint an unused (binding_id, workspace_name) pair, or fail closed. Names are
        visible deployment-wide; an owner that is not a legal slug becomes a digest."""
        owner_slug = owner_name.lower()
        if len(owner_slug) > _OWNER_SLUG_MAX_CHARS or not _CODER_WORKSPACE_NAME_RE.fullmatch(
            owner_slug
        ):
            digest = hashlib.sha256(owner_name.encode("utf-8")).hexdigest()[:8]
            owner_slug = f"user-{digest}"
        existing_names = {binding.workspace_name for binding in bindings.values()}
        for _attempt in range(_IDENTITY_ALLOCATION_ATTEMPTS):
            binding_id = (
                base64.b32encode(secrets.token_bytes(_BINDING_BYTES)).decode("ascii").lower()
            )
            owner_chars = _WORKSPACE_NAME_MAX_CHARS - len(prefix) - len(binding_id) - 2
            owner_fragment = owner_slug[:owner_chars].rstrip("-")
            workspace_name = f"{prefix}-{owner_fragment}-{binding_id}"
            if (
                binding_id not in bindings
                and workspace_name not in existing_names
                and _CODER_WORKSPACE_NAME_RE.fullmatch(workspace_name)
            ):
                return binding_id, workspace_name
        raise WorkspaceRegistryCorrupt("Could not allocate a unique Coder workspace identity")

    def allocate(
        self,
        session_key: str,
        *,
        template: str,
        preset: str,
        prefix: str,
        owner_name: str,
        deployment_id: str,
    ) -> WorkspaceBinding:
        """This session's binding, reserved on first call; check and write share the lock."""
        if not session_key:
            raise ValueError("session_key is required")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id is required")
        if not _SAFE_NAME_RE.fullmatch(template):
            raise ValueError("template must be one safe Coder name")
        if preset and not _SAFE_NAME_RE.fullmatch(preset):
            raise ValueError("preset must be one safe Coder name")
        if (
            not _CODER_WORKSPACE_NAME_RE.fullmatch(prefix)
            or len(prefix) > CODER_WORKSPACE_PREFIX_MAX_CHARS
        ):
            raise ValueError(
                "prefix must be one safe Coder name of at most "
                f"{CODER_WORKSPACE_PREFIX_MAX_CHARS} characters"
            )
        if not owner_name:
            raise ValueError("owner_name is required")
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                for binding in bindings.values():
                    if binding.session_key == session_key:
                        if binding.deployment_id != deployment_id:
                            raise WorkspaceRegistryCorrupt(
                                "Coder workspace deployment identity does not match"
                            )
                        return binding
                now = self._now()
                binding_id, workspace_name = self._new_workspace_identity(
                    bindings,
                    prefix=prefix,
                    owner_name=owner_name,
                )
                binding = WorkspaceBinding(
                    binding_id=binding_id,
                    session_key=session_key,
                    workspace_name=workspace_name,
                    workspace_uuid="",
                    owner_id="",
                    organization_id="",
                    deployment_id=deployment_id,
                    template=template,
                    preset=preset,
                    generation=1,
                    state="allocated",
                    created_at=now,
                    last_activity_at=now,
                    deletion_due_at="",
                    deleted_at="",
                    failure_code="",
                )
                bindings[binding_id] = binding
                self._write_unlocked(bindings)
                return binding

    def list_bindings(self) -> tuple[WorkspaceBinding, ...]:
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=False, required=True):
                return tuple(self._read_unlocked().values())

    def get(self, binding_id: str) -> WorkspaceBinding | None:
        return next((item for item in self.list_bindings() if item.binding_id == binding_id), None)

    def get_by_session(self, session_key: str) -> WorkspaceBinding | None:
        return next(
            (item for item in self.list_bindings() if item.session_key == session_key), None
        )

    def replace(self, binding: WorkspaceBinding) -> WorkspaceBinding:
        """Persist a lifecycle update; identity fields that authorize later actions are fixed."""
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = bindings.get(binding.binding_id)
                if current is None:
                    raise WorkspaceRegistryCorrupt("Coder workspace binding disappeared")
                if (
                    current.session_key != binding.session_key
                    or current.workspace_name != binding.workspace_name
                ):
                    raise WorkspaceRegistryCorrupt("Coder workspace binding identity changed")
                # Retargeting provenance would make the row an adopt-by-name.
                if current.deployment_id != binding.deployment_id:
                    raise WorkspaceRegistryCorrupt(
                        "Coder workspace deployment identity may not be retargeted"
                    )
                bindings[binding.binding_id] = binding
                self._write_unlocked(bindings)
                return binding

    # ── Archived-session retention: one critical section per transition ──

    def schedule_deletion(self, session_key: str, due_at: str) -> WorkspaceBinding | None:
        """Stamp ``session_key``'s row for deletion at ``due_at``; None if it has none."""
        if not session_key:
            return None
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = next((b for b in bindings.values() if b.session_key == session_key), None)
                if current is None or current.state == DELETING:
                    return None
                updated = replace(current, deletion_due_at=due_at)
                bindings[current.binding_id] = updated
                self._write_unlocked(bindings)
                return updated

    def cancel_deletion(self, binding_id: str) -> WorkspaceBinding | None:
        """Clear a pending deletion. None once the sweep has started deleting."""
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = bindings.get(binding_id)
                if current is None or current.state == DELETING:
                    return None
                if not current.deletion_due_at:
                    return current
                updated = replace(current, deletion_due_at="")
                bindings[binding_id] = updated
                self._write_unlocked(bindings)
                return updated

    def due_for_deletion(self, now: datetime) -> tuple[WorkspaceBinding, ...]:
        """Session rows whose retention has run out, plus any deletion left mid-way."""
        return tuple(
            binding
            for binding in self.list_bindings()
            if binding.state == DELETING or _is_due(binding.deletion_due_at, now)
        )

    def begin_deletion(self, binding_id: str, now: datetime) -> WorkspaceBinding | None:
        """Flip a due row to ``deleting``; None if a resume cancelled it first."""
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = bindings.get(binding_id)
                if current is None:
                    return None
                if current.state == DELETING:
                    return current
                if not _is_due(current.deletion_due_at, now):
                    return None
                updated = replace(current, state=DELETING)
                bindings[binding_id] = updated
                self._write_unlocked(bindings)
                return updated

    def remove_deleted(self, binding_id: str) -> bool:
        """Drop a row once its workspace is deleted. Only a ``deleting`` row goes."""
        with self._open_lock() as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                bindings = self._read_unlocked()
                current = bindings.get(binding_id)
                if current is None or current.state != DELETING:
                    return False
                del bindings[binding_id]
                self._write_unlocked(bindings)
                return True


def _is_due(stamp: str, now: datetime) -> bool:
    if not stamp:
        return False
    try:
        due = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        # An unreadable stamp never deletes anything.
        return False
    if due.tzinfo is None:
        return False
    return due <= now
