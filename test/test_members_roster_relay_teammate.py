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

import asyncio
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
    # Who has written in the owner's DM thread.
    "participants",
    "participants_dropped",
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
        approval_mode="normal",
        model="m1",
        workspace="owner-private-workspace",
        starred=True,
        star_order=1,
    )
    # Normal approvals too, so a refusal of it can only be the roster's.
    cfg.agents[SYNCED] = KiroCrewAgentConfig(
        kiro_agent="aim-agent", source="package", approval_mode="normal"
    )
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
    app.router.add_get("/api/members/{slug}/rules", m.api_member_rules_get)
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
    for field in (
        "slot_key",
        "running",
        "last_message",
        "projections",
        "workspace",
        "model",
        "participants",
    ):
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


def _slug(name: str) -> str:
    return members_mod.member_slug(name, _config())


async def _call(
    client: TestClient, method: str, path: str, **kwargs: Any
) -> tuple[int, dict[str, Any]]:
    with patch.object(m.KiroCrewConfig, "load", return_value=_config()):
        resp = await client.request(method, path, **kwargs)
        body = await resp.json()
    return resp.status, body


@pytest.mark.asyncio
async def test_a_relay_teammate_opens_a_bots_chat_as_they_open_a_session(
    tmp_path, monkeypatch
) -> None:
    records: list[dict] = []
    monkeypatch.setattr(
        m, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    path = f"/api/members/{_slug(MADE)}/thread"
    async with TestClient(TestServer(_app(tmp_path))) as client:
        status, body = await _call(client, "POST", path, headers=_teammate(client))
        owner_status, owner_body = await _call(
            client, "POST", path, params={"token": generate_token(OWNER)}
        )
    # Past the gate, the teammate's open runs the owner's own path and ends where it
    # does (this fixture has no memory store for the Bot, so both stop there).
    assert status != 403, body
    assert (status, body.get("code")) == (owner_status, owner_body.get("code"))
    assert {
        "caller": f"relay:{ANA}",
        "operation": "members.thread",
        "outcome": "allowed",
        "source": "dashboard",
        "resources": "relay_teammate",
    } in records


@pytest.mark.asyncio
async def test_a_relay_teammate_is_refused_a_bots_rules(tmp_path) -> None:
    # The rules are the owner's private safety instructions, and disclosure cannot
    # be undone, so the read stays owner-only like the write.
    async with TestClient(TestServer(_app(tmp_path))) as client:
        status, body = await _call(
            client,
            "GET",
            f"/api/members/{_slug(MADE)}/rules",
            params={"member": MADE},
            headers=_teammate(client),
        )
    assert status == 403, body
    assert body["code"] == "owner_only"


@pytest.mark.asyncio
async def test_a_relay_teammate_is_refused_a_bot_outside_their_roster(tmp_path) -> None:
    # The package-synced spec is not on the teammate's roster, so its chat is not theirs.
    async with TestClient(TestServer(_app(tmp_path))) as client:
        status, body = await _call(
            client, "POST", f"/api/members/{_slug(SYNCED)}/thread", headers=_teammate(client)
        )
    assert status == 403, body
    assert body["code"] == "owner_only"
    assert "reason" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", f"/api/members/{_slug(MADE)}/rules"),
        ("PUT", f"/api/members/{_slug(MADE)}/goals/schedules"),
        ("GET", f"/api/members/{_slug(MADE)}/briefing?member={MADE}"),
        ("DELETE", f"/api/agents/{MADE}"),
        ("PUT", f"/api/agents/{MADE}"),
    ],
)
async def test_a_relay_teammate_is_still_refused_the_owner_only_bot_routes(
    tmp_path, method: str, path: str
) -> None:
    app = _app(tmp_path)
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.handlers import member_goals

    app.router.add_put("/api/members/{slug}/rules", m.api_member_rules_put)
    app.router.add_put(
        "/api/members/{slug}/goals/schedules", member_goals.api_member_goals_schedules
    )
    app.router.add_get("/api/members/{slug}/briefing", m.api_member_briefing)
    app.router.add_delete("/api/agents/{name}", handlers.api_kirocrew_agent_delete)
    app.router.add_put("/api/agents/{name}", handlers.api_kirocrew_agent_update)
    async with TestClient(TestServer(app)) as client:
        status, body = await _call(
            client, method, path, json={"member": MADE, "rules": "x"}, headers=_teammate(client)
        )
    assert status == 403, body
    assert body["code"] == "owner_only"


def _create_app(tmp_path) -> web.Application:
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.handlers import member_export

    app = _app(tmp_path)
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_post("/api/members/{slug}/export", member_export.api_member_export)
    app.router.add_post(
        "/api/members/{slug}/export/lessons-draft", member_export.api_member_export_lessons_draft
    )
    return app


@pytest.mark.asyncio
async def test_a_relay_teammate_passes_the_create_gate_as_the_owner_does(tmp_path) -> None:
    # A nameless create stops at the handler's own first check, so reaching it
    # proves the gate let the teammate through without writing a Bot.
    body = {"name": "", "kiro_agent": "kirocrew"}
    async with TestClient(TestServer(_create_app(tmp_path))) as client:
        status, out = await _call(
            client, "POST", "/api/agents", json=body, headers=_teammate(client)
        )
        owner_status, owner_out = await _call(
            client, "POST", "/api/agents", json=body, params={"token": generate_token(OWNER)}
        )
    assert (status, out) == (owner_status, owner_out) == (400, {"error": "Agent name is required"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"kiro_agent": "aim-agent"}, "kiro_agent"),
        ({"kiro_agent": "kirocrew", "approval_mode": "trust"}, "approval_mode"),
        ({"kiro_agent": "kirocrew", "source": "package"}, "source"),
        ({"kiro_agent": "kirocrew", "triggers": "deploy, release"}, "triggers"),
        ({"kiro_agent": "kirocrew", "workspace": "default"}, "workspace"),
    ],
)
async def test_a_relay_teammates_create_cannot_set_a_crew_level_field(
    tmp_path, extra: dict, field: str
) -> None:
    body = {"name": "scout", **extra}
    async with TestClient(TestServer(_create_app(tmp_path))) as client:
        status, out = await _call(
            client, "POST", "/api/agents", json=body, headers=_teammate(client)
        )
    assert status == 403, out
    assert out["code"] == "owner_only"
    assert out["fields"] == [field]


@pytest.mark.asyncio
async def test_a_relay_teammates_refused_create_is_audited_denied_not_allowed(
    tmp_path, monkeypatch
) -> None:
    records: list[dict] = []
    monkeypatch.setattr(
        m, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    body = {"name": "scout", "kiro_agent": "aim-agent"}
    async with TestClient(TestServer(_create_app(tmp_path))) as client:
        status, _ = await _call(client, "POST", "/api/agents", json=body, headers=_teammate(client))
    assert status == 403
    create_rows = [r for r in records if r.get("operation") == "agent.create"]
    assert create_rows == [
        {
            "caller": f"relay:{ANA}",
            "operation": "agent.create",
            "outcome": "denied",
            "source": "dashboard",
            "resources": "fields=kiro_agent",
        }
    ]


@pytest.mark.asyncio
async def test_a_relay_teammates_bot_is_pinned_to_normal_approvals(tmp_path, monkeypatch) -> None:
    # An unset mode would open the Bot's thread in trust. The mode check sees the
    # pin; the bad display name then stops the create before anything is written.
    from kiro_crew import approval_mode_policy

    asked: list[str] = []
    monkeypatch.setattr(
        approval_mode_policy, "session_mode_permitted", lambda mode: asked.append(mode) or True
    )
    body = {"name": "scout", "kiro_agent": "kirocrew", "display_name": 7}
    async with TestClient(TestServer(_create_app(tmp_path))) as client:
        status, out = await _call(
            client, "POST", "/api/agents", json=body, headers=_teammate(client)
        )
        owner_status, _ = await _call(
            client, "POST", "/api/agents", json=body, params={"token": generate_token(OWNER)}
        )
    assert (status, out["code"]) == (400, "invalid_display_name")
    assert owner_status == 400
    # Asked once, for the teammate's create only: the owner's unset mode stays unset.
    assert asked == ["normal"]


@pytest.mark.asyncio
async def test_a_relay_teammate_exports_a_bot_but_not_its_lessons_draft(tmp_path) -> None:
    path = f"/api/members/{_slug(MADE)}/export"
    async with TestClient(TestServer(_create_app(tmp_path))) as client:
        # No member named: the export's own first body check answers, past the gate.
        status, out = await _call(client, "POST", path, json={}, headers=_teammate(client))
        draft_status, draft = await _call(
            client,
            "POST",
            path + "/lessons-draft",
            json={"member": MADE},
            headers=_teammate(client),
        )
    assert (status, out["code"]) == (400, "missing_member")
    assert (draft_status, draft["code"]) == (403, "owner_only")


@pytest.mark.asyncio
async def test_a_relay_teammates_export_carries_no_rules_or_crons(tmp_path) -> None:
    # The rules read is owner-only, and the crons are the owner's scheduled jobs, so
    # the export carries neither to a teammate whatever the body asks; the owner's
    # export is unchanged.
    from kiro_crew import crewmate_bundle_export as export_mod

    asked: list[tuple[bool, bool]] = []
    real_plan = export_mod.plan_export

    def _plan(sources, slug, choices):
        asked.append((choices.rules, choices.crons))
        return real_plan(sources, slug, choices)

    async def _no_jobs(**_kw):
        return []

    body = {"member": MADE, "preview": True, "rules": True, "crons": True}
    path = f"/api/members/{_slug(MADE)}/export"
    app = _create_app(tmp_path)
    app["state"].crons = SimpleNamespace(list_jobs_async=_no_jobs)
    with (
        patch.object(members_mod, "read_member_rules", return_value="OWNER RULE"),
        patch.object(export_mod, "plan_export", side_effect=_plan),
    ):
        async with TestClient(TestServer(app)) as client:
            status, mate = await _call(client, "POST", path, json=body, headers=_teammate(client))
            params = {"token": generate_token(OWNER)}
            owner_status, owner = await _call(client, "POST", path, json=body, params=params)
    assert (status, owner_status) == (200, 200), (mate, owner)
    assert asked == [(False, False), (True, True)]
    assert "OWNER RULE" not in json.dumps(mate)
    assert "OWNER RULE" in json.dumps(owner)


def _saved_bot(mode: str) -> str:
    """A person-created Bot with its own memory store and *mode*, saved to the config.

    Returns its slug. Real config on disk, so the thread open runs the owner's path
    end to end (binding, store pin, approval seed) instead of stopping at a stub.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents[MADE] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", source="kirocrew", approval_mode=mode
    )
    provision_member_memory(cfg, MADE)
    cfg.save()
    return members_mod.member_slug(MADE, KiroCrewConfig.load())


def _live_app(tmp_path) -> web.Application:
    from kiro_crew.dashboard.handlers import members_thread_turns

    app = _app(tmp_path)
    app.router.add_post("/api/members/{slug}/greet", members_thread_turns.api_member_greet)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["", "trust", "normal"])
@pytest.mark.parametrize("route", ["thread", "greet", "fresh-start"])
async def test_a_relay_teammate_is_admitted_regardless_of_bot_approval_mode(
    tmp_path, route: str, mode: str
) -> None:
    slug = await asyncio.to_thread(_saved_bot, mode)
    app = _live_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/members/{slug}/{route}", headers=_teammate(client))
        body = await resp.json()
    assert resp.status != 403, body
    if route == "thread":
        assert resp.status == 200, body
        slot = app["state"].get_slot(body["slot_key"])
        assert slot._trust is (mode != "normal")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "operation"),
    [
        ("POST", "/api/members/{slug}/greet", "members.greet"),
        ("POST", "/api/members/{slug}/fresh-start", "members.fresh_start"),
        ("GET", "/api/members/{slug}/projections?member={name}", "members.projections.read"),
        ("GET", "/api/members/{slug}/activity?member={name}", "members.activity.read"),
        ("GET", "/api/members/{slug}/goals?member={name}", "members.goals.read"),
        ("GET", "/api/agents/{name}/avatar", "agent.avatar_get"),
    ],
)
async def test_a_relay_teammate_is_admitted_to_each_bot_route_and_audited(
    tmp_path, monkeypatch, method: str, path: str, operation: str
) -> None:
    # Past the gate, each route answers what it answers the owner (no greeting owed,
    # no thread to clear, no picture), never the owner gate's 403.
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.handlers import member_goals

    records: list[dict] = []
    monkeypatch.setattr(
        m, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: records.append(kw))
    )
    slug = await asyncio.to_thread(_saved_bot, "normal")
    app = _live_app(tmp_path)
    app.router.add_get("/api/members/{slug}/projections", m.api_member_projections)
    app.router.add_get("/api/members/{slug}/activity", m.api_member_activity)
    app.router.add_get("/api/members/{slug}/goals", member_goals.api_member_goals_get)
    app.router.add_get("/api/agents/{name}/avatar", handlers.api_kirocrew_agent_avatar_get)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(
            method, path.format(slug=slug, name=MADE), headers=_teammate(client)
        )
        text = await resp.text()
    assert resp.status != 403, text
    assert {
        "caller": f"relay:{ANA}",
        "operation": operation,
        "outcome": "allowed",
        "source": "dashboard",
        "resources": "relay_teammate",
    } in records


@pytest.mark.asyncio
async def test_the_owner_still_opens_an_unset_bot_in_trust(tmp_path) -> None:
    slug = await asyncio.to_thread(_saved_bot, "")
    app = _live_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/members/{slug}/thread", params={"token": generate_token(OWNER)}
        )
        body = await resp.json()
    assert resp.status == 200, body
    assert app["state"].get_slot(body["slot_key"])._trust is True


@pytest.mark.asyncio
async def test_a_relay_teammate_opens_a_normal_bot_without_trust(tmp_path) -> None:
    slug = await asyncio.to_thread(_saved_bot, "normal")
    app = _live_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/members/{slug}/thread", headers=_teammate(client))
        body = await resp.json()
    assert resp.status == 200, body
    slot = app["state"].get_slot(body["slot_key"])
    assert not (slot._trust or slot._trust_reads or slot._trust_scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["trust", "command", "timed", "deny_waiver"])
@pytest.mark.parametrize("route", ["thread", "greet", "fresh-start"])
async def test_a_relay_teammate_is_admitted_when_the_bot_holds_an_owner_grant(
    tmp_path, grant: str, route: str
) -> None:
    from kiro_crew.trust_patterns import GrantDeadline

    slug = await asyncio.to_thread(_saved_bot, "normal")
    app = _live_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        opened = await client.post(
            f"/api/members/{slug}/thread", params={"token": generate_token(OWNER)}
        )
        assert opened.status == 200, await opened.text()
        slot = app["state"].get_slot((await opened.json())["slot_key"])
        if grant == "trust":
            slot._trust = True
        elif grant == "command":
            slot._trusted_patterns.add("git status")
        elif grant == "timed":
            slot._trusted_pattern_deadlines["git status"] = GrantDeadline(
                wall=float("inf"), mono=float("inf")
            )
        else:
            slot._deny_waivers.add("rm")
        resp = await client.post(f"/api/members/{slug}/{route}", headers=_teammate(client))
        body = await resp.json()
    assert resp.status != 403, body


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["slot_trust", "command_grant", "trust_mode"])
async def test_a_relay_teammates_chat_send_uses_a_bot_with_the_owners_approvals(
    tmp_path, held: str
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.testing.wait import await_until

    seen: list[str] = []

    async def _turn(_state, slot, *_args, **_kwargs) -> None:
        seen.append(slot.key)

    slug = await asyncio.to_thread(_saved_bot, "normal")
    app = _live_app(tmp_path)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    async with TestClient(TestServer(app)) as client:
        opened = await client.post(
            f"/api/members/{slug}/thread", params={"token": generate_token(OWNER)}
        )
        assert opened.status == 200, await opened.text()
        slot_key = (await opened.json())["slot_key"]
        slot = app["state"].get_slot(slot_key)
        if held == "slot_trust":
            slot._trust = True
        elif held == "command_grant":
            slot._trusted_patterns.add("git status")
        else:

            def _switch_to_trust() -> None:
                cfg = KiroCrewConfig.load()
                cfg.agents[MADE].approval_mode = "trust"
                cfg.save()

            await asyncio.to_thread(_switch_to_trust)
        with patch.object(chat_handlers, "_run_chat", side_effect=_turn):
            resp = await client.post(
                "/api/chat", json={"message": "hi", "slot": slot_key}, headers=_teammate(client)
            )
            assert resp.status == 200, await resp.text()
            assert slot.task is not None
            await await_until(slot.task, what="the admitted teammate's turn finishing")
    assert seen == [slot_key]


@pytest.mark.asyncio
async def test_the_owners_rules_read_is_unchanged_by_the_teammate_branch(tmp_path) -> None:
    app = _app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        params = {"token": generate_token(OWNER), "member": MADE}
        path = f"/api/members/{_slug(MADE)}/rules"
        status, body = await _call(client, "GET", path, params=params)
        with patch.object(routes_identity, "_relay_teammate", return_value=False):
            status_before, body_before = await _call(client, "GET", path, params=params)
    assert status == status_before == 200
    assert body == body_before
