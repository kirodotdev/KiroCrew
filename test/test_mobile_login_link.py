"""Regression coverage for the authenticated mobile-login recovery link."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from aiohttp import web

from kiro_crew.dashboard.handlers import auth_mobile
from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS


def _request(*, user: str = "alice", app: str = "", dashboard_url: str = "") -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.get.side_effect = {"user": user, "app": app}.get
    request.app = {"dashboard_url": dashboard_url}
    request.headers = {"Origin": "https://dashboard.example"}
    return request


def _call(request: MagicMock, *, valid_origin: bool = True):
    with patch("kiro_crew.dashboard.handlers.auth_mobile.check_origin", return_value=valid_origin):
        return asyncio.run(auth_mobile.api_auth_mobile_link(request))


def test_mobile_link_uses_configured_external_origin():
    response = _call(_request(dashboard_url="https://dashboard.example"))

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["url"].startswith("https://dashboard.example?token=")
    assert "localhost" not in payload["url"]
    assert payload["expires_in"] == LINK_WINDOW_SECS
    assert response.headers["Cache-Control"] == "no-store"


def test_mobile_link_refuses_missing_external_origin():
    response = _call(_request())

    assert response.status == 409
    assert json.loads(response.text) == {
        "error": "external_origin_unavailable",
        "code": "external_origin_unavailable",
    }


def test_mobile_link_refuses_app_scoped_token():
    response = _call(_request(app="calendar", dashboard_url="https://dashboard.example"))

    assert response.status == 403
    assert json.loads(response.text) == {
        "error": "app_token_forbidden",
        "code": "app_token_forbidden",
    }


def test_mobile_link_requires_an_authenticated_dashboard_session():
    response = _call(_request(user="", dashboard_url="https://dashboard.example"))

    assert response.status == 401
    assert json.loads(response.text) == {"error": "unauthenticated", "code": "unauthenticated"}


def test_mobile_link_refuses_invalid_origin():
    response = _call(_request(dashboard_url="https://dashboard.example"), valid_origin=False)

    assert response.status == 403
    assert json.loads(response.text) == {"error": "bad_origin", "code": "bad_origin"}
