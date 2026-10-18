"""The fixture and request builders the webhook management test modules share.

``wired`` (declared ``@pytest.fixture(name="wired")`` and imported under its
function name) points both webhook stores and ``hooks.json`` at ``tmp_path`` and
resets the process-global auth throttle, replay set and in-flight registry around
each test; ``_req`` builds a mocked request whose raw body matches its parsed body,
signed and attributed to a caller on request; ``_payload`` reads a handler's JSON
response; ``_bearer`` builds a request carrying a bearer token.
``test_webhooks_api.py`` and ``test_webhooks_api_read_and_tokens.py`` import them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew import webhooks
from kiro_crew.dashboard.handlers import hooks as H


@pytest.fixture(name="wired")
def wired_fixture(tmp_path, monkeypatch):
    """Point both webhook stores and hooks.json at a temp dir."""
    monkeypatch.setattr(webhooks, "config_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(H, "_HOOK_STORE_PATH", Path(tmp_path) / "hooks.json")
    monkeypatch.setattr(H, "_sel", lambda: MagicMock())
    monkeypatch.setattr(H, "_legacy_hook_token", lambda: "")
    monkeypatch.setattr(
        H, "_installed_agent_names", lambda: {"kirocrew", "code-reviewer", "oncall"}
    )
    # The failed-auth throttle, the replay seen-set and the in-flight session
    # registry are all process-global, so leaking them between tests would make a
    # later 401 come back as a 429, or a fresh call come back as a 409.
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()
    yield Path(tmp_path)
    webhooks._reset_auth_throttle()
    webhooks._reset_signature_replay()
    H._reset_hook_inflight()


def _req(
    method: str,
    path: str,
    body=None,
    match_info=None,
    headers=None,
    sign_with: str | None = None,
    timestamp: int | None = None,
    user: str = "local-app",
    app_token: str = "",
    owner_id: str = "",
):
    """Build a mocked request whose raw body matches its parsed body.

    ``sign_with`` adds the timestamp + signature headers for that signing
    secret, computed over the same bytes ``request.read()`` returns -- the
    handler verifies raw bytes, so the test double has to be byte-accurate.

    ``user`` / ``app_token`` / ``owner_id`` describe the CALLER, because
    ``api_webhook_token_create`` is owner-gated
    (``handlers._shared.require_owner_dashboard_request``) and the predicate
    reads the claims the token-auth middleware publishes plus
    ``state.owner_id``. The defaults are the standalone-local owner shape --
    ``owner_id == ""`` with the signed local bootstrap subject ``local-app`` --
    which is the same shape ``test/dashboard_owner_helpers.NoConfiguredOwner``
    encodes for the fixtures that go through a ``TestClient``. A test that wants
    a non-owner passes ``user=`` and ``owner_id=``; one that wants an app token
    passes ``app_token=``.
    """
    hdrs = dict(headers or {})
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    if sign_with is not None:
        stamp = int(time.time()) if timestamp is None else timestamp
        hdrs[webhooks.TIMESTAMP_HEADER] = str(stamp)
        hdrs[webhooks.SIGNATURE_HEADER] = webhooks.sign_payload(sign_with, stamp, raw)
    if body is not None:
        # A real body stream with its declared type: the dashboard routes read
        # through ``read_bounded_json``, which checks the media type and drains
        # ``request.content`` rather than calling ``read()``.
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("Content-Length", str(len(raw)))
    req = make_mocked_request(
        method,
        path,
        match_info=match_info or {},
        headers=hdrs,
        payload=BodyStreamPayload(raw),
    )
    state = MagicMock()
    state.owner_id = owner_id
    req.app["state"] = state
    req["user"] = user
    req["app"] = app_token
    # make_mocked_request's app is a Mock, so .get() would hand back a Mock
    # rather than the port the real Application holds.
    req.app.get = lambda key, default=None: {"port": 6776}.get(key, default)
    req.read = AsyncMock(return_value=raw)
    if body is not None:
        req.json = AsyncMock(return_value=body)
    else:
        req.json = AsyncMock(side_effect=ValueError("no body"))
    return req


async def _payload(resp):
    return json.loads(resp.body.decode("utf-8"))


def _bearer(raw: str):
    return make_mocked_request(
        "POST", "/api/hooks/agent", headers={"Authorization": f"Bearer {raw}"}
    )
