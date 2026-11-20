"""Tailnet mobile access on a tailnet that requires device approval.

A host signed in to such a tailnet sits in ``BackendState "NeedsMachineAuth"``
until an admin approves it. Another sign-in cannot help, so the probe marks it
apart from a signed-out host and the card names the admin console instead of
the sign-in step. Sibling of ``test_tailnet_mobile.py``, which pins the rest of
the step order.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.handlers import tailnet_mobile
from kiro_crew.dashboard.tailnet import DaemonProbe

_HOST = "desk.tail-abc.ts.net"


def _probe_for(state: str) -> DaemonProbe:
    with (
        patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"),
        patch.object(tailnet, "_run_json_detail", return_value=({"BackendState": state}, False)),
    ):
        return tailnet.probe_daemon()


def _step(probe: DaemonProbe) -> str:
    return tailnet_mobile._derive_step(
        pinned=False,
        probe=probe,
        trusted=True,
        startup_host=_HOST,
        published=True,
        port_free=True,
    )


def test_needs_machine_auth_is_marked_as_awaiting_approval() -> None:
    p = _probe_for("NeedsMachineAuth")
    assert (p.reachable, p.logged_in, p.name, p.needs_approval) == (True, False, "", True)
    assert "admin console" in p.detail
    assert "not signed in" not in p.detail


@pytest.mark.parametrize("state", ["NeedsLogin", "NoState"])
def test_signed_out_states_are_not_awaiting_approval(state: str) -> None:
    p = _probe_for(state)
    assert (p.logged_in, p.needs_approval) == (False, False)


def test_device_awaiting_approval_is_not_sent_to_sign_in() -> None:
    assert _step(_probe_for("NeedsMachineAuth")) == "approve_device"


def test_signed_out_host_still_derives_sign_in() -> None:
    assert _step(_probe_for("NeedsLogin")) == "sign_in"


def test_qr_refuses_a_device_awaiting_approval() -> None:
    assert tailnet_mobile._QR_REFUSALS["approve_device"][0] == "no_name"
