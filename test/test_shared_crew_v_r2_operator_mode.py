"""The local operator's own sessions never make a personal Crew read as network.

``GET /api/shared-crew/mode`` counts session creators as people seen. A creator
is rebuilt from the session's public attribution, which carries no provenance,
so it is matched against the viewer's own server-derived principal (and the
machine-local bootstrap subjects of the same operator) by id. A creator dict
never makes anyone the operator: only its id is compared, and a real second
principal still turns the Crew to ``network``.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all
from kiro_crew.shared_crew import crew_mode, people, routes_identity
from kiro_crew.shared_crew.attribution import creator_of_public, public_attribution
from kiro_crew.shared_crew.contracts import (
    AttributionAvailability,
    Principal,
    Provenance,
    SessionAttribution,
)
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal

STARTUP = local_operator_principal("local-startup")
APP_ALIAS = local_operator_principal("local-app")
ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)


def _row(key: str, creator: Principal) -> dict[str, Any]:
    value = SessionAttribution(
        creator=creator, availability=AttributionAvailability.RECORDED, created_at=1.0
    )
    return {"key": key, "attribution": public_attribution(value)}


def _legacy_row(key: str) -> dict[str, Any]:
    return {"key": key, "attribution": public_attribution(SessionAttribution.legacy())}


def _creators(*rows: dict[str, Any]) -> list[Principal]:
    return [creator_of_public(r["attribution"]) for r in rows]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"shared-crew-r2-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    people.reset_for_tests()
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.crons = None
    yield st
    people.reset_for_tests()


@contextlib.asynccontextmanager
async def _serve(state, rows: list[dict[str, Any]], monkeypatch):
    async def _rows(_request: web.Request) -> list[dict[str, Any]]:
        return list(rows)

    monkeypatch.setattr(routes_identity, "_catalog_rows", _rows)
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    async with TestClient(TestServer(app)) as client:
        yield client


async def _mode(state, rows, monkeypatch, subject: str = "local-startup") -> str:
    token = token_auth.generate_token(subject)
    async with _serve(state, rows, monkeypatch) as client:
        resp = await client.get(routes_identity.MODE_PATH, params={"token": token})
        assert resp.status == 200, await resp.text()
        return str((await resp.json())["mode"])


# ── the gateway route ──


@pytest.mark.asyncio
async def test_operator_own_session_keeps_the_crew_personal(state, monkeypatch):
    rows = [_row("dashboard_personal-notes", STARTUP), _legacy_row("dashboard_starter")]
    assert await _mode(state, rows, monkeypatch) == "personal"


@pytest.mark.asyncio
async def test_bootstrap_alias_session_is_the_same_operator(state, monkeypatch):
    rows = [_row("dashboard_a", APP_ALIAS), _row("dashboard_b", STARTUP)]
    assert await _mode(state, rows, monkeypatch) == "personal"


@pytest.mark.asyncio
async def test_bootstrap_session_is_the_configured_owner(state, monkeypatch):
    state.owner_id = "owner-1"
    rows = [_row("dashboard_old", STARTUP)]
    assert await _mode(state, rows, monkeypatch, subject="owner-1") == "personal"


@pytest.mark.asyncio
async def test_another_person_session_still_turns_network(state, monkeypatch):
    rows = [_row("dashboard_mine", STARTUP), _row("dashboard_alice", ALICE)]
    assert await _mode(state, rows, monkeypatch) == "network"


@pytest.mark.asyncio
async def test_another_local_operator_session_still_turns_network(state, monkeypatch):
    rows = [_row("dashboard_mine", STARTUP), _row("dashboard_u", local_operator_principal("u-2"))]
    assert await _mode(state, rows, monkeypatch) == "network"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", [routes_identity.MODE_PATH, routes_identity.SESSIONS_PATH, routes_identity.PEOPLE_PATH]
)
async def test_a_non_owner_dashboard_session_is_refused(state, monkeypatch, path):
    """A channel allowlist link holds a dashboard token too; it reads no Shared Crew rows."""
    rows = [_row("dashboard_owner", APP_ALIAS)]
    token = token_auth.generate_token("u-2")
    async with _serve(state, rows, monkeypatch) as client:
        resp = await client.get(path, params={"token": token})
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"


@pytest.mark.asyncio
async def test_a_forged_local_provenance_creator_still_counts(state, monkeypatch):
    forged = _row("dashboard_x", ALICE)
    forged["attribution"]["creator"]["provenance"] = Provenance.LOCAL_OPERATOR.value
    forged["attribution"]["creator"]["provider"] = "local"
    assert await _mode(state, [forged], monkeypatch) == "network"


# ── the pure decision ──


def test_operator_creator_is_not_another_person():
    creators = _creators(_row("a", STARTUP))
    assert crew_mode.others_observed(STARTUP, [STARTUP], creators=creators) is False


def test_creator_provenance_is_never_read():
    creator = Principal(
        id=ALICE.id,
        kind=ALICE.kind,
        provider="local",
        issuer="crew",
        subject="local-startup",
        provenance=Provenance.LOCAL_OPERATOR,
    )
    assert crew_mode.others_observed(STARTUP, [], creators=[creator]) is True


def test_unknown_viewer_counts_every_person_creator():
    creators = _creators(_row("a", STARTUP))
    assert crew_mode.others_observed(Principal.unknown(), [], creators=creators) is True


def test_unattributed_and_system_creators_never_count():
    creators = _creators(_legacy_row("a"), _row("b", Principal.system("cron")))
    assert crew_mode.others_observed(STARTUP, [], creators=creators) is False


def test_operator_ids_widen_only_for_a_local_bootstrap_or_owner_viewer():
    assert crew_mode.operator_ids(STARTUP) == {STARTUP.id, APP_ALIAS.id}
    owner = local_operator_principal("owner-1")
    assert crew_mode.operator_ids(owner, owner_id="owner-1") == {
        owner.id,
        STARTUP.id,
        APP_ALIAS.id,
    }
    other = local_operator_principal("u-2")
    assert crew_mode.operator_ids(other) == {other.id}
    assert crew_mode.operator_ids(ALICE) == {ALICE.id}
    assert crew_mode.operator_ids(Principal.unknown()) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(("direct_admitted", "passes"), [(False, False), (True, True)])
async def test_only_the_direct_listener_admission_skips_the_owner_gate(
    monkeypatch, direct_admitted, passes
):
    """A token whose request only resolves to a tailnet peer is still owner-gated."""
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import _shared_access
    from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Transport
    from kiro_crew.shared_crew.identity import tailnet_principal

    refused = web.json_response({"code": "owner_only"}, status=403)

    async def owner_gate(_request, _operation):
        return refused

    monkeypatch.setattr(_shared_access, "require_owner_for_dashboard_caller", owner_gate)
    request = make_mocked_request("GET", routes_identity.SESSIONS_PATH)
    request["is_dashboard_user"] = True
    request["app"] = ""
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=tailnet_principal("mallory@example.com", "phone", Provenance.TAILSCALE_DIRECT),
        request_id="r",
        transport=Transport.HTTP,
    )
    if direct_admitted:
        request["auth_tailnet_direct"] = True
    result = await routes_identity._refusal(request)
    assert (result is None) is passes
