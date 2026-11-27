"""The App Store search tool: what it advertises and what it does.

``schemas()`` returns the advertisement; ``HANDLERS`` maps the name to the function
that runs it. The search runs in the gateway (``GET /api/app-store/search``,
``apps.routes_registry.handle_app_store_search``), over the listing the Apps tab's
Discover view reads from ``GET /api/apps/registry``, so this process holds no catalog
and installs nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.app_store import schemas  # noqa: F401
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MAX_RESPONSE_LEN, sanitize_string

#: Covers a cold listing's catalog and inventory fetches and manifest budget in the
#: gateway (about 25 s) with room for ``detectInstalled`` probes. An expired external
#: registry's clone can still outrun it; the retry then reads the refilled cache.
_SEARCH_TIMEOUT_S = 60.0


def app_store_search(name: str, args: dict[str, Any]) -> str:
    params: dict[str, Any] = {"q": str(args.get("query", "")).strip()}
    if args.get("limit") is not None:
        params["limit"] = args["limit"]
    resp = mcp_core._get("/api/app-store/search?" + urlencode(params), timeout=_SEARCH_TIMEOUT_S)
    if resp.get("error"):
        return f"Error: app_store_search failed: {redact(str(resp['error']))}"
    # The rows carry publishers' display copy, so the answer is redacted like
    # any text this process hands the model from outside. On its way out the text
    # is normalized and cut at MAX_RESPONSE_LEN, and a cut answer is not JSON,
    # so the last apps go until it fits; ``total`` still counts them.
    text = redact(json.dumps(resp, ensure_ascii=False))
    apps = resp.get("apps")
    while isinstance(apps, list) and apps and len(sanitize_string(text)) > MAX_RESPONSE_LEN:
        apps.pop()
        text = redact(json.dumps(resp, ensure_ascii=False))
    return text


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "app_store_search": app_store_search,
}
