"""``GET /api/kas-login/sources``: which stored Kiro sign-in sessions run as.

The handler is driven with a faked request, like ``test_kas_login_api.py``. The
store read, the route check and ``kiro-cli whoami`` are replaced, so no test
spawns a process or reads this machine's sign-ins.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import lts_sign_in as sign_in
from kiro_crew.dashboard.handlers import kas_login_sources as sources
from kiro_crew.testing.wait import await_until

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"
_EMAIL = "person@example.com"
_TEAMMATE = "teammate"


class _FakeState:
    owner_id = ""


class _FakeRequest:
    def __init__(self, *, user: str = "local-app", app_id: str = "", app: dict | None = None):
        self.app = app if app is not None else {}
        self.app.setdefault("state", _FakeState())
        self.path = "/api/kas-login/sources"
        self._items = {"app": app_id, "user": user}

    def __getitem__(self, key):
        return self._items[key]

    def __contains__(self, key):
        return key in self._items

    def get(self, key, default=""):
        return self._items.get(key, default)


def _stored(kind: str, *, live: bool = True, arn: str = "", stamp: str = "s1"):
    return sign_in.StoredSignIn(kind=kind, live=live, profile_arn=arn, stamp=stamp)


@pytest.fixture(name="world")
def world_fixture(monkeypatch: pytest.MonkeyPatch):
    """Set what the two stores hold, what the vault is called, and what whoami answers."""
    state = {
        "stored": sign_in.StoredSignIns(None, True, None, True),
        "crew_fields": {},
        "relay": True,
        "whoami": {},
        "whoami_calls": 0,
        "gate": None,
        "kiro_cli_harness": False,
        "api_key": False,
        "cli_signed_out": True,
        "cli_store_relocated": False,
    }

    async def whoami():
        state["whoami_calls"] += 1
        if state["gate"] is not None:
            await state["gate"].wait()
        if isinstance(state["whoami"], Exception):
            raise state["whoami"]
        return state["whoami"]

    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.dashboard.handlers import kiro_vault_identity

    # The handler's own store read runs; only what it reads from is replaced.
    monkeypatch.setattr(sign_in, "stored_sign_ins", lambda: state["stored"])
    monkeypatch.setattr(kas_bundle, "relay_runs_on_kiro_cli_sign_in", lambda: state["relay"])
    monkeypatch.setattr(
        kas_bundle, "kiro_cli_identity_store_is_relocated", lambda: state["cli_store_relocated"]
    )
    monkeypatch.setattr(sources, "main_harness_is_kiro_cli", lambda: state["kiro_cli_harness"])
    from kiro_crew.acp import kas_route

    # kiro-cli's own login beyond its store, as the Kiro CLI harness reads it.
    monkeypatch.setattr(kas_route, "kiro_cli_api_key_set", lambda environ: state["api_key"])
    monkeypatch.setattr(kas_route, "kiro_cli_signed_out", lambda **_: state["cli_signed_out"])
    monkeypatch.setattr(
        kiro_vault_identity,
        "vault_identity_snapshot_sync",
        lambda: ("fingerprint", state["crew_fields"]),
    )
    monkeypatch.setattr(sources, "_whoami", whoami)
    from kiro_crew.shared_crew import teammate_access

    # A Shared Crew teammate with owner rights is the caller named "teammate".
    monkeypatch.setattr(
        teammate_access,
        "teammate_passes_owner_gate",
        lambda request: request.get("app") == "" and request.get("user") == _TEAMMATE,
    )
    from kiro_crew.platform import enterprise_policy_fetch as epf

    # The account lock pins nothing unless a test sets "pin"; never the real stores.
    monkeypatch.setattr(epf, "pinned_scope", lambda: state["pin"])
    state["pin"] = ""
    return state


async def _get(request: _FakeRequest) -> dict:
    response = await sources.api_kas_login_sources(request)
    assert response.status == 200
    return json.loads(response.text)


async def _get_named(request: _FakeRequest) -> dict:
    """The answer once the name read the first request started has finished."""
    await _get(request)
    await await_until(request.app[sources._NAME_CACHE_KEY]["task"], what="the name read")
    return await _get(request)


@pytest.mark.asyncio
async def test_nothing_stored(world) -> None:
    body = await _get(_FakeRequest())
    assert body == {
        "active": "none",
        "kiro_crew": {"stored": False},
        "kiro_cli": {"stored": False, "usable": False},
        "name_pending": False,
        "name_unreadable": False,
        "same_account": None,
        "can_use_kiro_cli": False,
        "kiro_cli_harness": False,
        "relay_runs_on_kiro_cli": False,
    }
    assert world["whoami_calls"] == 0


@pytest.mark.asyncio
async def test_only_kiro_cli_is_the_one_in_use_and_is_named(world) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", arn=_ARN), True)
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://example.awsapps.com/start",
        "email": _EMAIL,
        "_profile_arn": _ARN,
    }
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli"] == {
        "stored": True,
        "usable": True,
        "account_type": "IamIdentityCenter",
        "portal_host": "example.awsapps.com",
        "email": _EMAIL,
    }
    assert body["can_use_kiro_cli"] is False


@pytest.mark.asyncio
async def test_the_first_answer_does_not_wait_for_the_name(world) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = {"account_type": "BuilderId"}
    world["gate"] = asyncio.Event()
    request = _FakeRequest()
    # Bounded by the lost-run ceiling only: an answer that waited on the gated
    # name read would never come, and the bound names that hang.
    first = await await_until(asyncio.ensure_future(_get(request)), what="the first answer")
    assert first["active"] == "kiro_cli"
    assert first["kiro_cli"] == {"stored": True, "usable": True}
    assert first["name_pending"] is True
    world["gate"].set()
    await await_until(request.app[sources._NAME_CACHE_KEY]["task"], what="the name read")
    second = await _get(request)
    assert second["name_pending"] is False
    assert second["kiro_cli"]["account_type"] == "BuilderId"
    assert world["whoami_calls"] == 1, "the reading is kept for the process"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, RuntimeError("boom")])
async def test_a_failed_name_read_is_unknown_never_signed_out(world, answer) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = answer
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli"] == {"stored": True, "usable": True}
    assert body["name_pending"] is False
    assert body["name_unreadable"] is True


@pytest.mark.asyncio
async def test_a_name_read_in_flight_or_answered_is_not_unreadable(world) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = None
    world["gate"] = asyncio.Event()
    request = _FakeRequest()
    assert (await _get(request))["name_unreadable"] is False, "still being read"
    world["gate"].set()
    await await_until(request.app[sources._NAME_CACHE_KEY]["task"], what="the name read")
    assert (await _get(request))["name_unreadable"] is True
    # whoami answering "no identity" is an answer, not a failed read.
    world["gate"] = None
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", stamp="s2"), True)
    world["whoami"] = {}
    body = await _get_named(request)
    assert (body["name_unreadable"], body["kiro_cli"]) == (False, {"stored": True, "usable": True})


@pytest.mark.asyncio
async def test_a_changed_kiro_cli_sign_in_is_read_again(world) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", stamp="one"), True)
    world["whoami"] = {"account_type": "BuilderId"}
    request = _FakeRequest()
    await _get_named(request)
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", stamp="two"), True)
    world["whoami"] = {"account_type": "IamIdentityCenter"}
    body = await _get_named(request)
    assert body["kiro_cli"]["account_type"] == "IamIdentityCenter"
    assert world["whoami_calls"] == 2


@pytest.mark.asyncio
async def test_two_different_accounts_offer_the_kiro_cli_one(world) -> None:
    world["stored"] = sign_in.StoredSignIns(
        _stored("builder_id"), True, _stored("aws", arn=_ARN), True
    )
    world["crew_fields"] = {"account_type": "BuilderId"}
    world["whoami"] = {"account_type": "IamIdentityCenter", "email": _EMAIL}
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_crew"
    assert body["kiro_crew"] == {"stored": True, "account_type": "BuilderId"}
    assert body["same_account"] is False
    assert body["can_use_kiro_cli"] is True


@pytest.mark.asyncio
async def test_only_the_owner_is_offered_the_kiro_cli_account(world) -> None:
    # The switch runs the card's sign-out, which answers anyone else with 403.
    # A teammate passes the route gate but is not the machine owner.
    world["stored"] = sign_in.StoredSignIns(
        _stored("builder_id"), True, _stored("aws", arn=_ARN), True
    )
    body = await _get_named(_FakeRequest(user=_TEAMMATE))
    assert body["active"] == "kiro_crew"
    assert body["can_use_kiro_cli"] is False


@pytest.mark.asyncio
async def test_a_different_kind_named_by_whoami_is_a_different_account(world) -> None:
    # The stores alone cannot tell: kiro-cli keeps both kinds under one key.
    world["stored"] = sign_in.StoredSignIns(_stored("builder_id"), True, _stored("aws"), True)
    world["crew_fields"] = {"account_type": "BuilderId"}
    world["whoami"] = {"account_type": "IamIdentityCenter"}
    request = _FakeRequest()
    assert (await _get(request))["same_account"] is None
    assert (await _get_named(request))["same_account"] is False


@pytest.mark.asyncio
async def test_two_portals_are_two_accounts_and_one_portal_decides_nothing(world) -> None:
    world["stored"] = sign_in.StoredSignIns(_stored("identity_center"), True, _stored("aws"), True)
    world["crew_fields"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://one.awsapps.com/start",
    }
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://one.awsapps.com/start",
    }
    request = _FakeRequest()
    assert (await _get_named(request))["same_account"] is None
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center"), True, _stored("aws", stamp="s2"), True
    )
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://two.awsapps.com/start",
    }
    assert (await _get_named(request))["same_account"] is False


@pytest.mark.asyncio
async def test_one_function_decides_and_is_given_what_whoami_named(world, monkeypatch) -> None:
    asked = []

    def decide(*args, **named):
        asked.append((args, named))
        return None

    monkeypatch.setattr(sign_in, "same_account", decide)
    world["stored"] = sign_in.StoredSignIns(_stored("builder_id"), True, _stored("aws"), True)
    world["crew_fields"] = {"account_type": "BuilderId"}
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://two.awsapps.com/start",
    }
    body = await _get_named(_FakeRequest())
    assert body["same_account"] is None, "the handler adds no verdict of its own"
    assert asked[-1] == (
        ("builder_id", "", "aws", ""),
        {
            "vault_type": "BuilderId",
            "cli_type": "IamIdentityCenter",
            "vault_portal": "",
            "cli_portal": "two.awsapps.com",
        },
    )


@pytest.mark.asyncio
async def test_the_button_is_not_offered_where_kiro_cli_would_not_run(world) -> None:
    world["stored"] = sign_in.StoredSignIns(
        _stored("builder_id"), True, _stored("social", arn=_ARN), True
    )
    world["relay"] = False
    body = await _get_named(_FakeRequest())
    assert body["same_account"] is False
    assert body["can_use_kiro_cli"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build, cli_arn, offered",
    [
        ("lts", "", False),  # the resolver would refuse it as not_eligible
        ("lts", _ARN, True),
        ("oss", "", True),  # oss has no profile rule: unchanged
    ],
)
async def test_the_button_is_offered_only_for_a_kiro_cli_sign_in_the_build_accepts(
    world, monkeypatch, build, cli_arn, offered
) -> None:
    from kiro_crew import edition

    monkeypatch.setattr(edition, "EDITION", build)
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center", arn=_ARN), True, _stored("social", arn=cli_arn), True
    )
    body = await _get_named(_FakeRequest())
    assert body["can_use_kiro_cli"] is offered


def test_the_button_and_the_resolver_ask_one_profile_rule() -> None:
    assert sign_in.profile_eligible("", sign_in.LTS) is False
    assert sign_in.profile_eligible(_ARN, sign_in.LTS) is True
    assert sign_in.profile_eligible("", sign_in.OSS) is True


_OTHER_ARN = "arn:aws:codewhisperer:us-east-1:210987654321:profile/ZYXWVUTSRQPO"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pinned, offered",
    [
        (_ARN, False),  # the gate would close as ACCOUNT_CHANGED
        (_OTHER_ARN, True),
        ("", True),  # nothing pinned yet
    ],
)
async def test_the_button_is_not_offered_for_an_account_the_lock_would_refuse(
    world, monkeypatch, pinned, offered
) -> None:
    from kiro_crew import edition
    from kiro_crew.platform import enterprise_policy_fetch as epf

    monkeypatch.setattr(edition, "EDITION", "lts")
    world["pin"] = epf.profile_scope(pinned) if pinned else ""
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center", arn=pinned or _ARN),
        True,
        _stored("aws", arn=_OTHER_ARN),
        True,
    )
    body = await _get_named(_FakeRequest())
    assert body["can_use_kiro_cli"] is offered


@pytest.mark.asyncio
async def test_oss_never_asks_the_account_lock(world, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr(sign_in, "account_lock_allows", lambda arn: asked.append(arn) or False)
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center", arn=_ARN), True, _stored("aws", arn=_OTHER_ARN), True
    )
    body = await _get_named(_FakeRequest())
    assert (body["can_use_kiro_cli"], asked) == (True, [])


@pytest.mark.asyncio
async def test_the_button_and_the_gate_ask_one_account_lock(world, monkeypatch) -> None:
    from kiro_crew import edition

    asked = []

    def lock(arn: str) -> bool:
        asked.append(arn)
        return False

    monkeypatch.setattr(edition, "EDITION", "lts")
    monkeypatch.setattr(sign_in, "account_lock_allows", lock)
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center", arn=_ARN), True, _stored("aws", arn=_OTHER_ARN), True
    )
    assert (await _get_named(_FakeRequest()))["can_use_kiro_cli"] is False
    monkeypatch.setattr(
        sign_in, "resolve", lambda **_: sign_in.Selection(sign_in.Source.KIRO_CLI, _OTHER_ARN)
    )
    assert sign_in.SignInLoginSource._check() is False
    assert set(asked) == {_OTHER_ARN} and len(asked) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build, in_use",
    [
        # The LTS gate presents kiro-cli's token as stored and never renews it.
        ("lts", False),
        # The relay runs on any stored kiro-cli sign-in and kiro-cli renews its own.
        ("oss", True),
    ],
)
async def test_a_lapsed_kiro_cli_sign_in_is_in_use_only_where_it_is_renewed(
    world, monkeypatch, build, in_use
) -> None:
    from kiro_crew import edition

    monkeypatch.setattr(edition, "EDITION", build)
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", live=False, arn=_ARN), True)
    body = await _get_named(_FakeRequest())
    assert body["active"] == ("kiro_cli" if in_use else "none")
    assert (body["kiro_cli"]["stored"], body["kiro_cli"]["usable"]) == (True, in_use)
    # Beside a Kiro Crew sign-in for another account, the switch follows the same rule.
    world["stored"] = sign_in.StoredSignIns(
        _stored("builder_id"), True, _stored("aws", live=False, arn=_ARN), True
    )
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_crew"
    assert (body["kiro_cli"]["usable"], body["can_use_kiro_cli"]) == (in_use, in_use)


@pytest.mark.asyncio
async def test_a_live_kiro_cli_sign_in_the_relay_would_not_run_on_is_not_in_use(world) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["relay"] = False
    body = await _get_named(_FakeRequest())
    assert body["active"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        sign_in.StoredSignIns(None, False, None, True),
        sign_in.StoredSignIns(None, True, None, False),
    ],
)
async def test_an_unreadable_store_is_unknown(world, stored) -> None:
    world["stored"] = stored
    body = await _get(_FakeRequest())
    assert body["active"] == "unknown"
    assert None in (body["kiro_crew"]["stored"], body["kiro_cli"]["stored"])


@pytest.mark.asyncio
async def test_a_failed_kiro_cli_read_beside_a_crew_sign_in_is_null_not_absent(world) -> None:
    # The card tells "could not check" (null) from "not signed in" (false) by this alone.
    world["stored"] = sign_in.StoredSignIns(_stored("identity_center"), True, None, False)
    body = await _get(_FakeRequest())
    assert body["active"] == "kiro_crew"
    assert body["kiro_cli"]["stored"] is None
    world["stored"] = sign_in.StoredSignIns(_stored("identity_center"), True, None, True)
    body = await _get(_FakeRequest())
    assert body["kiro_cli"]["stored"] is False


@pytest.mark.asyncio
async def test_the_payload_carries_only_the_named_fields(world) -> None:
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center", arn=_ARN), True, _stored("aws", arn=_ARN), True
    )
    world["crew_fields"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://example.awsapps.com/start",
        "access_token": "vault-access",
        "profile_arn": _ARN,
    }
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://example.awsapps.com/start",
        "email": _EMAIL,
        "_profile_arn": _ARN,
        "access_token": "cli-access",
        "profile_id": "ABCDEFGHIJKL",
    }
    request = _FakeRequest()
    await _get(request)
    await await_until(request.app[sources._NAME_CACHE_KEY]["task"], what="the name read")
    response = await sources.api_kas_login_sources(request)
    body = json.loads(response.text)
    assert set(body) == {
        "active",
        "kiro_crew",
        "kiro_cli",
        "name_pending",
        "name_unreadable",
        "same_account",
        "can_use_kiro_cli",
        "kiro_cli_harness",
        "relay_runs_on_kiro_cli",
    }
    assert set(body["kiro_crew"]) == {"stored", "account_type", "portal_host"}
    assert set(body["kiro_cli"]) == {"stored", "usable", "account_type", "portal_host", "email"}
    for secret in ("vault-access", "cli-access", "arn:aws", "ABCDEFGHIJKL", "/start"):
        assert secret not in response.text
    assert body["same_account"] is True


@pytest.mark.asyncio
async def test_the_email_goes_to_the_owner_only_and_into_no_log(world, caplog) -> None:
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = {"account_type": "BuilderId", "email": _EMAIL}
    app: dict = {}
    with caplog.at_level("DEBUG"):
        owner = await _get_named(_FakeRequest(app=app))
        other = await _get(_FakeRequest(user=_TEAMMATE, app=app))
    assert owner["kiro_cli"]["email"] == _EMAIL
    assert other["kiro_cli"] == {"stored": True, "usable": True, "account_type": "BuilderId"}
    assert _EMAIL not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_store_read_is_a_coded_500(world, monkeypatch) -> None:
    def broken():
        raise OSError("disk")

    monkeypatch.setattr(sources, "_read_stores", broken)
    response = await sources.api_kas_login_sources(_FakeRequest())
    assert response.status == 500
    assert json.loads(response.text)["code"] == "sign_in_sources_failed"


def test_the_route_is_registered_as_a_read() -> None:
    from aiohttp import web

    from kiro_crew.dashboard.routes import realtime

    app = web.Application()
    realtime.register(app)
    methods = {
        r.method for r in app.router.routes() if r.resource.canonical == "/api/kas-login/sources"
    }
    assert methods == {"GET", "HEAD"} or methods == {"GET"}


def test_kiro_cli_would_run_only_on_the_relay_with_kiro_cli_present(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp import kas_route
    from kiro_crew.config.loader import KiroCrewConfig

    class _Agent:
        kas_bundle = False

    class _Config:
        agent = _Agent()

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _Config()))
    found = {"bin": "/opt/kiro/kiro-cli"}
    monkeypatch.setattr("kiro_crew.acp.client._resolve_kiro_bin", lambda **_: found["bin"])
    reasons = {"value": ""}
    monkeypatch.setattr(kas_route, "relay_unusable_reason", lambda **_: reasons["value"])

    def would_run() -> bool:
        return kas_route.relay_runs_on_kiro_cli_sign_in(environ={}, home=tmp_path)

    assert would_run() is True
    reasons["value"] = kas_route.REASON_KIRO_CLI_SIGNED_OUT
    assert would_run() is False
    reasons["value"] = ""
    found["bin"] = None
    assert would_run() is False
    found["bin"] = "/opt/kiro/kiro-cli"
    _Agent.kas_bundle = True
    assert would_run() is False


@pytest.mark.parametrize(
    "build, stored, relay_asked, named",
    [
        ("oss", sign_in.StoredSignIns(None, True, None, True), False, False),
        ("oss", sign_in.StoredSignIns(None, True, _stored("aws"), True), True, False),
        # oss decides by presence, as the relay does: a lapsed sign-in is still asked.
        ("oss", sign_in.StoredSignIns(None, True, _stored("aws", live=False), True), True, False),
        ("lts", sign_in.StoredSignIns(None, True, _stored("aws"), True), True, False),
        ("lts", sign_in.StoredSignIns(None, True, _stored("aws", live=False), True), False, False),
        (
            "oss",
            sign_in.StoredSignIns(_stored("builder_id"), True, _stored("aws"), True),
            True,
            True,
        ),
    ],
)
def test_the_store_read_asks_only_what_the_stores_call_for(
    monkeypatch, build, stored, relay_asked, named
) -> None:
    from kiro_crew import edition
    from kiro_crew.acp import kas_route
    from kiro_crew.dashboard.handlers import kiro_vault_identity

    monkeypatch.setattr(edition, "EDITION", build)
    asked = []
    monkeypatch.setattr(sign_in, "stored_sign_ins", lambda: stored)
    monkeypatch.setattr(
        kas_route, "relay_runs_on_kiro_cli_sign_in", lambda **_: asked.append("relay") or True
    )
    monkeypatch.setattr(
        kiro_vault_identity,
        "vault_identity_snapshot_sync",
        lambda: ("fingerprint", {"account_type": "BuilderId"}),
    )
    monkeypatch.setattr(sources, "main_harness_is_kiro_cli", lambda: False)
    found, crew_fields, relay, harness_active, cli_name_allowed = sources._read_stores()
    assert harness_active is None
    assert cli_name_allowed is True
    assert found is stored
    assert (asked == ["relay"], relay) == (relay_asked, relay_asked)
    assert crew_fields == ({"account_type": "BuilderId"} if named else {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_id"),
    [
        ("someone-else", ""),  # an authenticated subject that is not a teammate
        ("local-app", "some-app"),  # an app iframe or internal caller
        (_TEAMMATE, "some-app"),
    ],
)
async def test_a_caller_without_owner_rights_is_refused_before_any_read(
    world, monkeypatch, user: str, app_id: str
) -> None:
    from kiro_crew.dashboard.handlers import kas_login

    audited: list[tuple[str, str]] = []

    async def audit(_request, operation, outcome, error=""):
        audited.append((operation, outcome))

    monkeypatch.setattr(kas_login, "_audit", audit)
    world["stored"] = sign_in.StoredSignIns(
        _stored("builder_id"), True, _stored("aws", arn=_ARN), True
    )
    world["whoami"] = {"account_type": "BuilderId", "email": _EMAIL}
    app: dict = {}
    response = await sources.api_kas_login_sources(_FakeRequest(user=user, app_id=app_id, app=app))
    assert response.status == 403
    assert json.loads(response.text)["code"] == "owner_only"
    assert sources._NAME_CACHE_KEY not in app
    assert world["whoami_calls"] == 0
    assert audited == [("kas_login_sources", "denied")]


@pytest.fixture(name="harness_home")
def harness_home_fixture(tmp_path, monkeypatch):
    """A real ``config.json`` the loader reads, with Kiro CLI and KAS both selectable."""
    from kiro_crew.agent_sdk import backends
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(
        backends, "_selectable", {backends.ACP_BACKEND_KIRO, backends.ACP_BACKEND_KAS}
    )
    monkeypatch.setattr(backends, "_host_unservable", {})

    def store(acp_backend: str) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"agent": {"acp_backend": acp_backend}}), encoding="utf-8"
        )
        loader._invalidate_config_cache()

    yield store
    loader._invalidate_config_cache()


@pytest.mark.parametrize(
    "acp_backend, v3_started, expected",
    [
        # A Kiro CLI pick with no v3 start confirmed keeps kiro-cli's v2 launch.
        ("kiro", False, True),
        # Once a v3 start is confirmed the pick runs on KAS, which takes the vault.
        ("kiro", True, False),
        ("kas", False, False),
        ("kas", True, False),
    ],
)
def test_the_main_harness_is_kiro_cli_only_on_the_v2_launch(
    harness_home, monkeypatch, acp_backend, v3_started, expected
) -> None:
    from kiro_crew.agent_sdk import backends

    monkeypatch.setattr(backends, "_kiro_cli_kas_started", v3_started)
    harness_home(acp_backend)
    assert sources.main_harness_is_kiro_cli() is expected


def test_a_config_that_cannot_load_is_not_the_kiro_cli_harness(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def broken(cls):
        raise OSError("disk")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(broken))
    assert sources.main_harness_is_kiro_cli() is False


@pytest.mark.asyncio
async def test_the_kiro_cli_harness_uses_kiro_cli_sign_in_over_the_vault(world) -> None:
    """kiro-cli's v2 launch never asks Kiro Crew: the vault is not the one in use."""
    world["kiro_cli_harness"] = True
    world["relay"] = True
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center"), True, _stored("aws", arn=_ARN), True
    )
    world["crew_fields"] = {"account_type": "IamIdentityCenter"}
    world["whoami"] = {"account_type": "BuilderId"}
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli_harness"] is True
    # Nothing to switch to: kiro-cli's sign-in is already the one in use.
    assert body["can_use_kiro_cli"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key, relocated", [(True, False), (False, True), (True, True)])
async def test_the_kiro_cli_harness_does_not_name_a_different_stored_login(
    world, api_key, relocated
) -> None:
    world["kiro_cli_harness"] = True
    world["api_key"] = api_key
    world["cli_store_relocated"] = relocated
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center"), True, _stored("aws", arn=_ARN), True
    )
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://other.awsapps.com/start",
        "email": "other@example.com",
    }
    request = _FakeRequest()
    body = await _get(request)
    assert body["active"] == ("kiro_cli" if api_key else "unknown")
    assert body["kiro_cli"] == {"stored": True, "usable": True}
    assert body["name_pending"] is False
    assert body["name_unreadable"] is False
    assert sources._NAME_CACHE_KEY not in request.app
    assert world["whoami_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["kiro_cli_api_key_set", "kiro_cli_identity_store_is_relocated"])
async def test_the_kiro_cli_harness_does_not_name_an_unknown_login_environment(
    world, monkeypatch, check
) -> None:
    from kiro_crew.agent_sdk import kas_bundle

    def unreadable():
        raise OSError("login environment unreadable")

    monkeypatch.setattr(kas_bundle, check, unreadable)
    world["kiro_cli_harness"] = True
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    request = _FakeRequest()
    body = await _get(request)
    assert body["active"] == "unknown"
    assert body["kiro_cli"] == {"stored": True, "usable": True}
    assert (body["name_pending"], body["name_unreadable"]) == (False, False)
    assert sources._NAME_CACHE_KEY not in request.app
    assert world["whoami_calls"] == 0


@pytest.mark.asyncio
async def test_a_key_on_the_kiro_cli_harness_does_not_publish_a_cached_store_name(world) -> None:
    world["kiro_cli_harness"] = True
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = {"account_type": "BuilderId", "email": _EMAIL}
    request = _FakeRequest()
    assert (await _get_named(request))["kiro_cli"]["email"] == _EMAIL
    world["api_key"] = True
    body = await _get(request)
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli"] == {"stored": True, "usable": True}
    assert (body["name_pending"], body["name_unreadable"]) == (False, False)
    assert world["whoami_calls"] == 1


@pytest.mark.asyncio
async def test_the_kiro_cli_harness_names_only_the_default_stored_login(world) -> None:
    world["kiro_cli_harness"] = True
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws", arn=_ARN), True)
    world["whoami"] = {
        "account_type": "IamIdentityCenter",
        "start_url": "https://example.awsapps.com/start",
        "email": _EMAIL,
    }
    body = await _get_named(_FakeRequest())
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli"] == {
        "stored": True,
        "usable": True,
        "account_type": "IamIdentityCenter",
        "portal_host": "example.awsapps.com",
        "email": _EMAIL,
    }
    assert world["whoami_calls"] == 1


@pytest.mark.asyncio
async def test_kas_keeps_naming_the_stored_login_with_a_key_and_relocated_home(world) -> None:
    world["api_key"] = True
    world["cli_store_relocated"] = True
    world["stored"] = sign_in.StoredSignIns(None, True, _stored("aws"), True)
    world["whoami"] = {"account_type": "BuilderId", "email": _EMAIL}
    body = await _get_named(_FakeRequest())
    assert body["kiro_cli_harness"] is False
    assert body["active"] == "kiro_cli"
    assert body["kiro_cli"] == {
        "stored": True,
        "usable": True,
        "account_type": "BuilderId",
        "email": _EMAIL,
    }
    assert world["whoami_calls"] == 1


_VAULT_ONLY = sign_in.StoredSignIns(_stored("identity_center"), True, None, True)


@pytest.mark.parametrize(
    "stored, api_key, cli_signed_out, active",
    [
        # The vault alone runs nothing on the Kiro CLI harness.
        (_VAULT_ONLY, False, True, "none"),
        # A KIRO_API_KEY is a whole kiro-cli login with nothing in its store.
        (_VAULT_ONLY, True, True, "kiro_cli"),
        # Not proven signed out (a relocated store, say) is unknown, never "none".
        (_VAULT_ONLY, False, False, "unknown"),
        # An unreadable kiro-cli store is unknown, whatever the vault holds.
        (
            sign_in.StoredSignIns(_stored("identity_center"), True, None, False),
            False,
            True,
            "unknown",
        ),
        # A lapsed kiro-cli token still runs: kiro-cli renews its own.
        (
            sign_in.StoredSignIns(None, True, _stored("aws", live=False), True),
            False,
            True,
            "kiro_cli",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_kiro_cli_harness_reads_only_kiro_cli_login(
    world, stored, api_key, cli_signed_out, active
) -> None:
    world["kiro_cli_harness"] = True
    world["relay"] = False
    world["stored"] = stored
    world["api_key"] = api_key
    world["cli_signed_out"] = cli_signed_out
    body = await _get(_FakeRequest())
    assert body["active"] == active
    assert body["kiro_cli_harness"] is True


@pytest.mark.parametrize("api_key, active", [(False, "unknown"), (True, "kiro_cli")])
@pytest.mark.asyncio
async def test_the_kiro_cli_harness_does_not_trust_a_relocated_stores_leftover(
    world, api_key, active
) -> None:
    world["kiro_cli_harness"] = True
    world["cli_store_relocated"] = True
    world["api_key"] = api_key
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center"), True, _stored("aws", arn=_ARN), True
    )
    body = await _get(_FakeRequest())
    assert body["active"] == active
    assert body["kiro_cli_harness"] is True
    assert body["can_use_kiro_cli"] is False


@pytest.mark.parametrize("runs", [True, False])
def test_the_store_read_still_asks_the_relay_on_the_kiro_cli_harness(
    world, monkeypatch, runs
) -> None:
    """Kiro Agent's row on that harness says what the relay would do, so it is asked."""
    from kiro_crew.agent_sdk import kas_bundle
    from kiro_crew.dashboard.handlers import kiro_vault_identity

    asked: list[str] = []
    monkeypatch.setattr(
        sign_in, "stored_sign_ins", lambda: sign_in.StoredSignIns(None, True, _stored("aws"), True)
    )
    monkeypatch.setattr(
        kas_bundle, "relay_runs_on_kiro_cli_sign_in", lambda: asked.append("relay") or runs
    )
    monkeypatch.setattr(kiro_vault_identity, "vault_identity_snapshot_sync", lambda: ("f", {}))
    monkeypatch.setattr(sources, "main_harness_is_kiro_cli", lambda: True)
    _found, _crew, relay, harness_active, cli_name_allowed = sources._read_stores()
    assert (relay, harness_active, asked) == (runs, "kiro_cli", ["relay"])
    assert cli_name_allowed is True


@pytest.mark.parametrize("runs", [True, False])
@pytest.mark.asyncio
async def test_the_kiro_cli_harness_reports_what_the_relay_would_do(world, runs) -> None:
    world["kiro_cli_harness"] = True
    world["relay"] = runs
    world["stored"] = sign_in.StoredSignIns(
        _stored("identity_center"), True, _stored("aws", arn=_ARN), True
    )
    body = await _get(_FakeRequest())
    assert body["kiro_cli_harness"] is True
    assert body["active"] == "kiro_cli"
    assert body["relay_runs_on_kiro_cli"] is runs
    # The switch is never offered there, whatever the relay would do.
    assert body["can_use_kiro_cli"] is False
