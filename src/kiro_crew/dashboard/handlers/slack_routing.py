"""Brokered Slack channel routing writes for edition-supplied apps.

An app (the internal recipes app) performs its own Slack calls with the bot
token it reads from the credential store; it owns channel create, archive,
invite, and purpose. The one thing it cannot do from its own process is reach
into the running gateway to make a routing change take effect, so core exposes
exactly one operation:

  ``PUT /api/slack/channels/{channel_id}/routing``
      Write (or remove) ``slack.channels[channel_id]`` agent / activation, then
      refresh the in-memory routing table.

Why this and nothing more: a ``config.json`` read-modify-write must hold BOTH
the sidecar advisory flock and the loop-side asyncio lock (see
``chat_utils.run_config_write``), so a second process writing the file itself
would interleave with the gateway's own writers and revert them from a stale
snapshot. Funneling the write through the gateway keeps a single writer and
removes the race. Channel lifecycle (create / archive) is the app's job, not
core's, so there is no provisioning or destruction endpoint here.

The write itself is NOT implemented here. It delegates to
``slack.handler._persist_channel_config`` through ``run_config_write``, which is
the repo's required path for a new ``config.json`` mutation: it takes both locks
and fails closed on an unreadable config rather than rewriting a ``{}`` baseline
over your other settings.

Provenance is deliberately not recorded in ``config.json``. An earlier revision
stamped an ``_owner`` block into the channel entry, but ``ChannelConfig`` carries
only ``activation`` / ``agent`` / ``thread_follow``, so the first typed
``KiroCrewConfig.save()`` by any unrelated writer silently dropped it. An
installing app already knows which channels it created and is the durable record
of that; core does not pretend to keep a second one.

This is an ordinary dashboard endpoint, so an app reaches it with its app-scoped
token (``POST /api/apps/{name}/token``) and must declare the path in its manifest
``permissions.api`` allowlist, which ``token_auth`` enforces.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
)
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.sel import sel as _sel_impl

# Imported as a module, not ``from ... import _persist_channel_config``: the
# writer is resolved as an attribute at call time, which keeps the delegation
# patchable in tests. Binding the bare name here would silently make the
# regression test that proves this endpoint delegates pass vacuously.
from kiro_crew.slack import handler as slack_handler

logger = logging.getLogger(__name__)

#: Activation modes ``ChannelConfig`` accepts.
_ACTIVATION_VALUES = frozenset(
    {
        ACTIVATION_ALWAYS,
        ACTIVATION_MENTION,
        ACTIVATION_OBSERVE,
        ACTIVATION_REVIEW,
        ACTIVATION_OFF,
    }
)

#: Slack channel ids are ``C``/``G`` followed by uppercase alphanumerics. Bounded
#: and anchored so a path parameter cannot smuggle a traversal or an absurd value
#: into the config document we are about to write.
_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{2,31}$")


def _caller(request: web.Request) -> str:
    """Best-effort caller identity for the audit log.

    ``request["app"]`` is the verified app identity set by ``token_auth`` when
    the call arrived on an app-scoped token; fall back to the generic dashboard
    caller so a browser-driven call is still attributable.
    """
    app_name = request.get("app") or ""
    return f"app:{app_name}" if app_name else "dashboard"


def _sel() -> Any:
    return _sel_impl()


# ---------------------------------------------------------------------------
# PUT /api/slack/channels/{channel_id}/routing
# ---------------------------------------------------------------------------


async def api_slack_channel_routing_put(request: web.Request) -> web.Response:
    """Write or remove a channel's routing, then refresh the gateway.

    Body (set):    ``{"agent": str?, "activation": str?}``
    Body (remove): ``{"remove": true}`` -- deletes ``slack.channels[channel_id]``
    Returns ``{"ok": True, "changed": [str], "routing_refreshed": bool}``.
    """
    caller = _caller(request)
    channel_id = str(request.match_info.get("channel_id", "")).strip()

    def _audit_refusal(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="slack.channels.routing",
            outcome="denied",
            source="dashboard",
            resources=f"channel={channel_id} {msg}",
        )

    def _deny(msg: str, code: str) -> web.Response:
        """A malformed request. Status is the literal 400, never a variable.

        The dashboard renders ``error`` verbatim into a localized page, so
        ``code`` is the part a client can switch on and translate for itself.
        Both responders here spell their status literally so the error-code
        contract gate can classify them statically.
        """
        _audit_refusal(msg)
        return web.json_response({"code": code, "error": msg}, status=400)

    def _write_failed(msg: str, code: str) -> web.Response:
        """The write could not be performed. Status is the literal 500."""
        _audit_refusal(msg)
        return web.json_response({"code": code, "error": msg}, status=500)

    if not _CHANNEL_ID_RE.match(channel_id):
        return _deny("channel_id must be a Slack channel id (C… or G…)", "invalid_channel_id")

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _deny("body must be an object", "body_not_an_object")

    remove = body.get("remove", False)
    if not isinstance(remove, bool):
        return _deny("remove must be a boolean", "invalid_remove")

    agent = body.get("agent")
    activation = body.get("activation")

    if remove:
        # Teardown: agent/activation are meaningless alongside a delete.
        if any(x is not None for x in (agent, activation)):
            return _deny(
                "remove cannot be combined with agent/activation",
                "remove_with_updates",
            )
    else:
        if agent is not None and not isinstance(agent, str):
            return _deny("agent must be a string", "invalid_agent")
        if activation is not None:
            if not isinstance(activation, str) or activation not in _ACTIVATION_VALUES:
                return _deny(
                    f"activation must be one of: {', '.join(sorted(_ACTIVATION_VALUES))}",
                    "invalid_activation",
                )
        if agent is None and activation is None:
            return _deny("nothing to update", "nothing_to_update")

    # The shared channel-config writer, reached through the one helper that holds
    # BOTH config locks. Deliberately not re-implemented here: a second spelling
    # of this read-modify-write is what lets the two writer families revert each
    # other, which is the race this endpoint exists to remove.
    try:
        changed = await run_config_write(
            slack_handler._persist_channel_config,
            channel_id,
            activation=activation,
            agent=agent,
            remove=remove,
        )
    except ValueError as exc:
        # The writer fails closed on an unreadable/unwritable config rather than
        # resetting it, so surface that instead of half-applying the change.
        logger.warning("routing write refused for %s: %s", channel_id, exc)
        return _write_failed(
            "config.json could not be read or written; routing was not changed",
            "config_write_failed",
        )

    refreshed = _refresh_routing()

    _sel().log_api_access(
        caller=caller,
        operation="slack.channels.routing",
        outcome="success",
        source="dashboard",
        resources=f"channel={channel_id} changed={','.join(changed) or 'none'}",
    )
    return web.json_response({"ok": True, "changed": changed, "routing_refreshed": refreshed})


def _refresh_routing() -> bool:
    """Copy on-disk channel routing into the running gateway.

    Returns False when the Slack orchestrator is not bound yet (gateway not
    running, or Slack disabled). That is not an error: the on-disk write already
    landed and is read at next boot, so the caller is told the durable part
    succeeded and only the live refresh was skipped.
    """
    try:
        if slack_handler.get_orch_cfg() is None:
            return False
        slack_handler._reload_orch_cfg()
        return True
    except Exception as exc:
        logger.warning("in-memory routing refresh failed: %s", exc, exc_info=True)
        return False
