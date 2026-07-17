"""Simplified AIDLC store — project CRUD, activity log, comments. File-based JSON."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Activity, Comment, _new_id, _now, serialize


class AidlcStore:
    """File-backed store for projects, activities, and comments."""

    COLLECTIONS = ("projects", "activities", "comments")

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir) / "aidlc"
        self._base.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, list] = {}
        for col in self.COLLECTIONS:
            self._data[col] = self._load(col)

    def _path(self, col: str) -> Path:
        return self._base / f"{col}.json"

    def _load(self, col: str) -> list:
        p = self._path(col)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save(self, col: str) -> None:
        self._path(col).write_text(json.dumps(self._data[col], indent=2, default=str))

    # ── Projects ──

    def create_project(self, name: str, description: str = "") -> dict:
        p = {
            "id": _new_id(),
            "name": name,
            "description": description,
            "status": "active",
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._data["projects"].append(p)
        self._save("projects")
        self._log_activity(str(p["id"]), "project", str(p["id"]), "system", "created")
        return p

    def get_project(self, pid: str) -> dict | None:
        return next((p for p in self._data["projects"] if p["id"] == pid), None)

    def list_projects(self) -> list[dict]:
        return sorted(self._data["projects"], key=lambda p: p.get("updated_at", 0), reverse=True)

    def update_project(self, pid: str, **fields) -> dict | None:
        p = self.get_project(pid)
        if not p:
            return None
        fields.pop("id", None)
        fields.pop("created_at", None)
        p.update(fields)
        p["updated_at"] = _now()
        self._save("projects")
        self._log_activity(
            pid, "project", pid, "system", "updated", json.dumps(list(fields.keys()))
        )
        return p

    def delete_project(self, pid: str) -> None:
        self._data["projects"] = [p for p in self._data["projects"] if p["id"] != pid]
        self._save("projects")

    # ── Activities ──

    def log_activity(
        self,
        project_id: str,
        target_type: str,
        target_id: str,
        actor_id: str = "system",
        action: str = "",
        value: str = "",
    ) -> dict:
        return self._log_activity(project_id, target_type, target_id, actor_id, action, value)

    def _log_activity(
        self,
        project_id: str,
        target_type: str,
        target_id: str,
        actor_id: str,
        action: str,
        value: str = "",
    ) -> dict:  # type: ignore[override]
        a: dict = serialize(
            Activity(  # type: ignore[assignment]
                project_id=project_id,
                target_type=target_type,
                target_id=target_id,
                actor_id=actor_id,
                action=action,
                value=value,
            )
        )
        self._data["activities"].append(a)
        self._save("activities")
        return a

    def list_activities(
        self,
        project_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        items = self._data["activities"]
        if project_id:
            items = [a for a in items if a.get("project_id") == project_id]
        if target_type:
            items = [a for a in items if a.get("target_type") == target_type]
        if target_id:
            items = [a for a in items if a.get("target_id") == target_id]
        return sorted(items, key=lambda a: a.get("timestamp", 0), reverse=True)[:limit]

    # ── Comments ──

    def add_comment(
        self, target_type: str, target_id: str, content: str, author_id: str = "system"
    ) -> dict:
        c: dict = serialize(
            Comment(  # type: ignore[assignment]
                target_type=target_type,
                target_id=target_id,
                content=content,
                author_id=author_id,
            )
        )
        self._data["comments"].append(c)
        self._save("comments")
        return c

    def list_comments(self, target_type: str, target_id: str) -> list[dict]:
        return sorted(
            [
                c
                for c in self._data["comments"]
                if c.get("target_type") == target_type and c.get("target_id") == target_id
            ],
            key=lambda c: c.get("timestamp", 0),
        )

    def delete_comment(self, comment_id: str) -> None:
        self._data["comments"] = [c for c in self._data["comments"] if c.get("id") != comment_id]
        self._save("comments")
