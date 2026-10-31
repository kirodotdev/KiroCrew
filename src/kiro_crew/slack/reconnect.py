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
token's workspace is pinned (at boot whenever boot published a Web API
client, or by a connected reconnect), a saved bot token for a different
workspace -- or a pinned workspace that could not be confirmed -- keeps Slack
off with ``workspace_switch_needs_restart``. A boot that published no client
(a leftover bot token without its app token) pins nothing: no work can be
bound to a workspace no client was ever built for.

While a reconnect runs (``_slack_reconnecting``) and no client is published,
the Slack queue drains hold their entries instead of dispatching them into
the cleared client (see ``events_queue.hold_if_slack_down``); a connected
reconnect starts them again. A reconnect that retires a published client and
fails keeps that hold (``_slack_client_lost``) for the next reconnect, the
retry, for :data:`HELD_WORK_GIVE_UP_SECS`. When that window ends with no
client, the hold gives up: each held message is taken out of its queue and
the owner gets a dashboard notification for it
(``events_queue.give_up_held_slack_work``). An inbound message with
attachments waits for the attempt to finish (:func:`wait_for_reconnect`) so
its files are fetched with the client the reconnect publishes. A gateway no
reconnect took a client from handles a missing client as it always has.

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


#: Slack errors that say a bot token never authenticated to any workspace. A
#: revoked, expired or deactivated token did authenticate once, so work may be
#: bound to its workspace and those errors keep the pin.
_NEVER_AUTHENTICATED = frozenset({"invalid_auth", "not_authed"})


def _slack_work_admitted(orch: GatewayOrchestrator) -> bool:
    """True when Slack work may already be bound to the boot workspace.

    A connected socket delivers inbound messages, and a queued, held or
    running turn names that workspace's channels. Each one keeps the pin.
    """
    from kiro_crew.slack.events_queue import _held_while_slack_down

    state = orch.dashboard_state
    if state is not None and getattr(state, "slack_socket_connected", False) is True:
        return True
    for name in ("_pending_queue", "_session_tasks"):
        value = getattr(orch, name, None)
        if isinstance(value, dict) and value:
            return True
    return bool(_held_while_slack_down.get(orch))


def reconnect_in_progress(orch: Any) -> bool:
    """True while a reconnect runs: between its start and its recorded outcome."""
    return getattr(orch, "_slack_reconnecting", False) is True


#: How long Slack work admitted before a failed reconnect stays held for the
#: next reconnect (the retry) before the hold gives up and notifies.
HELD_WORK_GIVE_UP_SECS = 300.0


def client_lost_to_reconnect(orch: Any) -> bool:
    """True after a failed reconnect retired a published client, until Slack is back.

    Set when a reconnect closes a published client and ends without a new
    one; cleared when a reconnect publishes a client or the give-up window
    ends (:func:`_give_up_after`). A gateway that never published a client,
    or one no reconnect touched, reads False.
    """
    return getattr(orch, "_slack_client_lost", False) is True


def _cancel_give_up(orch: Any) -> None:
    task = getattr(orch, "_slack_give_up_task", None)
    orch._slack_give_up_task = None
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()


async def _give_up_after(orch: GatewayOrchestrator, delay: float) -> None:
    """End the held-work window *delay* seconds after a failed reconnect."""
    from kiro_crew.slack.events_queue import give_up_held_slack_work

    await asyncio.sleep(delay)
    if orch.slack is not None or reconnect_in_progress(orch):
        return
    orch._slack_give_up_task = None
    orch._slack_client_lost = False
    noticed = give_up_held_slack_work(orch)
    logger.warning(
        "Slack did not reconnect within %.0fs; %d held message(s) were not delivered "
        "and the owner was notified",
        delay,
        noticed,
    )


def _schedule_give_up(orch: GatewayOrchestrator) -> None:
    _cancel_give_up(orch)
    task = asyncio.create_task(
        _give_up_after(orch, HELD_WORK_GIVE_UP_SECS), name="slack-held-work-give-up"
    )
    orch._slack_give_up_task = task
    background = getattr(orch, "_background_tasks", None)
    if isinstance(background, set):
        background.add(task)
        task.add_done_callback(background.discard)


async def wait_for_reconnect(orch: Any) -> None:
    """Wait for the reconnect in progress to finish, without cancelling it.

    Returns at once when none runs. The attempt's own outcome (connected or
    not) is read from ``orch.slack`` afterwards; its errors are not raised here.
    """
    task = getattr(orch, "_slack_reconnect_task", None)
    if not reconnect_in_progress(orch) or not isinstance(task, asyncio.Task) or task.done():
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Slack reconnect failed while a message waited for it", exc_info=True)


def pin_boot_workspace(orch: GatewayOrchestrator) -> asyncio.Task[None] | None:
    """Pin the workspace of the Web API client boot published, if it published one.

    Boot publishes ``orch.slack`` before (and whether or not) the socket
    connects, so work can bind to its workspace either way. A boot that built
    no client (``orch.slack is None``: a bot token without its app token, say)
    pins nothing, so a later reconnect may adopt the workspace the user saves.
    Returns the probe task that names the pinned workspace, or ``None``.
    The dashboard's copy counts too: boot publishes ``dashboard_state.slack_client``
    before ``init_socket_mode``, which can clear ``orch.slack`` alone (an
    enterprise or owner check that fails), and dashboard senders keep posting
    through that copy.
    """
    state = orch.dashboard_state
    published = orch.slack is not None or (
        state is not None and getattr(state, "slack_client", None) is not None
    )
    if not published:
        return None
    orch._slack_workspace_pinned = True
    return asyncio.create_task(record_connected_workspace(orch))


async def record_connected_workspace(orch: GatewayOrchestrator) -> None:
    """Name the workspace boot's Slack clients belong to (best effort).

    Run at boot by ``pin_boot_workspace``, with ``_slack_workspace_pinned``
    already set: boot publishes that token's Web
    API client whether or not the socket connects. The pin is released only
    when Slack says the token never authenticated (``invalid_auth``) and no
    Slack work was admitted (see :func:`_slack_work_admitted`). Any other
    failure -- a revoked token included -- leaves the workspace pinned and
    unconfirmed, and a later reconnect refuses to guess and asks for a
    restart instead.
    """
    try:
        orch._slack_team_id = await probe_workspace(orch._bot_token)
    except Exception as exc:
        if _slack_error_code(exc) in _NEVER_AUTHENTICATED and not _slack_work_admitted(orch):
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
            # propagate=False: this post-boot read must not write a credential
            # back into os.environ, where it could undo a removal the live
            # config watcher applies concurrently.
            creds = dict(await asyncio.to_thread(cfg.load_credentials, propagate=False))
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
    # The reconnect window: queue drains and inbound routes that find no
    # published client hold their work while it is open (see
    # ``events_queue.hold_if_slack_down`` and ``wait_for_reconnect``).
    orch._slack_reconnecting = True
    # This attempt is the retry a pending give-up waits for; it schedules a
    # fresh one if it ends without a client.
    _cancel_give_up(orch)
    try:
        creds = await _read_credentials()
        state = orch.dashboard_state
        if orch.slack is not None or (
            state is not None and getattr(state, "slack_client", None) is not None
        ):
            # Work admitted through this client stays held until Slack is back
            # or the give-up window ends (see ``client_lost_to_reconnect``).
            orch._slack_client_lost = True
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
    finally:
        orch._slack_reconnecting = False
        if orch.slack is None and client_lost_to_reconnect(orch):
            _schedule_give_up(orch)


async def _bring_up(orch: GatewayOrchestrator) -> dict[str, Any]:
    from kiro_crew.slack.client import RealSlackClient
    from kiro_crew.slack.events import SeenCache, init_socket_mode
    from kiro_crew.slack.events_queue import resume_held_slack_drains
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
    orch._slack_client_lost = False
    orch._slack_workspace_pinned = True
    orch._slack_team_id = team_id
    result = _record(orch, True, "")
    logger.info("Slack reconnected in place (workspace %s)", team_id)
    # Queue drains that ran while the socket was down held their entries;
    # start them now that a client is published again.
    resume_held_slack_drains(orch)
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
