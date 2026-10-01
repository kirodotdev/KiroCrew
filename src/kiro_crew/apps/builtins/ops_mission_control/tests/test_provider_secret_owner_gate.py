"""Owner gate on the provider-secret writers.

``PUT`` and ``DELETE /providers/{provider_id}/secret`` replace or revoke the
owner's incident-provider credentials, so only the dashboard owner may reach
them. Each row drives the routed handler (``_require_enabled`` included) with
the secret store swapped for a recorder, so no secret is written or read.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.ops_mission_control.backend import routes

_PATH = "/api/apps/ops-mission-control/providers/pagerduty/secret"
_OWNER = "owner-user"


class _Registry:
    def catalog(self):
        return [SimpleNamespace(id="pagerduty", secret_fields=("api_token",))]


@web.middleware
async def _identity(request: web.Request, handler):
    request["user"] = request.headers.get("X-Test-User", _OWNER)
    request["app"] = request.headers.get("X-Test-App", "")
    return await handler(request)


class TestProviderSecretOwnerGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.put_secret = mock.MagicMock()
        self.delete_secret = mock.MagicMock(return_value=True)
        for target, value in (
            ("get_registry", mock.MagicMock(return_value=_Registry())),
            ("put_secret", self.put_secret),
            ("delete_secret", self.delete_secret),
            ("is_app_enabled", mock.MagicMock(return_value=True)),
        ):
            patcher = mock.patch.object(routes, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = web.Application(middlewares=[_identity])
        app["state"] = SimpleNamespace(owner_id=_OWNER)
        routes.register_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def _put(self, headers: dict[str, str]):
        return await self.client.put(
            _PATH, json={"field": "api_token", "value": "caller-token"}, headers=headers
        )

    async def test_the_owner_saves_and_revokes(self) -> None:
        self.assertEqual((await self._put({})).status, 200)
        self.put_secret.assert_called_once_with("pagerduty", "api_token", "caller-token")
        self.assertEqual((await self.client.delete(_PATH)).status, 200)
        self.delete_secret.assert_called_once_with("pagerduty")

    async def test_a_non_owner_dashboard_subject_cannot_replace_a_secret(self) -> None:
        resp = await self._put({"X-Test-User": "someone-else"})
        self.assertEqual(resp.status, 403)
        self.assertEqual((await resp.json())["code"], "owner_only")
        self.put_secret.assert_not_called()

    async def test_a_non_owner_dashboard_subject_cannot_revoke_a_secret(self) -> None:
        resp = await self.client.delete(_PATH, headers={"X-Test-User": "someone-else"})
        self.assertEqual(resp.status, 403)
        self.assertEqual((await resp.json())["code"], "owner_only")
        self.delete_secret.assert_not_called()

    async def test_the_apps_own_token_cannot_touch_a_secret(self) -> None:
        token = {"X-Test-User": "app:ops-mission-control", "X-Test-App": "ops-mission-control"}
        self.assertEqual((await self._put(token)).status, 403)
        self.assertEqual((await self.client.delete(_PATH, headers=token)).status, 403)
        self.put_secret.assert_not_called()
        self.delete_secret.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
