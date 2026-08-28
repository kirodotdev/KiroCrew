"""Core-derived AgentCore session principals.

``SessionPrincipal`` is defined on the CPP seam
(``platform.interfaces``). This module is the only place the core *builds*
one: surface + already-known identity + the existing session key. A tool
argument, a client body, or an injected cron/subagent envelope is never a
user.

A companion may *annotate* (attach a verified JWT) through
``AgentIdentityProvider.annotate_principal``. It may not replace ``subject``.
Gateway inbound attach is a later stack PR — this module only derives,
annotates, and stores.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from kiro_crew.constants import (
    CRON_NOTIFY_PREFIX,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.platform.context import (
    async_safe_context_call,
    current_context,
)
from kiro_crew.platform.interfaces import SessionPrincipal

logger = logging.getLogger(__name__)

# Keys a tool_input dict must never be allowed to use as identity. The core
# already knows surface / raw_id / session_key; taking any of these from the
# model would let a prompt mint ``slack+U0123`` as ``dashboard+admin``.
_TOOL_INPUT_IDENTITY_KEYS = frozenset(
    {
        "subject",
        "userId",
        "user_id",
        "user_jwt",
        "raw_id",
        "surface",
        "session_key",
    }
)


def reject_tool_input_identity(tool_input: Mapping[str, Any]) -> None:
    """Refuse identity fields supplied through a tool argument dict.

    Session principals are core-derived only. A helper that a tool-dispatch
    path can call before (or instead of) ``derive_session_principal`` so a
    model-authored ``userId`` / ``subject`` never becomes the vault key.
    """
    hits = _TOOL_INPUT_IDENTITY_KEYS.intersection(tool_input)
    if hits:
        raise ValueError(
            "SessionPrincipal is core-derived; tool_input cannot supply " + ", ".join(sorted(hits))
        )


def derive_session_principal(
    *,
    surface: str,
    raw_id: str,
    session_key: str,
    tool_input: Mapping[str, Any] | None = None,
) -> SessionPrincipal:
    """Build a partitioned principal from ground truth the core already has.

    ``subject`` is ``{surface}+{raw_id}`` so ``slack+U0123`` and
    ``dashboard+U0123`` cannot collide. ``user_jwt`` stays ``None`` until a
    companion annotates. ``session_key`` is the existing session address —
    this does not invent a second key.

    ``tool_input``, if passed, is inspected only so it can be *rejected*
    when it tries to supply identity. It is never read as a source.
    """
    if tool_input is not None:
        reject_tool_input_identity(tool_input)
    return SessionPrincipal(
        surface=surface,
        subject=f"{surface}+{raw_id}",
        session_key=session_key,
        user_jwt=None,
    )


def is_injected_envelope(text: str) -> bool:
    """True when *text* is a cron / subagent-completion injection, not a user.

    This is the skip/bind discriminator. Callers that only need a boolean
    (``principal_bind_kwargs``, ``_run_chat``) should use this, not
    :func:`derive_session_principal_for_injected`.
    """
    return (
        text.startswith(CRON_NOTIFY_PREFIX)
        or text.startswith(SUBAGENT_COMPLETION_PREFIX)
        or text.startswith(SUBAGENT_BATCH_COMPLETION_PREFIX)
    )


def derive_session_principal_for_injected(text: str) -> SessionPrincipal | None:
    """Return ``None`` iff *text* is an injected envelope; raise otherwise.

    ``[Cron notification from "job"]`` and ``[Subagent completion event]``
    arrive from automation, not from a human. Do not mint a user-bound
    principal (or later a user-bound token) for them.

    A normal user message is not this helper's input: it raises
    ``ValueError`` so a silent ``None`` cannot mean both "injected, skip
    bind" and "not this helper's job". Callers that only need the boolean
    must use :func:`is_injected_envelope`.
    """
    if is_injected_envelope(text):
        return None
    raise ValueError(
        "derive_session_principal_for_injected is only for injected envelopes; "
        f"{text!r} is a user message — use is_injected_envelope to decide"
    )


def principal_bind_kwargs(message: str, *, surface: str, raw_id: str) -> dict[str, str]:
    """``surface`` / ``raw_id`` for ``publish_turn_identity``, or ``{}``.

    Empty when :func:`is_injected_envelope` is true: the envelope is not a
    user, so the caller publishes the pid sidecar only. An ordinary user
    turn still binds.
    """
    if is_injected_envelope(message):
        return {}
    return {"surface": surface, "raw_id": raw_id}


def inherit_parent_principal(parent: SessionPrincipal, *, session_key: str) -> SessionPrincipal:
    """Subagent principal: same subject as the parent, child's session key."""
    return SessionPrincipal(
        surface=parent.surface,
        subject=parent.subject,
        session_key=session_key,
        user_jwt=parent.user_jwt,
    )


async def apply_principal_annotation(principal: SessionPrincipal) -> SessionPrincipal:
    """Ask the companion to annotate; keep the core-derived ``subject``.

    Fallback is the core principal unchanged (Default adapter, or a
    transient adapter error). A companion may set ``user_jwt``. A rewrite of
    ``subject`` (or ``session_key`` / ``surface``) is ignored — those are
    core-derived and not a companion concern.
    """

    async def _annotate() -> SessionPrincipal:
        return await current_context().agent_identity.annotate_principal(principal)

    annotated = await async_safe_context_call(
        _annotate,
        fallback=principal,
        log_message="agent_identity.annotate_principal failed; keeping core principal",
    )
    if (
        annotated.subject != principal.subject
        or annotated.session_key != principal.session_key
        or annotated.surface != principal.surface
    ):
        logger.warning("annotate_principal rewrote a core-derived field; keeping core subject")
        return SessionPrincipal(
            surface=principal.surface,
            subject=principal.subject,
            session_key=principal.session_key,
            user_jwt=annotated.user_jwt,
        )
    return annotated


async def bind_session_principal(
    sessions: Any,
    *,
    surface: str,
    raw_id: str,
    session_key: str,
) -> SessionPrincipal:
    """Derive, annotate, and store the principal on the live session.

    ``sessions.set_principal`` is the SessionManager hook; a stub without it
    is a no-op store so identity binding can never break a turn.
    Gateway inbound attach is a later stack PR.
    """
    principal = derive_session_principal(surface=surface, raw_id=raw_id, session_key=session_key)
    annotated = await apply_principal_annotation(principal)
    setter = getattr(sessions, "set_principal", None)
    if callable(setter):
        setter(session_key, annotated)
    return annotated
