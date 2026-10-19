"""The command handlers that answer without a turn: /model, /sessions, /yolo, dashboard.

``handle_message`` parses each command and dispatches it here. ``/model`` lists what the
session's backend advertised and applies a numbered pick (``_model_choices``,
``_handle_model``, ``_apply_model``, ``_model_label``); ``/sessions`` lists this
conversation's own past generations (``_bucket_stem``, ``_handle_sessions``); ``/yolo``
reports or changes the process-wide auto-approve grant (``_handle_yolo``); and
``/kirocrew dashboard`` mints a presigned dashboard link in a direct room
(``_handle_dashboard``, with ``_ttl_spec`` and ``_format_ttl``). ``/new``, ``/help``,
``/agent``, ``/stop``, ``/link``, ``/unlink`` and ``/compact`` stay in
``transport_dispatch.py``: repository guards read ``/stop`` and ``/link`` there, and
``/compact`` shares its compaction with the context-length notice beside it. Each
function here whose first parameter is ``self`` is a ``WebexDispatcher`` method: the
class in :mod:`kiro_crew.webex.transport_dispatch` binds it by name, so it reads and
writes the dispatcher's own state on the instance, and a patch of the method on the
class or on an instance reaches every caller. Instance fields read (a call on one
included): ``_model_pref``, ``cfg``, ``conv_log`` and ``sessions``. Instance fields
assigned, or whose mapping is changed: ``_model_pref``. A new command's handler goes
here; its dispatch stays in ``handle_message``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.history import transcript_stem
from kiro_crew.messaging.link import build_dm_session_key
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.sel import sel
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.commands import parse_command_argument
from kiro_crew.webex.transport import ROOM_DIRECT
from kiro_crew.webex.transport_dispatch_origin import _chat_type_of, _route_of

if TYPE_CHECKING:
    from kiro_crew.webex.transport_dispatch import WebexDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.webex.transport_dispatch")


# How many conversations ``/sessions`` prints. A Webex message is byte-capped,
# and a list longer than this stops being scannable anyway.
_SESSIONS_LIST_MAX = 15


def _model_choices(self: WebexDispatcher, session_key: str) -> list[tuple[str, str]]:
    """``(model_id, label)`` rows this session may actually use.

    The ONLY source is what the session's backend advertised at
    ``session/new`` — the set THIS account is entitled to, carrying the
    backend's own ids. A static catalogue would offer models the account
    cannot reach, which surfaces as a refusal mid-conversation, and its
    display keys would need per-backend translation before the wire.
    """
    rows: list[tuple[str, str]] = [("", "Auto (let the backend choose)")]
    provider = self.sessions.get_provider(session_key)
    advertised = getattr(provider, "available_models", None)
    if not callable(advertised):
        return rows
    try:
        entries = [m for m in advertised() if isinstance(m, dict)]
    except Exception:
        logger.warning("Webex /model: available_models failed", exc_info=True)
        return rows
    for entry in entries:
        model_id = str(entry.get("modelId") or "").strip()
        # "auto" is already the first row; listing it twice offers the same
        # choice two numbers.
        if not model_id or model_id == "auto":
            continue
        rows.append((model_id, str(entry.get("name") or model_id)))
    return rows


async def _handle_model(self: WebexDispatcher, inbound: "WebexInbound") -> None:
    """List the advertised models, or apply a numbered pick.

    Numbered rather than free-text: a model id is not something the user can
    enumerate, and a typo would land as a rejected ``set_model`` mid-
    conversation. The number indexes the list this reply printed.
    """
    email = inbound.person_email
    route = _route_of(inbound)
    session_key = self._session_key(route)
    choices = self._model_choices(session_key)
    arg = parse_command_argument(inbound.text)
    if not arg:
        current = self._model_pref.get(email, "")
        lines = [
            f"**Model** — currently {self._model_label(choices, current)}",
            "",
        ]
        lines += [
            f"{index}. {label}{' ·  current' if mid == current else ''}"
            for index, (mid, label) in enumerate(choices, start=1)
        ]
        if len(choices) == 1:
            lines.append("")
            lines.append("_No model list yet — send a message first, then `/model`._")
        lines += ["", "Reply `/model <number>` to switch."]
        await self._reply(inbound, "\n".join(lines))
        return
    if not arg.isdigit() or not 1 <= int(arg) <= len(choices):
        await self._reply(
            inbound,
            f"❌ Pick a number between 1 and {len(choices)} — send `/model` for the list.",
        )
        return
    model_id, label = choices[int(arg) - 1]
    await self._reply(inbound, await self._apply_model(inbound, model_id, label))


async def _apply_model(
    self: WebexDispatcher, inbound: "WebexInbound", model_id: str, label: str
) -> str:
    """Record *model_id* for the caller and push it to the live session.

    *model_id* comes verbatim from the session's advertised list, so it is
    already the id this backend accepts — no canonical translation, which
    would differ per backend and could mangle an id that was correct.

    The preference is stored unconditionally so it reaches the NEXT session
    even when there is nothing live to switch. When a session does exist the
    switch is attempted IN PLACE: ``session/set_model`` carries the
    conversation across, so a user who picks a model mid-conversation gets the
    model they picked rather than a promise about their next one. The session
    semaphore is taken atomically, because the switch and a turn share one
    stdio channel and interleaving JSON-RPC on it would corrupt both.

    Returns the user-facing outcome line — every branch says what actually
    happened, since a claimed switch the user cannot verify is worse than a
    deferral they can act on.
    """
    # The preference is per PERSON: which model to use is an individual
    # choice, while the conversation it applies to may be a shared space.
    self._model_pref[inbound.person_email] = model_id
    session_key = self._session_key(_route_of(inbound))
    live = self.sessions.has_session(session_key)
    # Two different promises, because the preference reaches a session only at
    # CREATION: ``get_or_create`` returns a reused session from its fast path
    # before it consults ``model=``. With nothing live the next message starts
    # the session, so it genuinely lands then.
    deferred = f"✅ Model set to **{label}** — it applies to your next message."
    next_new = (
        f"✅ Model set to **{label}** — this conversation keeps its current "
        f"model; the switch applies to your next one (`/new`)."
    )
    # "Auto" has no ACP id meaning "let the backend choose", so it can only be
    # recorded; the next session start resolves it. Claiming a live switch here
    # would be a lie.
    if not model_id:
        return next_new if live else deferred
    if not live:
        return deferred
    if not await self.sessions.try_acquire(session_key):
        return (
            f"✅ Model set to **{label}**, but a reply is still running — this "
            f"conversation keeps its current model; the switch applies to your "
            f"next one (`/new`)."
        )
    try:
        provider = self.sessions.get_provider(session_key)
        set_model = getattr(getattr(provider, "client", None), "set_model", None)
        if set_model is None:
            return next_new
        await set_model(model_id)
    except Exception as exc:
        logger.warning(
            "Webex /model: live set_model failed for %s: %s",
            session_key,
            type(exc).__name__,
            exc_info=True,
        )
        # The stored preference still stands, so the next session gets it —
        # but do not claim the running conversation switched when it did not.
        return (
            f"⚠️ Couldn't switch this conversation to **{label}** "
            f"({type(exc).__name__}) — it applies to your next conversation "
            f"(`/new`)."
        )
    finally:
        self.sessions.release(session_key)
    return f"✅ Now using **{label}**."


def _model_label(choices: list[tuple[str, str]], model_id: str) -> str:
    """The label for a stored preference, falling back to the raw id."""
    return next((label for mid, label in choices if mid == model_id), model_id or "Auto")


def _bucket_stem(self: WebexDispatcher, route: str) -> str:
    """The filename stem shared by every generation of *route*'s conversation.

    ``list_sessions`` reports a key as the transcript's filename STEM, which
    is the session key with every character outside ``[\\w\\-.]`` folded to
    ``_`` — so a colon-bearing session key never matches a raw prefix test
    against it, and a filter written that way silently lists nothing at all.
    Folded through ``transcript_stem`` so this side uses the store's own rule
    rather than a second copy of it.
    """
    bucket = build_dm_session_key(
        "webex",
        self._resolve_agent(route),
        route,
        gen=0,
        dm_scope=str(self.cfg.messaging.dm_scope),
        chat_type=_chat_type_of(route),
    )
    return transcript_stem(bucket)


async def _handle_sessions(self: WebexDispatcher, inbound: "WebexInbound") -> None:
    """List THIS conversation's past generations, newest first.

    Scoped to the caller's own durable bucket — their DM, or the space the
    command was sent in — and not to the channel. A channel-wide list would
    print one person's conversation TITLES, which are the opening words of
    their messages, into whatever room asked; scoping to the bucket means the
    audience of the list is exactly the audience of the conversations in it.

    Deliberately not a dashboard-session picker: resuming one of those needs
    the durable resume-expectation store and an inbound path that resolves the
    mirror binding. Webex implements neither and therefore leaves
    ``supports_session_resume`` false; Discord and Telegram are the shipped
    transports that currently declare it.
    """
    if self.conv_log is None:
        await self._reply(inbound, "ℹ️ Conversation history is not available.")
        return
    route = _route_of(inbound)
    stem = self._bucket_stem(route)
    try:
        rows = [
            row
            for row in await asyncio.to_thread(self.conv_log.list_sessions)
            # The bucket itself (generation 0) or one of its generations. The
            # separator is required so a bucket cannot match a longer route
            # that merely starts with the same characters.
            if (key := str(row.get("key", ""))) == stem or key.startswith(f"{stem}_gen")
        ]
    except Exception:
        logger.warning("Webex /sessions: listing failed", exc_info=True)
        await self._reply(inbound, "⚠️ Couldn't read the conversation list.")
        return
    sel().log_api_access(
        caller=f"webex:{inbound.person_email}",
        operation="webex.sessions_list",
        outcome="success",
        source="webex",
        resources=f"bucket={stem} rows={len(rows)}",
    )
    if not rows:
        await self._reply(inbound, "ℹ️ No earlier conversations here yet.")
        return
    current = transcript_stem(self._session_key(route))
    lines = ["**This conversation's history**", ""]
    for row in rows[:_SESSIONS_LIST_MAX]:
        title = str(row.get("title") or "").strip() or "(untitled)"
        marker = " ·  current" if str(row.get("key", "")) == current else ""
        lines.append(f"- {title}{marker}")
    if len(rows) > _SESSIONS_LIST_MAX:
        lines.append(f"- _…and {len(rows) - _SESSIONS_LIST_MAX} older_")
    lines += ["", "Send `/new` to start a fresh one."]
    await self._reply(inbound, "\n".join(lines))


async def _handle_yolo(self: WebexDispatcher, inbound: "WebexInbound") -> None:
    """Report or change the global auto-approve grant.

    Reads and writes the process-wide :func:`safety_override` grant — the
    SAME one the dashboard toggle and the other channels drive, so a grant
    taken here shows up (and expires) everywhere. Reachable only by an
    allow-listed Webex user, because ``transport.receive`` is deny-by-default
    before dispatch ever runs.

    Turning it on does NOT weaken the PreToolUse security gate: the
    sensitive-path keystone, governance ceiling and deny-list all run ahead
    of the auto-approve ladder in ``TurnDriver``, so a hard DENY still wins.

    The three grant mutators run off-loop: ``activate`` resolves the ad-hoc
    duration through a live config read and each writes a SEL record, so
    calling them inline would put filesystem latency on the event loop.
    """
    so = safety_override()
    arg = parse_command_argument(inbound.text)
    action = arg.lower().split()[0] if arg else ""

    if action in ("on", "off", "renew"):
        outcome = "allowed"
        if action == "on":
            if so.is_active():
                reply = f"🟢 YOLO is already ON ({describe_grant_lifetime()})."
            elif (await asyncio.to_thread(so.activate, "webex")).active:
                reply = (
                    f"🟢 YOLO ON ({describe_grant_lifetime()}) — every tool "
                    f"auto-approves. Denied-by-policy tools are still blocked."
                )
            else:
                reply = "❌ Couldn't turn YOLO on (audit system unavailable)."
                outcome = "denied"
        elif action == "off":
            # Unconditional: deactivate() also zeroes the deadline of a grant
            # that already lapsed, which closes the renew grace window so a
            # later "/yolo renew" cannot resurrect it, and records the
            # operator's decision either way.
            await asyncio.to_thread(so.deactivate, "webex")
            reply = "🔴 YOLO OFF — tools ask for approval again."
        else:
            renewed = (await asyncio.to_thread(so.renew, "webex")).renewed
            reply = (
                f"🟢 YOLO renewed ({describe_grant_lifetime()})."
                if renewed
                else "🔴 YOLO is not active — use `/yolo on` first."
            )
        sel().log_api_access(
            caller=f"webex:{inbound.person_email}",
            operation="webex.yolo_mode",
            outcome=outcome,
            source="webex",
            resources=f"yolo_{action}",
        )
        await self._reply(inbound, reply)
        return

    status = f"ON 🟢 ({describe_grant_lifetime()})" if so.is_active() else "OFF 🔴"
    await self._reply(
        inbound,
        f"YOLO is {status}.\nUsage: `/yolo on | off | renew`",
    )


async def _handle_dashboard(self: WebexDispatcher, inbound: "WebexInbound") -> None:
    """Generate and send a presigned dashboard login link.

    Calls ``generate_token`` directly (never via shell) and builds the URL
    from the ``dashboard.url`` config (``KIROCREW_PORT`` overrides the port,
    matching every other link producer).

    The direct-room assertion is kept explicit rather than inherited: a
    presigned link is a credential and every member of a space can read it,
    while ``transport.receive``'s room gate deliberately DOES admit an
    allow-listed space. So this command needs its own narrower rule, not that
    layer's.
    """
    # Deferred deliberately: the dashboard package drags in ~370 transitive
    # modules, and this command is the only thing in the channel that needs
    # it. At module scope every gateway that merely enables Webex would pay
    # that import.
    from kiro_crew.dashboard.token_auth import (
        MAX_SESSION_TTL_SECS,
        generate_token,
        parse_duration,
    )
    from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

    # Positive test against the named constant, not ``!= "direct"``: a Webex
    # room type this code has not seen must not inherit what a direct room
    # gets, and a presigned dashboard login is the worst thing to hand a room
    # whose audience nobody enumerated.
    if inbound.room_type != ROOM_DIRECT:
        await self._reply(
            inbound,
            "🔒 Dashboard links are only sent in a direct message.",
        )
        return
    arg = parse_command_argument(inbound.text).lower()
    if not arg.startswith("dashboard"):
        await self._reply(
            inbound,
            "Usage: `/kirocrew dashboard [<N>h|<N>m]`",
        )
        return
    # parse_duration owns the <N>h / <N>m grammar and already clamps to the
    # server maximum; the outer min() keeps the clamp explicit at the call
    # site. A malformed duration falls back rather than erroring: the user
    # asked for a link, and refusing one over a typo trades a working link
    # for a lecture.
    ttl_secs = min(parse_duration(_ttl_spec(arg)) or _DEFAULT_TTL_SECS, MAX_SESSION_TTL_SECS)
    caller = f"webex:{inbound.person_email}"
    try:
        token = generate_token(inbound.person_email, ttl_seconds=ttl_secs)
        origin = dashboard_origin(self.cfg.dashboard.url)
        if not origin:
            # No configured dashboard.url: fall back to the local port
            # (parse_dashboard_url applies the KIROCREW_PORT override).
            _, port = parse_dashboard_url(self.cfg.dashboard.url)
            origin = f"http://localhost:{port}"
        # Credential issuance MUST be audited (backend-security-controls).
        sel().log_api_access(
            caller=caller,
            operation="webex.dashboard_token",
            outcome="ok",
            source="webex",
            resources=f"ttl={ttl_secs}",
        )
        # self_minted: the URL carries a token THIS process just generated, and
        # it looks exactly like the credential-bearing link the exfiltration
        # redactor exists to catch — scanning it would deliver a login link
        # that cannot log in. Nothing here passed through the model.
        await self._reply(
            inbound,
            f"🔗 Dashboard link (valid {_format_ttl(ttl_secs)}):\n" f"{origin}/?token={token}",
            self_minted=True,
        )
    except Exception as exc:
        logger.warning("Webex /kirocrew dashboard: token generation failed", exc_info=True)
        try:
            sel().log_api_access(
                caller=caller,
                operation="webex.dashboard_token",
                outcome="error",
                source="webex",
                resources=f"ttl={ttl_secs}",
            )
        except Exception:
            # The audit trail must never turn a user-facing failure reply
            # into a crash; the warning above already captured the error.
            pass
        await self._reply(
            inbound,
            f"⚠️ Could not generate dashboard link: {type(exc).__name__}",
        )


# How long a dashboard link lives when the user names no duration.
_DEFAULT_TTL_SECS = 3600


def _ttl_spec(arg: str) -> str:
    """The duration token out of ``dashboard [<N>h|<N>m]``, or ``""``.

    Only the SPLITTING is local; the grammar itself belongs to
    ``token_auth.parse_duration``, so a unit added there reaches this command
    too instead of diverging silently.
    """
    parts = arg.split()
    return parts[1].strip().lower() if len(parts) > 1 else ""


def _format_ttl(ttl_secs: int) -> str:
    """A short human duration for the link's validity line."""
    mins = max(1, ttl_secs // 60)
    if mins % 60 == 0:
        return f"{mins // 60}h"
    if mins > 60:
        return f"{mins // 60}h {mins % 60}m"
    return f"{mins}m"
