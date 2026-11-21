"""Slack owner claim: an ownerless gateway learns its owner from a DM confirmed locally.

The person clicks "Claim in Slack" on the direct-local Settings page. That opens
a claim window (:data:`WINDOW_SECS`) and, only then, connects a Socket Mode
client of its own. The person sends the bot any direct message; the window
lists the sender as a candidate, with the text and the time it arrived. The
person clicks Confirm on the local page, which writes ``KIROCREW_OWNER_ID`` and
runs the ordinary Reconnect, which accepts a first owner.

While the window is open the gateway admits nothing and sends nothing:

- the claim client is private to this module. ``orch.slack``,
  ``orch._socket_client`` and the dashboard's ``slack_client`` stay unset, so
  no outbound path has a client to post through;
- its listener acknowledges every envelope and hands ``message`` events to
  :meth:`OwnerClaim.offer`, which never routes, replies or reacts. Slash
  commands, interactions and Home tab events are acknowledged and dropped.

So the premise behind Reconnect's first-owner rule (a gateway with no owner yet
has admitted nothing) still holds after a window. The window closes on expiry,
Cancel, Confirm and any Slack credential write; a close that is not a confirm
leaves the gateway in the ownerless state it started in. Window state lives in
memory only: a restart drops it.

Design: ``docs/request-for-change/rfc-slack-owner-claim.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from kiro_crew.config.loader import CRED_OWNER_ID, CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN
from kiro_crew.http_proxy import SLACK_API_URL, resolve_proxy
from kiro_crew.sel import sel
from kiro_crew.validation import USER_ID_RE

logger = logging.getLogger(__name__)

#: How long a claim window stays open.
WINDOW_SECS = 600.0
#: Distinct senders kept as candidates: the first 3 stay, a further sender is ignored.
MAX_CANDIDATES = 3
#: Characters of a candidate's message kept for the confirm row.
TEXT_LIMIT = 300
#: Distinct senders whose ``users.info`` answer is cached per window.
# shortcut: a sender past this bound is ignored; raise it if a real workspace
# ever needs more distinct senders inside one 10-minute window.
_USER_CACHE_LIMIT = 256
_SLACK_TIMEOUT_SECS = 10.0
_CLOSE_TIMEOUT_SECS = 2.0
#: ``users.info`` flags that rule a sender out.
_REFUSED_USER_FLAGS = (
    "deleted",
    "is_bot",
    "is_app_user",
    "is_restricted",
    "is_ultra_restricted",
    "is_stranger",
)


def _persisted_slack_credentials(env_text: str) -> dict[str, str]:
    """Bot token, app token and owner as *env_text* (a ``.env`` body) saves them.

    ``secret://`` references are resolved from the vault; one that does not
    resolve reads as ``""``, which refuses a confirm. A key the file does not
    hold falls back to the process environment, where a deployment may set it.
    The process environment never overrides a saved line here: it lags a CLI
    save until the config watcher's next poll.
    """
    from kiro_crew.config.loader import _env_name_key
    from kiro_crew.config.loader_credentials import (
        _effective_entry_values,
        parse_env_lines,
        resolve_env_credentials,
    )

    keys = (CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN, CRED_OWNER_ID)
    # Each entry as boot would propagate it (on Windows a lowercase spelling of
    # a key is the same entry), then the process value for an entry the file
    # lacks, then one resolution of the whole map, references from either.
    saved = _effective_entry_values(parse_env_lines(env_text))
    merged = {k: saved.get(_env_name_key(k)) or os.environ.get(k, "") for k in keys}
    resolved = resolve_env_credentials({k: v for k, v in merged.items() if v})
    return {k: resolved.get(k, "") for k in keys}


def _saved_state(env_text: str) -> tuple[bool, str, str]:
    """``(owner saved, pin, token-value digest)`` for *env_text*.

    The pin is what confirm and every re-check compare with start; the
    token-value digest alone is what start compares with the tokens it
    connects with.

    The owner counts as saved when its line holds anything, even a reference
    that does not resolve. The digest also records, per token, whether the
    file or the process environment supplied it, so removing a saved line is
    a change even while the process still holds the old value.
    Blocking (vault IO): call off the event loop.
    """
    from kiro_crew.config.loader import _env_name_key
    from kiro_crew.config.loader_credentials import _effective_entry_values, parse_env_lines

    raw = _effective_entry_values(parse_env_lines(env_text))
    creds = _persisted_slack_credentials(env_text)
    owner_saved = bool(raw.get(_env_name_key(CRED_OWNER_ID)) or creds[CRED_OWNER_ID])
    sources = "".join(
        "f" if raw.get(_env_name_key(k)) else "e"
        for k in (CRED_SLACK_BOT_TOKEN, CRED_SLACK_APP_TOKEN)
    )
    digest = _token_digest(creds[CRED_SLACK_BOT_TOKEN], creds[CRED_SLACK_APP_TOKEN])
    return owner_saved, f"{sources}:{digest}", digest


def _saved_env_state() -> tuple[bool, str, str]:
    """:func:`_saved_state` of the ``.env`` on disk. Blocking IO."""
    return _saved_state(_saved_env_text())


def _saved_env_text() -> str:
    """The ``.env`` body, ``""`` when it is absent or cannot be read. Blocking IO."""
    from kiro_crew.config.loader import env_path, read_env_text

    try:
        return read_env_text(env_path(), encoding="utf-8")
    except Exception:
        # Confirm's writer refuses a file it cannot read, so this only decides
        # which digest a later confirm is compared against.
        return ""


class ClaimRefused(Exception):
    """A claim request that cannot go ahead; *code* names why, *status* is the HTTP answer."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class Candidate:
    """One sender offered for confirmation. Held in memory, shown only on the direct-local GET."""

    user_id: str
    display_name: str
    handle: str
    avatar: str
    team_name: str
    text: str
    received_at: float


def member_hash(user_id: str) -> str:
    """The audit form of a member ID: a short SHA-256 digest, never the ID itself."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _token_digest(bot: str, app: str) -> str:
    return hashlib.sha256(f"{bot}\0{app}".encode()).hexdigest()


def _audit(event: str, *, outcome: str = "ok", user_id: str = "", reason: str = "") -> None:
    resources = event
    if user_id:
        resources += f" member={member_hash(user_id)}"
    if reason:
        resources += f" reason={reason}"
    sel().log_api_access(
        caller="dashboard",
        operation="slack.owner_claim",
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


#: Lengths a cached ``users.info`` field is cut to: the cache keeps only what
#: the filters and the candidate row read, each value bounded.
_ID_LIMIT = 32
_NAME_LIMIT = 80
_AVATAR_LIMIT = 512


def _https(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        return ""
    return value if len(value) <= _AVATAR_LIMIT else ""


def _cut(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _trimmed_user(raw: dict[str, Any]) -> dict[str, Any]:
    """The bounded part of a ``users.info`` user record that the claim reads."""
    raw_profile, raw_grid = raw.get("profile"), raw.get("enterprise_user")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    grid: dict[str, Any] = raw_grid if isinstance(raw_grid, dict) else {}
    out: dict[str, Any] = {flag: bool(raw.get(flag)) for flag in _REFUSED_USER_FLAGS}
    out["team_id"] = _cut(raw.get("team_id"), _ID_LIMIT)
    out["enterprise_user"] = {"enterprise_id": _cut(grid.get("enterprise_id"), _ID_LIMIT)}
    out["name"] = _cut(raw.get("name"), _NAME_LIMIT)
    out["profile"] = {
        "display_name": _cut(profile.get("display_name"), _NAME_LIMIT),
        "real_name": _cut(profile.get("real_name"), _NAME_LIMIT),
        "image_72": _https(profile.get("image_72")),
    }
    return out


class OwnerClaim:
    """The claim window of one gateway. Built at boot; inert until :meth:`start`."""

    def __init__(self, orch: Any, *, window_secs: float = WINDOW_SECS) -> None:
        self._orch = orch
        self._window_secs = window_secs
        self._lock = asyncio.Lock()
        self._socket: Any = None
        self._web: Any = None
        self._team = ""
        self._enterprise = ""
        self._team_name = ""
        self._bot_name = ""
        self._tokens = ""
        self._expires_at = 0.0
        self._expiry: asyncio.Task[None] | None = None
        self._candidates: OrderedDict[str, Candidate] = OrderedDict()
        self._users: dict[str, dict[str, Any] | None] = {}
        self._ignored = 0
        # The owner this process confirmed, for the "Owner: @handle" row.
        self._confirmed: Candidate | None = None

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    def snapshot(self) -> dict[str, Any]:
        """The direct-local GET answer. Never put into any other surface."""
        confirmed = self._confirmed
        return {
            "open": self.is_open,
            "expires_at": self._expires_at if self.is_open else 0,
            "team_name": self._team_name if self.is_open else "",
            "bot_name": self._bot_name if self.is_open else "",
            "candidates": [asdict(c) for c in reversed(self._candidates.values())],
            "multiple": len(self._candidates) > 1,
            "ignored": self._ignored,
            "confirmed": (
                {
                    "user_id": confirmed.user_id,
                    "handle": confirmed.handle,
                    "team_name": confirmed.team_name,
                }
                if confirmed is not None
                else None
            ),
        }

    # ── opening ──────────────────────────────────────────────────────────────

    async def start(self) -> dict[str, Any]:
        """Open the window and connect in claim mode. Raises :class:`ClaimRefused`.

        Raises ``CredentialStoreUnreadable`` (from the reconnect module) when
        the saved credentials cannot be read.
        """
        from kiro_crew.dashboard.handlers.agents import _get_config_lock
        from kiro_crew.slack.reconnect import _read_credentials

        if self.is_open:
            raise ClaimRefused("window_open")
        # Read first: it takes the config lock itself. Tokens saved after this
        # read differ from the saved state read below, which refuses the start.
        creds = await _read_credentials()
        # Then the config lock, then the window lock, as confirm takes them:
        # the enterprise validation below reads the allowlist, and must not
        # read a Settings save's half-committed config that the save then
        # rolls back (the same reason Reconnect validates under this lock).
        async with _get_config_lock(), self._lock:
            if self.is_open:
                raise ClaimRefused("window_open")
            # Either owner refuses: the two differ after a manual Save that no
            # Reconnect has applied yet.
            if self._orch._owner_id or creds.get(CRED_OWNER_ID, ""):
                raise ClaimRefused("owner_set")
            bot = creds.get(CRED_SLACK_BOT_TOKEN, "")
            app = creds.get(CRED_SLACK_APP_TOKEN, "")
            if not (bot and app):
                raise ClaimRefused("tokens_missing", 400)
            # Read the way confirm's check reads them (the saved line first),
            # not the way the connect does (the process environment first), so
            # a host that exports a token beside a different .env line can
            # still confirm. Only a later change to what is saved refuses.
            # Vault IO, so the whole read runs off the loop.
            owner_saved, saved_tokens, saved_values = await asyncio.to_thread(_saved_env_state)
            if owner_saved:
                raise ClaimRefused("owner_set")
            # The client connects with the tokens the gateway would use (the
            # process environment first); the pin protects the saved ones. They
            # must be the same tokens, or a confirm could save a member of one
            # workspace beside the tokens of another: an exported token that
            # differs from its .env line, or a CLI save between the two reads.
            if saved_values != _token_digest(bot, app):
                raise ClaimRefused("credentials_mismatch", 400)
            web = self._web_client(bot)
            auth = await self._auth_test(web)
            team = auth.get("team_id")
            if not isinstance(team, str) or not team.startswith("T"):
                # An org-wide Enterprise Grid install names an E… org instead.
                raise ClaimRefused("org_install_refused", 400)
            if not await self._enterprise_permits(bot):
                raise ClaimRefused("enterprise_validation_failed", 400)
            if not await self._transport_permitted():
                raise ClaimRefused("denied_by_policy", 400)
            socket = self._socket_client(app, web)
            socket.socket_mode_request_listeners.append(self._on_request)
            try:
                # Bounded: Cancel and every close wait on this lock.
                await asyncio.wait_for(socket.connect(), timeout=_SLACK_TIMEOUT_SECS)
            except Exception as exc:
                await self._close_client(socket)
                code = str(getattr(exc, "status_reason", "") or type(exc).__name__)[:120]
                raise ClaimRefused(code, 502) from exc
            self._web = web
            self._socket = socket
            self._team = team
            enterprise = auth.get("enterprise_id")
            self._enterprise = enterprise if isinstance(enterprise, str) else ""
            team_name = auth.get("team")
            self._team_name = _cut(team_name, _NAME_LIMIT)
            # The bot's own Slack name, so the page can say whom to DM.
            self._bot_name = _cut(auth.get("user"), _NAME_LIMIT)
            self._tokens = saved_tokens
            self._candidates.clear()
            self._users.clear()
            self._ignored = 0
            self._expires_at = time.time() + self._window_secs
            self._expiry = asyncio.create_task(self._expire(), name="slack-owner-claim-expiry")
        logger.info("Slack owner claim window opened for %.0fs", self._window_secs)
        _audit("claim_window_opened")
        return self.snapshot()

    def _web_client(self, bot: str) -> Any:
        from slack_sdk.web.async_client import AsyncWebClient

        return AsyncWebClient(
            token=bot,
            timeout=int(_SLACK_TIMEOUT_SECS),
            proxy=resolve_proxy(SLACK_API_URL),
        )

    def _socket_client(self, app: str, web: Any) -> Any:
        from kiro_crew.slack.socket_mode_proxy import ProxySelectingSocketModeClient

        return ProxySelectingSocketModeClient(app_token=app, web_client=web)

    async def _auth_test(self, web: Any) -> dict[str, Any]:
        from kiro_crew.slack.reconnect import _slack_error_code

        try:
            resp = await asyncio.wait_for(web.auth_test(), timeout=_SLACK_TIMEOUT_SECS)
        except Exception as exc:
            raise ClaimRefused(_slack_error_code(exc), 400) from exc
        return dict(resp.data) if hasattr(resp, "data") else dict(resp or {})

    async def _enterprise_permits(self, bot: str) -> bool:
        # The workspace gate boot runs before it connects (fail-closed where an
        # allowlist is configured); a blocking auth.test, so off the loop.
        from kiro_crew.platform import current_context

        validate = current_context().slack_gate.validate_enterprise
        extra = self._orch._cfg.slack_enterprise_ids
        return bool(await asyncio.to_thread(validate, bot, extra_ids=extra))

    async def _transport_permitted(self) -> bool:
        from kiro_crew.slack.gateway import _channel_transport_permitted

        return bool(await asyncio.to_thread(_channel_transport_permitted, "slack"))

    # ── closing ──────────────────────────────────────────────────────────────

    async def _expire(self) -> None:
        await asyncio.sleep(self._window_secs)
        await self.close("expired")

    async def close(self, reason: str) -> bool:
        """Close an open window and disconnect its client. False when none was open."""
        async with self._lock:
            return await self._close_locked(reason)

    async def _close_locked(self, reason: str) -> bool:
        """:meth:`close` for a caller already holding the window lock."""
        socket = self._socket
        if socket is None:
            return False
        self._socket = None
        self._web = None
        self._expires_at = 0.0
        self._candidates.clear()
        self._users.clear()
        expiry, self._expiry = self._expiry, None
        if expiry is not None and expiry is not asyncio.current_task():
            expiry.cancel()
        await self._close_client(socket)
        logger.info("Slack owner claim window closed (%s)", reason)
        _audit("claim_closed", reason=reason)
        return True

    def recheck_soon(self, reason: str) -> None:
        """After a Slack credential write: close the window if what is saved differs from its pin.

        Called from code that cannot await it (a save, the config watcher), so
        it schedules :meth:`recheck`. Scheduled even when no window is open
        yet: a ``start()`` that holds the window lock while it connects is
        re-checked as soon as it finishes.
        """
        task = asyncio.get_running_loop().create_task(self.recheck(reason))
        background = getattr(self._orch, "_background_tasks", None)
        if isinstance(background, set):
            background.add(task)
            task.add_done_callback(background.discard)

    async def recheck(self, reason: str) -> bool:
        """Close an open window whose saved owner or tokens changed since it opened.

        A write that saved the same tokens again (the watcher's late notice of
        the very save the window opened after) leaves it open. True when this
        call closed it.
        """
        async with self._lock:
            if not self.is_open:
                return False
            owner_saved, tokens, _values = await asyncio.to_thread(_saved_env_state)
            if not owner_saved and tokens == self._tokens:
                return False
            return await self._close_locked(reason)

    async def _close_client(self, socket: Any) -> None:
        try:
            await asyncio.wait_for(socket.close(), timeout=_CLOSE_TIMEOUT_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Closing the Slack owner claim client failed", exc_info=True)

    # ── inbound ──────────────────────────────────────────────────────────────

    async def _on_request(self, client: Any, req: Any) -> None:
        """The claim client's only listener: acknowledge, then offer a message event."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            return
        if req.type != "events_api":
            return
        payload = req.payload or {}
        event = payload.get("event") or {}
        if isinstance(event, dict) and event.get("type") in ("message", "app_mention"):
            await self.offer(payload, event)

    async def offer(self, payload: dict[str, Any], event: dict[str, Any]) -> bool:
        """Keep *event*'s sender as a candidate when every check holds. Never replies."""
        if await self._admissible(payload, event):
            return True
        if self.is_open:
            self._ignored += 1
        return False

    async def _admissible(self, payload: dict[str, Any], event: dict[str, Any]) -> bool:
        from kiro_crew.platform import current_context

        if not self.is_open:
            return False
        if event.get("type") != "message" or event.get("channel_type") != "im":
            return False
        if event.get("bot_id") or event.get("subtype"):
            return False
        if payload.get("team_id") != self._team:
            return False
        if not current_context().slack_gate.check_message_origin(self._team):
            return False
        user = event.get("user")
        if not isinstance(user, str) or not USER_ID_RE.match(user):
            return False
        info = await self._user_info(user)
        if info is None or not self.is_open:
            return False
        if any(info.get(flag) for flag in _REFUSED_USER_FLAGS):
            return False
        if not self._same_team(event.get("user_team"), info):
            return False
        raw_profile = info.get("profile")
        profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
        name = profile.get("display_name") or profile.get("real_name") or info.get("name") or ""
        text = event.get("text")
        if user not in self._candidates and len(self._candidates) >= MAX_CANDIDATES:
            return False
        # A sender already listed keeps their place; only the row's content
        # (the latest message and its time) changes.
        self._candidates[user] = Candidate(
            user_id=user,
            display_name=str(name)[:80],
            handle=str(info.get("name") or "")[:80],
            avatar=_https(profile.get("image_72")),
            team_name=self._team_name,
            text=(text if isinstance(text, str) else "")[:TEXT_LIMIT],
            received_at=time.time(),
        )
        _audit("candidate_seen", user_id=user)
        return True

    def _same_team(self, user_team: Any, info: dict[str, Any]) -> bool:
        """Fail closed: at least one team field is present, and every present one matches."""
        checks: list[bool] = []
        if user_team:
            checks.append(user_team in (self._team, self._enterprise or self._team))
        info_team = info.get("team_id")
        if info_team:
            grid = info.get("enterprise_user")
            grid_org = grid.get("enterprise_id") if isinstance(grid, dict) else ""
            if self._enterprise and grid_org:
                checks.append(grid_org == self._enterprise)
            else:
                checks.append(info_team == self._team)
        return bool(checks) and all(checks)

    async def _user_info(self, user: str) -> dict[str, Any] | None:
        if user in self._users:
            return self._users[user]
        if len(self._users) >= _USER_CACHE_LIMIT or self._web is None:
            return None
        info: dict[str, Any] | None
        try:
            resp = await asyncio.wait_for(
                self._web.users_info(user=user), timeout=_SLACK_TIMEOUT_SECS
            )
            raw = resp.get("user") if resp is not None else None
            info = _trimmed_user(raw) if isinstance(raw, dict) else None
        except Exception:
            logger.debug("users.info failed for a Slack owner claim sender", exc_info=True)
            info = None
        self._users[user] = info
        return info

    # ── confirming ───────────────────────────────────────────────────────────

    async def confirm(
        self,
        user_id: str,
        *,
        write_owner: Callable[[str, Callable[[str], None]], Awaitable[None]],
        reconnect: Callable[[], Awaitable[dict[str, Any]]] | None,
    ) -> dict[str, Any]:
        """Write *user_id* as the owner, close the window and reconnect.

        *write_owner* writes ``KIROCREW_OWNER_ID`` through the dashboard's
        credential writer, passing it a check that the writer runs on the
        ``.env`` text under the cross-process ``.env`` lock. The config lock
        every Slack save holds is taken first, so a Settings save cannot slip
        between either.
        """
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        if not self.is_open:
            raise ClaimRefused("no_window")
        candidate = self._candidates.get(user_id)
        if candidate is None:
            raise ClaimRefused("not_a_candidate", 404)
        # Lock order: config lock, then window lock. Nothing holding the window
        # lock waits for the config lock (start reads credentials before it).
        # The window lock makes the checks, the write and the close one step,
        # so a Cancel either lands before it (and this answers no_window) or
        # after it (and finds no window to cancel).
        async with _get_config_lock(), self._lock:
            stale = None
            if not self.is_open:
                stale = "no_window"
            elif self._orch._owner_id:
                stale = "owner_set"
            else:
                try:
                    # Exactly as Slack sent it: is_owner cross-matches U/W, so a
                    # rewrite here would hide a mismatch instead of fixing it.
                    # The saved owner and tokens are compared inside the writer,
                    # under the cross-process .env lock, so a CLI save cannot
                    # land between the comparison and the write.
                    await write_owner(candidate.user_id, self._unchanged_since_start)
                except ClaimRefused as exc:
                    stale = exc.code
            await self._close_locked(stale or "confirmed")
        if stale is not None:
            raise ClaimRefused(stale)
        self._confirmed = candidate
        _audit("claim_confirmed", user_id=candidate.user_id)
        result: dict[str, Any] = {
            "connected": False,
            "connect_error": "slack_reconnect_unavailable",
        }
        if reconnect is not None:
            result = await reconnect()
        if result.get("connected"):
            await self._welcome(candidate.user_id)
        return {
            "owner_id": candidate.user_id,
            "connected": bool(result.get("connected")),
            "connect_error": str(result.get("connect_error", ""))[:120],
        }

    def _unchanged_since_start(self, env_text: str) -> None:
        """The writer's precondition, run on the ``.env`` text under its lock. Raises to refuse."""
        owner_saved, tokens, _values = _saved_state(env_text)
        if owner_saved:
            raise ClaimRefused("owner_set")
        if tokens != self._tokens:
            raise ClaimRefused("credentials_changed")

    async def _welcome(self, user_id: str) -> None:
        # One DM, so the app sits in the owner's DM list on desktop and mobile.
        slack = getattr(self._orch, "slack", None)
        if slack is None or self._orch._owner_id != user_id:
            return
        try:
            channel = await slack.open_dm(user_id)
            await slack.post_message(
                channel,
                "You are now the owner of this Kiro Crew. Send a message here to talk to your agents.",
            )
        except Exception:
            logger.warning("Could not send the Slack owner welcome message", exc_info=True)
