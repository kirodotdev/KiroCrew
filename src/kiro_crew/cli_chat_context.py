"""The session context a ``kirocrew chat`` session opens with.

Every other surface (the dashboard, a channel, cron, ``kirocrew run``) opens a
session with the message ``ContextBuilder.build_message`` assembles: the agent's
``[AGENT SYSTEM PROMPT]``, the session context with its memory, lessons, critical
rules and skill directory, and ``file://`` steering on Claude Code. A harness
other than Kiro's reads a custom agent's ``prompt`` and its ``skill://`` skills
from that message alone; every mirror rules both withheld on that premise. The
CLI therefore builds the same message for those harnesses.

kiro-cli and KAS load the agent's prompt and skills themselves, so a CLI session
on either keeps sending the typed text as it did before: no session context is
added there.

Only a session's FIRST message carries it, as on those surfaces: the session holds
it from then on. ``build_message`` decides per harness what that message holds.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Protocol

from kiro_crew.agent_discovery import served_provider_label
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly.budget import window_for_provider_client
from kiro_crew.context_assembly.inclusion import CONTEXT_GROUP_PROJECT
from kiro_crew.essential_delivery import RECEIPT_RESETTING_COMMANDS
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import HookManager
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import named_store_or_empty
from kiro_crew.quick_prompts import QUICK_PROMPTS, user_quick_prompt
from kiro_crew.skills import SkillsLoader
from kiro_crew.terminal_safe import safe_terminal_line

logger = logging.getLogger(__name__)


class _LiveSession(Protocol):
    """What :meth:`CliSessionContext.owed` reads off the chat's live provider."""

    @property
    def context_incarnation(self) -> object: ...


#: The provider capabilities that name a Kiro harness, which loads the agent itself.
_KIRO_HARNESS_FLAGS = ("is_kiro_backend", "is_kas_backend")


def _on_kiro_harness(provider: object) -> bool:
    return any(getattr(provider, flag, None) is True for flag in _KIRO_HARNESS_FLAGS)


def _first_word(text: str) -> str:
    words = text.split(maxsplit=1)
    return words[0] if words else ""


async def _is_harness_command(text: str) -> bool:
    """Whether *text* opens with a slash command, which a harness runs only bare.

    A harness runs a command only when the command opens the prompt text, so
    behind the session context it would reach the model as words. A quick prompt
    is a macro ``build_message`` expands, not a command, so it keeps the context.
    A user prompt is a file, so its lookup runs off the loop.
    """
    word = _first_word(text)
    if not word.startswith("/") or word.lower() in QUICK_PROMPTS:
        return False
    return await asyncio.to_thread(user_quick_prompt, word) is None


@dataclass(frozen=True)
class CliSessionContext:
    """Builds the first message of each session a CLI chat opens."""

    builder: ContextBuilder
    session_key: str
    #: The kiro agent the session runs: the execution template ``build_message``
    #: reads the prompt and the ``skill://`` mapping from.
    agent: str
    #: The store ``resolve_agent_bindings`` resolved for the session.
    memory_store: str
    #: ``None`` for every group. A member bound to a named store gets neither
    #: memory nor lessons: the CLI does not bind that store, and Global memory is
    #: not the member's to read.
    context_groups: frozenset[str] | None

    @staticmethod
    def clears_history(text: str) -> bool:
        """Whether *text* is a harness command that discards the session's history."""
        return _first_word(text) in RECEIPT_RESETTING_COMMANDS

    def owed(self, provider: _LiveSession, delivered_to: object | None) -> bool:
        """Whether the next turn goes to a session the context has not reached.

        *delivered_to* is ``provider.context_incarnation`` read after the last turn
        that carried the context, ``None`` before any did. A changed incarnation is
        another conversation or a respawned process, so it owes the context again.

        shortcut: a backend process that exits between turns is seen only once the
        send that respawned it changed the incarnation, so that one turn goes as
        typed and the next carries the context. Asking the process itself would
        attribute a shared runtime's liveness to this session; upgrade when a
        provider exposes a per-session liveness answer.
        """
        return delivered_to is None or provider.context_incarnation != delivered_to

    async def first_message(self, provider: object, text: str) -> str | None:
        """*text* as the first message of the session *provider* serves.

        ``None`` means *text* goes as typed: the provider runs kiro-cli or KAS,
        which load the agent themselves, or, with the context still owed, *text* is
        a harness command or the build failed, which a one-line notice reports.
        *provider* is the live provider; ``build_message`` admits it as the
        session's context provider through ``context_provider_of``.
        """
        if _on_kiro_harness(provider) or await _is_harness_command(text):
            return None
        try:
            # Off-loop: the build reads memory and may embed the recall query.
            # The hook result is not acted on, as on the dashboard and the task
            # runner: a transform or injected context is already in the message.
            message, _hook = await run_in_embed_pool(
                self.builder.build_message,
                text,
                True,
                self.session_key,
                # A terminal renders neither the dashboard's [OPTIONS:] chips nor
                # its widgets, so their guidance is left out.
                interactive=False,
                agent=self.agent,
                memory_store=self.memory_store,
                context_groups=self.context_groups,
                provider_type=served_provider_label(provider),
                model_window=window_for_provider_client(provider),
                context_provider=provider,
            )
        except Exception as exc:
            # A failed build costs the context, never the turn: the typed text goes.
            logger.debug("The kirocrew chat session context was not built", exc_info=True)
            print(
                f"⚠️  Session context not built ({safe_terminal_line(str(exc))}); "
                "sending your message without it.",
                file=sys.stderr,
            )
            return None
        return message


async def build_cli_session_context(
    *, hooks: HookManager, bot_name: str, session_key: str, agent: str, memory_store: str
) -> CliSessionContext:
    """The context builder for one CLI chat.

    *hooks* is the CLI's own gate manager, so the message hooks are the ones its
    tool gate runs. The builtin skills are not synced here: the gateway owns that
    sync, and the loader reads the tree already on disk.
    """

    def _stores() -> tuple[MemoryStore, SkillsLoader, LessonStore]:
        return MemoryStore(), SkillsLoader(install_builtins=False), LessonStore()

    memory, skills, lessons = await asyncio.to_thread(_stores)
    builder = ContextBuilder(
        memory=memory, skills=skills, hooks=hooks, lessons=lessons, bot_name=bot_name
    )
    return CliSessionContext(
        builder=builder,
        session_key=session_key,
        agent=agent,
        memory_store=memory_store,
        context_groups=(
            frozenset({CONTEXT_GROUP_PROJECT}) if named_store_or_empty(memory_store) else None
        ),
    )
