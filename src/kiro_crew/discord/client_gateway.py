"""The Discord Gateway WebSocket: connect, identify or resume, heartbeat, dispatch.

``_gateway_loop`` keeps one connection up, resuming the session and backing off its
reconnects; ``_run_connection`` runs hello, identify or resume and the read loop; and
the op-1 heartbeat loop recycles a connection whose acknowledgement went missing.
``_on_dispatch`` normalizes ``READY``, ``RESUMED``, ``MESSAGE_CREATE`` and
``INTERACTION_CREATE`` into :class:`DiscordInbound` and :class:`DiscordInteraction`
records and runs each handler in a task of its own. Each function here whose first
parameter is ``self`` is bound as the ``DiscordClient`` attribute of the same name in
:mod:`kiro_crew.discord.client`, which keeps the client's state, so ``self.<name>(...)``
calls and instance and class patches reach it as they reach a method defined in the
class body. They read ``_closed``, ``_hb_acked``, ``_hb_task``, ``_intents``,
``_on_interaction``, ``_on_message``, ``_proxy``, ``_resume_url``, ``_seq``,
``_session_id``, ``_token``, ``bot_user_id``, ``fatal_error`` and ``ready``, write
``_closed``, ``_hb_acked``, ``_hb_task``, ``_resume_url``, ``_seq``, ``_session_id``,
``_ws``, ``application_id``, ``bot_user_id`` and ``fatal_error``, and update
``_handler_tasks`` and ``ready`` in place. New Gateway work goes here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger("kiro_crew.discord.client")


_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Transport-level WS ping interval (seconds). Deliberately a SECOND liveness
# layer under Discord's op-1 application heartbeat, because the two have
# different failure domains: op-1 runs in a cancellable user task and proves
# the Discord session is alive, while the aiohttp ping runs inside the
# library's read loop and proves the TCP path (host -> NAT -> edge) is alive.
# Without it, a NAT-evicted half-open connection blocks the dispatch loop
# forever with no error while outbound REST keeps working. 60s is longer than
# Discord's ~41s heartbeat so healthy traffic keeps the path warm anyway, and
# generous enough that a briefly stalled event loop does not flap the
# connection with false-positive pong timeouts.
_WS_HEARTBEAT_SECS = 60.0

# A Gateway connection must live at least this long for its close to count as
# "healthy" and reset the reconnect backoff; a shorter one stays on the backoff
# curve so a repeating clean close cannot hot-loop. Mirrors WebexClient and
# WeComClient, which carry the same guard for the same reason.
_MIN_HEALTHY_CONN_SECS = 5.0


# Gateway opcodes.
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Interaction types / callback types.
_INTERACTION_APPLICATION_COMMAND = 2
_INTERACTION_MESSAGE_COMPONENT = 3


#: Interaction types this client normalizes and forwards. Anything else is
#: dropped at the dispatch boundary rather than handed on as a half-filled
#: record — an autocomplete or modal submit needs its own response shape within
#: Discord's 3-second callback deadline, and a handler that received one without
#: knowing how to answer it would leave the user's client spinning.
_HANDLED_INTERACTIONS = frozenset(
    {_INTERACTION_APPLICATION_COMMAND, _INTERACTION_MESSAGE_COMPONENT}
)


@dataclass
class DiscordInbound:
    """Normalized inbound message from a MESSAGE_CREATE dispatch."""

    channel_id: str
    user_id: str
    username: str = ""
    text: str = ""
    message_id: str = ""
    guild_id: str = ""  # empty string == DM channel
    is_bot: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscordInteraction:
    """Normalized INTERACTION_CREATE dispatch — a button press or a command.

    One record covers both because everything the dispatcher needs first
    (identity, channel, guild, and the ack it owes Discord within ~3s) is shared;
    ``kind`` selects which of the two payload halves is populated.
    """

    interaction_id: str
    interaction_token: str
    channel_id: str
    user_id: str
    message_id: str
    custom_id: str = ""
    label: str = ""  # button text, recovered from the message's components
    guild_id: str = ""  # empty string == DM channel
    username: str = ""
    #: Discord interaction type — ``2`` application command, ``3`` message
    #: component. Defaults to the component type so the pre-existing button
    #: call sites (and the tests that construct this directly) keep their
    #: meaning without naming it.
    kind: int = _INTERACTION_MESSAGE_COMPONENT
    #: Application-command name (``kind == 2`` only), without a leading slash.
    command_name: str = ""
    #: Top-level command options as ``{name: value}``, values stringified.
    #: Discord types them, but every command here takes free text or nothing,
    #: and one shape keeps the parser shared with the ``!`` text commands.
    options: dict[str, str] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        """True for an application (slash) command invocation."""
        return self.kind == _INTERACTION_APPLICATION_COMMAND


async def _gateway_loop(self: DiscordClient) -> None:
    """Persistent Gateway connection with resume + exponential backoff."""
    attempt = 0
    while not self._closed:
        started = time.monotonic()
        reason: str | None = None
        try:
            await self._run_connection()
        except asyncio.CancelledError:
            break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            if self._closed:
                break
            # Log only the exception type — never the token.
            reason = type(exc).__name__
        except Exception:
            if self._closed:
                break
            logger.exception("Discord gateway unexpected error")
            # Flat, not on the curve: an unexpected exception is a code
            # fault rather than a network condition, and its retry cadence
            # is pinned separately.
            await asyncio.sleep(5.0)
            continue
        finally:
            self._stop_heartbeat()
        if self._closed:
            break
        if reason is None:
            # A clean close reconnects promptly — that is what an op-7
            # RECONNECT asks for — but only when the connection actually
            # lived a while. A connect->immediate-clean-close (a proxy
            # terminating the upgrade, a server-side condition that
            # persists) would otherwise spin with ZERO delay: `attempt`
            # reset on every pass, so the backoff curve was unreachable
            # from this path and nothing bounded the rate. Discord bans an
            # identity for 10 minutes after 10,000 invalid requests, so an
            # unbounded loop costs the channel, not just CPU.
            if time.monotonic() - started >= _MIN_HEALTHY_CONN_SECS:
                attempt = 0
                continue
            reason = "gateway closed the connection immediately"
        attempt += 1
        delay = min(1.0 * (2 ** (attempt - 1)), 60.0) + random.random()
        logger.warning("Discord gateway disconnected (%s), reconnect in %.1fs", reason, delay)
        await asyncio.sleep(delay)


async def _run_connection(self: DiscordClient) -> None:
    """One Gateway connection: hello -> identify/resume -> dispatch loop."""
    session = await self._ensure_session()
    url = self._resume_url if (self._session_id and self._resume_url) else _GATEWAY_URL
    ws: Any = None
    try:
        async with session.ws_connect(
            url, proxy=self._proxy, heartbeat=_WS_HEARTBEAT_SECS, max_msg_size=0
        ) as ws:
            self._ws = ws
            try:
                async for raw in ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        # One bad frame costs that frame, not the connection:
                        # escaping here would land in _gateway_loop's
                        # catch-all and reconnect. Log metadata only, never
                        # the body.
                        try:
                            frame = json.loads(raw.data)
                        except json.JSONDecodeError:
                            frame = None
                        if not isinstance(frame, dict):
                            logger.warning(
                                "Discord gateway: dropping non-object frame (%d bytes)",
                                len(raw.data),
                            )
                            continue
                        await self._handle_frame(ws, frame)
                    elif raw.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
            finally:
                self._ws = None
        # Clean close only: classify the close code (4004 etc. surfaces
        # via ws.close_code). An exception path skips this block and gets
        # its log line from the gateway loop's reconnect handler instead.
        code = ws.close_code or 0
        if code in (4004, 4010, 4011, 4012, 4013, 4014):
            # Non-recoverable (bad token / bad intents). Stop rather than
            # hammering the gateway forever; the failure is already logged.
            logger.error(
                "Discord gateway closed with non-recoverable code %s — "
                "check the bot token and intents. Channel stopped.",
                code,
            )
            self._closed = True
            self.fatal_error = f"gateway close {code} (check bot token/intents)"
            self._notify_state(False, self.fatal_error)
        elif code in (4007, 4009):
            # Invalid seq / session timed out — must re-identify from scratch.
            self._session_id = ""
            self._seq = None
        if not self._closed:
            # Every connection end is WARNING-visible: the default log
            # level hides INFO, and an unlogged silent reconnect loop is
            # indistinguishable from a dead channel when diagnosing
            # "Discord stopped responding".
            logger.warning(
                "Discord gateway connection ended (close code %s) — reconnecting",
                code or "none",
            )
    finally:
        # Runs on clean closes AND exception escapes (a send raising
        # mid-dispatch would otherwise skip this): the READY flag and the
        # dashboard badge must never keep asserting a connection that no
        # longer exists. The 4004 branch sets _closed before its own
        # fatal notify, so this cannot overwrite that reason; deliberate
        # close() also sets _closed and is skipped the same way.
        self.ready.clear()
        if not self._closed:
            close_code = ws.close_code if ws is not None else None
            self._notify_state(False, f"reconnecting (close {close_code or 'none'})")


async def _handle_frame(self: DiscordClient, ws: Any, frame: dict) -> None:
    op = frame.get("op")
    if frame.get("s") is not None:
        self._seq = frame["s"]
    if op == _OP_HELLO:
        interval = float(frame["d"]["heartbeat_interval"]) / 1000.0
        self._start_heartbeat(ws, interval)
        if self._session_id and self._resume_url:
            await ws.send_json(
                {
                    "op": _OP_RESUME,
                    "d": {
                        "token": self._token,
                        "session_id": self._session_id,
                        "seq": self._seq or 0,
                    },
                }
            )
        else:
            await self._identify(ws)
    elif op == _OP_HEARTBEAT:
        await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
    elif op == _OP_HEARTBEAT_ACK:
        self._hb_acked = True
    elif op == _OP_RECONNECT:
        await ws.close()
    elif op == _OP_INVALID_SESSION:
        if not frame.get("d"):
            self._session_id = ""
            self._seq = None
        await asyncio.sleep(1.0 + random.random() * 4.0)  # per docs
        await ws.close()
    elif op == _OP_DISPATCH:
        self._on_dispatch(frame.get("t") or "", frame.get("d") or {})


async def _identify(self: DiscordClient, ws: Any) -> None:
    await ws.send_json(
        {
            "op": _OP_IDENTIFY,
            "d": {
                "token": self._token,
                "intents": self._intents,
                "properties": {
                    "os": "linux",
                    "browser": "kirocrew",
                    "device": "kirocrew",
                },
            },
        }
    )


def _start_heartbeat(self: DiscordClient, ws: Any, interval: float) -> None:
    self._stop_heartbeat()
    self._hb_acked = True
    self._hb_task = asyncio.create_task(self._heartbeat_loop(ws, interval))


def _stop_heartbeat(self: DiscordClient) -> None:
    task, self._hb_task = self._hb_task, None
    if task is not None and not task.done():
        task.cancel()


async def _heartbeat_loop(self: DiscordClient, ws: Any, interval: float) -> None:
    """Heartbeat at the server's interval; a missed ack forces a reconnect
    (close -> the gateway loop resumes the session)."""
    try:
        await asyncio.sleep(interval * random.random())  # jitter per docs
        while not self._closed and not ws.closed:
            if not self._hb_acked:
                logger.warning("Discord gateway heartbeat not acked — reconnecting")
                await ws.close()
                return
            self._hb_acked = False
            await ws.send_json({"op": _OP_HEARTBEAT, "d": self._seq})
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception:
        # This task is the only sender of Discord's mandatory op-1
        # heartbeat: if it dies and the connection stays up, the server
        # eventually drops the session and — behind NAT, where the close
        # frame can be lost — the dispatch loop blocks on a half-open
        # socket with heartbeats gone. Close the socket so the gateway
        # loop reconnects instead of carrying a heartbeat-less connection.
        logger.warning("Discord heartbeat loop failed — recycling connection", exc_info=True)
        try:
            await ws.close()
        except Exception:
            logger.debug("Discord heartbeat close failed", exc_info=True)


def _on_dispatch(self: DiscordClient, event: str, d: dict) -> None:
    if event == "READY":
        self._session_id = d.get("session_id", "")
        self._resume_url = d.get("resume_gateway_url", "")
        if self._resume_url and "?" not in self._resume_url:
            self._resume_url += "/?v=10&encoding=json"
        self.bot_user_id = str((d.get("user") or {}).get("id", ""))
        # READY carries the application object, so the id needed to register
        # application commands arrives without a second REST call — and it
        # is kept as a STRING because a snowflake exceeds 2^53 and would
        # lose precision as a float on any round-trip through JSON.
        self.application_id = str((d.get("application") or {}).get("id", "") or "")
        logger.info("Discord gateway READY (bot user %s)", self.bot_user_id)
        self.ready.set()
        self._notify_state(True, "")
        return
    if event == "RESUMED":
        logger.info("Discord gateway session resumed")
        self.ready.set()
        self._notify_state(True, "")
        return
    if event == "MESSAGE_CREATE":
        author = d.get("author") or {}
        inbound = DiscordInbound(
            channel_id=str(d.get("channel_id", "")),
            user_id=str(author.get("id", "")),
            username=author.get("username", ""),
            text=d.get("content", ""),
            message_id=str(d.get("id", "")),
            guild_id=str(d.get("guild_id", "") or ""),
            is_bot=bool(author.get("bot", False)),
            attachments=[
                attachment
                for attachment in (d.get("attachments") or [])
                if isinstance(attachment, dict)
            ],
        )
        if inbound.is_bot or inbound.user_id == self.bot_user_id:
            return  # never respond to bots (incl. ourselves) — loop guard
        task = asyncio.create_task(self._invoke_message(inbound))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)
        return
    if event == "INTERACTION_CREATE":
        kind = d.get("type")
        if kind not in _HANDLED_INTERACTIONS:
            return
        data = d.get("data") or {}
        msg = d.get("message") or {}
        user = d.get("user") or (d.get("member") or {}).get("user") or {}
        custom_id = data.get("custom_id", "")
        interaction = DiscordInteraction(
            interaction_id=str(d.get("id", "")),
            interaction_token=d.get("token", ""),
            channel_id=str(d.get("channel_id", "")),
            user_id=str(user.get("id", "")),
            message_id=str(msg.get("id", "")),
            custom_id=custom_id,
            label=_find_button_label(msg.get("components") or [], custom_id),
            guild_id=str(d.get("guild_id", "") or ""),
            username=user.get("username", ""),
            kind=int(kind),
            command_name=str(data.get("name", "") or "").lower(),
            options=_command_options(data.get("options")),
        )
        task = asyncio.create_task(self._invoke_interaction(interaction))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)


async def _invoke_message(self: DiscordClient, inbound: DiscordInbound) -> None:
    if self._on_message is None:
        return
    try:
        await self._on_message(inbound)
    except Exception:
        logger.exception("Discord on_message handler raised for user=%s", inbound.user_id)


async def _invoke_interaction(self: DiscordClient, interaction: DiscordInteraction) -> None:
    if self._on_interaction:
        try:
            await self._on_interaction(interaction)
        except Exception:
            logger.exception("Discord on_interaction handler raised")


def _command_options(raw: Any) -> dict[str, str]:
    """Flatten an application command's top-level options to ``{name: value}``.

    Only the top level is read: every command registered here is flat, so a
    nested SUB_COMMAND row (option type 1 or 2, which carries its own
    ``options`` list instead of a ``value``) has no name to bind and is skipped
    rather than recorded with an empty value that a handler would then treat as
    "the user passed nothing".
    """
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for opt in raw:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name", "") or "")
        if not name or "value" not in opt:
            continue
        out[name] = str(opt["value"])
    return out


def _find_button_label(components: list[dict], custom_id: str) -> str:
    """Recover the pressed button's display text from the message's components
    (custom_id carries only our compact routing data)."""
    for row in components:
        for btn in row.get("components", []):
            if btn.get("custom_id") == custom_id:
                return btn.get("label", "")
    return ""
