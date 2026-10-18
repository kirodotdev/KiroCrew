"""Tests for the App Kit reverse proxy's signed principal claim.

The proxy signs a principal claim only for an app backend that opted in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.routes import register_app_routes


class TestSignedPrincipalProxy:
    """The proxy signs a principal claim only for a backend that opted in."""

    _SECRET = "test-secret-abc123"
    _OWNER_CONTEXT = {
        "user": "owner",
        "app": "",
        "is_dashboard_user": True,
        "auth_from_query_token": False,
    }

    @pytest.fixture(autouse=True)
    def _proxy_env(self, tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
        home = tmp_path / "kirocrew-home"
        app_dir = home / "apps" / "proxy-app"
        app_dir.mkdir(parents=True)
        _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
        (app_dir / ".app_secret").write_text(self._SECRET)
        (app_dir / "installed.json").write_text(
            json.dumps(
                {
                    "name": "proxy-app",
                    "version": "1.0.0",
                    "displayName": "Proxy App",
                    "enabled": True,
                    "origin": "local",
                    "resources": "gateway",
                    "lifecycle": "gateway",
                    "schemaVersion": 2,
                }
            )
        )
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as bridges_mod
        from kiro_crew.apps.routes import _app_secret_cache

        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir()
        _floor_monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
        bmod._processes.clear()
        bmod._allocated_ports.clear()
        _app_secret_cache.clear()
        self._app_dir = app_dir

    def _write_manifest(self, *, opted_in: bool) -> None:
        manifest: dict[str, Any] = {
            "name": "proxy-app",
            "version": "1.0.0",
            "displayName": "Proxy App",
            "description": "For proxy testing",
            "author": "tester",
        }
        if opted_in:
            manifest["backend"] = {"entryPoint": "server.py", "signedPrincipal": True}
        (self._app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))

    async def _proxy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        opted_in: bool = True,
        context: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        target: str = "/apps/proxy-app/api/claim-test",
        expected_status: int = 200,
        internal_secret: str = "",
        mixed_internal: bool = False,
        cookie_from: str = "",
        owner_id: str = "owner",
    ) -> tuple[dict[str, list[str]], str]:
        """Send one request through the proxy; return what the backend received.

        With *context*, a stub middleware stamps those authentication marks. Without
        it, the real ``token_auth_middleware`` authenticates the request. With
        *mixed_internal*, that middleware treats the proxied route as a mixed
        internal path, the class ``/apps/design-tweak/api/thread`` belongs to.
        """
        from types import SimpleNamespace

        import kiro_crew.apps.routes as routes
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        self._write_manifest(opted_in=opted_in)
        received: dict[str, list[str]] = {}

        async def capture(request: web.Request) -> web.Response:
            for key, value in request.raw_headers:
                received.setdefault(key.decode().lower(), []).append(value.decode())
            await request.read()
            return web.json_response({"ok": True})

        backend = web.Application()
        backend.router.add_route("*", "/{path:.*}", capture)
        runner = web.AppRunner(backend)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        monkeypatch.setattr(
            routes, "_resolve_app_backend_url", lambda _name: f"http://127.0.0.1:{port}"
        )

        if context is not None:

            @web.middleware
            async def stamp(request: web.Request, handler):
                for key, value in context.items():
                    request[key] = value
                return await handler(request)

            middlewares = [stamp]
        else:
            internal = frozenset({"/apps/proxy-app/api/claim-test"} if internal_secret else ())
            mixed = frozenset({"/apps/proxy-app/api/claim-test"} if mixed_internal else ())
            middlewares = [
                token_auth_middleware(
                    internal_paths=internal,
                    mixed_internal_paths=mixed,
                    internal_secret=internal_secret,
                )
            ]

        gateway = web.Application(middlewares=middlewares)
        gateway["state"] = SimpleNamespace(owner_id=owner_id)

        async def cookie_bootstrap(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        gateway.router.add_get("/cookie-bootstrap", cookie_bootstrap)
        register_app_routes(gateway)
        try:
            async with TestClient(TestServer(gateway)) as client:
                if cookie_from:
                    bootstrap = await client.get(cookie_from)
                    assert bootstrap.status == 200
                resp = await client.get(target, headers=headers)
                assert resp.status == expected_status
                body = await resp.text()
        finally:
            await runner.cleanup()
        return received, body

    @staticmethod
    def _only(received: dict[str, list[str]], name: str) -> str:
        values = received.get(name, [])
        assert len(values) == 1, f"expected one {name}, got {values!r}"
        return values[0]

    def _claim(self, received: dict[str, list[str]], target: str = "/api/claim-test"):
        from kiro_crew.apps.proxy_auth import (
            ProxyPrincipalReplayCache,
            verify_proxy_principal_claim,
        )

        return verify_proxy_principal_claim(
            self._only(received, "x-kirocrew-principal"),
            method="GET",
            target=target,
            body=b"",
            secret=self._SECRET,
            replay_cache=ProxyPrincipalReplayCache(),
        )

    @pytest.mark.asyncio
    async def test_owner_session_receives_owner_claim(self, monkeypatch):
        received, _ = await self._proxy(monkeypatch, context=self._OWNER_CONTEXT)
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("owner-session", "owner")

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_receives_none(self, monkeypatch):
        received, _ = await self._proxy(
            monkeypatch, context={**self._OWNER_CONTEXT, "user": "someone-else"}
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("none", "")

    @pytest.mark.asyncio
    async def test_app_token_receives_app_token_claim_without_an_owner(self, monkeypatch):
        received, _ = await self._proxy(
            monkeypatch,
            context={**self._OWNER_CONTEXT, "app": "proxy-app", "is_dashboard_user": False},
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("app-token", "")

    @pytest.mark.asyncio
    async def test_internal_auth_wins_over_owner_shaped_marks(self, monkeypatch):
        received, _ = await self._proxy(
            monkeypatch, context={**self._OWNER_CONTEXT, "internal_auth": True}
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("agent-tool", "")

    @pytest.mark.asyncio
    async def test_unknown_credential_source_receives_none(self, monkeypatch):
        """Without the positive cookie mark, an owner-shaped request is not the owner."""
        context = {k: v for k, v in self._OWNER_CONTEXT.items() if k != "auth_from_query_token"}
        received, _ = await self._proxy(monkeypatch, context=context)
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("none", "")

    @pytest.mark.asyncio
    async def test_app_without_opt_in_gets_no_principal_and_keeps_proxy_hmac(self, monkeypatch):
        from kiro_crew.apps.proxy_auth import verify_proxy_request

        received, _ = await self._proxy(monkeypatch, opted_in=False, context=self._OWNER_CONTEXT)
        assert "x-kirocrew-principal" not in received
        assert verify_proxy_request(
            self._only(received, "x-kirocrew-proxy"),
            method="GET",
            target="/api/claim-test",
            body=b"",
            secret=self._SECRET,
        )

    @pytest.mark.parametrize(
        "header_name", ["X-KiroCrew-Principal", "x-kirocrew-principal", "X-KiRoCrEw-PrInCiPaL"]
    )
    @pytest.mark.parametrize("opted_in", [False, True])
    @pytest.mark.asyncio
    async def test_caller_sent_principal_never_reaches_the_backend(
        self, monkeypatch, header_name, opted_in
    ):
        received, _ = await self._proxy(
            monkeypatch,
            opted_in=opted_in,
            context=self._OWNER_CONTEXT,
            headers={header_name: "forged"},
        )
        if not opted_in:
            assert "x-kirocrew-principal" not in received
        else:
            assert self._only(received, "x-kirocrew-principal") != "forged"
            claim = self._claim(received)
            assert claim is not None and claim.kind == "owner-session"

    @pytest.mark.asyncio
    async def test_caller_sent_proxy_header_is_replaced_by_the_gateway_one(self, monkeypatch):
        from kiro_crew.apps.proxy_auth import verify_proxy_request

        received, _ = await self._proxy(
            monkeypatch,
            opted_in=False,
            context=self._OWNER_CONTEXT,
            headers={"x-kirocrew-proxy": "forged"},
        )
        assert verify_proxy_request(
            self._only(received, "x-kirocrew-proxy"),
            method="GET",
            target="/api/claim-test",
            body=b"",
            secret=self._SECRET,
        )

    @pytest.mark.asyncio
    async def test_unsignable_owner_id_refuses_instead_of_forwarding(self, monkeypatch):
        received, body = await self._proxy(
            monkeypatch,
            context={**self._OWNER_CONTEXT, "user": "own\x00er"},
            owner_id="own\x00er",
            expected_status=502,
        )
        assert received == {}
        assert json.loads(body)["code"] == "proxy_principal_sign_failed"

    @pytest.mark.asyncio
    async def test_real_middleware_owner_cookie_receives_owner_claim(self, monkeypatch):
        from kiro_crew.dashboard.token_auth import generate_token

        received, _ = await self._proxy(
            monkeypatch, cookie_from=f"/cookie-bootstrap?token={generate_token('owner')}"
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("owner-session", "owner")

    @pytest.mark.asyncio
    async def test_real_middleware_owner_query_link_receives_none(self, monkeypatch):
        from kiro_crew.dashboard.token_auth import generate_token

        target = f"/api/claim-test?token={generate_token('owner')}"
        received, _ = await self._proxy(monkeypatch, target=f"/apps/proxy-app{target}")
        claim = self._claim(received, target=target)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("none", "")

    @pytest.mark.asyncio
    async def test_real_middleware_bearer_owner_credential_never_reaches_the_proxy(
        self, monkeypatch
    ):
        from kiro_crew.dashboard.token_auth import generate_token

        received, _ = await self._proxy(
            monkeypatch,
            headers={"Authorization": f"Bearer {generate_token('owner')}"},
            expected_status=403,
        )
        assert received == {}

    @pytest.mark.asyncio
    async def test_real_middleware_internal_call_with_owner_cookie_is_agent_tool(self, monkeypatch):
        from kiro_crew.dashboard.token_auth import generate_token

        secret = "test-internal-secret"
        received, _ = await self._proxy(
            monkeypatch,
            internal_secret=secret,
            cookie_from=f"/cookie-bootstrap?token={generate_token('owner')}",
            headers={"X-Internal-Secret": secret},
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("agent-tool", "")

    @pytest.mark.asyncio
    async def test_real_middleware_owner_cookie_on_mixed_internal_path_receives_owner_claim(
        self, monkeypatch
    ):
        """The internal-path browser branch marks a cookie credential like the main path."""
        from kiro_crew.dashboard.token_auth import generate_token

        received, _ = await self._proxy(
            monkeypatch,
            mixed_internal=True,
            cookie_from=f"/cookie-bootstrap?token={generate_token('owner')}",
        )
        claim = self._claim(received)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("owner-session", "owner")

    @pytest.mark.asyncio
    async def test_real_middleware_owner_query_link_on_mixed_internal_path_receives_none(
        self, monkeypatch
    ):
        from kiro_crew.dashboard.token_auth import generate_token

        target = f"/api/claim-test?token={generate_token('owner')}"
        received, _ = await self._proxy(
            monkeypatch, mixed_internal=True, target=f"/apps/proxy-app{target}"
        )
        claim = self._claim(received, target=target)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("none", "")

    @pytest.mark.asyncio
    async def test_real_middleware_stale_link_beside_owner_cookie_on_mixed_internal_path_is_owner(
        self, monkeypatch
    ):
        """The mark names the credential that authenticated, not the one presented.

        A dead ``?token=`` beside a live cookie authenticates on the cookie, so the
        caller is the owner's session even though the URL still carries a link.
        """
        from kiro_crew.dashboard.token_auth import generate_token

        with patch("kiro_crew.dashboard.token_auth.time") as mock_time:
            mock_time.time.return_value = 1000.0
            stale = generate_token("owner", ttl_seconds=300)
        target = f"/api/claim-test?token={stale}"
        received, _ = await self._proxy(
            monkeypatch,
            mixed_internal=True,
            target=f"/apps/proxy-app{target}",
            cookie_from=f"/cookie-bootstrap?token={generate_token('owner')}",
        )
        claim = self._claim(received, target=target)
        assert claim is not None
        assert (claim.kind, claim.owner_id) == ("owner-session", "owner")
