"""Layer 1 -- Weixin (iLink) as a concrete :class:`MessagingTransport`.

Wraps the low-level :class:`WeixinClient` (iLink long-poll + send) in the
channel-neutral transport contract so the Weixin channel rides the shared
``TurnDriver`` instead of a hand-rolled loop. Dependency direction is
``weixin -> messaging`` (allowed); ``messaging`` never imports ``weixin``.

Security: :meth:`authorize` is **deny-by-default**. ``dm_policy=open`` serves
any DM; ``allowlist`` restricts to configured user ids; ``disabled`` serves
nobody. An unconfigured transport (empty allow-list under ``allowlist``)
authorizes nobody.

SCAFFOLD NOTES / TODO:
  * Inbound media (image/voice/file/video) is not yet decrypted+cached -- only
    the text item is extracted. Port ``_download_and_decrypt_media`` next.
  * Outbound chunking uses a naive splitter; swap for ``renderer`` once the
    Markdown block splitter lands.
  * Group delivery is intentionally unsupported for now (iLink bot identities
    rarely receive group events); DM-only, matching Hermes' practical default.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from kiro_crew.messaging.transport import (
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    TransportCapabilities,
)
from kiro_crew.sel import sel
from kiro_crew.weixin.client import (
    ITEM_TEXT,
    MESSAGE_DEDUP_TTL_SECONDS,
    RATE_LIMIT_ERRCODE,
    SESSION_EXPIRED_ERRCODE,
    ContextTokenStore,
    WeixinClient,
    protocol_error_code,
)
from kiro_crew.weixin.renderer import WEIXIN_CHUNK_LIMIT, render_chunks

logger = logging.getLogger(__name__)

DispatchFn = Callable[[InboundMessage], Awaitable[None]]

# iLink renders Markdown natively; DM-only; no threads. Both file directions
# are False because this transport has NO media path: send_message carries text
# only (files_outbound=False), and inbound media (image/voice/file/video) is
# not decrypted or cached (files_inbound=False; see the module docstring).
# Flip each in the same change that lands that direction's media support --
# declaring a capability the transport cannot perform makes the contract lie
# to every future capability-aware caller.
WEIXIN_CAPABILITIES = TransportCapabilities(
    streaming=False,
    edit=False,
    reactions=False,
    files_inbound=False,
    files_outbound=False,
    rich_blocks=False,
    threads=False,
    max_message_chars=WEIXIN_CHUNK_LIMIT,
    max_buttons=0,
    supports_proactive_send=True,
)


def _chunk(text: str, limit: int = WEIXIN_CHUNK_LIMIT) -> list[str]:
    """Deprecated shim -> the Markdown-aware ``renderer.render_chunks``."""
    return render_chunks(text, limit)


class WeixinTransport(MessagingTransport):
    """Concrete Weixin transport over the low-level ``WeixinClient``."""

    channel_type = "weixin"

    def __init__(
        self,
        client: WeixinClient,
        *,
        account_id: str,
        ctx_store: ContextTokenStore,
        allowed_user_ids: Iterable[str] = (),
        dm_policy: str = "allowlist",
        dispatch: DispatchFn | None = None,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._ctx = ctx_store
        self._allowed: frozenset[str] = frozenset(str(u) for u in allowed_user_ids)
        self._known_users: set[str] = set()
        self._dm_policy = dm_policy
        self._dispatch = dispatch
        self.capabilities = WEIXIN_CAPABILITIES
        self._poll_task: asyncio.Task[None] | None = None
        self._sync_buf = ""
        self._seen: dict[str, float] = {}
        self._running = False
        # Optional observer called with (connected, error) when the poll loop
        # dies without a shutdown — lets the gateway keep the dashboard badge
        # truthful after boot (mirrors DiscordClient.on_state_change).
        self.on_state_change: Callable[[bool, str], None] | None = None

    @property
    def client(self) -> WeixinClient:
        return self._client

    # -- Tier-1 core -----------------------------------------------------------
    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        ctx_token = self._ctx.get(self._account_id, conversation_id)
        last = ""
        for i, part in enumerate(_chunk(content, WEIXIN_CAPABILITIES.max_message_chars)):
            if i:
                await asyncio.sleep(0.3)  # avoid iLink rate-limit drops
            client_id = uuid.uuid4().hex
            await self._client.send_message(
                to=conversation_id, text=part, context_token=ctx_token, client_id=client_id
            )
            last = client_id
        return last

    async def resolve_conversation(self, user_id: str) -> str:
        # iLink DM: the conversation id IS the peer's user id.
        return user_id

    async def fetch_history(
        self, conversation_id: str, thread_id: str | None = None
    ) -> list[InboundMessage]:
        # iLink getupdates has no history paging; sessions persist elsewhere.
        return []

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        identities = self._allowed | self._known_users
        return [
            ConfiguredChannelTarget(f"user:{user_id}", f"Weixin DM · {user_id}")
            for user_id in sorted(identities)
        ]

    async def resolve_configured_target(self, target_id: str) -> tuple[str, str | None] | None:
        kind, separator, value = target_id.partition(":")
        identities = self._allowed | self._known_users
        if kind != "user" or not separator or value not in identities:
            return None
        return await self.resolve_conversation(value), None

    # -- Lifecycle -------------------------------------------------------------
    async def connect(self) -> None:
        await self._client.connect()
        # Off-loop: reads and JSON-parses the persisted peer-token file, which
        # grows with the number of DM peers. Doing it inline would stall gateway
        # startup (and the heartbeat) behind unbounded disk I/O.
        await asyncio.to_thread(self._ctx.restore, self._account_id)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
        await self._client.close()

    async def _poll_loop(self) -> None:
        """Long-poll ``getupdates`` and dispatch each inbound message."""
        cancelled = False
        try:
            await self._poll_forever()
        except asyncio.CancelledError:
            # Task cancellation IS the production shutdown path: gateway
            # teardown cancels the poll task without calling ``disconnect()``,
            # so ``_running`` alone cannot distinguish teardown from death.
            cancelled = True
            raise
        finally:
            # Reached on a normal return and on a non-cancel escape alike: a
            # loop that ended any way other than cancellation/shutdown means
            # inbound is dead. Make that loud (default log level is WARNING)
            # and flip the dashboard badge, which otherwise reports the
            # startup outcome forever.
            if not cancelled and self._running:
                logger.warning("weixin: inbound poll loop ended unexpectedly — inbound is dead")
                if self.on_state_change is not None:
                    try:
                        self.on_state_change(False, "poll loop ended unexpectedly")
                    except Exception:
                        logger.debug("weixin on_state_change observer raised", exc_info=True)

    async def _poll_forever(self) -> None:
        failures = 0
        timeouts = 0
        while self._running:
            try:
                resp = await self._client.get_updates(self._sync_buf)
                if resp.pop("_timed_out", False):
                    # Zero server contact for a full client-deadline window.
                    # Warn ONCE per streak, at a threshold well past normal
                    # long-poll behavior, so this stays quiet whether the
                    # server answers early when idle or holds requests to the
                    # deadline — only a sustained black-hole crosses it.
                    timeouts += 1
                    if timeouts == 20:
                        logger.warning(
                            "weixin: %d consecutive long-poll timeouts (~%d min without "
                            "a single server response) — either the network path is dead "
                            "or the server is holding polls past the client deadline",
                            timeouts,
                            timeouts * 35 // 60,
                        )
                else:
                    timeouts = 0
                # iLink reports failure as `errcode` on some endpoints and `ret`
                # on others (getupdates uses `ret`), both HTTP 200. Read BOTH via
                # the shared helper: inspecting only `errcode` let a `ret`-keyed
                # error skip every backoff branch and fall through to an
                # immediate re-poll — an unbounded hot loop that fires exactly
                # when the session expires or iLink is already rate-limiting us,
                # which risks getting the user's personal account limited.
                errcode = protocol_error_code(resp)
                if errcode == SESSION_EXPIRED_ERRCODE:
                    logger.warning(
                        "weixin: session expired (-14); pausing — re-login via Settings QR."
                    )
                    await asyncio.sleep(600)
                    continue
                if errcode == RATE_LIMIT_ERRCODE:
                    await asyncio.sleep(30)  # iLink frequency limit — back off
                    continue
                if errcode is not None:
                    # Unknown protocol error: still back off. Without this an
                    # unrecognized code would spin the same way the two known
                    # codes used to.
                    failures += 1
                    delay = 2 if failures < 3 else 30
                    logger.warning(
                        "weixin: poll returned error code %r (%d) — retrying in %ds",
                        errcode,
                        failures,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._sync_buf = resp.get("get_updates_buf", self._sync_buf)
                for msg in resp.get("msgs") or []:
                    await self.receive(msg)
                failures = 0
            except asyncio.CancelledError:
                # Propagate: cancellation is shutdown, and swallowing it here
                # would make teardown indistinguishable from loop death in
                # ``_poll_loop``'s unexpected-end detection.
                raise
            except Exception as exc:
                failures += 1
                delay = 2 if failures < 3 else 30
                logger.warning(
                    "weixin: poll error (%d): %s — retrying in %ds", failures, exc, delay
                )
                await asyncio.sleep(delay)

    # -- Inbound adapter -------------------------------------------------------
    def authorize(self, msg: InboundMessage) -> bool:
        """Deny-by-default DM authorization.

        Only the three known policies are honored, and each branch is explicit:
        an unrecognized ``dm_policy`` (a typo in config.json) DENIES rather than
        falling through to ``open``, so a malformed value can never authorize
        arbitrary senders.
        """
        if self._dm_policy == "open":
            allowed = bool(msg.user_id)
        elif self._dm_policy == "allowlist":
            allowed = bool(msg.user_id) and msg.user_id in self._allowed
        elif self._dm_policy == "disabled":
            allowed = False
        else:
            logger.warning(
                "weixin: unknown dm_policy %r — denying (expected open/allowlist/disabled)",
                self._dm_policy,
            )
            allowed = False
        if not allowed:
            sel().log_api_access(
                caller=msg.user_id or "unknown",
                operation="weixin_transport.authorize",
                outcome="denied",
                source="weixin",
            )
        return allowed

    def _dedup(self, msg_id: str) -> bool:
        """True if already seen within the TTL window (drop duplicates)."""
        now = time.time()
        # opportunistic prune
        for k, ts in list(self._seen.items()):
            if now - ts >= MESSAGE_DEDUP_TTL_SECONDS:
                self._seen.pop(k, None)
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False

    async def receive(self, raw_envelope: Any) -> None:
        """Normalize one iLink message dict -> authorize -> dispatch."""
        if not isinstance(raw_envelope, dict):
            return
        from_user = str(raw_envelope.get("from_user_id") or "").strip()
        if not from_user:
            return
        msg_id = str(raw_envelope.get("msg_id") or raw_envelope.get("client_id") or "")
        if msg_id and self._dedup(msg_id):
            return

        # Text only for now — media (voice/file/image/video) is not supported yet.
        text = ""
        for item in raw_envelope.get("item_list") or []:
            if item.get("type") == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                break
        if not text:
            return

        msg = InboundMessage(
            channel_type="weixin",
            user_id=from_user,
            conversation_id=from_user,  # DM: peer id is the conversation
            text=text,
        )
        # Authorize FIRST: an unallowlisted stranger must not be able to mutate
        # any server-side state, so the reply-context write happens only after
        # the sender is accepted. Otherwise anyone who can message the bot could
        # grow that file at will.
        if not self.authorize(msg):
            return
        self._known_users.add(from_user)

        # Persist the peer's latest context_token for reply continuity. The store
        # rewrites + atomically replaces a JSON file, so it runs OFF the loop —
        # this executes inside the long-poll loop and a slow (or grown) store
        # would otherwise stall every other gateway task.
        ctx_token = raw_envelope.get("context_token")
        if isinstance(ctx_token, str) and ctx_token:
            await asyncio.to_thread(self._ctx.set, self._account_id, from_user, ctx_token)

        if self._dispatch is not None:
            await self._dispatch(msg)
