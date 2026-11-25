"""A machine waiting for device approval reaches the phone-access card as its own step.

On a tailnet that requires device approval, tailscaled reports
``BackendState "NeedsMachineAuth"`` after sign-in until an admin approves the
machine. Another sign-in cannot clear that, so the card must not show the
sign-in step there; a signed-out machine still gets it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_tailnet_mobile import _request

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_state", "step"),
    [("NeedsMachineAuth", "approve_device"), ("NeedsLogin", "sign_in")],
)
async def test_a_machine_waiting_for_approval_is_not_sent_to_sign_in(
    backend_state: str, step: str
) -> None:
    """The real ``probe_daemon`` and ``_derive_step`` run on the status document."""
    status = {"BackendState": backend_state}
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
    assert json.loads(resp.body)["step"] == step
