"""A ``config.json`` with a change pending owner review, and the layer that answers it.

Where the sandbox seals the keystone files, ``config.json`` differing from its endorsed
copy is pending, and every ordinary save of it raises ``PendingEditBlocksSave``
(``config.loosening_provenance``). The dashboard chain's ``spa_fallback`` layer turns
that refusal into one 409 with the code the frontend translates. A test module is not
importable from another one, so both live in this helpers module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir

HAND_EDIT = (json.dumps({"agent": {"bot_name": "Hand"}}) + "\n").encode()


def make_config_json_pending(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Turn the content check on and leave ``config.json`` pending.

    The bytes on disk become the endorsed copy, so every read still serves them, and
    the file is then changed outside Kiro Crew.
    """
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    path = config_dir() / lp.LEAF_CONFIG
    lp.record(lp.LEAF_CONFIG, path.read_bytes(), strict=True)
    path.write_bytes(HAND_EDIT)
    assert lp.leaf_pending(lp.LEAF_CONFIG)
    return path


def dashboard_refusal_layer() -> object:
    """The dashboard chain's own ``spa_fallback`` layer, built by its installer."""
    from kiro_crew.dashboard import server

    @web.middleware
    async def _through(request, handler):
        return await handler(request)

    chain = web.Application()
    server._install_dashboard_middlewares(
        chain,
        deny_audit_middleware=_through,
        host_canonical_redirect=_through,
        host_validation_middleware=_through,
        csrf_middleware=_through,
        internal_secret="per-boot",
        port=25300,
        local_only=True,
        tailnet_trust=None,
        tailnet_host="",
        configured_host="127.0.0.1",
        dashboard_url="",
    )
    return next(mw for mw in chain.middlewares if mw.__name__ == "spa_fallback")
