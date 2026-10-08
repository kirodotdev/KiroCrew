"""Tool approvals a floored run hands back to the remote hub that placed it.

A run carries ``approval_floor="interactive"`` only when a remote hub placed it
and the hub's own posture for that child is not auto. The person who may answer
is in the hub's session, not on this crew's dashboard, so the permission ladder
parks each request here instead of prompting locally. The hub reads the pending
requests from the run's status (``GET /api/spawn/{id}``), asks its person, and
answers through ``POST /api/spawn/{id}/approvals/{approval_id}``.

Nothing here is persisted: a pending request lives only as long as the turn
that raised it, and an unanswered one is a rejection when its wait runs out.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: A person on the hub answers; give them the interactive window, not the
#: deny-fast background one (``DashboardState._APPROVAL_TIMEOUT``).
HUB_APPROVAL_TIMEOUT_SECS = 7200.0
_MAX_TITLE_CHARS = 500
_MAX_INPUT_CHARS = 4000
_MAX_PURPOSE_CHARS = 500


def _clean(text: object, limit: int) -> str:
    # Bounded only: the status route that lists these scrubs them on the way
    # out, and the hub scrubs them again on receipt.
    return str(text or "")[:limit]


@dataclass
class _Pending:
    approval_id: str
    title: str
    tool_input: str
    tool_purpose: str
    future: asyncio.Future[bool]


# run id -> approval id -> pending request. Run ids are unique per gateway, and
# an entry is removed as soon as its wait ends, so the map holds only live waits.
_PENDING: dict[str, dict[str, _Pending]] = {}


async def ask_hub(run_id: str, event: Any, *, timeout: float | None = None) -> bool:
    """Park *event* for the hub's person; True only on their explicit approval."""
    loop = asyncio.get_running_loop()
    approval_id = secrets.token_hex(8)
    pending = _Pending(
        approval_id=approval_id,
        title=_clean(getattr(event, "title", ""), _MAX_TITLE_CHARS),
        tool_input=_clean(getattr(event, "tool_input", ""), _MAX_INPUT_CHARS),
        tool_purpose=_clean(getattr(event, "tool_purpose", ""), _MAX_PURPOSE_CHARS),
        future=loop.create_future(),
    )
    _PENDING.setdefault(run_id, {})[approval_id] = pending
    try:
        return await asyncio.wait_for(
            asyncio.shield(pending.future),
            timeout=HUB_APPROVAL_TIMEOUT_SECS if timeout is None else timeout,
        )
    except asyncio.TimeoutError:
        logger.info("Hub approval %s for run %s went unanswered; rejected", approval_id, run_id)
        return False
    finally:
        # Once off the map the future is unreachable: ``resolve`` cannot find it
        # and nothing awaits it again, so it is dropped rather than settled.
        waits = _PENDING.get(run_id)
        if waits is not None:
            waits.pop(approval_id, None)
            if not waits:
                _PENDING.pop(run_id, None)


def floor_enforceable() -> bool:
    """Whether this gateway's sub-agent harness can carry an approval floor.

    The floor rests on a kiro-cli agent-spec mechanism: a floored run launches
    as the derived ``<agent>--readonly`` spec, whose emptied grants make every
    tool call raise a permission request the ladder hands to the hub. Another
    harness pre-approves through a surface that spec cannot reach
    (claude-agent-acp ``permissions.allow`` / ``bypassPermissions``), so a call
    it pre-approves would run with no person and no SEL row. This is the same
    positive capability the side chat's read-only tools rest on
    (``ACP_BACKENDS_SIDE_READONLY``, harness-parity H6), never a negation, and an
    unreadable config reads as not enforceable.
    """
    from kiro_crew.acp_backends import ACP_BACKENDS_SIDE_READONLY
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        backend = KiroCrewConfig.load().agent.acp_backend
    except Exception:
        logger.warning("agent.acp_backend is unreadable; the approval floor is not enforceable")
        return False
    return backend in ACP_BACKENDS_SIDE_READONLY


def floored_session(subagents: Any, session_key: str) -> bool:
    """Whether *session_key* is the session of a local run that carries the floor.

    A floored run keeps the kirocrew MCP server, so it can ask this gateway to
    start or continue another run. That run would take its approval posture
    from this gateway, where yolo or ``auto_approve_subagent_tools`` approve
    every tool, and its requests would never reach the hub. The spawn routes
    refuse such a caller instead. A run's session is its continuable
    conversation key or ``subagent:<id>``, the same key ``run.py`` opens.
    """
    if not session_key.startswith("subagent:") or subagents is None:
        return False
    try:
        runs = list(subagents.all_agents)
    except Exception:
        logger.warning("run listing is unreadable; treating %s as floored", session_key)
        return True
    return any(
        getattr(info, "approval_floor", "") == "interactive"
        and (getattr(info, "conversation_key", "") or f"subagent:{info.id}") == session_key
        for info in runs
    )


def pending_for(run_id: str) -> list[dict[str, str]]:
    """The run's unanswered requests, oldest first, as the status route shows them."""
    return [
        {
            "id": p.approval_id,
            "title": p.title,
            "tool_input": p.tool_input,
            "tool_purpose": p.tool_purpose,
        }
        for p in list(_PENDING.get(run_id, {}).values())
        if not p.future.done()
    ]


def resolve(run_id: str, approval_id: str, approved: bool) -> bool:
    """Answer one pending request of *run_id*; False when it is not pending."""
    pending = _PENDING.get(run_id, {}).get(approval_id)
    if pending is None or pending.future.done():
        return False
    pending.future.set_result(bool(approved))
    return True
