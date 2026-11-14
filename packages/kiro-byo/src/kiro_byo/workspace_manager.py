"""Isolated writer workspaces for multi-agent execution.

Git worktrees are preferred because they preserve repository identity without
allowing concurrent writers to mutate the parent working tree. The manager is
fail-closed: a repository that cannot create a detached worktree does not fall
back to writing the shared workspace.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .paths import app_home
from .state_io import _atomic_json_write, _file_transaction_lock


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceLease:
    workspace_id: str
    task_id: str
    repo_root: str
    path: str
    kind: str
    base_ref: str
    created_at: float
    state: str = "ready"

    def public_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "repo_root": self.repo_root,
            "path": self.path,
            "kind": self.kind,
            "base_ref": self.base_ref,
            "created_at": self.created_at,
            "state": self.state,
        }


class WorkspaceManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or (app_home() / "data" / "agent_workspaces")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError("workspace manifest is unreadable; refusing to overwrite") from exc
        if not isinstance(raw, dict):
            raise WorkspaceError("workspace manifest has invalid shape; refusing to overwrite")
        return raw

    def _save(self, rows: dict[str, dict[str, Any]]) -> None:
        _atomic_json_write(self.manifest_path, rows)

    @staticmethod
    def _git(repo: Path, args: Sequence[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args], text=True, capture_output=True,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git command failed: {type(exc).__name__}: {exc}") from exc

    @classmethod
    def git_root(cls, cwd: str | os.PathLike[str]) -> Path:
        path = Path(cwd).expanduser().resolve()
        result = cls._git(path, ["rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            raise WorkspaceError((result.stderr or "not a git repository").strip())
        return Path(result.stdout.strip()).resolve()

    def provision(self, *, cwd: str, task_id: str, base_ref: str = "HEAD") -> WorkspaceLease:
        repo = self.git_root(cwd)
        workspace_id = uuid.uuid4().hex
        short = "".join(ch for ch in str(task_id) if ch.isalnum())[:12] or workspace_id[:12]
        target = (self.root / f"writer-{short}-{workspace_id[:8]}").resolve()
        if self.root not in target.parents:
            raise WorkspaceError("workspace target escaped runtime root")
        result = self._git(repo, ["worktree", "add", "--detach", str(target), str(base_ref)], timeout=60.0)
        if result.returncode != 0:
            raise WorkspaceError((result.stderr or result.stdout or "git worktree add failed").strip())
        lease = WorkspaceLease(
            workspace_id=workspace_id,
            task_id=str(task_id),
            repo_root=str(repo),
            path=str(target),
            kind="git_worktree",
            base_ref=str(base_ref),
            created_at=time.time(),
        )
        with self._lock, _file_transaction_lock(self.manifest_path):
            rows = self._load()
            rows[workspace_id] = lease.public_dict()
            self._save(rows)
        return lease

    def get(self, workspace_id: str) -> WorkspaceLease | None:
        with self._lock:
            row = self._load().get(str(workspace_id or ""))
        if not isinstance(row, dict):
            return None
        try:
            return WorkspaceLease(
                workspace_id=str(row["workspace_id"]), task_id=str(row.get("task_id") or ""),
                repo_root=str(row["repo_root"]), path=str(row["path"]), kind=str(row.get("kind") or "git_worktree"),
                base_ref=str(row.get("base_ref") or "HEAD"), created_at=float(row.get("created_at") or 0.0),
                state=str(row.get("state") or "ready"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def list(self) -> tuple[WorkspaceLease, ...]:
        with self._lock:
            ids = tuple(self._load())
        values = [self.get(row) for row in ids]
        return tuple(sorted((row for row in values if row is not None), key=lambda row: row.created_at))

    def status(self, workspace_id: str) -> str:
        lease = self.get(workspace_id)
        if lease is None:
            raise WorkspaceError("unknown workspace")
        result = self._git(Path(lease.path), ["status", "--short"])
        if result.returncode != 0:
            raise WorkspaceError((result.stderr or "git status failed").strip())
        return result.stdout

    def diff(self, workspace_id: str, *, binary: bool = False) -> str:
        lease = self.get(workspace_id)
        if lease is None:
            raise WorkspaceError("unknown workspace")
        args = ["diff", "--no-ext-diff"]
        if binary:
            args.append("--binary")
        result = self._git(Path(lease.path), args, timeout=60.0)
        if result.returncode not in {0, 1}:
            raise WorkspaceError((result.stderr or "git diff failed").strip())
        return result.stdout

    def run_check(self, workspace_id: str, argv: Sequence[str], *, timeout: float = 300.0) -> dict[str, Any]:
        lease = self.get(workspace_id)
        if lease is None:
            raise WorkspaceError("unknown workspace")
        command = [str(row) for row in argv if str(row)]
        if not command:
            raise WorkspaceError("check command is empty")
        try:
            proc = subprocess.run(
                command, cwd=lease.path, text=True, capture_output=True,
                timeout=max(1.0, min(float(timeout), 1800.0)), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"workspace check failed: {type(exc).__name__}: {exc}") from exc
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-20000:],
            "stderr": proc.stderr[-20000:],
        }

    def compare(self, workspace_ids: Sequence[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for workspace_id in workspace_ids:
            lease = self.get(str(workspace_id))
            if lease is None:
                rows.append({"workspace_id": str(workspace_id), "error": "unknown workspace"})
                continue
            try:
                diff = self.diff(lease.workspace_id)
                status = self.status(lease.workspace_id)
                changed_files = [line for line in status.splitlines() if line.strip()]
                rows.append({
                    "workspace_id": lease.workspace_id,
                    "task_id": lease.task_id,
                    "path": lease.path,
                    "changed_files": len(changed_files),
                    "status": status,
                    "diff_chars": len(diff),
                    "diff": diff,
                })
            except WorkspaceError as exc:
                rows.append({"workspace_id": lease.workspace_id, "error": str(exc)})
        return rows

    def apply_candidate(self, workspace_id: str) -> dict[str, Any]:
        """Apply one selected candidate patch to its clean parent repository.

        This is intentionally conservative: the parent must be completely clean
        and ``git apply --check`` must succeed. There is no automatic conflict
        resolution and no implicit commit.
        """
        lease = self.get(workspace_id)
        if lease is None:
            raise WorkspaceError("unknown workspace")
        repo = Path(lease.repo_root)
        parent_status = self._git(repo, ["status", "--porcelain"])
        if parent_status.returncode != 0:
            raise WorkspaceError((parent_status.stderr or "git status failed").strip())
        if parent_status.stdout.strip():
            raise WorkspaceError("parent repository is not clean; refusing candidate apply")
        patch = self.diff(workspace_id, binary=True)
        if not patch.strip():
            return {"applied": False, "reason": "candidate has no diff", "workspace_id": workspace_id}
        try:
            check = subprocess.run(
                ["git", "-C", str(repo), "apply", "--check", "--whitespace=nowarn", "-"],
                input=patch, text=True, capture_output=True, timeout=60.0, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"candidate apply check failed: {type(exc).__name__}: {exc}") from exc
        if check.returncode != 0:
            raise WorkspaceError((check.stderr or "candidate patch does not apply cleanly").strip())
        applied = subprocess.run(
            ["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-"],
            input=patch, text=True, capture_output=True, timeout=60.0, check=False,
        )
        if applied.returncode != 0:
            raise WorkspaceError((applied.stderr or "candidate patch apply failed").strip())
        result_status = self._git(repo, ["status", "--short"])
        return {
            "applied": True,
            "workspace_id": workspace_id,
            "repo_root": str(repo),
            "status": result_status.stdout,
        }

    def remove(self, workspace_id: str, *, force: bool = False) -> bool:
        lease = self.get(workspace_id)
        if lease is None:
            return False
        repo = Path(lease.repo_root)
        target = Path(lease.path)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(target))
        result = self._git(repo, args, timeout=60.0)
        if result.returncode != 0:
            if not force:
                raise WorkspaceError((result.stderr or "git worktree remove failed").strip())
            shutil.rmtree(target, ignore_errors=True)
            self._git(repo, ["worktree", "prune"], timeout=30.0)
        with self._lock, _file_transaction_lock(self.manifest_path):
            rows = self._load()
            rows.pop(workspace_id, None)
            self._save(rows)
        return True
