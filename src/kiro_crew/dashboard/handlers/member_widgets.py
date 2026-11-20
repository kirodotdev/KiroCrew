"""HTTP for phone widgets: the catalog of every Bot's widgets, and one widget's snapshot.

``GET /api/widgets`` answers the catalog the phone's configuration step lists (mobile
widgets design 3.2.2). ``GET /api/members/{slug}/widgets/{widget_id}?member=<name>``
answers one widget's snapshot (3.2.3), resolved by ``dashboard.widget_snapshot``.

**Behind the ``widgets`` feature preview.** While it is off both routes answer
``404 widgets_off`` to every caller and read nothing else.

**The machine owner only, checked before anything is resolved.** These routes are a
new way for fold and agent data to leave the gateway, so v1 serves them to the Crew's
owner alone (design open question 6). The check is the gateway's own owner predicate,
``source_providers.is_host_owner_dashboard_request``, not the owner gate a Shared Crew
teammate passes: a relay teammate, an SSM guest, a tailnet collaborator, an app token,
an internal-secret caller and any other subject get ``403 owner_only``, even for a Bot
whose Dashboard page admits that teammate. A refused request never reaches the package
selection, the field read or the resolver, and the refusal is audited.

**Per-reader validators.** Both answers go through ``conditional_get`` with
``Cache-Control: private, no-cache`` and a strong ETag over the reader, the request's
identity and the body without ``generated_at`` (:func:`widget_etag`), so one reader's
``If-None-Match`` can never score a ``304`` on another's body. Every ``200`` and
``304`` carries ``Kiro-Widget-Checked-At``, the gateway's clock when it resolved the
request.

``server.py`` binds these paths through its deferred binder
(``server_runtime.mcp_routes``) so this module is not imported at boot;
:func:`register_member_widgets_routes` is the same table for tests, and
``test_member_widgets_routes`` pins the two spellings against each other.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import conditional_get
from kiro_crew.dashboard import widget_snapshot as ws
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard.handlers.members import _member_names_for_slug
from kiro_crew.members import MemberSlugError
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)

__all__ = ["register_member_widgets_routes", "widget_etag", "widgets_on"]

#: The ``dashboard.feature_previews`` name, and ``mc-preview-widgets`` in
#: ``website/src/utils/previewFlags.ts`` (the two lists are pinned equal).
PREVIEW_NAME: Final = "widgets"

#: The catalog wire version.
CATALOG_V: Final = 1

#: Body caps (design 3.2.2 and 3.2.3, design choices). The phone's parser refuses a
#: larger body, so the gateway refuses to send one rather than send what cannot be read.
#: shortcut: a widget at every 3.2.1 limit is about 7 KB in ASCII but about 16.8 KB when
#: every list row is CJK, so that widget answers `widget_too_large`; raise the cap on
#: both sides, or cut rows to fit, if Bots author such widgets.
MAX_CATALOG_BYTES: Final = 64 * 1024
MAX_SNAPSHOT_BYTES: Final = 16 * 1024

#: The header that says when the gateway last confirmed the body is current.
CHECKED_AT_HEADER: Final = "Kiro-Widget-Checked-At"

CACHE_CONTROL: Final = "private, no-cache"


def _bad(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def widgets_on(cfg: Any) -> bool:
    """Whether the edition permits the ``widgets`` preview and *cfg* stores it ``True``."""
    from kiro_crew.edition_locks import feature_previews

    try:
        previews = feature_previews(
            getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
        )
    except Exception:  # noqa: BLE001 - unread means off
        logger.debug("widgets: feature previews unreadable; treating as off", exc_info=True)
        return False
    return isinstance(previews, Mapping) and previews.get(PREVIEW_NAME) is True


async def _config() -> KiroCrewConfig | None:
    """The live config, else the file; ``None`` when neither reads (the routes are off)."""
    from kiro_crew.config import live

    cfg = live.snapshot()
    if cfg is not None:
        return cfg
    try:
        return await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:  # noqa: BLE001 - unread means off
        logger.debug("widgets: config unreadable; treating the preview as off", exc_info=True)
        return None


def _off() -> web.Response:
    return _bad("widgets_off", "phone widgets are off on this gateway", 404)


def _owner_refusal(request: web.Request, operation: str) -> web.Response | None:
    """``None`` for the machine owner's own dashboard session; else the audited refusal.

    Reuses the gateway's owner predicate and its denial (which relabels a stale
    pre-owner session as ``401``). An internal-secret caller is refused even when it
    acts as the owner: no agent needs a phone's widget body.
    """
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import (
        is_host_owner_dashboard_request,
    )

    internal = bool(request.get("internal_auth")) or "X-Internal-Secret" in request.headers
    if not internal and is_host_owner_dashboard_request(request):
        return None
    try:
        from kiro_crew.sel import sel as _sel

        _sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="widgets_owner_only",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for the %s refusal failed", operation, exc_info=True)
    return _owner_denial_response(request)


async def _gate(request: web.Request, operation: str) -> KiroCrewConfig | web.Response:
    """The config for an owner's request while the preview is on; else the answer."""
    cfg = await _config()
    if cfg is None or not widgets_on(cfg):
        return _off()
    denied = _owner_refusal(request, operation)
    if denied is not None:
        return denied
    return cfg


def widget_etag(reader: str, scope: Sequence[str], body: Mapping[str, Any]) -> str:
    """The strong validator of one reader's widget body.

    Hashes the authenticated reader, then each *scope* part (the slug, member, widget
    ID and locale for a snapshot), then the body without ``generated_at``, joined with
    NUL. Two readers, or two widgets, never share a validator even when their bodies
    are equal, and a body that differs only in when it was generated keeps its tag.
    """
    stable = {key: value for key, value in body.items() if key != "generated_at"}
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    head = "\x00".join((reader, *scope)) + "\x00"
    return conditional_get.strong_content_etag(head.encode("utf-8") + payload)


def _answer(
    request: web.Request, body: dict[str, Any], scope: Sequence[str], now: datetime, cap: int
) -> web.Response:
    # UTF-8, not \u escapes: the cap counts the bytes the phone receives.
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > cap:
        logger.warning("widgets: a %d-byte body is over the %d-byte cap", len(payload), cap)
        return _bad("widget_too_large", "this widget body is too large to send", 500)
    return conditional_get.conditional_response(
        request,
        payload,
        "application/json",
        etag=widget_etag(str(request.get("user") or ""), scope, body),
        cache_control=CACHE_CONTROL,
        extra_headers={CHECKED_AT_HEADER: ws.iso(now)},
        charset="utf-8",
    )


def _clean(text: str) -> str:
    """A catalog string as the snapshot's strings are made: redacted, made plain and
    redacted again (a key a control character split is whole only after that), then
    cut for display."""
    return ws.sanitize_text(redact_via_context(ws.plain_text(redact_via_context(text))))


def _bot_slug(cfg: KiroCrewConfig, name: str) -> str:
    """*name*'s slug when it addresses exactly one Bot, else ``""``."""
    if not members_mod.is_dispatchable_member_name(name):
        return ""
    try:
        slug = members_mod.member_slug(name, cfg)
    except MemberSlugError:
        return ""
    return slug if _member_names_for_slug(cfg, slug) == [name] else ""


def _display_name(cfg: KiroCrewConfig, name: str) -> str:
    return str(getattr(cfg.agents.get(name), "display_name", "") or name)


async def _catalog_bot(
    cfg: KiroCrewConfig, name: str, slug: str, clean: Callable[[str], str]
) -> dict[str, Any]:
    package, layout_version = await ws.package_in_force(slug, name)
    entries = []
    for widget in ws.widgets_of(package):
        default = widget.get("id") == ws.RESERVED_WIDGET_ID
        entries.append(
            {
                "id": widget.get("id"),
                "title": clean(str(widget.get("title") or "")),
                "sizes": list(widget.get("sizes") or ()),
                "default": default,
                "layout_version": 0 if default else int(layout_version),
            }
        )
    return {
        "slug": slug,
        "member": name,
        "name": clean(_display_name(cfg, name)),
        "widgets": entries,
    }


async def api_widgets(request: web.Request) -> web.Response:
    """GET /api/widgets -- every Bot's widgets, for the owner's phone (design 3.2.2)."""
    gated = await _gate(request, "widgets.catalog")
    if isinstance(gated, web.Response):
        return gated
    cfg = gated
    now = datetime.now(timezone.utc)
    bots = []
    for name in list(cfg.agents):
        slug = _bot_slug(cfg, name)
        if slug:
            bots.append(await _catalog_bot(cfg, name, slug, _clean))
    # The page's own pass over the finished body, as the snapshot gets.
    body: dict[str, Any] = md.page_safe_for(request.app.get("state"))(
        {"v": CATALOG_V, "bots": bots}
    )
    return _answer(request, body, ("catalog",), now, MAX_CATALOG_BYTES)


async def api_member_widget(request: web.Request) -> web.Response:
    """GET /api/members/{slug}/widgets/{widget_id}?member=<name>[&locale=<tag>] (3.2.3)."""
    gated = await _gate(request, "widgets.snapshot")
    if isinstance(gated, web.Response):
        return gated
    cfg = gated
    resolved = await md._resolve(request)
    if isinstance(resolved, web.Response):
        if resolved.status == 404:
            return _bad("bot_not_found", "no Bot for this slug", 404)
        return resolved
    slug, member = resolved
    widget_id = request.match_info.get("widget_id", "")
    locale = request.query.get("locale", "")
    now = datetime.now(timezone.utc)
    body = await ws.resolve_snapshot(
        slug,
        member,
        widget_id,
        name=_display_name(cfg, member),
        now=now,
        state=request.app.get("state"),
    )
    if body is None:
        return _bad("widget_not_found", "this Bot has no such widget", 404)
    return _answer(request, body, (slug, member, widget_id, locale), now, MAX_SNAPSHOT_BYTES)


def register_member_widgets_routes(app: web.Application) -> None:
    """Register the two widget reads. ``server.py`` binds the same paths deferred."""
    app.router.add_get("/api/widgets", api_widgets)
    app.router.add_get("/api/members/{slug}/widgets/{widget_id}", api_member_widget)
