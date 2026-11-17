"""Shared Crew attribution for people the Kiro relay hands over.

Driven over a real loopback listener with the relay ingress, the client-sent strip
and token auth chained as both gateway chains order them; the relay client is the
seam it calls, ``RelayIngress.stamp_headers``. A relayed teammate is published as
their own ``kiro`` principal, keyed by their Kiro user ID, and the relayed owner of a
personal Crew as the owner at the machine, with ``kiro_relay`` provenance. The
second half walks every owner check that compares principal ids only (listed in the
module's own table below) and pins what each answers for the relayed owner.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from shared_crew_host_isolation import host_isolation  # noqa: F401
from token_auth_helpers import clear_nonces  # noqa: F401 -- the autouse fixture registers by import

from kiro_crew.dashboard.kiro_relay.caller import (
    CrewKind,
    HostBinding,
    RelayCaller,
    RelayCallerKind,
)
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import identity, owner, people
from kiro_crew.shared_crew.contracts import (
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import (
    actor_of,
    local_operator_principal,
    tailnet_principal,
)


def relay_principal(
    caller: RelayCaller, owner_subject: str, handle_email_domains: frozenset[str] = frozenset()
):
    """Looked up at call time, so each test fails on its own before the branch."""
    return identity.relay_principal(caller, owner_subject, handle_email_domains)


pytestmark = pytest.mark.usefixtures("host_isolation")

OWNER = "kiro-user.owner-0001"
ANA = "kiro-user.teammate-ana-0002"
BEA = "kiro-user.teammate-bea-0003"
NAME = "Ren\u00e9e \u00d8"
EMAIL = "renee@example.com"


@pytest.fixture(autouse=True)
def _fresh(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    people.reset_for_tests()
    yield
    people.reset_for_tests()


async def _whoami(request: web.Request) -> web.Response:
    from kiro_crew.shared_crew import routes_invites
    from kiro_crew.shared_crew.admission_gate import actor_budget
    from kiro_crew.shared_crew.attribution import with_request_author
    from kiro_crew.shared_crew.collaborators import is_collaborator_actor

    actor = actor_of(request)
    refusal = await routes_invites._owner_refusal_decision(request)
    return web.json_response(
        {
            "principal": actor.principal.to_dict(),
            "transport": actor.transport.value,
            "budget": actor_budget(request),
            "author": (with_request_author({}) or {}).get("author"),
            "collaborator": is_collaborator_actor(actor),
            "owner_page": refusal.status if refusal is not None else 200,
            "owner_page_code": json.loads(refusal.text)["code"] if refusal is not None else "",
        }
    )


def _app(ingress: RelayIngress, *, owner_id: str = "") -> web.Application:
    app = web.Application(
        middlewares=[
            relay_ingress_middleware(strict_internal_paths=_STRICT_INTERNAL_API_PATHS),
            relay_header_strip_middleware,
            token_auth_middleware(port=5476),
        ]
    )
    app["state"] = SimpleNamespace(owner_id=owner_id)
    app["allowed_origins"] = set()
    app[RELAY_INGRESS_APP_KEY] = ingress
    app.router.add_get("/api/whoami", _whoami)
    return app


def _ingress(kind: CrewKind) -> RelayIngress:
    ingress = RelayIngress(enabled=True)
    owner_id = OWNER if kind is CrewKind.PERSONAL else ""
    ingress.bind_host(HostBinding(crew_kind=kind, owner_user_id=owner_id))
    return ingress


async def _as(client: TestClient, user: str, **labels: str) -> dict[str, Any]:
    ingress = client.server.app[RELAY_INGRESS_APP_KEY]
    headers = ingress.stamp_headers([], relay_user=user, **labels)
    resp = await client.get("/api/whoami", headers=headers)
    assert resp.status == 200, await resp.text()
    return await resp.json()


# ---------------------------------------------------------------------------
# Teammates on a team Crew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_relayed_teammate_is_their_own_kiro_principal() -> None:
    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        body = await _as(client, ANA, relay_email=EMAIL, relay_name=NAME)
    principal = body["principal"]
    assert principal["kind"] == PrincipalKind.HUMAN.value
    assert principal["provider"] == "kiro"
    assert principal["issuer"] == "kiro-relay"
    assert principal["subject"] == ANA
    assert principal["provenance"] == Provenance.KIRO_RELAY.value
    assert principal["id"] == principal_id_for("kiro", "kiro-relay", ANA)
    assert principal["display"]["name"] == NAME
    # The email is never the display login: an author stamp read back from a
    # transcript has no provenance, so a login could reach an audit record.
    assert principal["display"]["login"] == ""
    assert body["transport"] == Transport.REMOTE.value
    # Their messages carry their own principal, not Unknown.
    assert body["author"]["id"] == principal["id"]
    # People lists them.
    assert [obs.principal.id for obs in people.registry().snapshot()] == [principal["id"]]


@pytest.mark.asyncio
async def test_the_id_is_the_kiro_user_id_alone_never_the_labels() -> None:
    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        named = await _as(client, ANA, relay_email=EMAIL, relay_name=NAME)
        renamed = await _as(client, ANA, relay_email="other@example.com", relay_name="Other")
        bare = await _as(client, ANA)
        # Exact: no case folding, so another ID is another person.
        upper = await _as(client, ANA.upper())
    ids = {b["principal"]["id"] for b in (named, renamed, bare)}
    assert len(ids) == 1
    assert upper["principal"]["id"] not in ids


@pytest.mark.asyncio
async def test_two_teammates_are_two_people_with_two_budgets_and_one_typing_lease() -> None:
    from kiro_crew.shared_crew.collab import Activity
    from kiro_crew.shared_crew.contracts import Principal
    from kiro_crew.shared_crew.presence import PresenceRegistry
    from kiro_crew.shared_crew.typing_lease import TypingLeaseRegistry, can_hold

    ACTIVITY_TYPING = Activity.TYPING

    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        ana = await _as(client, ANA, relay_name="Ana")
        bea = await _as(client, BEA, relay_name="Bea")
    assert ana["principal"]["id"] != bea["principal"]["id"]
    assert ana["budget"] == ana["principal"]["id"]
    assert bea["budget"] == bea["principal"]["id"]
    a = Principal.from_dict(ana["principal"])
    b = Principal.from_dict(bea["principal"])

    presence = PresenceRegistry()
    presence.report("conn-a", a, "s1", ACTIVITY_TYPING)
    presence.report("conn-b", b, "s1", ACTIVITY_TYPING)
    assert {e.principal.id for e in presence.entries("s1")} == {a.id, b.id}

    leases = TypingLeaseRegistry()
    assert can_hold(a) and can_hold(b)
    leases.typed("s1", "conn-a", a)
    assert leases.holder("s1") == a
    # Bea's send is refused while Ana holds it (409 composer_locked at the route).
    assert leases.blocks("s1", b) == a
    assert leases.blocks("s1", a) is None
    # And Bea may take it over.
    leases.take_over("s1", "conn-b", b)
    assert leases.holder("s1") == b


@pytest.mark.asyncio
async def test_a_teammate_with_no_name_or_email_gets_the_fallback_label() -> None:
    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        bare = await _as(client, ANA)
        email_only = await _as(client, BEA, relay_email=EMAIL)
    assert bare["principal"]["display"]["name"] == "Kiro user ...a-0002"
    assert bare["principal"]["display"]["login"] == ""
    assert email_only["principal"]["display"]["name"] == EMAIL


HANDLE_DOMAINS = frozenset({"example.com"})


@pytest.mark.asyncio
async def test_a_teammate_in_a_handle_domain_is_labelled_by_their_handle() -> None:
    ingress = RelayIngress(enabled=True, handle_email_domains=HANDLE_DOMAINS)
    ingress.bind_host(HostBinding(crew_kind=CrewKind.TEAM))
    async with TestClient(TestServer(_app(ingress))) as client:
        body = await _as(client, ANA, relay_email="renee@example.com")
    assert body["principal"]["display"]["name"] == "renee"
    assert body["principal"]["display"]["login"] == ""
    assert body["author"]["display"]["name"] == "renee"


@pytest.mark.parametrize(
    ("email", "name", "label"),
    [
        ("renee@example.com", "", "renee"),
        ("renee@EXAMPLE.COM", "", "renee"),
        ("renee@example.com", NAME, NAME),
        ("renee@example.org", "", "renee@example.org"),
        ("", "", "Kiro user ...a-0002"),
        ("renee@sub.example.com", "", "renee@sub.example.com"),
        ("renee@example.com.example.io", "", "renee@example.com.example.io"),
        ("renee@notexample.com", "", "renee@notexample.com"),
        ("renee@example.co", "", "renee@example.co"),
    ],
)
def test_the_relayed_label_trims_only_a_handle_domain_address(
    email: str, name: str, label: str
) -> None:
    caller = RelayCaller(
        kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE, email=email, display_name=name
    )
    principal = relay_principal(caller, "local-app", HANDLE_DOMAINS)
    assert principal.display.name == label
    assert principal.display.login == ""


def test_the_handle_domains_setting_is_empty_by_default_and_parsed_narrowly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    assert KiroCrewConfig().tunnel.relay_handle_email_domains == []
    config_file = tmp_path / "config.json"
    stored = [" Example.COM ", "example.com", "", 7, "a@example.org", "example.org"]
    config_file.write_text(
        json.dumps({"tunnel": {"relay_handle_email_domains": stored}}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
    domains = KiroCrewConfig.load().tunnel.relay_handle_email_domains
    assert domains == ["example.com", "example.org"]
    config_file.write_text(
        json.dumps({"tunnel": {"relay_handle_email_domains": "example.com"}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().tunnel.relay_handle_email_domains == []


@pytest.mark.parametrize("email", ["renee@example.com", "renee@example.org"])
def test_with_no_handle_domains_the_label_is_the_full_address(email: str) -> None:
    caller = RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE, email=email)
    assert relay_principal(caller, "local-app").display.name == email


@pytest.mark.asyncio
async def test_a_relayed_teammate_is_never_a_collaborator_or_the_owner() -> None:
    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        body = await _as(client, ANA, relay_name=NAME)
    assert body["collaborator"] is False
    assert body["owner_page"] == 403
    pid = body["principal"]["id"]
    assert pid not in owner.owner_principal_ids()


# ---------------------------------------------------------------------------
# The relayed owner of a personal Crew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id", ["", "U0OWNER"])
async def test_the_relayed_owner_is_the_owner_at_the_machine_and_owner_pages_stay_refused(
    owner_id: str,
) -> None:
    owner.set_owner_subject(owner_id)
    async with TestClient(TestServer(_app(_ingress(CrewKind.PERSONAL), owner_id=owner_id))) as c:
        relayed = await _as(c, OWNER, relay_name=NAME)
        token = generate_token(owner_id or "local-app")
        resp = await c.get("/api/whoami", params={"token": token})
        local = await resp.json()
    machine = local_operator_principal(owner_id or "local-app")
    assert local["principal"]["id"] == machine.id
    # One person: the same id from the machine and over the relay.
    assert relayed["principal"]["id"] == machine.id
    assert relayed["budget"] == local["budget"]
    assert relayed["principal"]["provenance"] == Provenance.KIRO_RELAY.value
    assert relayed["author"]["id"] == machine.id
    # Shared Crew's owner pages need the local operator, so they stay refused.
    assert local["owner_page"] == 200
    assert relayed["owner_page"] == 403
    assert relayed["owner_page_code"] == "owner_only"
    # People recognizes them as the viewer and the owner, in one row.
    view = people.people_view(
        relayed_principal := relay_principal(
            RelayCaller(kiro_user_id=OWNER, kind=RelayCallerKind.OWNER), owner_id or "local-app"
        ),
        [],
        owner_id=owner_id,
    )
    rows = [r for r in view["people"] if r["principal"]["id"] == relayed_principal.id]
    assert len(rows) == 1
    assert rows[0]["is_viewer"] is True and rows[0]["is_owner"] is True


@pytest.mark.asyncio
async def test_a_tailnet_peer_keeps_its_tailscale_provenance() -> None:
    """Guard: the relay branch never runs without a published relay caller."""
    peer = tailnet_principal("alice@example.com", "alice-laptop", Provenance.TAILSCALE_SERVE)
    assert peer.provider == "tailscale" and peer.provenance is Provenance.TAILSCALE_SERVE
    async with TestClient(TestServer(_app(_ingress(CrewKind.TEAM)))) as client:
        token = generate_token("local-app")
        resp = await client.get(
            "/api/whoami",
            params={"token": token},
            headers={"X-Kiro-Relay-User": ANA, "X-Kiro-Relay-User-Name": "UTF-8''Forged"},
        )
        body = await resp.json()
    assert body["principal"]["provenance"] == Provenance.LOCAL_OPERATOR.value
    assert body["principal"]["provider"] == "local"


# ---------------------------------------------------------------------------
# Every owner check that compares principal ids only, for the relayed owner
#
# The relayed owner of a personal Crew shares the owner's principal id, so each
# check that compares ids alone now answers "owner" for them. Each one below is a
# label, a list filter, a fold of the owner's own ids or the typing lease, the
# same answers the owner gets over Tailscale. The checks that keep something
# refused remotely read the local-operator provenance, which a relayed principal
# never has, and stay refused.
# ---------------------------------------------------------------------------


def _relayed_owner(owner_subject: str = "local-app"):
    caller = RelayCaller(kiro_user_id=OWNER, kind=RelayCallerKind.OWNER, display_name=NAME)
    return relay_principal(caller, owner_subject)


def test_is_owner_principal_and_viewer_ids_name_the_relayed_owner() -> None:
    relayed = _relayed_owner()
    assert relayed.provenance is Provenance.KIRO_RELAY
    assert owner.is_owner_principal(relayed)
    assert owner.viewer_ids(relayed) == owner.owner_principal_ids()


def test_the_mine_filter_finds_the_owners_machine_sessions_for_the_relayed_owner() -> None:
    from kiro_crew.shared_crew.contracts import SessionQuery

    relayed = _relayed_owner()
    machine = local_operator_principal("local-app")
    mine = SessionQuery.from_query({"creator": "mine"})
    assert mine.matches(machine, relayed, viewer_ids=owner.viewer_ids(relayed))
    folded = SessionQuery.from_query({"creator": machine.id})
    assert folded.matches(machine, relayed, local_owner_ids=owner.local_owner_principal_ids())


def test_people_marks_the_relayed_owner_as_owner_and_lists_sharees_as_over_tailscale() -> None:
    from kiro_crew.shared_crew.tailscale_sharees import Sharee

    relayed = _relayed_owner()
    sharee_principal = tailnet_principal("bob@example.com", "bob", Provenance.TAILSCALE_SERVE)
    sharee = Sharee(
        user=SimpleNamespace(user_id="u-bob", profile_pic_url=""),  # type: ignore[arg-type]
        principal=sharee_principal,
    )
    view = people.people_view(relayed, [], sharees=(sharee,))
    ids = {r["principal"]["id"]: r for r in view["people"]}
    assert ids[relayed.id]["is_owner"] is True and ids[relayed.id]["is_viewer"] is True
    # The owner, from anywhere, sees who the machine is shared with.
    assert sharee_principal.id in ids
    # A relayed teammate does not.
    teammate = relay_principal(RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE), "")
    other = people.people_view(teammate, [], sharees=(sharee,))
    assert sharee_principal.id not in {r["principal"]["id"] for r in other["people"]}


def test_the_prompt_reads_the_relayed_owners_message_as_the_owners() -> None:
    from kiro_crew.shared_crew.attribution import author_meta, sender_line
    from kiro_crew.shared_crew.contracts import ActorContext

    relayed = _relayed_owner()
    meta = author_meta(
        ActorContext(
            principal=relayed, request_id="r", transport=Transport.REMOTE, correlation_id="r"
        )
    )
    row = {"role": "user", "meta": meta, "content": "hi"}
    slot = SimpleNamespace(messages=[row])
    assert sender_line(slot, row) == ""


def test_the_typing_lease_is_one_holder_for_the_owners_two_tabs() -> None:
    from kiro_crew.shared_crew.typing_lease import TypingLeaseRegistry, same_person

    relayed = _relayed_owner()
    machine = local_operator_principal("local-app")
    assert same_person(relayed, machine)
    leases = TypingLeaseRegistry()
    leases.typed("s1", "machine-tab", machine)
    assert leases.blocks("s1", relayed) is None


def test_the_operator_id_expansion_needs_the_local_operator() -> None:
    """The local-operator fold of every bootstrap subject is not opened by the relay."""
    from kiro_crew.shared_crew.crew_mode import operator_ids

    relayed = _relayed_owner()
    assert operator_ids(relayed) == frozenset({relayed.id})
    assert len(operator_ids(local_operator_principal("local-app"))) > 1


def test_a_relayed_owner_counts_as_a_network_path() -> None:
    """Provenance gates read the relay as remote: stricter, never wider."""
    from kiro_crew.shared_crew.crew_mode import others_observed
    from kiro_crew.shared_crew.memory_changes import crew_is_shared

    relayed = _relayed_owner()
    assert others_observed(local_operator_principal("local-app"), [relayed]) is True
    assert crew_is_shared(SimpleNamespace(), relayed) is True


# ---------------------------------------------------------------------------
# The relay's email and name never reach the audit record
# ---------------------------------------------------------------------------

MARK_EMAIL = "zq7-relay-mark@example.com"
MARK_NAME = "Zq7 Relay Marker Name"


def _marked(kind: RelayCallerKind, user: str):
    caller = RelayCaller(kiro_user_id=user, kind=kind, email=MARK_EMAIL, display_name=MARK_NAME)
    return relay_principal(caller, "local-app")


@pytest.mark.parametrize(
    ("kind", "user"), [(RelayCallerKind.TEAMMATE, ANA), (RelayCallerKind.OWNER, OWNER)]
)
def test_a_relayed_principals_audit_login_is_blank_and_its_id_is_kept(
    kind: RelayCallerKind, user: str
) -> None:
    from kiro_crew.shared_crew.audit import audit_principal

    principal = _marked(kind, user)
    audited = audit_principal(principal)
    assert audited is not None
    assert audited["login"] == ""
    assert audited["id"] == principal.id
    assert audited["provenance"] == Provenance.KIRO_RELAY.value


def test_a_tailnet_principal_keeps_its_audit_login() -> None:
    """Guard: only the relay's display labels are kept out of the audit, live or read back."""
    from kiro_crew.shared_crew.attribution import author_meta
    from kiro_crew.shared_crew.audit import audit_principal
    from kiro_crew.shared_crew.contracts import ActorContext
    from kiro_crew.shared_crew.decisions import turn_initiator

    peer = tailnet_principal("alice@example.com", "alice-laptop", Provenance.TAILSCALE_SERVE)
    audited = audit_principal(peer)
    assert audited is not None and audited["login"] == "alice@example.com"
    actor = ActorContext(principal=peer, request_id="r", transport=Transport.HTTP)
    read_back = turn_initiator([{"role": "user", "meta": {"author": author_meta(actor)}}])
    assert read_back.provenance is Provenance.NONE
    audited_back = audit_principal(read_back)
    assert audited_back is not None and audited_back["login"] == "alice@example.com"


@pytest.mark.parametrize(
    ("kind", "user"), [(RelayCallerKind.TEAMMATE, ANA), (RelayCallerKind.OWNER, OWNER)]
)
def test_an_action_audit_record_as_written_holds_no_relayed_email_or_name(
    kind: RelayCallerKind, user: str
) -> None:
    import dataclasses

    from kiro_crew.shared_crew.audit import new_action_record, security_event
    from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType, ActorContext

    principal = _marked(kind, user)
    actor = ActorContext(
        principal=principal, request_id="r1", transport=Transport.REMOTE, correlation_id="r1"
    )
    record = new_action_record(
        next(iter(ActionType)),
        session_key="dashboard:s1",
        initiator=actor,
        outcome=next(iter(ActionOutcome)),
        responder=actor,
    )
    written = json.dumps(dataclasses.asdict(security_event(record)), ensure_ascii=False)
    assert principal.id in written
    assert MARK_EMAIL not in written
    assert MARK_NAME not in written


# ---------------------------------------------------------------------------
# A principal read back from a transcript loses its provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("labels", ["both", "email-only"])
@pytest.mark.parametrize("action", ["MEMORY_CHANGE", "APPROVAL", "QUESTION_ANSWER"])
def test_an_audit_record_from_a_relayed_turn_holds_no_relayed_email_or_name(
    action: str, labels: str
) -> None:
    """A lesson write, approval or question raised by a relayed teammate's turn.

    Each reads its initiator back from the transcript's author stamp, which keeps no
    provenance, so the audit's own relay check can't see it was relayed.
    """
    import dataclasses

    from kiro_crew.shared_crew.attribution import author_meta
    from kiro_crew.shared_crew.audit import new_action_record, security_event
    from kiro_crew.shared_crew.contracts import ActionOutcome, ActionType, ActorContext
    from kiro_crew.shared_crew.decisions import turn_initiator

    name = MARK_NAME if labels == "both" else ""
    caller = RelayCaller(
        kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE, email=MARK_EMAIL, display_name=name
    )
    actor = ActorContext(
        principal=relay_principal(caller, ""),
        request_id="r1",
        transport=Transport.REMOTE,
        correlation_id="r1",
    )
    messages = [{"role": "user", "content": "hi", "meta": {"author": author_meta(actor)}}]
    initiator = turn_initiator(messages)
    assert initiator.id == actor.principal.id
    assert initiator.provenance is Provenance.NONE
    record = new_action_record(
        ActionType[action],
        session_key="dashboard:s1",
        initiator=ActorContext(
            principal=initiator, request_id="", transport=Transport.HTTP, correlation_id=""
        ),
        outcome=next(iter(ActionOutcome)),
    )
    written = json.dumps(dataclasses.asdict(security_event(record)), ensure_ascii=False)
    assert initiator.id in written
    assert MARK_EMAIL not in written
    assert MARK_NAME not in written


# ---------------------------------------------------------------------------
# The relayed owner never flips the owner's People entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alternating_relayed_and_local_requests_keep_the_owners_people_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.shared_crew import access_set
    from kiro_crew.shared_crew import tailscale_invite_store as store
    from kiro_crew.shared_crew import tailscale_invites as invites
    from kiro_crew.shared_crew.crew_mode import others_observed
    from kiro_crew.shared_crew.routes_invites import decorate_people

    monkeypatch.setattr(access_set, "owner_user_id", lambda: "u-owner-tail")
    monkeypatch.setattr(invites, "members_by_login", lambda: {})
    monkeypatch.setattr(
        store, "display_overrides", lambda: {"u-owner-tail": {"alias": "Boss", "avatar_id": ""}}
    )
    from kiro_crew.shared_crew.contracts import Principal
    from kiro_crew.shared_crew.crew_mode import operator_ids

    machine = local_operator_principal("local-app")
    async with TestClient(TestServer(_app(_ingress(CrewKind.PERSONAL)))) as c:
        token = generate_token("local-app")
        for step in ("relay", "local", "relay", "local", "relay"):
            if step == "relay":
                body = await _as(c, OWNER, relay_name=NAME, relay_email=EMAIL)
                # Request-time gates still see the relay: Shared Crew's owner pages
                # (invites, access card, preview, sharee credential share one gate)
                # refuse, and the operator-id fold stays closed.
                assert body["principal"]["provenance"] == Provenance.KIRO_RELAY.value
                assert body["owner_page"] == 403
                assert body["owner_page_code"] == "owner_only"
                relayed = Principal.from_dict(body["principal"])
                assert operator_ids(relayed) == frozenset({machine.id})
            else:
                resp = await c.get("/api/whoami", params={"token": token})
                body = await resp.json()
                assert body["owner_page"] == 200
            observed = [obs.principal for obs in people.registry().snapshot()]
            assert [p.id for p in observed] == [machine.id], step
            assert observed[0].provenance is Provenance.LOCAL_OPERATOR, step
            assert others_observed(machine, observed) is False, step
            view = decorate_people(people.people_view(machine, []))
            (row,) = [r for r in view["people"] if r["principal"]["id"] == machine.id]
            assert row["tailscale_user_id"] == "u-owner-tail", step
            assert row["member"]["alias"] == "Boss", step


# ---------------------------------------------------------------------------
# The relayed owner's typing lease is the owner's on every local tab
# ---------------------------------------------------------------------------


def test_the_relayed_owners_lease_is_the_owners_on_the_startup_tab() -> None:
    """No owner id configured: the startup tab is ``local-startup``, the relay tab ``local-app``."""
    from kiro_crew.shared_crew.crew_mode import operator_ids
    from kiro_crew.shared_crew.typing_lease import TypingLeaseRegistry

    relayed = _relayed_owner("local-app")
    startup = local_operator_principal("local-startup")
    leases = TypingLeaseRegistry()
    leases.typed("s1", "relay-tab", relayed)
    state = leases.state("s1")
    assert startup.id in state["holder_ids"]
    assert leases.blocks("s1", startup) is None
    # Take over from the startup tab leaves one holder, the owner.
    leases.take_over("s1", "startup-tab", startup)
    assert leases.holder("s1") is not None
    assert leases.holder("s1").id in (relayed.id, startup.id)
    assert leases.blocks("s1", relayed) is None
    # The request-time fold stays closed for the relayed owner.
    assert operator_ids(relayed) == frozenset({relayed.id})


def test_a_relayed_teammates_lease_names_only_them() -> None:
    """Guard: the lease fold widens for the relayed owner only."""
    from kiro_crew.shared_crew.typing_lease import TypingLeaseRegistry

    teammate = relay_principal(RelayCaller(kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE), "")
    leases = TypingLeaseRegistry()
    leases.typed("s1", "conn", teammate)
    assert leases.state("s1")["holder_ids"] == [teammate.id]
    assert leases.blocks("s1", local_operator_principal("local-startup")) == teammate


# ---------------------------------------------------------------------------
# The relay branch comes before the SSM guest branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_with_a_relay_caller_and_an_ssm_guest_resolves_to_the_relay() -> None:
    """Pins the order in ``_derive_actor``: the verified relay caller wins.

    No real request comes through both listeners. If one ever carried both keys, the
    relay caller (published only by a verified handover) decides, not the guest key.
    """
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY
    from kiro_crew.shared_crew.contracts import SSM_GUEST_REQUEST_KEY, SsmGuest
    from kiro_crew.shared_crew.identity import publish_actor

    request = make_mocked_request("GET", "/api/whoami")
    request[RELAY_CALLER_KEY] = RelayCaller(
        kiro_user_id=ANA, kind=RelayCallerKind.TEAMMATE, display_name="Ana"
    )
    request[SSM_GUEST_REQUEST_KEY] = SsmGuest(invite_id="inv-0001", alias="Guest")
    request["user"] = f"relay:{ANA}"
    request["app"] = ""
    actor = await publish_actor(request, trust=None, serve_peer=None)
    assert actor.principal.provider == "kiro"
    assert actor.principal.provenance is Provenance.KIRO_RELAY
    assert actor.principal.id == principal_id_for("kiro", "kiro-relay", ANA)
    assert actor.transport is Transport.REMOTE
