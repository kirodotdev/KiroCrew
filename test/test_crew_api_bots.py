"""``GET /v1/crew/bots``: an app lists the Bots of its own grant, and no other.

Two layers. Token auth is driven with the app credential a real local authorization
issued, to show which credentials the route admits. The handler is then called with
exactly what token auth published, on the same grant record.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import test_crew_api_app_credential as kit
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from jsonschema import Draft202012Validator
from redaction_structured_cases import github_classic
from test_shared_crew_teammate_owner_access import TEAMMATES, _publish
from token_auth_helpers import clear_nonces  # noqa: F401 -- autouse fixture

from kiro_crew import member_goals
from kiro_crew import members as members_mod
from kiro_crew.config import loader as config_loader
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.config.loader_document import config_path
from kiro_crew.dashboard import crew_api_auth
from kiro_crew.dashboard import crew_api_bots as bots
from kiro_crew.dashboard import crew_api_grant_requests as grant_requests
from kiro_crew.dashboard import crew_api_grant_self as grant_self
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard import running_bot_turns
from kiro_crew.dashboard.crew_api_scopes import CREDENTIAL_KEY, ROUTES
from kiro_crew.dashboard.handlers import members_app
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

# The fixtures of the app credential tests, registered here under their own names.
audit, clock, store = kit.audit, kit.clock, kit.store

OWNER = "U_HOST_OWNER"
PATH = bots.PATH
REVIEWER, WRITER, PLANNER, SECRET_KEEPER = "code-reviewer", "writer", "planner", "keeper"
#: A Bot the owner has and gave to no app in these tests.
PRIVATE = "the-owners-own-bot"
#: A generated token of a credential's shape, so the source holds no such literal.
SECRET = github_classic(seed=29)
CREDENTIAL_SHAPED = f"the key is {SECRET}"
_COMPONENTS = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1]
        / "docs/request-for-change/assets/crew-api/components.yaml"
    ).read_text(encoding="utf-8")
)["components"]
FIELDS = {"id", "object", "name", "description", "avatar_url", "status", "has_goal"}


def _bot(name: str, description: str = "", **fields: Any) -> KiroCrewAgentConfig:
    """A Bot record with a member id of its own, ``<name>-1`` unless one is given."""
    fields.setdefault("member_id", f"{name}-1")
    return KiroCrewAgentConfig(kiro_agent="kirocrew", description=description, **fields)


def _cfg(**more: KiroCrewAgentConfig) -> SimpleNamespace:
    agents = {
        REVIEWER: _bot(REVIEWER, "reviews code"),
        WRITER: _bot(WRITER, "writes", display_name="The Writer"),
        PLANNER: _bot(PLANNER, "plans"),
        SECRET_KEEPER: _bot(SECRET_KEEPER, CREDENTIAL_SHAPED),
        PRIVATE: _bot(PRIVATE, "only the owner's"),
        **more,
    }
    return SimpleNamespace(
        agents=agents,
        default_agent=REVIEWER,
        memory_stores={},
        workspaces={"default": SimpleNamespace(dir="workspace")},
        default_workspace="default",
        degraded_sections=frozenset(),
    )


@pytest.fixture
def world(monkeypatch) -> SimpleNamespace:
    """A roster of five Bots, and a record of every per-Bot file read."""
    seen = SimpleNamespace(cfg=_cfg(), goals=[], bindings=[], with_goal=set(), threads={})

    def _read_goals(slug: str, member: str) -> dict[str, Any]:
        seen.goals.append(member)
        return {"goals": [{"id": "g1"}]} if member in seen.with_goal else {"goals": []}

    def _read_dm_binding(slug: str) -> dict[str, Any] | None:
        # A Bot's slug is its member id, ``<name>-1`` here.
        name = slug.removesuffix("-1")
        seen.bindings.append(name)
        key = seen.threads.get(name)
        return {"member": name, "slot_key": key} if key else None

    # The real loader, for the test that reads a config file through it.
    seen.real_load = bots.KiroCrewConfig.load
    monkeypatch.setattr(bots.KiroCrewConfig, "load", lambda: seen.cfg)
    monkeypatch.setattr(member_goals, "read_goals", _read_goals)
    monkeypatch.setattr(members_mod, "read_dm_binding", _read_dm_binding)
    return seen


_FROM_THE_ROSTER: Any = object()


def _grant(
    store: la.LocalAuthorization,
    clock,
    given: list[str],
    scope: str = "bots:read",
    *,
    cfg: SimpleNamespace | None = None,
    identities: Any = _FROM_THE_ROSTER,
) -> dict:
    """The tokens of a grant holding *scope*, for which the owner gave the Bots *given*.

    The owner's answer records which Bot each name is as *cfg* (the usual roster when
    left out) has it; *identities* replaces that, and ``None`` records nothing, as a
    grant made before this was kept.
    """
    if identities is _FROM_THE_ROSTER:
        identities = la.bot_identities(cfg or _cfg())
    ask = {
        "response_type": "code",
        "client_id": kit.CLIENT,
        "client_name": "Notely",
        "redirect_uri": kit.REDIRECT,
        "scope": scope,
        "state": "xyz",
        "code_challenge": kit.CHALLENGE,
        "code_challenge_method": "S256",
    }
    kind, view = store.authorize(ask, bots=list(given))
    assert kind == "created"
    request_id = str(view["request_id"])
    store.decide(request_id, True, None, bots=list(given), identities=identities, caller=OWNER)
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ask, "request_id": request_id}, bots=list(given))
    assert kind == "redirect"
    code = parse_qs(urlsplit(location).query)["code"][0]
    return store.exchange_code(kit.CLIENT, code, kit.REDIRECT, kit.VERIFIER, kit.ISSUER)


def _request(query: str = "", *, slots: dict[str, Any] | None = None) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id=OWNER, _slots=slots or {})
    request = make_mocked_request("GET", PATH + (f"?{query}" if query else ""), app=app)
    request["app"] = ""
    request["user"] = OWNER
    return request


def _as_app(request: web.Request, store: la.LocalAuthorization, tokens: dict) -> web.Request:
    """*request* as token auth publishes a verified app credential on it."""
    credential = store.verify(tokens["access_token"])
    assert credential is not None
    request["user"] = f"app:{credential.grant_id}"
    request["app"] = f"local-grant:{credential.grant_id}"
    request["is_dashboard_user"] = False
    request[CREDENTIAL_KEY] = credential
    return request


async def _list(request: web.Request) -> tuple[int, Any]:
    response = await bots.api_bots(request)
    assert response.headers["X-Request-Id"].startswith("req_")
    assert response.headers["Crew-API-Version"] == "1.0"
    return response.status, json.loads(response.body)


def _valid(body: Any) -> None:
    schema = {"$ref": "#/components/schemas/BotList", "components": _COMPONENTS}
    Draft202012Validator(schema).validate(body)


# ── token auth: which credentials reach the handler ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["bots:read", "bots:chat", "bots:chat bots:read"])
async def test_token_auth_admits_an_app_that_holds_either_bot_scope(
    store, clock, audit, scope
) -> None:
    tokens = _grant(store, clock, [REVIEWER], scope)
    resp, request, ran = await kit._send(path=PATH, headers=kit._bearer(tokens["access_token"]))
    assert (resp.status, ran) == (200, True)
    row = audit.log_api_access.call_args.kwargs
    assert (row["caller"], row["outcome"]) == (f"app:{tokens['grant_id']}", "allowed")
    assert kit._published(request)[CREDENTIAL_KEY].grant_id == tokens["grant_id"]


@pytest.mark.asyncio
async def test_an_app_without_a_bot_scope_is_403_before_the_handler(store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER], "responses:write conversations:read")
    resp, _, ran = await kit._send(path=PATH, headers=kit._bearer(tokens["access_token"]))
    assert (resp.status, ran, kit._error(resp)["code"]) == (403, False, "insufficient_scope")


def test_the_route_serves_an_app_under_its_own_row() -> None:
    assert bots.ROUTE_KEY in crew_api_auth.APP_SERVED_ROUTES
    assert ROUTES[bots.ROUTE_KEY].scopes == frozenset({"bots:chat", "bots:read"})
    assert crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES[PATH] == "crew_api.bots.list"


@pytest.mark.asyncio
async def test_a_revoked_grants_token_is_401_before_the_handler(store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    assert store.revoke_grant(tokens["grant_id"], caller=OWNER) is True
    resp, _, ran = await kit._send(path=PATH, headers=kit._bearer(tokens["access_token"]))
    kit._assert_refused(resp, ran, "token_revoked", "The grant was revoked.")


@pytest.mark.asyncio
async def test_a_forwarded_app_credential_and_no_credential_are_401(store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    headers = {**kit._bearer(tokens["access_token"]), "X-Forwarded-For": "203.0.113.7"}
    resp, request, ran = await kit._send(path=PATH, headers=headers)
    kit._assert_401(resp, ran)
    assert CREDENTIAL_KEY not in kit._published(request)
    resp, _, ran = await kit._send(path=PATH)
    kit._assert_401(resp, ran)


# ── an app: the Bots of its own grant, in the contract's shape ───────────────


@pytest.mark.asyncio
async def test_an_app_lists_the_bots_of_its_grant_in_the_grants_order(world, store, clock) -> None:
    world.with_goal.add(WRITER)
    tokens = _grant(store, clock, [WRITER, REVIEWER])
    status, body = await _list(_as_app(_request(), store, tokens))
    assert status == 200
    _valid(body)
    assert body == {
        "object": "list",
        "data": [
            {
                "id": WRITER,
                "object": "bot",
                "name": "The Writer",
                "description": "writes",
                "avatar_url": None,
                "status": "idle",
                "has_goal": True,
            },
            {
                "id": REVIEWER,
                "object": "bot",
                "name": REVIEWER,
                "description": "reviews code",
                "avatar_url": None,
                "status": "idle",
                "has_goal": False,
            },
        ],
        "first_id": WRITER,
        "last_id": REVIEWER,
        "has_more": False,
    }
    # The ids are the grant's own, the ones GET /v1/crew/grant answers.
    assert [row["id"] for row in body["data"]] == store.grant(tokens["grant_id"])["bots"]["ids"]


def test_a_row_is_the_installed_apps_row_with_the_name_as_its_id() -> None:
    config = _bot(REVIEWER, "reviews code", display_name="Rev")
    # What an installed app is given: the slug as the id.
    for_an_app = members_app._bot_row("code-reviewer-1", REVIEWER, config, True)
    assert for_an_app == {
        "id": "code-reviewer-1",
        "object": "bot",
        "name": "Rev",
        "description": "reviews code",
        "avatar_url": None,
        "status": "idle",
        "has_goal": True,
    }
    # This route's row: that same row with the Bot's name as the id, and no slug in it.
    here = members_app._bot_row("code-reviewer-1", REVIEWER, config, True, bot_id=REVIEWER)
    assert here == {**for_an_app, "id": REVIEWER}
    assert "code-reviewer-1" not in json.dumps(here)
    assert set(here) == FIELDS <= set(_COMPONENTS["schemas"]["Bot"]["properties"])
    # One copy of the row, the thread key and the status value.
    assert bots._bot_row is members_app._bot_row
    assert bots._thread_key is members_app._thread_key
    assert bots.STATUS_BUSY is members_app.STATUS_BUSY


@pytest.mark.asyncio
async def test_free_text_the_redactors_would_alter_is_masked_whole(world, store, clock) -> None:
    world.cfg = _cfg(leaky=_bot("leaky", "fine", display_name=CREDENTIAL_SHAPED))
    tokens = _grant(store, clock, [SECRET_KEEPER, "leaky"], cfg=world.cfg)
    response = await bots.api_bots(_as_app(_request(), store, tokens))
    keeper, leaky = json.loads(response.body)["data"]
    assert keeper["description"] == _SENSITIVE_MASK
    assert (leaky["name"], leaky["description"]) == (_SENSITIVE_MASK, "fine")
    assert SECRET not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_a_grant_that_holds_no_bot_lists_none(world, store, clock) -> None:
    tokens = _grant(store, clock, [])
    status, body = await _list(_as_app(_request(), store, tokens))
    assert (status, body) == (
        200,
        {"object": "list", "data": [], "first_id": None, "last_id": None, "has_more": False},
    )
    assert (world.goals, world.bindings) == ([], [])


@pytest.mark.asyncio
async def test_a_granted_name_no_bot_has_today_is_left_out(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER, "retired-bot"])
    status, body = await _list(_as_app(_request(), store, tokens))
    assert status == 200
    assert [row["id"] for row in body["data"]] == [REVIEWER]


@pytest.mark.asyncio
async def test_nothing_of_a_bot_outside_the_grant_is_read_or_answered(world, store, clock) -> None:
    world.threads = {name: f"dm-thread-{name}" for name in world.cfg.agents}
    tokens = _grant(store, clock, [REVIEWER])
    response = await bots.api_bots(_as_app(_request(), store, tokens))
    text = response.body.decode("utf-8")
    assert response.status == 200
    for name in (WRITER, PLANNER, SECRET_KEEPER, PRIVATE):
        assert name not in text
    # No thread key, and no file of any other Bot was opened.
    assert "dm-thread-" not in text
    assert world.goals == [REVIEWER]
    assert world.bindings == [REVIEWER]


@pytest.mark.asyncio
async def test_a_bot_whose_own_thread_runs_a_turn_is_busy(world, store, clock) -> None:
    world.threads = {WRITER: "dm-thread-writer", REVIEWER: "dm-thread-reviewer"}
    # The thread slots name no agent here: the Bot's own thread counts by its binding.
    slots = {
        "dm-thread-writer": _slot("", running=True),
        "dm-thread-reviewer": _slot("", running=False),
    }
    tokens = _grant(store, clock, [WRITER, REVIEWER, PLANNER])
    _, body = await _list(_as_app(_request(slots=slots), store, tokens))
    assert [row["status"] for row in body["data"]] == ["busy", "idle", "idle"]


def _slot(
    agent: str, *, running: bool, agent_kind: str = "", linked: bool = False
) -> SimpleNamespace:
    """A live session: its agent, how it was picked, whether a turn runs in it, and
    whether one runs on the channel or scheduled session it is linked to (*linked*)."""
    return SimpleNamespace(
        agent=agent, agent_kind=agent_kind, running=running, linked_turn_running=linked
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "slot"),
    [
        ("chat-3", _slot(WRITER, running=True, agent_kind="member")),
        ("chat-4", _slot(WRITER, running=True)),
        ("slack-1", _slot(WRITER, running=False, linked=True)),
        ("cron-7", _slot("writer-1", running=False, linked=True)),
        ("task-review-2", _slot(WRITER, running=True)),
        ("resp-9", _slot(WRITER, running=True)),
        ("resp-9", _slot(WRITER, running=True, agent_kind="member")),
    ],
    ids=[
        "a dashboard chat on the Bot",
        "a chat picked by name alone",
        "a channel turn",
        "a scheduled turn",
        "a task review",
        "a POST /v1/responses turn",
        "a slot a caller named like one",
    ],
)
async def test_a_turn_outside_the_bots_own_thread_reads_idle(
    world, store, clock, key, slot
) -> None:
    """A slot's ``agent`` is the label it was picked under, not what the turn runs as,
    so no slot but the Bot's own thread is read. A session that names the Bot and
    runs a turn leaves it idle unless that turn's admission noted the Bot
    (``running_bot_turns``), and so does a turn that has no slot at all."""
    tokens = _grant(store, clock, [WRITER])
    _, body = await _list(_as_app(_request(slots={key: slot}), store, tokens))
    assert [row["status"] for row in body["data"]] == ["idle"]
    _, body = await _list(_as_app(_request(slots={}), store, tokens))
    assert [row["status"] for row in body["data"]] == ["idle"]


def _noted(*member_ids: str) -> None:
    """A dashboard turn admitted as each of these Bots, as ``_turn_admit`` notes it."""
    for member_id in member_ids:
        running_bot_turns.begin(
            SimpleNamespace(), SimpleNamespace(selection_kind="member", member_id=member_id)
        )


@pytest.mark.asyncio
async def test_a_bot_with_a_turn_admitted_as_it_is_busy_whatever_the_session(
    world, store, clock
) -> None:
    """The turn runs in no thread of the Bot's and in no slot this list reads: a
    ``POST /v1/responses`` turn, a dashboard chat. Its admission noted the member id."""
    tokens = _grant(store, clock, [WRITER, REVIEWER, PLANNER])
    _noted(f"{REVIEWER}-1")
    status, body = await _list(_as_app(_request(), store, tokens))
    assert status == 200
    _valid(body)
    assert [(row["id"], row["status"]) for row in body["data"]] == [
        (WRITER, "idle"),
        (REVIEWER, "busy"),
        (PLANNER, "idle"),
    ]
    # Busy or idle is all the app is told: which turn, and the member id, are not.
    assert all(set(row) == FIELDS for row in body["data"])
    assert f"{REVIEWER}-1" not in [value for row in body["data"] for value in row.values()]
    # The machine owner reads the same.
    _, body = await _list(_request())
    assert {row["id"] for row in body["data"] if row["status"] == "busy"} == {REVIEWER}
    # A Bot's scheduled run reads the same way.
    running_bot_turns.begin_scheduled(SimpleNamespace(), f"{WRITER}-1")
    _, body = await _list(_as_app(_request(), store, tokens))
    assert [row["status"] for row in body["data"]] == ["busy", "busy", "idle"]


@pytest.mark.asyncio
async def test_a_bot_made_again_under_the_name_is_idle_while_the_old_one_runs(world) -> None:
    """A turn admitted as the Bot that held the name before runs on. The Bot that
    holds the name now has another member id, and reads idle."""
    world.cfg = _cfg(**{WRITER: _bot(WRITER, "made again", member_id=f"{WRITER}-9f2c")})
    _noted(f"{WRITER}-1")
    _, body = await _list(_request())
    assert [row["status"] for row in body["data"] if row["id"] == WRITER] == ["idle"]
    _noted(f"{WRITER}-9f2c")
    _, body = await _list(_request())
    assert [row["status"] for row in body["data"] if row["id"] == WRITER] == ["busy"]


@pytest.mark.asyncio
async def test_a_turn_on_a_bot_outside_the_grant_changes_nothing_for_the_app(
    world, store, clock
) -> None:
    """A Bot outside the grant runs in every kind of session, its own thread and a
    responses turn included: the app's answer is what it is when nothing runs."""
    world.threads = {PRIVATE: "dm-thread-private"}
    tokens = _grant(store, clock, [WRITER, REVIEWER])
    quiet = await bots.api_bots(_as_app(_request(), store, tokens))
    slots = {
        "dm-thread-private": _slot(PRIVATE, running=True, agent_kind="member"),
        "resp-1": _slot(PRIVATE, running=True),
        "chat-2": _slot(PLANNER, running=True, agent_kind="member"),
        "slack-3": _slot(PRIVATE, running=False, linked=True),
        "cron-4": _slot(f"{PRIVATE}-1", running=False, linked=True),
        "task-review-5": _slot(PRIVATE, running=True),
    }
    # Their admissions noted both Bots the app was not given, and one runs its
    # schedule too.
    _noted(f"{PRIVATE}-1", f"{PLANNER}-1")
    running_bot_turns.begin_scheduled(SimpleNamespace(), f"{PRIVATE}-1")
    while_running = await bots.api_bots(_as_app(_request(slots=slots), store, tokens))
    assert while_running.body == quiet.body
    assert PRIVATE not in while_running.body.decode("utf-8")
    assert [row["status"] for row in json.loads(quiet.body)["data"]] == ["idle", "idle"]


@pytest.mark.asyncio
async def test_a_bot_whose_name_is_another_bots_member_id_does_not_make_it_busy(
    world, store, clock
) -> None:
    """``Alpha`` holds the member id ``alpha``, and another Bot is named ``alpha``. The
    app is given ``Alpha`` only. A turn on ``alpha``, in any session, leaves the
    app's answer as it is when nothing runs."""
    world.cfg = _cfg(
        Alpha=_bot("Alpha", "the granted one", member_id="alpha"),
        alpha=_bot("alpha", "not granted", member_id="alpha-7c1e"),
    )
    world.threads = {"alpha": "dm-thread-alpha"}
    tokens = _grant(store, clock, ["Alpha"], cfg=world.cfg)
    quiet = await bots.api_bots(_as_app(_request(), store, tokens))
    assert [row["id"] for row in json.loads(quiet.body)["data"]] == ["Alpha"]
    slots = {
        "dm-thread-alpha": _slot("alpha", running=True, agent_kind="member"),
        "resp-1": _slot("alpha", running=True),
        "chat-2": _slot("alpha", running=True, agent_kind="member"),
        "cron-3": _slot("alpha", running=False, linked=True),
    }
    # The turn on ``alpha`` is noted under that Bot's own member id.
    _noted("alpha-7c1e")
    while_running = await bots.api_bots(_as_app(_request(slots=slots), store, tokens))
    assert while_running.body == quiet.body
    assert json.loads(quiet.body)["data"][0]["status"] == "idle"
    # A turn admitted as ``Alpha`` is noted as ``alpha``, its member id: only the
    # Bot that holds that id is busy, not the Bot of that name.
    running_bot_turns._RUNNING.clear()
    _noted("alpha")
    _, granted = await _list(_as_app(_request(), store, tokens))
    assert [(row["id"], row["status"]) for row in granted["data"]] == [("Alpha", "busy")]
    _, everyone = await _list(_request())
    by_name = {row["id"]: row["status"] for row in everyone["data"]}
    assert (by_name["Alpha"], by_name["alpha"]) == ("busy", "idle")


# ── which Bot a granted name is ──────────────────────────────────────────────


async def _ids(request: web.Request) -> list[str]:
    status, body = await _list(request)
    assert status == 200
    return [row["id"] for row in body["data"]]


@pytest.mark.asyncio
async def test_a_bot_made_again_under_a_granted_name_is_not_listed(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER, WRITER])
    request = _as_app(_request(), store, tokens)
    assert await _ids(request) == [REVIEWER, WRITER]
    # The owner deletes the writer and makes a new Bot of that name: another member id.
    world.cfg = _cfg(**{WRITER: _bot(WRITER, "writes other things", member_id="writer-7f3a")})
    world.goals.clear()
    response = await bots.api_bots(request)
    assert [row["id"] for row in json.loads(response.body)["data"]] == [REVIEWER]
    assert "writes other things" not in response.body.decode("utf-8")
    # The new Bot's files are not opened either.
    assert world.goals == [REVIEWER]
    # The grant still names what the owner gave, and the owner still lists the new Bot.
    assert store.grant(tokens["grant_id"])["bots"]["ids"] == [REVIEWER, WRITER]
    assert WRITER in await _ids(_request("limit=100"))


@pytest.mark.asyncio
async def test_a_bot_made_again_from_its_kept_memory_is_listed(store, clock, monkeypatch) -> None:
    """Through the real create and delete routes and the real loader. A Bot made again
    under the name gets a new member id and is not listed; one made from the memory
    store the deleted Bot left behind holds that Bot's member id, and is. The routes'
    own caller check is passed here and is not what this test is about."""
    from kiro_crew.dashboard import handlers

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    app = web.Application()
    app.router.add_post("/api/agents", handlers.api_kirocrew_agents_create)
    app.router.add_delete("/api/agents/{name}", handlers.api_kirocrew_agent_delete)

    def _loaded() -> Any:
        config_loader._invalidate_config_cache()
        return bots.KiroCrewConfig.load()

    async with TestClient(TestServer(app)) as client:

        async def _make(**extra: str) -> dict[str, Any]:
            made = await client.post(
                "/api/agents", json={"name": "scout", "kiro_agent": "kirocrew", **extra}
            )
            assert made.status == 200, await made.text()
            return dict(await made.json())

        first = await _make()
        tokens = _grant(store, clock, ["scout"], cfg=_loaded())
        request = _as_app(_request(), store, tokens)
        assert await _ids(request) == ["scout"]

        assert (await client.delete("/api/agents/scout")).status == 200
        _loaded()
        assert await _ids(request) == []

        # Made again with nothing named: a new member id.
        fresh = await _make()
        assert fresh["member_id"] and fresh["member_id"] != first["member_id"]
        _loaded()
        assert await _ids(request) == []
        assert (await client.delete("/api/agents/scout")).status == 200

        # Made again from the first one's kept memory store: that Bot's member id.
        back = await _make(adopt_memory_store=first["memory_store"])
        assert back["member_id"] == first["member_id"]
        _loaded()
        assert await _ids(request) == ["scout"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("when_given", "now"),
    [("", ""), ("", "plain-1"), ("plain-1", "")],
    ids=["never had one", "got one later", "lost it"],
)
async def test_a_bot_without_a_member_id_is_not_listed_to_an_app(
    world, store, clock, when_given, now
) -> None:
    given = _cfg(plain=_bot("plain", "no identity", member_id=when_given))
    tokens = _grant(store, clock, [REVIEWER, "plain"], cfg=given)
    world.cfg = _cfg(plain=_bot("plain", "no identity", member_id=now))
    response = await bots.api_bots(_as_app(_request(), store, tokens))
    assert response.status == 200
    assert [row["id"] for row in json.loads(response.body)["data"]] == [REVIEWER]
    assert "no identity" not in response.body.decode("utf-8")
    # The owner lists it all the same.
    assert "plain" in await _ids(_request("limit=100"))


@pytest.mark.asyncio
async def test_a_grant_recorded_without_member_ids_lists_no_bot(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER, WRITER], identities=None)
    status, body = await _list(_as_app(_request(), store, tokens))
    assert (status, body["data"], body["has_more"]) == (200, [], False)
    # A record written before the ids were kept has no such key at all.
    record = json.loads(store._path.read_text(encoding="utf-8"))
    del record["grants"][tokens["grant_id"]]["bot_member_ids"]
    store._path.write_text(json.dumps(record), encoding="utf-8")
    assert await _ids(_as_app(_request(), store, tokens)) == []
    assert store.grant(tokens["grant_id"])["bots"]["ids"] == [REVIEWER, WRITER]


def test_the_member_ids_a_grant_keeps_are_in_no_answer(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER, WRITER])
    grant_id = tokens["grant_id"]
    stored = json.loads(store._path.read_text(encoding="utf-8"))
    assert stored["grants"][grant_id]["bot_member_ids"] == {
        REVIEWER: "code-reviewer-1",
        WRITER: "writer-1",
    }
    # The member ids and nothing else about the Bots is kept.
    assert not [key for key in stored["grants"][grant_id] if "default" in key]
    request_id = next(iter(stored["requests"]))
    answers = [
        tokens,
        store.grant(grant_id),
        store.grants(),
        store.request(request_id),
        grant_self.grant_self(store.grant(grant_id)),
        grant_requests._grant_request(store.request(request_id)),
    ]

    def _assert_no_member_ids(answer: Any) -> None:
        if isinstance(answer, dict):
            for key, value in answer.items():
                assert "member_id" not in key
                _assert_no_member_ids(value)
        elif isinstance(answer, list):
            for value in answer:
                _assert_no_member_ids(value)
        else:
            assert answer not in ("code-reviewer-1", "writer-1")

    _assert_no_member_ids(answers)


def test_only_an_active_grant_has_bots_in_range(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    roster = la.bot_identities(world.cfg)
    assert store.bots_in_range(tokens["grant_id"], roster) == [REVIEWER]
    assert store.bots_in_range("grt_no_such_grant", roster) == []
    assert store.bots_in_range(tokens["grant_id"], {}) == []
    assert store.revoke_grant(tokens["grant_id"], caller=OWNER) is True
    assert store.bots_in_range(tokens["grant_id"], roster) == []


def test_a_grant_past_its_end_has_no_bot_in_range(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    roster = la.bot_identities(world.cfg)
    clock.now += la.REFRESH_IDLE_SECS - 1
    assert store.bots_in_range(tokens["grant_id"], roster) == [REVIEWER]
    # Unused for the idle time: the refresh token has run out, and the grant with it.
    clock.now += 2
    assert store.bots_in_range(tokens["grant_id"], roster) == []
    assert store.grant(tokens["grant_id"]) is None


@pytest.mark.parametrize("kept", [["code-reviewer-1"], "code-reviewer-1", None, 7])
def test_kept_ids_that_are_not_a_mapping_put_no_bot_in_range(world, store, clock, kept) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    record = json.loads(store._path.read_text(encoding="utf-8"))
    record["grants"][tokens["grant_id"]]["bot_member_ids"] = kept
    store._path.write_text(json.dumps(record), encoding="utf-8")
    assert store.bots_in_range(tokens["grant_id"], la.bot_identities(world.cfg)) == []


@pytest.mark.parametrize("kept", [7, None, ["code-reviewer-1"]])
def test_a_kept_id_that_is_not_text_puts_that_bot_out_of_range(world, store, clock, kept) -> None:
    tokens = _grant(store, clock, [REVIEWER, WRITER])
    record = json.loads(store._path.read_text(encoding="utf-8"))
    record["grants"][tokens["grant_id"]]["bot_member_ids"][REVIEWER] = kept
    store._path.write_text(json.dumps(record), encoding="utf-8")
    assert store.bots_in_range(tokens["grant_id"], la.bot_identities(world.cfg)) == [WRITER]


# ── the default agent of an ordinary install has no member id ────────────────

MAIN, HELPER = "main", "helper"


def _plain_default(default: str = MAIN, **member_ids: str) -> SimpleNamespace:
    """The usual roster plus ``main`` and ``helper``, neither with a member id unless
    *member_ids* gives one, and *default* as the default agent."""
    cfg = _cfg(
        **{
            name: _bot(name, f"{name} Bot", member_id=member_ids.get(name, ""))
            for name in (MAIN, HELPER)
        }
    )
    cfg.default_agent = default
    return cfg


@pytest.mark.asyncio
async def test_the_default_agent_without_a_member_id_is_not_listed(world, store, clock) -> None:
    world.cfg = _plain_default()
    tokens = _grant(store, clock, [MAIN, HELPER, REVIEWER], cfg=world.cfg)
    # Only the Bot with a member id: being the default agent opens nothing.
    assert await _ids(_as_app(_request(), store, tokens)) == [REVIEWER]
    # The default agent gets a member id later: that grant still does not list it.
    world.cfg = _plain_default(main="main")
    assert await _ids(_as_app(_request(), store, tokens)) == [REVIEWER]
    # A grant the owner gives after that lists it, by its member id.
    again = _grant(store, clock, [MAIN], cfg=world.cfg)
    assert await _ids(_as_app(_request(), store, again)) == [MAIN]
    # The owner lists it either way.
    assert MAIN in await _ids(_request("limit=100"))


@pytest.mark.asyncio
@pytest.mark.parametrize("not_text", [7, None, False, [], {}])
async def test_a_member_id_that_is_not_text_is_never_in_range(
    world, store, clock, not_text
) -> None:
    """A damaged record is no known Bot, given so or read so."""
    damaged = _plain_default(main="main")
    damaged.agents[MAIN].member_id = not_text
    # Given with a member id, damaged later.
    world.cfg = _plain_default(main="main")
    tokens = _grant(store, clock, [MAIN, REVIEWER], cfg=world.cfg)
    assert await _ids(_as_app(_request(), store, tokens)) == [MAIN, REVIEWER]
    world.cfg = damaged
    assert await _ids(_as_app(_request(), store, tokens)) == [REVIEWER]
    # Given while damaged, whole later.
    again = _grant(store, clock, [MAIN, REVIEWER], cfg=damaged)
    assert await _ids(_as_app(_request(), store, again)) == [REVIEWER]
    world.cfg = _plain_default(main="main")
    assert await _ids(_as_app(_request(), store, again)) == [REVIEWER]
    # The owner still lists it.
    world.cfg = damaged
    assert MAIN in await _ids(_request("limit=100"))


_MISSING: Any = object()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [_MISSING, None, 7, True, "", ["foo"], "no-such-bot"])
async def test_a_default_agent_setting_the_loader_repairs_lists_no_bot_without_a_member_id(
    world, store, clock, value
) -> None:
    """A stored ``default_agent`` that is missing, not text or names no Bot, through the
    real loader, which answers ``default`` for it. Neither Bot has a member id, so
    neither is listed, whichever is the default agent when given and when listed."""

    def _loaded(_setting: Any = value) -> Any:
        """A fresh read, by the real loader, of the config file written below."""
        config_loader._invalidate_config_cache()
        return world.real_load()

    def _set_to(name: str) -> Any:
        """That config as it is once the owner sets ``default_agent`` to the Bot *name*."""
        cfg = _loaded()
        # Set on the loaded object: a config file changed under a data home that was
        # already read is held for the owner's review and not loaded.
        cfg.default_agent = name
        return cfg

    # Written once, where this test's data home keeps its config.
    doc: dict[str, Any] = {"agents": {"default": {}, "foo": {}}}
    if value is not _MISSING:
        doc["default_agent"] = value
    config_path().write_text(json.dumps(doc), encoding="utf-8")
    # The loader answers ``default`` for such a setting, and does not call the roster
    # degraded: ``default`` is then the default agent every turn is given.
    damaged = _loaded()
    assert damaged.default_agent == "default"
    assert la.bot_identities(damaged) == {"default": "", "foo": ""}
    assert not damaged.degraded_sections

    # 1. Given while ``foo`` was the default agent; the setting is damaged later.
    foo_is_default = _set_to("foo")
    first = _grant(store, clock, ["foo", "default"], cfg=foo_is_default)
    world.cfg = foo_is_default
    assert await _ids(_as_app(_request(), store, first)) == []
    world.cfg = _loaded(value)
    assert await _ids(_as_app(_request(), store, first)) == []

    # 2. Given while the setting was damaged; ``default`` is set after that.
    second = _grant(store, clock, ["foo", "default"], cfg=_loaded(value))
    assert await _ids(_as_app(_request(), store, second)) == []
    world.cfg = _set_to("default")
    assert await _ids(_as_app(_request(), store, second)) == []

    # 3. Given while ``default`` was set; ``foo`` is made the default agent, and the
    #    setting is damaged after that.
    third = _grant(store, clock, ["foo", "default"], cfg=_set_to("default"))
    world.cfg = _set_to("foo")
    assert await _ids(_as_app(_request(), store, third)) == []
    world.cfg = _loaded(value)
    assert await _ids(_as_app(_request(), store, third)) == []


@pytest.mark.asyncio
async def test_a_default_bot_the_package_sync_made_again_is_not_listed(world, store, clock) -> None:
    """The owner gives the shipped ``default``, makes ``foo`` the default agent and
    deletes ``default``; the package agent sync writes a ``default`` again; the stored
    default setting is then damaged, so the loader answers ``default``. The last config
    is read by the real loader and the list is the real handler's."""
    shipped = _cfg(
        default=_bot("default", "original Bot", member_id=""), foo=_bot("foo", member_id="")
    )
    shipped.default_agent = "default"
    tokens = _grant(store, clock, ["default"], cfg=shipped)
    world.cfg = shipped
    assert await _ids(_as_app(_request(), store, tokens)) == []

    config_path().write_text(
        json.dumps(
            {
                "default_agent": None,
                "agents": {
                    "foo": {},
                    "default": {
                        "kiro_agent": "default",
                        "description": "replacement package Bot",
                        "source": "package",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config_loader._invalidate_config_cache()
    world.cfg = world.real_load()
    assert (world.cfg.default_agent, la.bot_identities(world.cfg)["default"]) == ("default", "")
    response = await bots.api_bots(_as_app(_request(), store, tokens))
    assert (response.status, json.loads(response.body)["data"]) == (200, [])
    assert "replacement package Bot" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_a_default_bot_the_loader_seeds_again_is_not_listed(world, store, clock) -> None:
    """The owner gives the shipped ``default``, makes a package Bot the default agent
    and deletes ``default``; the package goes and the sync removes its Bot, so no Bot
    is left. The real loader then seeds a ``default`` again, from the stored template,
    and makes it the default agent. The list is the real handler's."""
    shipped = _cfg(default=_bot("default", "original Bot", member_id=""))
    shipped.default_agent = "default"
    tokens = _grant(store, clock, ["default", REVIEWER], cfg=shipped)
    world.cfg = shipped
    assert await _ids(_as_app(_request(), store, tokens)) == [REVIEWER]

    config_path().write_text(
        json.dumps({"agent": {"default_agent": "kirocrew"}, "agents": {}}), encoding="utf-8"
    )
    config_loader._invalidate_config_cache()
    world.cfg = world.real_load()
    seeded = world.cfg.agents["default"]
    assert (world.cfg.default_agent, list(world.cfg.agents)) == ("default", ["default"])
    assert (seeded.member_id, seeded.source, seeded.kiro_agent) == ("", "kirocrew", "kirocrew")
    response = await bots.api_bots(_as_app(_request(), store, tokens))
    assert (response.status, json.loads(response.body)["data"]) == (200, [])


# ── paging ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_and_after_page_through_the_grants_bots(world, store, clock) -> None:
    tokens = _grant(store, clock, [WRITER, REVIEWER, PLANNER])
    _, first = await _list(_as_app(_request("limit=2"), store, tokens))
    assert [row["id"] for row in first["data"]] == [WRITER, REVIEWER]
    assert (first["first_id"], first["last_id"], first["has_more"]) == (WRITER, REVIEWER, True)
    # Only the page's Bots were read from disk.
    assert world.goals == [WRITER, REVIEWER]
    _, second = await _list(_as_app(_request(f"limit=2&after={REVIEWER}"), store, tokens))
    assert [row["id"] for row in second["data"]] == [PLANNER]
    assert second["has_more"] is False
    _, past = await _list(_as_app(_request(f"after={PLANNER}"), store, tokens))
    assert (past["data"], past["first_id"], past["has_more"]) == ([], None, False)


def _without_request_id(response: web.Response) -> tuple[int, dict[str, str], Any]:
    body = json.loads(response.body)
    request_id = body["error"].pop("request_id")
    headers = {k: v for k, v in response.headers.items() if v != request_id}
    return response.status, headers, body


@pytest.mark.asyncio
async def test_a_cursor_outside_the_grant_is_answered_alike_whether_the_bot_exists(
    world, store, clock
) -> None:
    tokens = _grant(store, clock, [REVIEWER, WRITER])
    exists = await bots.api_bots(_as_app(_request(f"after={PRIVATE}"), store, tokens))
    absent = await bots.api_bots(_as_app(_request("after=no-such-bot"), store, tokens))
    assert _without_request_id(exists) == _without_request_id(absent)
    assert exists.status == 400
    assert json.loads(exists.body)["error"]["code"] == "invalid_request"
    assert PRIVATE not in exists.body.decode("utf-8")
    # Refused before any Bot's file is read.
    assert (world.goals, world.bindings) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["0", "101", "-1", "ten", "1.5", "", "9" * 5000])
async def test_a_limit_the_contract_does_not_allow_is_400(world, store, clock, limit) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    status, body = await _list(_as_app(_request(f"limit={limit}"), store, tokens))
    assert (status, body["error"]["code"]) == (400, "invalid_request")


# ── callers that are not a verified app credential ───────────────────────────


@pytest.mark.asyncio
async def test_the_machine_owner_lists_every_bot_in_the_same_shape(world) -> None:
    status, body = await _list(_request("limit=100"))
    assert status == 200
    _valid(body)
    assert [row["id"] for row in body["data"]] == list(world.cfg.agents)
    assert all(set(row) == FIELDS for row in body["data"])
    _, page = await _list(_request(f"limit=2&after={REVIEWER}"))
    assert [row["id"] for row in page["data"]] == [WRITER, PLANNER]
    assert page["has_more"] is True


def _no_read() -> None:
    raise AssertionError("a refused caller must not reach the roster")


@pytest.mark.asyncio
@pytest.mark.parametrize("who", TEAMMATES)
async def test_a_person_the_owner_let_in_is_refused_and_nothing_is_read(
    monkeypatch, who: str
) -> None:
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(bots.KiroCrewConfig, "load", _no_read)
    request = _request()
    _publish(request, who)
    status, body = await _list(request)
    assert (status, body["error"]["code"]) == (403, "owner_only")


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["someapp", "local-grant:grt_made_up"])
async def test_a_request_that_only_names_an_app_is_refused(monkeypatch, claim) -> None:
    """Only the credential token auth verified lists a grant's Bots, never a claim."""
    monkeypatch.setattr(bots.KiroCrewConfig, "load", _no_read)
    request = _request()
    request["app"] = claim
    request["user"] = "app:grt_made_up"
    request["is_dashboard_user"] = False
    status, body = await _list(request)
    assert (status, body["error"]["code"]) == (403, "owner_only")


# ── what cannot be read ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_degraded_roster_is_503_for_an_app_and_the_owner(world, store, clock) -> None:
    world.cfg.degraded_sections = frozenset({"agents"})
    tokens = _grant(store, clock, [REVIEWER])
    for request in (_as_app(_request(), store, tokens), _request()):
        status, body = await _list(request)
        assert (status, body["error"]["code"]) == (503, "instance_unavailable")
    assert (world.goals, world.bindings) == ([], [])


@pytest.mark.asyncio
async def test_a_grant_record_that_cannot_be_read_is_503_and_says_nothing_of_it(
    world, store, clock, monkeypatch, tmp_path
) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    request = _as_app(_request(), store, tokens)

    def _fail(_grant_id: str, _now_is: Any) -> None:
        raise OSError(f"cannot read {tmp_path}/state.json")

    monkeypatch.setattr(store, "bots_in_range", _fail)
    response = await bots.api_bots(request)
    assert response.status == 503
    assert str(tmp_path) not in response.body.decode("utf-8")
    assert REVIEWER not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_a_grant_revoked_after_token_auth_passed_lists_no_bot(world, store, clock) -> None:
    tokens = _grant(store, clock, [REVIEWER])
    request = _as_app(_request(), store, tokens)
    assert store.revoke_grant(tokens["grant_id"], caller=OWNER) is True
    status, body = await _list(request)
    assert (status, body["data"]) == (200, [])


@pytest.mark.asyncio
async def test_a_goal_record_that_cannot_be_read_counts_as_no_goal(
    world, store, clock, monkeypatch
) -> None:
    def _unreadable(slug: str, member: str) -> None:
        raise member_goals.GoalsUnreadable("damaged")

    monkeypatch.setattr(member_goals, "read_goals", _unreadable)
    tokens = _grant(store, clock, [REVIEWER])
    status, body = await _list(_as_app(_request(), store, tokens))
    assert (status, body["data"][0]["has_goal"]) == (200, False)


def test_the_startup_prune_holds_this_path_like_the_owners_roster() -> None:
    from kiro_crew.dashboard import server as server_mod

    assert server_mod._crewmate_prune_gate_holds_path(PATH)
    assert server_mod._crewmate_prune_gate_holds_path("/v1/crew/owner/bots")
    assert not server_mod._crewmate_prune_gate_holds_path("/v1/crew/botsy")
