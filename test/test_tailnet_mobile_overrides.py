"""Phone-access setup's Fix this: removing the config.local.json overrides it names.

Setup answers ``409 config_overlay_conflict`` when ``config.local.json`` overrides a
setting it needs. These tests pin what the card's Fix this relies on: the conflict
lists each override with the value that would apply, and the clear route deletes
only the confirmed phone-access keys, for the owner only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tailnet_mobile_helpers import quiet_audit_fixture  # noqa: F401
from tailnet_mobile_helpers import unrestricted_fixture  # noqa: F401
from tailnet_mobile_helpers import _probe, _request

from kiro_crew.config.loader import config_local_path, config_path
from kiro_crew.dashboard.handlers import tailnet_mobile


@pytest.mark.asyncio
async def test_fix_removes_only_the_listed_override_then_setup_passes(
    _unrestricted, _quiet_audit
) -> None:
    """The conflict shows the overlay value beside the one that would apply, and the
    clear call deletes that key alone, so a retry succeeds."""
    config_path().write_text("{}", encoding="utf-8")
    local_path = config_local_path()
    local_path.write_text(
        json.dumps(
            {
                "agent": {"log_level": "debug"},
                "dashboard": {"tailscale": {"allowed_logins": ["someone@example.com"]}},
            }
        ),
        encoding="utf-8",
    )
    with (
        patch.object(tailnet_mobile.tailnet, "is_governance_pinned_off", return_value=False),
        patch.object(tailnet_mobile.tailnet, "probe_daemon", return_value=_probe()),
    ):
        conflict = await tailnet_mobile.api_tailnet_mobile_configure(_request())
        assert conflict.status == 409
        overrides = json.loads(conflict.body)["overrides"]
        assert overrides == [
            {
                "field": "dashboard.tailscale.allowed_logins",
                "local": ["someone@example.com"],
                "applies": ["owner@example.com"],
            }
        ]

        unrelated = await tailnet_mobile.api_tailnet_mobile_clear_overrides(
            _request(body={"fields": ["agent.log_level"]})
        )
        assert unrelated.status == 400
        cleared = await tailnet_mobile.api_tailnet_mobile_clear_overrides(
            _request(body={"fields": [row["field"] for row in overrides]})
        )
        assert cleared.status == 200
        assert json.loads(local_path.read_text(encoding="utf-8")) == {
            "agent": {"log_level": "debug"},
            "dashboard": {"tailscale": {}},
        }

        retried = await tailnet_mobile.api_tailnet_mobile_configure(_request())
    assert retried.status == 200


def test_the_route_accepts_every_field_setup_can_name() -> None:
    """A conflict the card offers to fix is always one the clear route accepts."""
    failing_every_check = SimpleNamespace(
        dashboard=SimpleNamespace(
            tailscale=SimpleNamespace(enabled=False, trust_identity=False, allowed_logins=[]),
            qr_session_until_restart=True,
            qr_session_persist_across_restart=False,
        )
    )
    _persistent, conflicts = tailnet_mobile._effective_mobile_setup(
        failing_every_check, "owner@example.com"
    )
    assert set(conflicts) == tailnet_mobile._OVERLAY_SETUP_FIELDS


@pytest.mark.asyncio
async def test_non_owner_cannot_clear_overrides(_unrestricted, _quiet_audit) -> None:
    with patch.object(tailnet_mobile, "update_config_locked") as write:
        resp = await tailnet_mobile.api_tailnet_mobile_clear_overrides(
            _request(user="telegram-11893", body={"fields": ["dashboard.tailscale.enabled"]})
        )
    assert resp.status == 403
    write.assert_not_called()
