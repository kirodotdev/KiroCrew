"""Bounded multi-agent team coordination for Kiro BYO.

The team layer is deliberately small: it coordinates existing ``AgentTask``
objects instead of inventing a second execution engine. A team owns a shared
board, bounded membership, and a mailbox-style communication log. The actual
child execution still goes through :mod:`kiro_byo.agent_runtime`.

This keeps three safety properties explicit:

* parent/child depth is bounded;
* membership is capped per execution plan;
* messages are routed by stable task id and never imply shared mutable context.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


_TEAM_STATES = frozenset({"active", "completed", "cancelled", "failed"})
_BOARD_STATES = frozenset({"todo", "running", "done", "blocked", "cancelled"})


@dataclass
class TeamBoardItem:
    item_id: str
    title: str
    owner_task_id: str = ""
    status: str = "todo"
    depends_on: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    detail: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "owner_task_id": self.owner_task_id,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TeamMessage:
    message_id: str
    sender_task_id: str
    recipient_task_id: str
    text: str
    created_at: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender_task_id": self.sender_task_id,
            "recipient_task_id": self.recipient_task_id,
            "text": self.text,
            "created_at": self.created_at,
        }


@dataclass
class AgentTeam:
    team_id: str
    session_id: str
    plan_id: str
    leader_task_id: str
    max_agents: int
    max_parallel: int
    max_depth: int
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    member_task_ids: list[str] = field(default_factory=list)
    board: dict[str, TeamBoardItem] = field(default_factory=dict)
    messages: list[TeamMessage] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def public_dict(self, *, include_messages: bool = True) -> dict[str, Any]:
        with self.lock:
            payload = {
                "team_id": self.team_id,
                "session_id": self.session_id,
                "plan_id": self.plan_id,
                "leader_task_id": self.leader_task_id,
                "max_agents": self.max_agents,
                "max_parallel": self.max_parallel,
                "max_depth": self.max_depth,
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "members": list(self.member_task_ids),
                "board": [row.public_dict() for row in self.board.values()],
            }
            if include_messages:
                payload["messages"] = [row.public_dict() for row in self.messages[-100:]]
            return payload


class AgentTeamRegistry:
    """Thread-safe coordination state keyed by execution plan."""

    def __init__(self, *, max_teams: int = 64) -> None:
        self._lock = threading.RLock()
        self._teams: dict[str, AgentTeam] = {}
        self._plan_index: dict[str, str] = {}
        self._order: list[str] = []
        self._max_teams = max(4, int(max_teams))

    def create_for_plan(
        self,
        *,
        session_id: str,
        plan_id: str,
        leader_task_id: str,
        member_task_ids: list[str],
        max_agents: int,
        max_parallel: int,
        max_depth: int,
        objectives: dict[str, str] | None = None,
    ) -> AgentTeam:
        team = AgentTeam(
            team_id=uuid.uuid4().hex,
            session_id=str(session_id),
            plan_id=str(plan_id),
            leader_task_id=str(leader_task_id),
            max_agents=max(1, int(max_agents)),
            max_parallel=max(1, int(max_parallel)),
            max_depth=max(1, int(max_depth)),
            member_task_ids=list(dict.fromkeys(str(row) for row in member_task_ids if row)),
        )
        for task_id, objective in (objectives or {}).items():
            item_id = uuid.uuid4().hex
            team.board[item_id] = TeamBoardItem(
                item_id=item_id,
                title=str(objective or "Agent task"),
                owner_task_id=str(task_id),
            )
        with self._lock:
            self._teams[team.team_id] = team
            self._plan_index[team.plan_id] = team.team_id
            self._order.append(team.team_id)
            while len(self._order) > self._max_teams:
                old_id = self._order.pop(0)
                old = self._teams.pop(old_id, None)
                if old is not None and self._plan_index.get(old.plan_id) == old_id:
                    self._plan_index.pop(old.plan_id, None)
        return team

    def get(self, team_id: str) -> AgentTeam | None:
        with self._lock:
            return self._teams.get(str(team_id or ""))

    def for_plan(self, plan_id: str) -> AgentTeam | None:
        with self._lock:
            team_id = self._plan_index.get(str(plan_id or ""))
            return self._teams.get(team_id) if team_id else None

    def list(self, *, session_id: str = "") -> tuple[AgentTeam, ...]:
        with self._lock:
            values = tuple(self._teams.values())
        if session_id:
            values = tuple(row for row in values if row.session_id == session_id)
        return tuple(sorted(values, key=lambda row: (row.created_at, row.team_id)))

    def add_member(self, team_id: str, task_id: str) -> bool:
        team = self.get(team_id)
        if team is None:
            return False
        with team.lock:
            if task_id in team.member_task_ids:
                return True
            if len(team.member_task_ids) >= team.max_agents:
                return False
            team.member_task_ids.append(str(task_id))
            team.updated_at = time.time()
            return True

    def post_message(
        self, team_id: str, *, sender_task_id: str, recipient_task_id: str, text: str
    ) -> TeamMessage | None:
        team = self.get(team_id)
        body = str(text or "").strip()
        if team is None or not body:
            return None
        with team.lock:
            members = set(team.member_task_ids)
            if sender_task_id and sender_task_id not in members:
                return None
            if recipient_task_id not in members:
                return None
            msg = TeamMessage(
                message_id=uuid.uuid4().hex,
                sender_task_id=str(sender_task_id),
                recipient_task_id=str(recipient_task_id),
                text=body,
                created_at=time.time(),
            )
            team.messages.append(msg)
            if len(team.messages) > 500:
                del team.messages[:-400]
            team.updated_at = msg.created_at
            return msg

    def add_board_item(
        self, team_id: str, *, title: str, owner_task_id: str = "", depends_on: tuple[str, ...] = ()
    ) -> TeamBoardItem | None:
        team = self.get(team_id)
        if team is None or not str(title or "").strip():
            return None
        with team.lock:
            if owner_task_id and owner_task_id not in team.member_task_ids:
                return None
            item = TeamBoardItem(
                item_id=uuid.uuid4().hex,
                title=str(title).strip(),
                owner_task_id=str(owner_task_id),
                depends_on=tuple(str(row) for row in depends_on if row),
            )
            team.board[item.item_id] = item
            team.updated_at = item.updated_at
            return item

    def update_board_item(
        self, team_id: str, item_id: str, *, status: str = "", owner_task_id: str | None = None,
        detail: str | None = None,
    ) -> TeamBoardItem | None:
        team = self.get(team_id)
        if team is None:
            return None
        with team.lock:
            item = team.board.get(str(item_id or ""))
            if item is None:
                return None
            if status:
                normalized = str(status).strip().lower()
                if normalized not in _BOARD_STATES:
                    raise ValueError(f"invalid team board status: {status}")
                item.status = normalized
            if owner_task_id is not None:
                if owner_task_id and owner_task_id not in team.member_task_ids:
                    return None
                item.owner_task_id = str(owner_task_id)
            if detail is not None:
                item.detail = str(detail)
            item.updated_at = time.time()
            team.updated_at = item.updated_at
            return item

    def remove_member(self, team_id: str, task_id: str) -> bool:
        team = self.get(team_id)
        if team is None:
            return False
        with team.lock:
            if task_id == team.leader_task_id or task_id not in team.member_task_ids:
                return False
            team.member_task_ids.remove(task_id)
            for item in team.board.values():
                if item.owner_task_id == task_id and item.status not in {"done", "cancelled"}:
                    item.status = "cancelled"
                    item.updated_at = time.time()
            team.updated_at = time.time()
            return True

    def mark_member_status(self, team_id: str, task_id: str, status: str) -> None:
        team = self.get(team_id)
        if team is None:
            return
        mapping = {"planned": "todo", "running": "running", "completed": "done", "failed": "blocked", "cancelled": "cancelled", "skipped": "cancelled", "paused": "blocked"}
        board_status = mapping.get(status)
        if board_status is None:
            return
        with team.lock:
            for item in team.board.values():
                if item.owner_task_id == task_id:
                    item.status = board_status
                    item.updated_at = time.time()
            team.updated_at = time.time()

    def set_status(self, team_id: str, status: str) -> AgentTeam | None:
        normalized = str(status or "").strip().lower()
        if normalized not in _TEAM_STATES:
            raise ValueError(f"invalid team status: {status}")
        team = self.get(team_id)
        if team is None:
            return None
        with team.lock:
            team.status = normalized
            team.updated_at = time.time()
            return team
