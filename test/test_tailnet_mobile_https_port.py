"""The phone-access card on a configured ``dashboard.tailscale.https_port``.

Every serve read uses the configured port, the origin and the QR URL carry it, and a
port changed after the gateway started is never ``ready``: the running server trusts
the origin it was started with, so the card asks for the restart and mints nothing.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from tailnet_mobile_helpers import quiet_audit_fixture, unrestricted_fixture  # noqa: F401
from test_tailnet_mobile import _HOST, _machine, _request

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile


def _ours_on(ours: int):
    def _state(_port, https_port=443):
        mine = https_port == ours
        return tailnet_mobile.tailnet_serve.ServeState(
            published=mine, configured=True, detail="", port_free=not mine
        )

    return _state


def _request_started_on(https_port: int):
    req = _request(tailnet_host=_HOST)
    state = tailnet.TailnetOriginState(host=_HOST, resolved_at=1)
    state.https_port = https_port
    req.app["tailnet_origin_state"] = state
    return req


def _configured(port: int):
    return (
        patch.object(
            tailnet_mobile.tailnet_serve, "configured_https_port", lambda: port, create=True
        ),
        patch.object(tailnet_mobile.tailnet_serve, "serve_state", side_effect=_ours_on(port)),
        patch.object(tailnet_mobile, "generate_token", return_value="tok"),
        patch.object(tailnet_mobile, "render_qr_data_uri", return_value="data:image/png;base64,x"),
    )


class TestAPortChangedAfterStart:
    @pytest.mark.asyncio
    async def test_the_card_asks_for_a_restart_and_mints_nothing(
        self, _unrestricted, _quiet_audit
    ) -> None:
        port, state, mint, render = _configured(5479)
        with _machine(), port, state, mint, render:
            status = await tailnet_mobile.api_tailnet_mobile_status(_request_started_on(443))
            qr = await tailnet_mobile.api_tailnet_mobile_qr(_request_started_on(443))
        assert json.loads(status.body)["step"] == "restart_gateway"
        assert qr.status == 409
        assert json.loads(qr.body)["code"] == "restart_required"


class TestAPortTheGatewayStartedWith:
    @pytest.mark.asyncio
    async def test_ready_with_the_port_in_the_origin_and_the_qr_url(
        self, _unrestricted, _quiet_audit
    ) -> None:
        port, state, mint, render = _configured(5479)
        with _machine(), port, state, mint, render:
            status = await tailnet_mobile.api_tailnet_mobile_status(_request_started_on(5479))
            qr = await tailnet_mobile.api_tailnet_mobile_qr(_request_started_on(5479))
        body = json.loads(status.body)
        assert (body["step"], body["origin"]) == ("ready", f"https://{_HOST}:5479")
        assert qr.status == 200
        assert json.loads(qr.body)["url"].startswith(f"https://{_HOST}:5479/")
