"""``GET /api/connections/app-manifests``: the app a Connections card can create.

A pre-registered card needs an OAuth app the operator makes at the vendor. When
the gateway can generate that app's settings, the card's "Before you connect"
tip offers a create button and a copy button for them. The card holds no vendor
knowledge: it renders whatever entry this route answers for its slug.

Only Slack has a generator today: ``slack_manifest``, the same manifest the Slack
channel bot is made from, with the Slack MCP switch on. One Slack app then serves
the bot and the Slack card (RFC ``rfc-crew-oauth-sign-in-redesign.md`` section 2).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from aiohttp import web

from kiro_crew.connections import get_provider
from kiro_crew.connections.registry import is_preregistered, is_visible

_SCHEMA_VERSION = 1


def _slack_app(cfg: Any) -> dict[str, str]:
    """Slack's create link and the manifest it carries, Slack MCP switch on.

    The alias and the other Slack Code switches are this install's, read the way
    ``GET /api/slack/manifest`` reads them, so the app the card creates is one the
    gateway's bot can also run on. ``manifest`` is the link's own payload, so the
    copied text and the opened page describe the same app.
    """
    from kiro_crew import slack_manifest
    from kiro_crew.slack.features import manifest_features

    saved_command = str(getattr(getattr(cfg, "slack", None), "command", "") or "")
    alias = slack_manifest.alias_for_command(saved_command)
    try:
        saved = manifest_features(cfg) if cfg is not None else frozenset()
    except (
        Exception
    ):  # noqa: BLE001 -- an unreadable switch reads as off, as in GET /api/slack/manifest
        saved = frozenset()
    features = saved | {slack_manifest.SLACK_MCP}
    return {
        "create_url": slack_manifest.deep_link(alias, features),
        "manifest": slack_manifest.render(alias, strip_comments=True, features=features),
    }


#: Card slug -> the generator of the app that card needs. A slug is served only
#: while the registry shows it as a pre-registered card.
_GENERATORS: dict[str, Callable[[Any], dict[str, str]]] = {"slack": _slack_app}


def app_manifests() -> dict[str, dict[str, str]]:
    """Each visible pre-registered card's generated app. Worker-thread only.

    A generator that fails (a missing manifest template) drops its card from the
    answer, so that card shows its tip without the buttons.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 -- an unreadable config renders the defaults
        cfg = None
    apps: dict[str, dict[str, str]] = {}
    for slug, generate in _GENERATORS.items():
        provider = get_provider(slug)
        if provider is None or not is_preregistered(provider) or not is_visible(provider):
            continue
        try:
            apps[slug] = generate(cfg)
        except FileNotFoundError:
            continue
    return apps


async def api_connections_app_manifests(request: web.Request) -> web.Response:
    """GET /api/connections/app-manifests — ``{"apps": {slug: {create_url, manifest}}}``.

    Owner-only, like the OAuth app form the result is for. The payload is a
    public app template with no secret in it.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "connections_app_manifests")
    if owner_denied is not None:
        return owner_denied
    apps = await asyncio.to_thread(app_manifests)
    return web.json_response({"schema_version": _SCHEMA_VERSION, "apps": apps})
