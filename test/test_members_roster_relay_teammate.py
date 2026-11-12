"""``GET /api/members`` for a team Crew teammate the Kiro relay verified.

The roster is the owner's view of each Bot, so a relay teammate reads a narrower
row: who the Bot is (name, slug, label, description, face, origin) and none of the
owner's view of it (the DM slot, the last DM line, the member-log projections, the
local workspace, model and settings). Driven over a real loopback listener with
the relay ingress, the client-sent strip and token auth chained as the gateway
orders them, like the People, sessions and mode reads. The owner's read is
unchanged, any other non-owner is still refused, and the member writes stay
owner-only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_dm_thread_helpers import _fake_config
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers import members as m
from kiro_crew.dashboard.kiro_relay.caller import RELAY_USER_HEADER, CrewKind, HostBinding
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import owner, people, routes_identity

pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "U0OWNER"
ANA = "kiro-user.teammate-ana-0002"
MADE = "code-reviewer"
SYNCED = "package-agent"

#: Every field the owner's row carries that is the owner's view of a Bot, not who
#: the Bot is. A teammate's row carries none of them.
OWNER_PRIVATE = (
    "slot_key",
    "running",
    "last_message",
    "last_message_stopped",
    "last_active_ts",
    "has_dm_message",
    "has_user_dm_message",
    "projections",
    "has_goal",
    "heartbeat_on",
    "welcome_owed",
    "welcome_claimed",
    "starred",
    "star_order",
    "workspace",
    "memory_store",
    "memory_version",
    "memory_owner",
    "kiro_agent",
    "model",
    "reasoning_effort",
    "worker_model",
    "worker_reasoning_effort",
    "approval_mode",
    "dynamic_capabilities",
    "dashboard_card_send",
    "triggers",
)


@pytest.fixture(autouse=True)
def _fresh(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    people.reset_for_tests()
    yield
    people.reset_for_tests()


def _config():
    cfg = _fake_config([MADE, SYNCED])
    cfg.agents[MADE] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        source="kirocrew",
        member_id="mem-0001",
        description="Reviews pull requests",
        display_name="Reviewer",
        model="m1",
        workspace="owner-private-workspace",
        starred=True,
        star_order=1,
    )
    cfg.agents[SYNCED] = KiroCrewAgentConfig(kiro_agent="aim-agent", source="package")
    return cfg


def _app(tmp_path) -> web.Application:
    ingress = RelayIngress(enabled=True)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM, owner_user_id=""))
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    state = _make_state(tmp_path)
    state.owner_id = OWNER
    owner.set_owner_subject(OWNER)
    app["state"] = state
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_get("/api/members", m.api_members)
    app.router.add_post("/api/members/{slug}/thread", m.api_member_thread)
    app.router.add_post("/api/members/{slug}/fresh-start", m.api_member_fresh_start)
    return app


def _teammate(client: TestClient) -> Any:
    return client.server.app[RELAY_INGRESS_APP_KEY].stamp_headers([], relay_user=ANA)


async def _get(client: TestClient, **kwargs: Any) -> tuple[int, bytes]:
    with patch.object(m.KiroCrewConfig, "load", return_value=_config()):
        resp = await client.get("/api/members", **kwargs)
        return resp.status, await resp.read()


@pytest.mark.asyncio
async def test_a_relay_teammate_reads_the_bots_without_the_owners_view(tmp_path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        status, raw = await _get(client, headers=_teammate(client))
    assert status == 200, raw
    rows = json.loads(raw)["members"]
    # Only the Bot a person created here: the package-synced spec reaches the
    # owner's list through the owner's own DM history, which is not the teammate's.
    assert [row["name"] for row in rows] == [MADE]
    row = rows[0]
    # The identity every per-member route is keyed on, as the owner's row spells it.
    assert row["slug"] == members_mod.member_slug(MADE, _config())
    assert row["display_name"] == "Reviewer"
    assert row["description"] == "Reviews pull requests"
    assert row["source"] == "kirocrew"
    assert row["dashboard_created"] is True
    for field in OWNER_PRIVATE:
        assert field not in row, field
    assert "private-workspace" not in raw.decode()


@pytest.mark.asyncio
async def test_a_relay_teammates_read_is_audited_under_their_relay_label(
    tmp_path, monkeypatch
) -> None:
    records: list[dict] = []
    monkeypatch.setattr(
        m, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    async with TestClient(TestServer(_app(tmp_path))) as client:
        status, _ = await _get(client, headers=_teammate(client))
    assert status == 200
    granted = [r for r in records if r.get("operation") == "members.list.read"]
    assert granted == [
        {
            "caller": f"relay:{ANA}",
            "operation": "members.list.read",
            "outcome": "allowed",
            "source": "dashboard",
            "resources": "relay_teammate",
        }
    ]


@pytest.mark.asyncio
async def test_the_owners_read_is_unchanged_by_the_teammate_branch(tmp_path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        token = generate_token(OWNER)
        status, raw = await _get(client, params={"token": token})
        with patch.object(routes_identity, "_relay_teammate", return_value=False):
            status_before, raw_before = await _get(client, params={"token": token})
    assert status == status_before == 200
    assert raw == raw_before
    row = next(r for r in json.loads(raw)["members"] if r["name"] == MADE)
    # The owner's own row still carries the owner's view of the Bot.
    for field in ("slot_key", "running", "last_message", "projections", "workspace", "model"):
        assert field in row, field
    assert row["starred"] is True


@pytest.mark.asyncio
async def test_a_non_owner_who_is_not_a_relay_teammate_is_still_refused(tmp_path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        token = generate_token("U0SOMEONE")
        # The relay's user header from a client is stripped, so it proves nothing.
        status, raw = await _get(client, params={"token": token}, headers={RELAY_USER_HEADER: ANA})
    assert status == 403, raw
    assert json.loads(raw)["code"] == "owner_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["thread", "fresh-start"])
async def test_a_relay_teammates_member_write_is_still_refused(tmp_path, route: str) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as client:
        with patch.object(m.KiroCrewConfig, "load", return_value=_config()):
            resp = await client.post(f"/api/members/{MADE}/{route}", headers=_teammate(client))
            body = await resp.json()
    assert resp.status == 403, body
    assert body["code"] == "owner_only"
