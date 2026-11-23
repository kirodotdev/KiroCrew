"""The one notice a chat running a Bot gives when the Bot's own chat runs another harness.

A Bot's harness pin (``agents.<name>.acp_backend``) is the harness of that Bot's
own chat and its workers (crew-mode.md, "A Bot's own harness"). A dashboard chat
that runs the Bot through the agent picker, a folder default or a new chat on it
keeps its own harness. Turn admission hands each turn that reuses a ``member``
record to :func:`surface_harness_notice`, which tells the user once per chat and
per pair of harnesses with a ``notice`` row naming the Bot, the harness its own
chat runs on and the one this chat stays on (``meta.kind``
:data:`MEMBER_HARNESS_NOTICE_KIND`, localized by the dashboard from its
``gateway_notice`` token).

Both harnesses are read as the session factory and the Bot's own chat select
them; nothing here changes either. "Once" is a compare-and-set of
:data:`HARNESS_NOTICE_FIELD` on the session's metadata line, so a Reload, a
resume or a restart does not repeat it, and a later change of either harness is
told again. A Bot pin this gateway cannot start gives no notice: the Bot's own
chat refuses it with its own message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: ``meta.kind`` of the notice row.
MEMBER_HARNESS_NOTICE_KIND = "member_harness"
#: The metadata-line field naming the pair of harnesses this chat was last told about.
HARNESS_NOTICE_FIELD = "member_harness_notice"


def harnesses_apart(config: Any, session_key: str, execution: Any) -> tuple[str, str] | None:
    """``(bot, chat)`` runtime ids when *session_key* runs its Bot on another harness.

    ``None`` unless *execution* is a ``member`` record for a Bot *config* knows,
    *session_key* is neither that Bot's own thread nor a worker, and the harness
    the Bot's own chat runs on differs from the one this chat's session selects.
    Never raises. Blocking: the Bot's landing rules may read its worker spec.
    """
    from kiro_crew import crewmate_harness, members
    from kiro_crew.acp_backends import resolve_selected_backend

    member_id = str(getattr(execution, "member_id", "") or "")
    if getattr(execution, "selection_kind", "") != "member" or not member_id:
        return None
    if members.is_member_session_key(session_key) or members.is_crewmate_worker_session(
        session_key
    ):
        return None
    try:
        crew = crewmate_harness.crew_for_member_id(config, member_id)
        if not crew:
            return None
        pick = members.session_backend_pick(session_key)
        chat = members.select_provider_backend(
            session_key,
            config.agent.member_acp_backend,
            config.agent.acp_backend,
            backend_override=pick,
            crew_backend=(
                crewmate_harness.crewmate_backend_pin(config, session_key) if pick is None else None
            ),
        )
        bot = crewmate_harness.pinned_backend(
            config, crew, resolve_selected_backend(config.agent.member_acp_backend)
        )
    except Exception:  # noqa: BLE001 - an unstartable pin or a bad read tells nothing
        return None
    if not isinstance(bot, str) or not isinstance(chat, str) or bot == chat:
        return None
    return bot, chat


def claim_harness_notice(conversation_log: Any, session_key: str, bot: str, chat: str) -> bool:
    """Record that *session_key* is told about the pair *bot*, *chat*. True only for the first claim.

    A session with no transcript, or a line that cannot be written, claims
    nothing, so it is told nothing. Blocking file I/O.
    """
    from kiro_crew.acp_backends import backend_to_stored

    pair = {"bot": backend_to_stored(bot), "chat": backend_to_stored(chat)}
    return bool(
        conversation_log.update_metadata_if(
            session_key,
            {HARNESS_NOTICE_FIELD: pair},
            lambda metadata: metadata.get(HARNESS_NOTICE_FIELD) != pair,
            require_existing=True,
            require_write=True,
        )
    )


def harness_name(runtime: str) -> str:
    """The readable name of the harness *runtime* (a runtime id) for the notice."""
    from kiro_crew.acp_backends import backend_to_stored
    from kiro_crew.crewmate_harness import harness_label

    return harness_label(backend_to_stored(runtime))


def harness_notice_text(bot: str, bot_harness: str, chat_harness: str) -> str:
    """The notice row's English: where the Bot's own chat runs, and where this chat stays.

    Read by channel mirrors and the model; the dashboard draws its own copy from
    the row's token (:func:`harness_notice_meta`).
    """
    return (
        f"ℹ {bot} runs on {bot_harness} in its own chat, and this chat stays on "
        f"{chat_harness}. Open {bot}'s own chat to use {bot_harness}."
    )


def harness_notice_meta(bot: str, bot_harness: str, chat_harness: str) -> dict[str, object]:
    """The notice row's ``meta``: its kind, and the token and names the dashboard words it from."""
    from kiro_crew.dashboard import gateway_notice as gn

    return {
        "kind": MEMBER_HARNESS_NOTICE_KIND,
        **gn.gateway_notice_meta(
            gn.GATEWAY_NOTICE_MEMBER_HARNESS_ELSEWHERE,
            bot=bot,
            bot_harness=bot_harness,
            chat_harness=chat_harness,
        ),
    }


def _claimed_notice(
    conversation_log: Any, session_key: str, config: Any, execution: Any
) -> tuple[str, str, str] | None:
    """``(bot, bot_harness, chat_harness)`` names once this chat claimed its notice, else ``None``."""
    from kiro_crew.dashboard.member_rebind_notice import bot_name

    apart = harnesses_apart(config, session_key, execution)
    if apart is None:
        return None
    bot = bot_name(config, execution)
    if not bot or not claim_harness_notice(conversation_log, session_key, *apart):
        return None
    return bot, harness_name(apart[0]), harness_name(apart[1])


async def surface_harness_notice(
    state: Any, slot: Any, session_key: str, config: Any, execution: Any
) -> bool:
    """Tell the user once that this chat runs its Bot on another harness than the Bot's own chat.

    *execution* is the record the turn reuses. Returns whether a notice was
    given. Never raises: a notice that fails leaves the turn as it was.
    """
    try:
        names = await asyncio.to_thread(
            _claimed_notice, state.conversation_log, session_key, config, execution
        )
        if names is None:
            return False
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(
            state,
            slot,
            "notice",
            harness_notice_text(*names),
            "msg msg-info",
            meta=harness_notice_meta(*names),
        )
        return True
    except Exception:  # noqa: BLE001 - an informational notice must never fail a turn
        logger.warning(
            "harness notice for %s could not be given", getattr(slot, "key", "?"), exc_info=True
        )
        return False
