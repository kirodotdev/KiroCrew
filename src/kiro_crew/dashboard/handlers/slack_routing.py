"""Brokered Slack channel routing writes for edition-supplied apps.

An app (the internal recipes app) performs its own Slack calls with the bot
token it reads from the credential store; it owns channel create, archive,
invite, and purpose. The one thing it cannot do from its own process is reach
into the running gateway to make a routing change take effect, so core exposes
exactly one operation:

  ``PUT /api/slack/channels/{channel_id}/routing``
      Write (or remove) ``slack.channels[channel_id]`` agent / activation under
      the gateway's own config lock, then refresh the in-memory routing table.

Why this and nothing more: the config write lock is an in-process
``asyncio.Lock``, so if the app wrote ``config.json`` itself it would race the
gateway's other config writers with no shared OS lock. Funneling the write
through the gateway keeps a single writer and removes the race. Channel
lifecycle (create / archive) is the app's job, not core's, so there is no
provisioning or destruction endpoint here.

This is an ordinary dashboard endpoint, so an app reaches it with its app-scoped
token (``POST /api/apps/{name}/token``) and must declare the path in its manifest
``permissions.api`` allowlist, which ``token_auth`` enforces.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

#: Slack channel ids are ``C``/``G`` followed by uppercase alphanumerics. Bounded
#: and anchored so a path parameter cannot smuggle a traversal or an absurd value
#: into the config document we are about to write.
_CHANNEL_ID_RE = re.compile(r"^[CG][A-Z0-9]{2,31}$")


def _activation_values() -> frozenset[str]:
    """The activation modes ``ChannelConfig`` accepts."""
    from kiro_crew.config.loader import (
        ACTIVATION_ALWAYS,
        ACTIVATION_MENTION,
        ACTIVATION_OBSERVE,
        ACTIVATION_OFF,
        ACTIVATION_REVIEW,
    )

    return frozenset(
        {
            ACTIVATION_ALWAYS,
            ACTIVATION_MENTION,
            ACTIVATION_OBSERVE,
            ACTIVATION_REVIEW,
            ACTIVATION_OFF,
        }
    )


def _caller(request: web.Request) -> str:
    """Best-effort caller identity for the audit log.

    ``request["app"]`` is the verified app identity set by ``token_auth`` when
    the call arrived on an app-scoped token; fall back to the generic dashboard
    caller so a browser-driven call is still attributable.
    """
    app_name = request.get("app") or ""
    return f"app:{app_name}" if app_name else "dashboard"


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


# ---------------------------------------------------------------------------
# PUT /api/slack/channels/{channel_id}/routing
# ---------------------------------------------------------------------------


async def api_slack_channel_routing_put(request: web.Request) -> web.Response:
    """Write or remove a channel's routing, then refresh the gateway.

    Body (set):    ``{"agent": str?, "activation": str?, "owner": {"app","name"}?}``
    Body (remove): ``{"remove": true}`` -- deletes ``slack.channels[channel_id]``
    Returns ``{"ok": True, "changed": [str], "routing_refreshed": bool}``.

    Serialized with every other ``config.json`` writer through the shared
    ``_get_config_lock()``; this is the only writer path an app should use, so
    the gateway remains the single config writer and there is no cross-process
    write race.
    """
    caller = _caller(request)
    channel_id = str(request.match_info.get("channel_id", "")).strip()

    def _deny(msg: str, code: str) -> web.Response:
        """Reject the request with a machine-readable ``code`` and advisory prose.

        Every rejection here is a malformed request, so the status is the literal
        400 rather than a parameter: the dashboard renders ``error`` verbatim into
        a localized page, so ``code`` is the part a client can switch on and
        translate for itself.
        """
        _sel().log_api_access(
            caller=caller,
            operation="slack.channels.routing",
            outcome="denied",
            source="dashboard",
            resources=f"channel={channel_id} {msg}",
        )
        return web.json_response({"code": code, "error": msg}, status=400)

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
    owner = body.get("owner")

    if remove:
        # Teardown: agent/activation/owner are meaningless alongside a delete.
        if any(x is not None for x in (agent, activation, owner)):
            return _deny(
                "remove cannot be combined with agent/activation/owner",
                "remove_with_updates",
            )
    else:
        if agent is not None and not isinstance(agent, str):
            return _deny("agent must be a string", "invalid_agent")
        if activation is not None:
            if not isinstance(activation, str) or activation not in _activation_values():
                return _deny(
                    f"activation must be one of: {', '.join(sorted(_activation_values()))}",
                    "invalid_activation",
                )
        if owner is not None:
            if not isinstance(owner, dict):
                return _deny("owner must be an object", "invalid_owner")
            if not str(owner.get("app", "")).strip() or not str(owner.get("name", "")).strip():
                return _deny("owner requires non-empty 'app' and 'name'", "incomplete_owner")
        if agent is None and activation is None and owner is None:
            return _deny("nothing to update", "nothing_to_update")

    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    async with _get_config_lock():
        changed = _write_routing_locked(channel_id, agent, activation, owner, remove)

    refreshed = _refresh_routing()

    _sel().log_api_access(
        caller=caller,
        operation="slack.channels.routing",
        outcome="success",
        source="dashboard",
        resources=f"channel={channel_id} changed={','.join(changed) or 'none'}",
    )
    return web.json_response({"ok": True, "changed": changed, "routing_refreshed": refreshed})


def _write_routing_locked(
    channel_id: str,
    agent: str | None,
    activation: str | None,
    owner: dict[str, Any] | None,
    remove: bool,
) -> list[str]:
    """Read-modify-write ``slack.channels[channel_id]``. Caller holds the lock."""
    import json

    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config.paths import config_dir

    path = config_dir() / "config.json"
    doc: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc = loaded
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("config.json unreadable, starting from empty: %s", exc)

    slack = doc.setdefault("slack", {})
    if not isinstance(slack, dict):
        slack = {}
        doc["slack"] = slack
    channels = slack.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        slack["channels"] = channels

    changed: list[str] = []

    if remove:
        if channel_id in channels:
            del channels[channel_id]
            changed.append("removed")
            atomic_write(path, json.dumps(doc, indent=2) + "\n")
        return changed

    entry = channels.setdefault(channel_id, {})
    if not isinstance(entry, dict):
        entry = {}
        channels[channel_id] = entry

    if agent is not None and entry.get("agent") != agent:
        entry["agent"] = agent
        changed.append("agent")
    if activation is not None and entry.get("activation") != activation:
        entry["activation"] = activation
        changed.append("activation")
    if owner is not None:
        entry["_owner"] = {
            "app": str(owner["app"]).strip(),
            "name": str(owner["name"]).strip(),
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        changed.append("_owner")

    if changed:
        atomic_write(path, json.dumps(doc, indent=2) + "\n")
    return changed


def _refresh_routing() -> bool:
    """Copy on-disk channel routing into the running gateway.

    Returns False when the Slack orchestrator is not bound yet (gateway not
    running, or Slack disabled). That is not an error: the on-disk write already
    landed and is read at next boot, so the caller is told the durable part
    succeeded and only the live refresh was skipped.
    """
    try:
        from kiro_crew.slack import handler as slack_handler

        if slack_handler.get_orch_cfg() is None:
            return False
        slack_handler._reload_orch_cfg()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("in-memory routing refresh failed: %s", exc, exc_info=True)
        return False
