"""The phone-access QR refuses a caller pinned to one device.

A session pinned at node scope lends that pin to any child it mints, so its QR
could sign in only the device already showing it. The mint names that reason
instead of answering 200 with a code every other device is refused.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_tailnet_mobile import _HOST, _machine, _owner_session_token, _request

from kiro_crew.dashboard.handlers import tailnet_mobile


@pytest.mark.asyncio
async def test_a_node_pinned_caller_is_refused_instead_of_minting_a_dead_code() -> None:
    caller = _owner_session_token(
        require_peer="1", peer_key="ts:node:owner@example.com|ipad.tail.ts.net"
    )
    with (
        _machine(),
        patch.object(tailnet_mobile, "_is_restricted_session", return_value=False),
        patch.object(tailnet_mobile, "_audit") as audit,
        patch.object(tailnet_mobile, "generate_token") as mint,
    ):
        resp = await tailnet_mobile.api_tailnet_mobile_qr(
            _request(tailnet_host=_HOST, cookie_token=caller)
        )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "caller_device_bound"
    mint.assert_not_called()
    assert audit.call_args.args[1:] == ("tailnet.mobile.qr", "denied", "caller-device-bound")
