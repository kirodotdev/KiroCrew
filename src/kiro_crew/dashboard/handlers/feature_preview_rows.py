"""The config keys behind edition rows in Settings > Developer > Feature Previews.

An edition registers its rows in the browser (``registerFeaturePreviewRows``,
``website/src/pages/settings/featurePreviewRows.ts``) and names the boolean
config keys those rows toggle through the optional
``DashboardContributor.feature_preview_config_keys()`` method. These routes read
and write ONLY those keys:

* ``GET /api/feature-previews/edition`` answers ``{"values": {key: bool | null}}``
  for every accepted key (``null`` when unset or not a boolean, so the row shows
  its own default).
* ``PUT /api/feature-previews/edition`` with ``{"config_key", "enabled"}`` writes
  one accepted key and answers ``{"config_key", "enabled"}``.

Both are owner-only (``require_owner_dashboard_request``); the PUT also requires a
same-origin ``Origin`` header. A key must be a dotted snake_case path of two to
four parts under a top-level section the core does not own (no schema section,
no reserved key, no core-owned key), so an edition row can never become a second
write path into the core's own settings. A key that fails is dropped with a
warning. The write changes nothing in the running process: a row the edition
applies on restart applies on the next gateway start.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

EDITION_PATH = "/api/feature-previews/edition"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){1,3}$")
_MAX_KEYS = 16
_MAX_JSON_BYTES = 1024


def _core_top_keys() -> set[str]:
    from kiro_crew.config.loader import CONFIG_RESERVED_TOP_KEYS
    from kiro_crew.config.schema import SCHEMA_REGISTRY
    from kiro_crew.config.validation import _CORE_OWNED_TOP_KEYS

    keys = {e.path.split(".", 1)[0] for e in SCHEMA_REGISTRY if e.path != "*"}
    return keys | set(CONFIG_RESERVED_TOP_KEYS) | set(_CORE_OWNED_TOP_KEYS)


def accepted_keys(raw: Any, core_top_keys: set[str]) -> list[str]:
    """The keys of *raw* an edition may toggle, in order; anything else is dropped."""
    keys: list[str] = []
    for key in raw or ():
        if len(keys) >= _MAX_KEYS:
            break
        if not (isinstance(key, str) and _KEY_RE.match(key)) or key in keys:
            continue
        if key.split(".", 1)[0] in core_top_keys:
            logger.warning("feature preview key %s refused: it is under a core config section", key)
            continue
        keys.append(key)
    return keys


def contributed_keys() -> list[str]:
    """The accepted keys this gateway's edition contributes; none when it predates the seam."""
    from kiro_crew.platform import current_context, safe_context_call

    def _read() -> list[str]:
        reader = getattr(current_context().dashboard, "feature_preview_config_keys", None)
        if reader is None:
            return []
        return accepted_keys(list(reader()), _core_top_keys())

    def _degraded() -> list[str]:
        logger.warning("dashboard contributor feature_preview_config_keys failed", exc_info=True)
        return []

    return safe_context_call(_read, fallback_factory=_degraded, log_message=None)


def _stored(cfg: Mapping[str, Any], key: str) -> bool | None:
    node: Any = cfg
    for part in key.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node if isinstance(node, bool) else None


def _read_config() -> dict:
    from kiro_crew.config.loader import config_path
    from kiro_crew.config.loader_document import read_config_for_update

    return read_config_for_update(config_path())


def _write(key: str, enabled: bool) -> None:
    from kiro_crew.config.loader import config_path, update_config_locked

    head, *middle, leaf = key.split(".")

    def mutate(cfg: dict) -> dict | None:
        section = cfg.get(head)
        root = dict(section) if isinstance(section, dict) else {}
        node = root
        for part in middle:
            child = node.get(part)
            node[part] = dict(child) if isinstance(child, dict) else {}
            node = node[part]
        if node.get(leaf) is enabled:
            return None
        node[leaf] = enabled
        cfg[head] = root
        return cfg

    update_config_locked(config_path(), mutate=mutate)


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code, "code": code}, status=status)


def _audit(request: web.Request, key: str, enabled: bool, outcome: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation="feature_preview_edition.set",
            outcome=outcome,
            source="feature_preview_edition",
            resources=f"key={key} enabled={enabled}",
            critical=True,
        )
    except Exception:  # noqa: BLE001 -- the audit record never changes the outcome
        logger.debug("feature preview edition audit failed", exc_info=True)


async def api_get_edition(request: web.Request) -> web.Response:
    from kiro_crew.config.loader_document import ConfigReadError
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    refused = await require_owner_dashboard_request(request, "feature_preview_edition_get")
    if refused is not None:
        return refused
    keys = await asyncio.to_thread(contributed_keys)
    if not keys:
        return web.json_response({"values": {}})
    try:
        cfg = await asyncio.to_thread(_read_config)
    except (OSError, ConfigReadError):
        logger.error("could not read config for feature preview keys", exc_info=True)
        return _error(500, "config_read_failed")
    return web.json_response({"values": {k: _stored(cfg, k) for k in keys}})


async def api_put_edition(request: web.Request) -> web.Response:
    from kiro_crew.config.loader_document import ConfigReadError, ConfigWriteRefused
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.dashboard.handlers._shared_request_body import read_bounded_json
    from kiro_crew.dashboard.origin import check_origin

    refused = await require_owner_dashboard_request(request, "feature_preview_edition_set")
    if refused is not None:
        return refused
    if not request.headers.get("Origin") or not check_origin(request):
        return _error(403, "forbidden")
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_JSON_BYTES)
    if body_err is not None:
        return body_err
    key = body.get("config_key") if isinstance(body, dict) else None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(key, str) or not isinstance(enabled, bool):
        return _error(400, "invalid_body")
    if key not in await asyncio.to_thread(contributed_keys):
        return _error(404, "unknown_key")
    try:
        await asyncio.to_thread(_write, key, enabled)
        stored = _stored(await asyncio.to_thread(_read_config), key)
    except (OSError, ConfigReadError, ConfigWriteRefused):
        logger.error("could not write feature preview key %s", key, exc_info=True)
        await asyncio.to_thread(_audit, request, key, enabled, "write_failed")
        return _error(500, "write_failed")
    await asyncio.to_thread(_audit, request, key, enabled, "enabled" if enabled else "disabled")
    return web.json_response({"config_key": key, "enabled": stored})
