"""The usage poll asks kiro-cli nothing on a gateway where nothing signs in via kiro-cli.

``GET /api/sessions/usage`` is polled by every open dashboard tab. Its gate
(``reject_if_kiro_unverified`` -> ``verified_ready``) re-probes with ``kiro-cli
whoami`` after a skipped status probe, and its refresh runs the ``kiro-cli /usage``
scrape. On a claude default with no slot routed to kiro or kas no session uses
that sign-in, so the poll answers "no reading" without either. A slot routed to
Kiro CLI keeps the gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_usage_helpers import _reset_usage_globals

import kiro_crew.dashboard.handlers.sessions as sessions_mod
from kiro_crew import members
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

pytestmark = pytest.mark.usefixtures("no_ambient_kiro_api_key")


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _reset_usage_globals()
    yield
    _reset_usage_globals()


def _config(monkeypatch: pytest.MonkeyPatch, agents: dict[str, Any]) -> None:
    config = SimpleNamespace(
        agent=SimpleNamespace(
            acp_backend=ACP_BACKEND_CLAUDE, kas_bundle=False, member_acp_backend="claude"
        ),
        agents=agents,
    )
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", lambda: config)
    picks = members._SESSION_BACKEND_PICKS
    for key in list(picks):
        monkeypatch.delitem(picks, key)


def _request() -> MagicMock:
    request = MagicMock()
    request.app = {"state": SimpleNamespace(_background_tasks=set())}
    return request


@pytest.mark.asyncio
async def test_a_claude_only_gateway_poll_runs_no_gate_and_no_usage_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(monkeypatch, agents={})
    request = _request()
    gate = AsyncMock(return_value=None)
    with (
        patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
        patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()) as fetch,
    ):
        resp = await sessions_mod.api_sessions_usage(request)

    # The gate is the path to whoami; the refresh is the path to `kiro-cli /usage`.
    gate.assert_not_awaited()
    fetch.assert_not_called()
    assert request.app["state"]._background_tasks == set()
    assert resp.status == 200
    assert resp.text is not None and '"available": false' in resp.text


@pytest.mark.asyncio
async def test_a_slot_routed_to_kiro_cli_keeps_the_usage_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(monkeypatch, agents={"helper": SimpleNamespace(acp_backend="kiro")})
    gate = AsyncMock(return_value=None)
    with (
        patch.object(sessions_mod, "_vault_owns_usage", AsyncMock(return_value=False)),
        patch.object(sessions_mod, "reject_if_kiro_unverified", gate),
        patch.object(sessions_mod, "_fetch_usage_bg", AsyncMock()),
    ):
        await sessions_mod.api_sessions_usage(_request())

    gate.assert_awaited_once()
