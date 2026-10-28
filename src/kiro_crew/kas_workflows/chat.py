"""KAS's own workflow surface in dashboard chats (``agent.kas_workflows``).

KAS offers a session its model-facing workflow surface -- ``run_workflow``,
``inspect_workflow``, ``update_workflow``, the ``/workflow-*`` commands, the
``wf-*`` step agents and its orchestrator steering -- only when that session is
started with ``_meta.kiro.settings.workflows.enabled: true`` on ``session/new``
or ``session/load``. Nothing process-wide turns it on.

Kiro Crew turns it on for one kind of session only: a DASHBOARD chat, and only
while the operator has opted in. Every other tenant of a KAS process (a
sub-agent, a cron turn, a task-runner step) keeps the surface off, because KAS's
steering tells the model to hand substantive work to workflows, and a headless
worker that did so would start runs nobody is watching.

A run such a chat's model starts is parented by the chat's own session, so the
router adopts it by that parent (:meth:`KasWorkflowRouter.note_chat_session`)
and it joins the same run list, approvals and reconnect path as a run the
dashboard launched.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

#: The one gateway setting that opts dashboard chats in (``agent.kas_workflows``),
#: read through the shared :func:`kiro_crew.kas_workflows.engine.kas_workflows_enabled`
#: gate so the backend-is-Kiro-Agent half is applied too.
CONFIG_PATH = "agent.kas_workflows"


def chat_workflows_enabled() -> bool:
    """Whether a dashboard chat starts with KAS's workflow surface on. Off on any doubt.

    The single gate: the backend is Kiro Agent AND ``agent.kas_workflows`` is on.
    """
    try:
        from kiro_crew.config import live
        from kiro_crew.kas_workflows.engine import kas_workflows_enabled

        cfg = live.snapshot()
        if cfg is None:
            from kiro_crew.config import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        return kas_workflows_enabled(cfg)
    except Exception:  # noqa: BLE001 -- an unreadable config leaves the surface off
        logger.debug("kas_workflows unreadable; treating it as off", exc_info=True)
        return False


#: The config path naming the creator step's effort (``agent.kas_creator_effort``).
CREATOR_EFFORT_PATH = "agent.kas_creator_effort"


def creator_effort_setting() -> str:
    """``agent.kas_creator_effort`` if it names a KAS level, else "" (KAS's default)."""
    try:
        from kiro_crew.config import live
        from kiro_crew.config.sections import KAS_CREATOR_EFFORT_LEVELS

        cfg = live.snapshot()
        if cfg is None:
            from kiro_crew.config import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        value = getattr(cfg.agent, "kas_creator_effort", "")
        return value if value in KAS_CREATOR_EFFORT_LEVELS else ""
    except Exception:  # noqa: BLE001 -- an unreadable config leaves KAS's default
        logger.debug("kas_creator_effort unreadable; leaving KAS's default", exc_info=True)
        return ""


async def creator_effort() -> str:
    """:func:`creator_effort_setting` off the event loop (the fallback reads disk)."""
    return await asyncio.to_thread(creator_effort_setting)


def is_plan_handoff(harness_tool_id: str) -> bool:
    """Whether a chat permission request is KAS's workflow plan handoff.

    The chat's model can hand a run the definition it saved
    (``update_workflow`` with a ``workflowRef``); KAS then asks the chat to
    approve the plan, stamping the request ``_meta.kiro.toolId:
    "update_workflow"`` (read into ``AcpEvent.harness_tool_id``). Approving a
    plan is the person deciding what the run does next, so no approval mode,
    Trust included, answers it for them.
    """
    from kiro_crew.kas_workflows import wire

    return harness_tool_id == wire.TOOL_UPDATE_WORKFLOW


def is_question(event: Any) -> bool:
    """Whether a chat permission request is a KAS ``user_input`` question.

    Kiro Crew does not advertise KAS's ``_kiro/userInput`` capability, so a
    question the chat's own model asks arrives through KAS's legacy bridge
    (kiro-agent ``acp-event-adapter.ts`` ``handleUserInputViaPermissionRequest``):
    one ``allow_once`` option per answer and nothing that refuses
    (``AcpEvent.answers_only``), with no ``_meta.kiro.toolId`` on the request.
    "Approving" it selects the first answer, so like the plan handoff it is the
    person's call under every approval mode. KAS only (``kas_builtin_ids``).
    """
    # ``is True``, not truthiness: both fields are real bools on AcpEvent /
    # LLMEvent, and a bare test double (MagicMock) must not read as a question.
    return getattr(event, "kas_builtin_ids", False) is True and (
        getattr(event, "answers_only", False) is True
    )


def needs_a_person(event: Any) -> bool:
    """Whether no approval mode, grant or hook may answer this chat request.

    A KAS plan handoff (:func:`is_plan_handoff`) or a KAS question
    (:func:`is_question`): each always waits on the person's card.
    """
    return is_plan_handoff(str(getattr(event, "harness_tool_id", "") or "")) or is_question(event)


# ── The chat's own question card ──
#
# A held question is keyed by the approval FUTURE the chat runner waits on, not
# by its request id: ACP request ids recur within a session, and the future is
# the one object both the runner and the approve route hold for exactly this
# card. Weak keys, so a turn that ends any way at all leaves nothing behind.

_held_questions: "weakref.WeakKeyDictionary[asyncio.Future[Any], Any]" = weakref.WeakKeyDictionary()
_answers: "weakref.WeakKeyDictionary[asyncio.Future[Any], str]" = weakref.WeakKeyDictionary()


def hold_question(future: "asyncio.Future[Any]", question: Any) -> None:
    """Mark *future*'s card as the chat's own question (an ``asks.PendingQuestion``)."""
    _held_questions[future] = question


def held_question(future: "asyncio.Future[Any] | None") -> Any:
    """The question *future*'s card asks while it is still open, else None."""
    if future is None or future.done():
        return None
    return _held_questions.get(future)


def record_answer(future: "asyncio.Future[Any]", option_id: str) -> None:
    """Remember the option a person picked on *future*'s card, before it resolves."""
    _answers[future] = option_id


def take_answer(future: "asyncio.Future[Any]") -> str | None:
    """The option a person picked on *future*'s card (once), or None when none was."""
    return _answers.pop(future, None)


def is_chat_session_key(session_key: str) -> bool:
    """Whether *session_key* names a dashboard chat (not a worker or a channel)."""
    from kiro_crew.validation import infer_use_case

    return bool(session_key) and infer_use_case(session_key) == "dashboard"


def _chat_id(session_key: str) -> str:
    """One spelling per chat: callers carry both ``chat-…`` and ``dashboard:chat-…``."""
    return session_key.removeprefix("dashboard:")


async def wants_workflows(session_key: str) -> bool:
    """Whether a KAS session owned by *session_key* starts with workflows on.

    Also records the answer for :func:`has_kas_workflows`, so Crew's own
    workflow tool knows which chats' models hold KAS's instead. Async because
    the config fallback reads disk: the caller is on the event loop, inside a
    session start.
    """
    if not is_chat_session_key(session_key):
        return False
    enabled = await asyncio.to_thread(chat_workflows_enabled)
    chat_id = _chat_id(session_key)
    if enabled:
        _ENABLED_CHATS.pop(chat_id, None)
        _ENABLED_CHATS[chat_id] = None
        while len(_ENABLED_CHATS) > _MAX_ENABLED_CHATS:
            _ENABLED_CHATS.pop(next(iter(_ENABLED_CHATS)))
    else:
        _ENABLED_CHATS.pop(chat_id, None)
    return enabled


def has_kas_workflows(session_key: str) -> bool:
    """Whether *session_key*'s current KAS session was started with workflows on.

    In memory, by design: a gateway restart starts every chat's session again,
    and that start records the answer anew.
    """
    return bool(session_key) and _chat_id(session_key) in _ENABLED_CHATS


#: Insertion-ordered set (dict keys) of chats whose session has the surface on.
_ENABLED_CHATS: dict[str, None] = {}
_MAX_ENABLED_CHATS = 1024


#: KAS's chat-pool workflow tools. A custom agent's ``tools`` allowlist filters
#: KAS's native tools too, so an agent that spells its list out (Crew's do) must
#: name these or the setting turns the surface on with nothing in it.
#: ``send_message`` is left out: Crew's own ``@kirocrew-core/send_message``
#: means a dashboard notification, and one name for two acts would mislead the
#: model more than parent-to-step messaging would help it. ``validate_workflow``
#: stays for KAS builds that still ship it (newer ones removed it); a listed
#: name KAS lacks enables nothing.
WORKFLOW_CHAT_TOOLS = (
    "run_workflow",
    "inspect_workflow",
    "update_workflow",
    "save_workflow_definition",
    "validate_workflow",
)


def with_workflow_tools(
    agents: list[dict[str, Any]] | None, active_agent: str
) -> list[dict[str, Any]] | None:
    """*agents* with KAS's workflow tools added to *active_agent*'s allowlist.

    Copies rather than mutates: the batch is also the record of what the session
    was sent, and other readers hold it. An ``"*"`` allowlist already admits
    them and is left as it is; every other agent in the batch is untouched.
    """
    if not agents:
        return agents
    out: list[dict[str, Any]] = []
    for definition in agents:
        tools = definition.get("tools") if isinstance(definition, dict) else None
        if definition.get("id") == active_agent and isinstance(tools, list):
            missing = [t for t in WORKFLOW_CHAT_TOOLS if t not in tools]
            definition = {**definition, "tools": [*tools, *missing]}
        out.append(definition)
    return out


def attach_workflow_settings(
    params: dict[str, Any], *, enabled: bool = True, creator_effort: str = ""
) -> None:
    """Merge ``_meta.kiro.settings.workflows.enabled`` into *params*.

    Merges at every level, so the custom-agent batch that shares ``_meta.kiro``
    (``attach_kas_custom_agents``) survives whichever of the two writes last.

    *enabled* is sent as an EXPLICIT boolean, even when ``False``: KAS resolves
    ``workflows?.enabled ?? persisted ?? false`` on ``session/load``, so a chat
    that started with the surface on and is loaded again after the operator turns
    ``agent.kas_workflows`` off would keep KAS's ``/workflow-*`` commands and
    workflow steering unless we say ``false`` out loud. The caller sends ``False``
    for a dashboard chat whose switch is off, so a reload drops the surface.

    *creator_effort* (``agent.kas_creator_effort``) rides along as
    ``workflows.creatorEffort`` when set and *enabled*. KAS saves, on each
    ``session/new`` or ``session/load`` that carries a ``workflows`` object, that
    object's ``creatorEffort`` -- and NONE when the field is absent (kiro-agent
    ``creatorEffortAfter``). This always sends the object, so the caller sends it
    on BOTH: a set level is kept, and ``""`` clears an earlier one back to KAS's
    default on the chat's next start or load.
    """
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        params["_meta"] = meta
    kiro = meta.get("kiro")
    if not isinstance(kiro, dict):
        kiro = {}
        meta["kiro"] = kiro
    settings = kiro.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        kiro["settings"] = settings
    workflows = settings.get("workflows")
    if not isinstance(workflows, dict):
        workflows = {}
        settings["workflows"] = workflows
    workflows["enabled"] = enabled
    if enabled and creator_effort:
        workflows["creatorEffort"] = creator_effort


# ── Editing an earlier message while a run is live (gap 3) ──
#
# A rewind (``dashboard/chat_rewind.py``) discards the chat's provider
# conversation, and that conversation can be the parent session of a workflow
# run the chat launched -- the process driving it. Like the Kiro IDE's
# checkpoint guard, an edit is refused while a run launched at or after the
# edited message is still live (a paused run counts), and allowed with a note
# once every such run has ended: the edit does not undo what the run did.

#: The 409 ``code`` a refused rewind answers with.
REWIND_REFUSED_CODE = "rewind_workflow_running"
REWIND_REFUSED_MESSAGE = (
    "A workflow started from this message or after it is still running. "
    "Stop the workflow before editing this message."
)
REWIND_UNREADABLE_MESSAGE = (
    "Kiro Crew could not check this chat's workflow runs, so the edit was not made. "
    "Try again in a moment."
)


class RunsUnreadable(Exception):
    """The router exists but its rows could not be read: no run can be ruled out."""


def _epoch(value: Any) -> float | None:
    """*value* (ISO-8601 text or epoch seconds) as epoch seconds; None when unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone

    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def runs_since(slot_key: str, message_ts: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(live, ended)``: *slot_key*'s runs launched at or after *message_ts*.

    Read from the gateway's router only if it already exists (a gateway that
    never touched Kiro Workflows has none and holds no run). A run whose start
    time or the message's cannot be read counts as launched after it, so an
    unknown is never a reason to let the edit through. Each entry is
    ``{"workflowId", "name"}``. Raises :class:`RunsUnreadable` when the router
    exists but its rows cannot be read.
    """
    from kiro_crew.kas_workflows import router as router_mod
    from kiro_crew.kas_workflows import wire

    router = getattr(router_mod, "_ROUTER", None)
    if router is None or not slot_key:
        return [], []
    since = _epoch(message_ts)
    live: list[dict[str, Any]] = []
    ended: list[dict[str, Any]] = []
    try:
        rows = router.rows()
    except Exception:  # noqa: BLE001 -- unreadable: the caller must not let the edit through
        logger.warning("KAS run rows unreadable for the rewind guard", exc_info=True)
        raise RunsUnreadable from None
    for row in rows:
        if row.get("slotKey") != _chat_id(slot_key) and row.get("slotKey") != slot_key:
            continue
        created = _epoch(row.get("createdAt"))
        if since is not None and created is not None and created < since:
            continue
        entry = {"workflowId": row.get("workflowId"), "name": row.get("name") or ""}
        if row.get("status") in wire.TERMINAL_RUN_STATUSES:
            ended.append(entry)
        else:
            live.append(entry)
    return live, ended


def rewind_refusal(slot_key: str, message_ts: Any) -> dict[str, Any] | None:
    """The 409 body refusing a history rewind from *message_ts*, or None to allow it.

    One answer for every route that discards *slot_key*'s conversation from a
    user message on (rewind, edit-resend, regenerate), so no door skips the
    guard. A router whose rows cannot be read refuses too: it cannot say no run
    is live, and the edit is the irreversible half.
    """
    try:
        live, _ended = runs_since(slot_key, message_ts)
    except RunsUnreadable:
        return {
            "error": REWIND_UNREADABLE_MESSAGE,
            "code": REWIND_REFUSED_CODE,
            "runs": [],
        }
    if not live:
        return None
    return {"error": REWIND_REFUSED_MESSAGE, "code": REWIND_REFUSED_CODE, "runs": live}


#: What Crew's ``workflow_run`` answers in a chat whose model has KAS's own tool.
AUTHOR_REDIRECT = (
    "This chat runs on Kiro Agent with its own workflow engine turned on, so new "
    "workflows run there: call run_workflow (Kiro Agent's workflow tool) instead. "
    "Saved Crew workflows still run here with workflow_run(workflow=...)."
)


__all__ = [
    "AUTHOR_REDIRECT",
    "CONFIG_PATH",
    "CREATOR_EFFORT_PATH",
    "REWIND_REFUSED_CODE",
    "REWIND_REFUSED_MESSAGE",
    "REWIND_UNREADABLE_MESSAGE",
    "RunsUnreadable",
    "WORKFLOW_CHAT_TOOLS",
    "attach_workflow_settings",
    "chat_workflows_enabled",
    "creator_effort",
    "creator_effort_setting",
    "has_kas_workflows",
    "is_chat_session_key",
    "runs_since",
    "wants_workflows",
    "with_workflow_tools",
]
