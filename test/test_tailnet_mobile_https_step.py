"""A tailnet without HTTPS reaches the phone-access card as the HTTPS step.

tailscaled sends ``"CertDomains": null`` when the tailnet grants no certificate
names. The card must then link to the HTTPS settings rather than offer the
one-click setup, whose publish cannot succeed there.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_tailnet_mobile import _HOST, _request

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile


@pytest.mark.asyncio
async def test_null_cert_domains_reaches_the_card_as_enable_https() -> None:
    """The real ``probe_daemon`` and ``_derive_step`` run on the status document,
    with the origin not yet trusted and nothing published."""
    status = {
        "BackendState": "Running",
        "Self": {"DNSName": f"{_HOST}.", "UserID": 1},
        "CurrentTailnet": {"MagicDNSSuffix": _HOST.split(".", 1)[1]},
        "CertDomains": None,
    }
    free = tailnet_mobile.tailnet_serve.ServeState(
        published=False, configured=False, detail="none", port_free=True
    )
    with (
        patch.object(tailnet_mobile, "_is_restricted_session", return_value=False),
        patch.object(tailnet_mobile, "_audit"),
        patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
        patch.object(tailnet, "_run_json_detail", return_value=(status, False)),
        patch.object(tailnet, "_run_json", return_value=status),
        patch.object(tailnet_mobile.tailnet_serve, "serve_state", return_value=free),
    ):
        resp = await tailnet_mobile.api_tailnet_mobile_status(_request())
    assert json.loads(resp.body)["step"] == "enable_https"
