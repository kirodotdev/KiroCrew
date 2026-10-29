"""Reconnect Slack in place: re-read the saved credentials and redo the socket handshake.

Slack's tokens and owner ID are hoisted once, when the orchestrator is built.
A reconnect applies a credential saved from Settings by doing the Slack part
of a gateway restart and nothing else:

1. Read the credential store (off the loop, under the dashboard config lock
   the Slack save holds across its ``.env`` write, so a save in progress is
   read whole or not at all). A store that cannot be read -- including a
   ``secret://`` reference the vault could not resolve -- raises
   :class:`CredentialStoreUnreadable` and leaves the running connection alone.
2. Close the current socket client and stop publishing its Web API client.
   The saved credentials are what must be live: a connection built from the
   previous ones (a previous owner, say) does not stay up behind a failure.
3. Hoist the new tokens and owner, recompute ``_slack_enabled`` from them
   (``init_socket_mode`` returns early on a stale ``False``), and re-run
   ``init_socket_mode`` + ``_connect_slack`` on the gateway loop -- the
   socket client must be built on a thread with a running loop. The
   declared YOLO grant is a start-up step, so the reconnect skips it: an
   owner's ``!yolo off`` stays in force across a reconnect.
4. Record the outcome where ``GET /api/slack/config`` reads it.

A reconnect never switches Slack workspaces. Work that arrived through the
current workspace (queued turns, in-flight replies, persisted thread links)
names that workspace's channels; a restart drops the in-process part of it, a
reconnect would not. That work can exist as soon as a Slack Web API client
is published, which boot does before the socket connects. So once a bot
token's workspace is pinned (at boot whenever a bot token was present, or by
a connected reconnect), a saved bot token for a different workspace -- or a
pinned workspace that could not be confirmed -- keeps Slack off with
``workspace_switch_needs_restart``.

Concurrent callers share one attempt, so a double click never races two
handshakes, and a caller that goes away does not cancel the attempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from kiro_crew.config import live
from kiro_crew.config.loader import (
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    KiroCrewConfig,
)

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

#: Reasons a reconnect declines without asking Slack, named so the panel can
#: explain each one. Slack API errors (``invalid_auth``, ...) pass through as is.
TOKENS_MISSING = "tokens_missing"
OWNER_ID_MISSING = "owner_id_missing"
ENTERPRISE_VALIDATION_FAILED = "enterprise_validation_failed"
DENIED_BY_POLICY = "denied_by_policy"
WORKSPACE_SWITCH_NEEDS_RESTART = "workspace_switch_needs_restart"

#: Bound on closing the previous socket client: a hung close must not hold up
#: the new handshake (the shutdown path bounds the same close).
_CLOSE_TIMEOUT_SECS = 2.0
#: Bound on the ``auth.test`` that names the workspace a bot token belongs to.
_WORKSPACE_PROBE_TIMEOUT_SECS = 10.0


class CredentialStoreUnreadable(Exception):
    """The saved credentials could not be read; nothing was changed."""


def _slack_error_code(exc: BaseException) -> str:
    """Slack's own error code for *exc* (``invalid_auth``), else its class name."""
    reason = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            reason = str(resp.get("error", "") or "")
        except Exception:
            reason = ""
    return (reason or type(exc).__name__)[:120]


async def _team_of(client: Any) -> str:
    """The ``team_id`` the Web API *client* authenticates to. Raises on any failure."""
    resp = await asyncio.wait_for(client.auth_test(), timeout=_WORKSPACE_PROBE_TIMEOUT_SECS)
    team_id = resp.get("team_id", "") if resp is not None else ""
    if not isinstance(team_id, str) or not team_id:
        raise ValueError("auth.test returned no team_id")
    return team_id


async def probe_workspace(bot_token: str) -> str:
    """The ``team_id`` *bot_token* authenticates to. Raises on any failure."""
    from slack_sdk.web.async_client import AsyncWebClient

    return await _team_of(
        AsyncWebClient(token=bot_token, timeout=int(_WORKSPACE_PROBE_TIMEOUT_SECS))
    )


#: Slack errors that prove a bot token authenticates to no workspace at all,
#: so no work can have been bound through a client built from it.
_UNAUTHENTICATED = frozenset(
    {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"}
)


async def record_connected_workspace(orch: GatewayOrchestrator) -> None:
    """Name the workspace boot's Slack clients belong to (best effort).

    Run at boot whenever a bot token was present, with
    ``_slack_workspace_pinned`` already set: boot publishes that token's Web
    API client whether or not the socket connects. Slack proving the token
    belongs to no workspace releases the pin. Any other failure leaves the
    workspace unconfirmed, and a later reconnect refuses to guess and asks for
    a restart instead.
    """
    try:
        orch._slack_team_id = await probe_workspace(orch._bot_token)
    except Exception as exc:
        if _slack_error_code(exc) in _UNAUTHENTICATED:
            orch._slack_workspace_pinned = False
            return
        logger.warning("Could not confirm the Slack workspace", exc_info=True)


async def reconnect_slack(orch: GatewayOrchestrator) -> dict[str, Any]:
    """Re-run the Slack handshake from the saved credentials.

    Returns ``{"connected": bool, "connect_error": str}``. Raises
    :class:`CredentialStoreUnreadable` when the store cannot be read.
    """
    task = orch._slack_reconnect_task
    if task is None or task.done():
        task = asyncio.create_task(_reconnect(orch), name="slack-reconnect")
        # Retrieved here so an attempt whose callers all went away does not
        # log "exception was never retrieved" when it raises.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        orch._slack_reconnect_task = task
    # Shielded: a caller that goes away (closed tab, cancelled request) must
    # not cancel the attempt half-way between the teardown and the handshake.
    return await asyncio.shield(task)


_SLACK_CREDENTIAL_KEYS = (CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN, CRED_OWNER_ID)


def _unresolved_reference(missing: list[str]) -> bool:
    """True when a *missing* key was saved but did not resolve, or its source is unreadable.

    ``load_credentials`` drops a ``secret://`` reference the vault could not
    resolve, so it reads the same as a key never saved. This tells the two
    apart from the raw value, read with the loader's precedence: a non-empty
    process environment value wins over the ``.env`` line. Blocking file IO:
    call via ``asyncio.to_thread``.
    """
    from kiro_crew.config.loader_credentials import read_env_file_credential
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

    for key in missing:
        raw = os.environ.get(key) or ""
        if not raw:
            definitive: list[bool] = []
            raw = read_env_file_credential(key, definitive=definitive)
            if not all(definitive):
                return True
        if raw.startswith(SECRET_URI_PREFIX):
            return True
    return False


async def _read_credentials() -> dict[str, str]:
    # circular import: dashboard.handlers imports the gateway at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    async with _get_config_lock():
        try:
            cfg = live.snapshot() or await asyncio.to_thread(KiroCrewConfig.load)
            if cfg is None:
                raise ValueError("config did not load")
            creds = dict(await asyncio.to_thread(cfg.load_credentials))
            missing = [k for k in _SLACK_CREDENTIAL_KEYS if not creds.get(k)]
            if missing:
                unresolved: bool = await asyncio.to_thread(_unresolved_reference, missing)
                if unresolved:
                    raise ValueError("a saved Slack credential did not resolve")
            return creds
        except Exception as exc:
            raise CredentialStoreUnreadable(type(exc).__name__) from exc


async def _close_socket(orch: GatewayOrchestrator) -> None:
    """Retire the current socket client and its published Web API client."""
    old = orch._socket_client
    orch._socket_client = None
    orch.slack = None
    state = orch.dashboard_state
    if state is not None:
        state.slack_client = None
        state.slack_socket_connected = False
    if old is None:
        return
    try:
        await asyncio.wait_for(old.close(), timeout=_CLOSE_TIMEOUT_SECS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Closing the previous Slack socket client failed", exc_info=True)


def _hoist(orch: GatewayOrchestrator, creds: dict[str, str]) -> None:
    from kiro_crew.slack import handler as slack_handler

    orch._app_token = creds.get(CRED_SLACK_APP_TOKEN, "")
    orch._bot_token = creds.get(CRED_SLACK_BOT_TOKEN, "")
    orch._owner_id = creds.get(CRED_OWNER_ID, "")
    orch._allowed_users = {orch._owner_id} if orch._owner_id else set()
    orch._slack_enabled = bool(orch._app_token and orch._bot_token)
    orch._slack_connect_error = ""
    # The owner-only checks read these; init_socket_mode sets them again on
    # the way to a handshake, but a declined attempt must not leave the
    # previous owner authorized.
    slack_handler.set_owner_id(orch._owner_id)
    slack_handler.set_allowed_users(set(orch._allowed_users))
    if orch.dashboard_state is not None:
        # Owner notices and dashboard-sent DMs address this owner.
        orch.dashboard_state.owner_id = orch._owner_id


def _record(orch: GatewayOrchestrator, connected: bool, error: str) -> dict[str, Any]:
    orch._slack_connect_error = "" if connected else error
    state = orch.dashboard_state
    if state is not None:
        state.slack_socket_connected = connected
        state.slack_connect_error = orch._slack_connect_error
        # Published only behind a connected socket, so no dashboard sender can
        # use a workspace the gate did not admit.
        state.slack_client = orch.slack if connected else None
    return {"connected": connected, "connect_error": orch._slack_connect_error}


async def _reconnect(orch: GatewayOrchestrator) -> dict[str, Any]:
    creds = await _read_credentials()
    await _close_socket(orch)
    _hoist(orch, creds)
    try:
        return await _bring_up(orch)
    except Exception as exc:
        # Whatever failed, Slack ends down and says why; it is never left
        # half-built behind an error.
        logger.exception("Slack reconnect failed")
        await _close_socket(orch)
        orch._slack_enabled = False
        return _record(orch, False, type(exc).__name__)


async def _bring_up(orch: GatewayOrchestrator) -> dict[str, Any]:
    from kiro_crew.slack.client import RealSlackClient
    from kiro_crew.slack.events import SeenCache, init_socket_mode
    from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels

    if not orch._slack_enabled:
        return _record(orch, False, TOKENS_MISSING)
    if not orch._owner_id:
        orch._slack_enabled = False
        return _record(orch, False, OWNER_ID_MISSING)

    try:
        team_id = await probe_workspace(orch._bot_token)
    except Exception as exc:
        orch._slack_enabled = False
        return _record(orch, False, _slack_error_code(exc))
    if orch._slack_workspace_pinned and team_id != orch._slack_team_id:
        orch._slack_enabled = False
        logger.warning(
            "Slack reconnect refused: the saved Slack app is installed in workspace %s, "
            "this gateway connected to %s; restart the gateway to switch",
            team_id,
            orch._slack_team_id or "an unconfirmed workspace",
        )
        return _record(orch, False, WORKSPACE_SWITCH_NEEDS_RESTART)

    if orch._slack_seen is None:
        orch._slack_seen = SeenCache()
    # The Web API client stays unpublished (orch.slack is None since the
    # close) until the workspace gate and the transport policy both admit
    # it, so no sender can reach a workspace the gate has not checked.
    client = RealSlackClient(orch._bot_token)
    await init_socket_mode(orch, orch._slack_seen, declared_yolo=False)
    if orch._socket_client is None:
        orch.slack = None
        return _record(orch, False, ENTERPRISE_VALIDATION_FAILED)

    connected = await orch._connect_slack()
    if not connected:
        error = orch._slack_connect_error or DENIED_BY_POLICY
        await _close_socket(orch)
        return _record(orch, False, error)

    orch.slack = client
    orch._slack_workspace_pinned = True
    orch._slack_team_id = team_id
    result = _record(orch, True, "")
    logger.info("Slack reconnected in place (workspace %s)", team_id)
    if orch.slack is not None and orch._tracking_channels:
        # The same tracked-channel readability probe boot runs after connecting.
        probe = asyncio.create_task(
            warn_unreadable_tracked_channels(
                orch.slack,
                set(orch._tracking_channels),
                notify=orch.dashboard_state.notify if orch.dashboard_state else None,
            )
        )
        orch._background_tasks.add(probe)
        probe.add_done_callback(orch._background_tasks.discard)
        probe.add_done_callback(log_probe_failure)
    return result
