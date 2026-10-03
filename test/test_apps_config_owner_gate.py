"""Owner gate on the generic ``PUT /api/apps/{name}/config`` (``handle_app_config``).

The generic route serves every app that does not register its own ``/config``.
issue-radar is one: its ``data/config.json`` ``repos`` list is the read gate for
its ``/issues``, ``/pulls`` and ``/pull`` routes, which run the owner's ``gh``.
So a dashboard subject that is not the owner must not write it.

Caller rows, through the production route order (``system.register``):

* non-owner dashboard subject: 403 ``owner_only``, ``config.json`` untouched;
* owner: 200, written;
* the app's own token: 200, written (``token_auth`` scoped it to its own path);
* a foreign app's token: refused by ``token_auth`` unless its manifest grants the
  path; with a grant it is let through like the app's own token.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import routes as app_routes
from kiro_crew.dashboard import token_auth

pytestmark = pytest.mark.asyncio

OWNER = "owner-subject"
NON_OWNER = "channel-user"
APP = "issue-radar"
PATH = f"/api/apps/{APP}/config"

_OWNER_CONFIG = {"repos": [{"owner": "acme", "repo": "public-repo"}]}
_PLANTED = {"repos": [{"owner": "acme", "repo": "secret-repo"}]}


class _Sel:
    def log_api_access(self, *a: Any, **k: Any) -> None:
        return None


def _app(*, user: str = "", app_claim: str | None = "") -> web.Application:
    """The production route table, with the claims token_auth would publish.

    ``app_claim=None`` leaves the ``app`` key unset, the shape of a request no
    authenticator stamped.
    """
    from kiro_crew.dashboard.routes import system

    @web.middleware
    async def _claims(request: web.Request, handler: Any) -> web.StreamResponse:
        if user:
            request["user"] = user
        if app_claim is not None:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = SimpleNamespace(owner_id=OWNER, broadcast_ws=lambda *a, **k: None)
    system.register(app)
    return app


@pytest.fixture
def config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setattr(app_routes, "sel", lambda: _Sel())
    monkeypatch.setattr(app_routes, "get_app", lambda name: {"name": name, "enabled": True})
    # token_auth.app_token_path_allowed now denies at the door for any app whose
    # enablement is not provably True (tri-state fail-closed). It reads that
    # through app_enabled_state (imported function-level from apps.manager), not
    # through get_app, so stub it here to match the enabled:True get_app above --
    # these tests exercise a path grant for an enabled app, not the disable gate.
    import kiro_crew.apps.manager as _mgr

    monkeypatch.setattr(_mgr, "app_enabled_state", lambda name: True)
    from kiro_crew.apps.manager import app_data_dir

    path = app_data_dir(APP) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_OWNER_CONFIG, indent=2) + "\n", encoding="utf-8")
    return path


async def _put(app: web.Application, body: Any) -> tuple[int, Any]:
    async with TestClient(TestServer(app)) as client:
        resp = await client.put(PATH, json=body)
        return resp.status, await resp.json()


@pytest.mark.parametrize("body", [_PLANTED, {"repos": []}, {}], ids=["plant", "wipe", "empty"])
async def test_non_owner_dashboard_put_is_refused_and_writes_nothing(
    config_path: Path, body: Any
) -> None:
    before = config_path.read_bytes()
    status, payload = await _put(_app(user=NON_OWNER), body)
    assert (status, payload.get("code")) == (403, "owner_only"), payload
    assert config_path.read_bytes() == before


async def test_missing_app_claim_is_refused(config_path: Path) -> None:
    before = config_path.read_bytes()
    status, _ = await _put(_app(user=OWNER, app_claim=None), _PLANTED)
    assert status == 403
    assert config_path.read_bytes() == before


async def test_non_owner_get_is_unchanged(config_path: Path) -> None:
    async with TestClient(TestServer(_app(user=NON_OWNER))) as client:
        resp = await client.get(PATH)
        assert resp.status == 200
        assert await resp.json() == _OWNER_CONFIG


async def test_owner_put_writes(config_path: Path) -> None:
    status, payload = await _put(_app(user=OWNER), _PLANTED)
    assert status == 200, payload
    assert json.loads(config_path.read_text(encoding="utf-8")) == _PLANTED


async def test_own_app_token_put_writes(config_path: Path) -> None:
    assert token_auth.app_token_path_allowed(APP, PATH)
    status, payload = await _put(_app(user=APP, app_claim=APP), _PLANTED)
    assert status == 200, payload
    assert json.loads(config_path.read_text(encoding="utf-8")) == _PLANTED


async def test_foreign_app_token_needs_a_manifest_grant(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = "other-app"
    monkeypatch.setattr(token_auth, "_app_api_allowlist", lambda name: ())
    assert not token_auth.app_token_path_allowed(foreign, PATH)

    monkeypatch.setattr(token_auth, "_app_api_allowlist", lambda name: (f"/api/apps/{APP}/*",))
    assert token_auth.app_token_path_allowed(foreign, PATH)
    status, payload = await _put(_app(user=foreign, app_claim=foreign), _PLANTED)
    assert status == 200, payload
    assert json.loads(config_path.read_text(encoding="utf-8")) == _PLANTED
