"""Shared Crew over AWS Systems Manager: detection, invites, and the owner invariant.

The invariant under test: an SSM port forward arrives as a loopback connection,
which is what the owner's own browser looks like, so a connection on the SSM
port is never the owner whatever it carries. The gate tests use real sockets:
one aiohttp app, the real token middleware, and two loopback sites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from kiro_crew.config.sections_shared_crew import SharedCrewConfig, shared_crew_config_from
from kiro_crew.dashboard import ssm_gate, tailnet_live_allowlist
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import access, access_set, routes_ssm, ssm, ssm_invites
from kiro_crew.shared_crew.contracts import CapabilityState, Provenance
from kiro_crew.shared_crew.identity import actor_of, ssm_principal
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, finished


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(ssm_invites, "root", lambda: tmp_path / "ssm")
    _floor_monkeypatch.setattr(rg, "_gen", 0)
    _floor_monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ssm_invites._reset_for_tests()
    ssm._reset_for_tests()
    ta._state.clear_all()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    access_set.install((), owner_login="owner@example.com", owner_user_id="11")
    yield
    ta._state.clear_all()
    ssm_invites._reset_for_tests()
    ssm._reset_for_tests()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()


# ── config ──


def test_ssm_is_off_by_default_and_reads_off_for_anything_but_true() -> None:
    assert SharedCrewConfig().ssm is False
    assert SharedCrewConfig().ssm_port == 5477
    assert shared_crew_config_from({"ssm": True}).ssm is True
    for raw in ("true", 1, None, [True]):
        assert shared_crew_config_from({"ssm": raw}).ssm is False


def test_the_port_is_clamped_to_unprivileged_ports() -> None:
    assert shared_crew_config_from({"ssm_port": 80}).ssm_port == 1024
    assert shared_crew_config_from({"ssm_port": 70000}).ssm_port == 65535
    assert shared_crew_config_from({"ssm_port": "x"}).ssm_port == 5477


# ── detection and the command ──

INSTANCE = "i-0123456789abcdef0"


def test_detect_target_prefers_the_operator_override_and_validates_it() -> None:
    env = {"KIROCREW_SSM_TARGET": "mi-0123456789abcdef0", "AWS_REGION": "eu-west-1"}
    found = ssm.detect_target(env, imds=lambda: pytest.fail("override must skip metadata"))
    assert found == ssm.Target("mi-0123456789abcdef0", "eu-west-1")
    for bad in ("i-xyz", "i-0123456789abcdef0; rm -rf /", "$(id)", "i-0123456789abcdef0 extra"):
        assert ssm.detect_target({"KIROCREW_SSM_TARGET": bad}, imds=lambda: None) is None


def test_detect_target_reads_instance_metadata_and_fills_a_missing_region() -> None:
    assert ssm.detect_target({}, imds=lambda: ssm.Target(INSTANCE, "us-east-1")) == ssm.Target(
        INSTANCE, "us-east-1"
    )
    assert ssm.detect_target({"AWS_REGION": "us-west-2"}, imds=lambda: ssm.Target(INSTANCE)) == (
        ssm.Target(INSTANCE, "us-west-2")
    )
    assert ssm.detect_target({}, imds=lambda: None) is None


class _FakeImds:
    """An IMDSv2 endpoint: the token PUT first, then the metadata GETs with that token."""

    def __init__(
        self, answers: dict[str, Any], seen: list[tuple[str, str, dict[str, str]]]
    ) -> None:
        self._answers = answers
        self._seen = seen
        self._path = ""

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        self._seen.append((method, path, dict(headers or {})))
        self._path = path

    def getresponse(self) -> Any:
        answer = self._answers[self._path]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return SimpleNamespace(status=status, read=lambda _n=None: body.encode())

    def close(self) -> None:
        pass


def _imds(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[Any]:
    seen: list[Any] = []
    monkeypatch.setattr(
        ssm.http.client, "HTTPConnection", lambda *_a, **_k: _FakeImds(answers, seen)
    )
    return seen


def test_read_imds_takes_a_token_first_and_reads_the_id_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _imds(
        monkeypatch,
        {
            "/latest/api/token": (200, "tok"),
            "/latest/meta-data/instance-id": (200, INSTANCE),
            "/latest/meta-data/placement/region": (200, "eu-west-1"),
        },
    )
    assert ssm.read_imds() == ssm.Target(INSTANCE, "eu-west-1")
    assert [m for m, _p, _h in seen] == ["PUT", "GET", "GET"]
    assert seen[1][2]["X-aws-ec2-metadata-token"] == "tok"


def test_read_imds_keeps_the_id_when_only_the_region_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _imds(
        monkeypatch,
        {
            "/latest/api/token": (200, "tok"),
            "/latest/meta-data/instance-id": (200, INSTANCE),
            "/latest/meta-data/placement/region": OSError("down"),
        },
    )
    assert ssm.read_imds() == ssm.Target(INSTANCE, "")


@pytest.mark.parametrize(
    "answers",
    [
        {"/latest/api/token": OSError("not on EC2")},
        {"/latest/api/token": (404, "")},
        {
            "/latest/api/token": (200, "tok"),
            "/latest/meta-data/instance-id": (200, "not-an-instance"),
            "/latest/meta-data/placement/region": (200, "eu-west-1"),
        },
    ],
)
def test_read_imds_is_none_off_ec2_or_for_an_id_that_is_not_a_node(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]
) -> None:
    _imds(monkeypatch, answers)
    assert ssm.read_imds() is None


def test_a_bad_region_is_dropped_not_passed_to_the_command() -> None:
    found = ssm.detect_target({"KIROCREW_SSM_TARGET": INSTANCE, "AWS_REGION": "x; id"})
    assert found == ssm.Target(INSTANCE, "")


def test_the_start_session_command_is_an_argument_list_naming_the_fixed_port_document() -> None:
    args = ssm.start_session_args(ssm.Target(INSTANCE, "us-east-1"), 5477)
    assert args == [
        "aws",
        "ssm",
        "start-session",
        "--target",
        INSTANCE,
        "--document-name",
        "Kiro-Crew-Shared-Crew-5477",
        "--parameters",
        "localPortNumber=5477",
        "--region",
        "us-east-1",
    ]
    assert " --region" not in ssm.start_session_command(ssm.Target(INSTANCE), 5477)
    assert ssm.invite_url(5477, "abc.def") == "http://localhost:5477/?ssm_invite=abc.def"


GUIDE = Path(__file__).resolve().parent.parent / "docs" / "guides" / "remote-and-mobile.md"


def _guide_section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index("### Share one Crew over AWS SSM")
    return text[start : text.index("\n### ", start + 1)]


def test_the_copied_command_names_the_document_the_guide_tells_owners_to_create() -> None:
    """A guest who copies the command must hit the document the owner was told to allow.

    The guide has the owner create the fixed-port document and allow ``ssm:StartSession`` on
    it alone, so a command naming any other document is refused with AccessDenied.
    """
    import re

    section = _guide_section()
    created = re.findall(r"create-document\s+--name\s+(\S+)", section)
    told_to_run = re.findall(r"--document-name\s+(\S+)", section)
    args = ssm.start_session_args(ssm.Target(INSTANCE), 5477)
    command_document = args[args.index("--document-name") + 1]
    assert command_document == ssm.session_document(5477)
    assert created == [command_document]
    assert told_to_run == [command_document]
    assert f"document/{command_document}" in section, "the IAM policy names the same document"
    assert "portNumber=5477" not in ssm.start_session_command(ssm.Target(INSTANCE), 5477)


# ── invites ──


def test_an_invite_verifies_until_it_is_revoked_and_stores_only_a_hash() -> None:
    invite_id, credential = ssm_invites.create("  Dana\n")
    secret = credential.split(".", 1)[1]
    assert ssm_invites.verify(credential) == ssm_invites.SsmGuest(invite_id, "Dana")
    stored = (ssm_invites.root() / ssm_invites.STATE_FILE).read_text()
    assert secret not in stored and credential not in stored
    assert json.loads(stored)["invites"][invite_id]["secret_sha256"]
    assert ssm_invites.verify(f"{invite_id}.wrong") is None
    assert ssm_invites.verify(f"nope.{secret}") is None
    for odd in ("", ".", invite_id, f"{invite_id}.", None, 7):
        assert ssm_invites.verify(odd) is None
    assert ssm_invites.revoke(invite_id) is True
    assert ssm_invites.verify(credential) is None
    assert ssm_invites.revoke(invite_id) is False


def test_invites_survive_a_restart_and_the_list_carries_no_secret() -> None:
    invite_id, credential = ssm_invites.create("Lee")
    ssm_invites._reset_for_tests()
    assert ssm_invites.verify(credential) is not None
    rows = ssm_invites.public_rows()
    assert [r["id"] for r in rows] == [invite_id]
    assert set(rows[0]) == {"id", "alias", "created_at", "joined", "principal_id"}


def test_warm_reads_the_store_so_a_request_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _id, credential = ssm_invites.create("Dana")
    ssm_invites._reset_for_tests()
    ssm_invites.warm()
    monkeypatch.setattr(ssm_invites, "_read", lambda: pytest.fail("a request read the disk"))
    assert ssm_invites.verify(credential) is not None
    assert ssm_invites.has_invites() is True


def test_the_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssm_invites, "MAX_INVITES", 2)
    ssm_invites.create("a")
    ssm_invites.create("b")
    with pytest.raises(ssm_invites.InviteLimitError):
        ssm_invites.create("c")


def test_the_ssm_principal_is_keyed_by_the_invite_not_the_alias() -> None:
    one = ssm_principal("abc123", "Dana")
    assert one.id == ssm_principal("abc123", "Renamed").id
    assert one.id != ssm_principal("abc124", "Dana").id
    assert one.provenance is Provenance.SSM_INVITE
    assert one.display.name == "Dana"


# ── setup path ──


def test_the_setup_path_follows_the_listener() -> None:
    assert access.setup_paths_dict()["ssm"]["reason"] == "not_configured"
    ssm.note_listener(ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, 5477))
    assert access.setup_paths_dict()["ssm"] == {"state": "unavailable", "reason": "no_target"}
    ssm.note_listener(ssm.Listener(ssm.STATE_ACTIVE, "", 5477, ssm.Target(INSTANCE)))
    assert access.setup_paths_dict()["ssm"]["state"] == CapabilityState.AVAILABLE.value


# ── resolve ──


def test_resolve_never_binds_without_a_target_a_free_port_and_the_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = SharedCrewConfig(ssm=True, ssm_port=5477)
    monkeypatch.setattr(ssm, "detect_target", lambda: None)
    assert ssm_gate.resolve(SharedCrewConfig(), 5476)[1] is None
    answer, gate = ssm_gate.resolve(on, 5476)
    assert (answer.reason, gate) == (ssm.REASON_NO_TARGET, None)
    answer, gate = ssm_gate.resolve(on, 5477)
    assert (answer.reason, gate) == (ssm.REASON_PORT_IN_USE, None)
    monkeypatch.setattr(ssm, "detect_target", lambda: ssm.Target(INSTANCE, "us-east-1"))
    answer, gate = ssm_gate.resolve(on, 5476)
    assert answer.active and gate is not None and gate.port == 5477
    assert ssm_invites._cache is not None, "resolve warms the invite store off the event loop"


# ── the gate: real sockets, the real token middleware ──


OWNER = "local-app"


async def _whoami(request: web.Request) -> web.Response:
    actor = actor_of(request)
    return web.json_response(
        {
            "user": request.get("user"),
            "provenance": actor.principal.provenance.value,
            "kind": actor.principal.kind.value,
            "owner": is_owner_dashboard_request(request),
            "internal_auth": request.get("internal_auth") is True,
            "app": request.get("app"),
        }
    )


async def _identity_gate(request: web.Request) -> web.Response:
    from kiro_crew.shared_crew import routes_identity

    refused = await routes_identity._refusal(request)
    return web.json_response({"admitted": refused is None})


class Servers:
    def __init__(self, main: int, ssm_port: int, runner: web.AppRunner) -> None:
        self.main = main
        self.ssm = ssm_port
        self.runner = runner


@pytest_asyncio.fixture
async def servers() -> AsyncIterator[Servers]:
    ref = ssm_gate.GateRef()
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_secret="s3cret",
                internal_paths=frozenset({"/api/internal-only"}),
                ssm_gate=ref,
            )
        ]
    )
    app["state"] = SimpleNamespace(owner_id="")
    app.router.add_get("/api/whoami", _whoami)
    app.router.add_get("/api/identity-gate", _identity_gate)
    app.router.add_get("/api/internal-only", _whoami)
    routes_ssm.register(app)
    runner = web.AppRunner(app)
    await runner.setup()
    ports: list[int] = []
    for _ in range(2):
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        ports.append(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
    ref.gate = ssm_gate.SsmGate(port=ports[1])
    ssm.note_listener(ssm.Listener(ssm.STATE_ACTIVE, "", ports[1], ssm.Target(INSTANCE)))
    try:
        yield Servers(ports[0], ports[1], runner)
    finally:
        await runner.cleanup()


def _jar() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())


async def _get(
    port: int,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: Any = None,
    cookies: dict[str, str] | None = None,
) -> tuple[int, Any, aiohttp.ClientResponse]:
    async with _jar() as client:
        async with client.get(
            f"http://127.0.0.1:{port}{path}",
            params=params,
            headers=headers,
            cookies=cookies,
            allow_redirects=False,
        ) as resp:
            text = await resp.text()
            try:
                body = json.loads(text)
            except ValueError:
                body = text
            return resp.status, body, resp


async def _join(port: int, credential: str) -> dict[str, str]:
    status, _body, resp = await _get(port, "/", params={"ssm_invite": credential})
    assert status == 302
    cookie = resp.cookies[f"{ssm_gate.COOKIE_PREFIX}{port}"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert "ssm_invite" not in resp.headers["Location"]
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    return {f"{ssm_gate.COOKIE_PREFIX}{port}": cookie.value}


def _owner_token() -> str:
    return generate_token(OWNER, ttl_seconds=FALLBACK_TIMEOUT_SECS)


@pytest.mark.asyncio
async def test_the_owner_still_is_the_owner_on_the_dashboard_port(servers: Servers) -> None:
    status, body, _ = await _get(servers.main, "/api/whoami", params={"token": _owner_token()})
    assert status == 200 and body["user"] == OWNER and body["owner"] is True


@pytest.mark.asyncio
async def test_a_connection_on_the_ssm_port_with_no_invite_is_refused(servers: Servers) -> None:
    status, body, _ = await _get(servers.ssm, "/api/whoami")
    assert status == 403 and body["code"] == "ssm_refused"
    status, body, _ = await _get(servers.ssm, "/api/whoami", params={"ssm_invite": "a.b"})
    assert status == 403 and body["code"] == "ssm_refused"


@pytest.mark.asyncio
async def test_the_owners_dashboard_token_does_not_make_an_ssm_connection_the_owner(
    servers: Servers,
) -> None:
    token = _owner_token()
    cookie = {f"mc_token_{servers.ssm}": token, f"mc_token_{servers.main}": token}
    for kwargs in ({"params": {"token": token}}, {"cookies": cookie}):
        status, _body, _ = await _get(servers.ssm, "/api/whoami", **kwargs)
        assert status == 403


@pytest.mark.asyncio
async def test_the_internal_secret_is_worth_nothing_on_the_ssm_port(servers: Servers) -> None:
    for path in ("/api/whoami", "/api/internal-only"):
        status, _body, _ = await _get(servers.ssm, path, headers={"X-Internal-Secret": "s3cret"})
        assert status == 403
    _id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    status, _body, _ = await _get(servers.ssm, "/api/internal-only", cookies=cookie)
    assert status == 403


@pytest.mark.asyncio
async def test_a_forwarded_connection_is_never_the_owner_even_holding_every_credential(
    servers: Servers,
) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    token = _owner_token()
    status, body, _ = await _get(
        servers.ssm,
        "/api/whoami",
        params={"token": token},
        headers={"X-Internal-Secret": "s3cret"},
        cookies={**cookie, f"mc_token_{servers.ssm}": token},
    )
    assert status == 403, "the internal secret header alone refuses the request"
    status, body, _ = await _get(
        servers.ssm,
        "/api/whoami",
        params={"token": token},
        cookies={**cookie, f"mc_token_{servers.ssm}": token},
    )
    assert status == 200
    assert body["user"] == f"ssm:{invite_id}"
    assert body["owner"] is False
    assert body["provenance"] == Provenance.SSM_INVITE.value
    assert body["internal_auth"] is False and body["app"] == ""
    # The owner-only routes refuse the guest.
    for path in ("/api/shared-crew/ssm",):
        status, refused, _ = await _get(servers.ssm, path, cookies=cookie)
        assert status == 403 and refused["code"] == "owner_only"
    async with _jar() as client:
        async with client.post(
            f"http://127.0.0.1:{servers.ssm}/api/shared-crew/ssm/invites",
            json={"alias": "x"},
            cookies=cookie,
        ) as resp:
            assert resp.status == 403
    assert len(ssm_invites.public_rows()) == 1


@pytest.mark.asyncio
async def test_an_offered_credential_of_the_wrong_shape_is_refused_and_never_set_as_a_cookie(
    servers: Servers,
) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    for offered in (
        credential + "\r\nSet-Cookie: x=1",
        credential + ";",
        f"{invite_id}.",
        "no-dot",
        "é." + credential,
    ):
        status, _body, resp = await _get(servers.ssm, "/", params={"ssm_invite": offered})
        assert status == 403, offered
        assert not any(name.startswith(ssm_gate.COOKIE_PREFIX) for name in resp.cookies)


@pytest.mark.asyncio
async def test_the_redirect_after_an_invite_never_leaves_this_host(servers: Servers) -> None:
    _id, credential = ssm_invites.create("Dana")
    status, _body, resp = await _get(
        servers.ssm, "//evil.example/x", params={"ssm_invite": credential}
    )
    assert status == 302
    assert resp.headers["Location"].startswith("/evil.example/x")
    assert not resp.headers["Location"].startswith("//")


def test_an_origin_policy_that_refuses_the_ssm_origins_keeps_the_listener_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import urls

    asked: list[str] = []

    def refuse(origin: str, *, audit_tool: str = "") -> bool:
        asked.append(audit_tool)
        return False

    monkeypatch.setattr(urls, "origin_permitted", refuse)
    monkeypatch.setenv("KIROCREW_SSM_TARGET", INSTANCE)
    cfg = shared_crew_config_from({"ssm": True})
    answer, gate = ssm_gate.resolve(cfg, 5470)
    assert gate is None
    assert (answer.state, answer.reason) == (ssm.STATE_UNAVAILABLE, ssm.REASON_GOVERNED)
    assert asked and set(asked) == {"ssm_listener"}
    # With the policy permitting them, the same config runs the listener.
    monkeypatch.setattr(urls, "origin_permitted", lambda origin, *, audit_tool="": True)
    answer, gate = ssm_gate.resolve(cfg, 5470)
    assert gate is not None and answer.active


@pytest.mark.asyncio
async def test_the_owner_routes_answer_404_while_governance_keeps_the_listener_off(
    servers: Servers,
) -> None:
    ssm.note_listener(ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_GOVERNED, servers.ssm))
    token = _owner_token()
    base = f"http://127.0.0.1:{servers.main}/api/shared-crew/ssm"
    async with _jar() as client:
        async with client.get(base, params={"token": token}) as resp:
            assert resp.status == 404
        async with client.post(base + "/invites", params={"token": token}, json={}) as resp:
            assert resp.status == 404
        async with client.delete(base + "/invites/abc", params={"token": token}) as resp:
            assert resp.status == 404


@pytest.mark.asyncio
async def test_an_admitted_guest_can_read_the_people_surfaces(servers: Servers) -> None:
    _id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    status, body, _ = await _get(servers.ssm, "/api/identity-gate", cookies=cookie)
    assert status == 200 and body == {"admitted": True}


class _FakeSite:
    """A TCPSite that binds nothing: it records the address and starts or refuses on demand."""

    refuse = False
    bound: list[tuple[str, int]] = []

    def __init__(self, _runner: Any, host: str, port: int, **_kw: Any) -> None:
        self._address = (host, port)

    async def start(self) -> None:
        if _FakeSite.refuse:
            raise OSError("address in use")
        _FakeSite.bound.append(self._address)


async def _start_listener(
    monkeypatch: pytest.MonkeyPatch, cfg: SharedCrewConfig, *, refuse: bool = False
) -> tuple[Any, Any, Any]:
    _FakeSite.refuse = refuse
    _FakeSite.bound = []
    monkeypatch.setattr(web, "TCPSite", _FakeSite)
    app = web.Application()
    app["allowed_origins"] = set()
    ref = ssm_gate.GateRef()
    sites = await ssm_gate.start_after_bind(app, SimpleNamespace(), cfg, 5470, ref)
    return app, ref, sites


@pytest.mark.asyncio
async def test_the_listener_binds_loopback_sets_its_gate_and_adds_its_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_SSM_TARGET", INSTANCE)
    cfg = shared_crew_config_from({"ssm": True, "ssm_port": 5481})
    app, ref, sites = await _start_listener(monkeypatch, cfg)
    assert len(sites) == 1 and _FakeSite.bound == [("127.0.0.1", 5481)]
    assert ref.gate is not None and ref.gate.port == 5481
    assert ssm.listener().active
    # Its origins are never added to the set every listener shares.
    assert app["allowed_origins"] == set() and app["ssm_gate_ref"] is ref


@pytest.mark.asyncio
async def test_a_port_that_is_taken_leaves_no_gate_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_SSM_TARGET", INSTANCE)
    cfg = shared_crew_config_from({"ssm": True, "ssm_port": 5481})
    app, ref, sites = await _start_listener(monkeypatch, cfg, refuse=True)
    assert sites == [] and ref.gate is None
    assert (ssm.listener().state, ssm.listener().reason) == (
        ssm.STATE_UNAVAILABLE,
        ssm.REASON_BIND_FAILED,
    )
    assert app["allowed_origins"] == set()


@pytest.mark.asyncio
async def test_a_listener_that_cannot_be_resolved_stays_off_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("no metadata")

    monkeypatch.setattr(ssm_gate, "resolve", boom)
    cfg = shared_crew_config_from({"ssm": True, "ssm_port": 5481})
    _app, ref, sites = await _start_listener(monkeypatch, cfg)
    assert sites == [] and ref.gate is None
    assert ssm.listener().reason == ssm.REASON_NO_TARGET


@pytest.mark.asyncio
async def test_a_browser_refused_on_the_ssm_port_gets_a_page_and_a_script_gets_json(
    servers: Servers,
) -> None:
    async with _jar() as client:
        url = f"http://127.0.0.1:{servers.ssm}/"
        async with client.get(url, headers={"Accept": "text/html"}) as resp:
            assert resp.status == 403 and resp.content_type == "text/html"
            assert "can't open this Crew" in await resp.text()
        async with client.get(url) as resp:
            assert resp.status == 403 and resp.content_type == "application/json"


def test_the_invite_cookie_is_keyed_by_the_port_the_browser_connects_to() -> None:
    def request(host: str, sockname: Any = ("127.0.0.1", 5477)) -> Any:
        transport = SimpleNamespace(get_extra_info=lambda name: sockname)
        return SimpleNamespace(headers={"Host": host}, transport=transport)

    # Two Crews forwarded to different local ports never share a cookie.
    assert ssm_gate.cookie_name(request("localhost:5477")) == "mc_ssm_5477"
    assert ssm_gate.cookie_name(request("localhost:6001")) == "mc_ssm_6001"
    # No port in Host: the listener's own port stands in.
    assert ssm_gate.cookie_name(request("localhost")) == "mc_ssm_5477"
    assert ssm_gate.cookie_name(SimpleNamespace(headers={}, transport=None)) == "mc_ssm_0"


def _checked_request(*, local_port: int, host: str, origin: str) -> Any:
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["allowed_origins"] = {"http://127.0.0.1:5470", "http://localhost:5470"}
    ref = ssm_gate.GateRef()
    ref.gate = ssm_gate.SsmGate(port=5481)
    app["ssm_gate_ref"] = ref
    transport = SimpleNamespace(
        get_extra_info=lambda name, default=None: (
            ("127.0.0.1", local_port) if name == "sockname" else ("127.0.0.1", 40000)
        )
    )
    return make_mocked_request(
        "POST",
        "/api/x",
        headers={"Host": host, "Origin": origin},
        app=app,
        transport=transport,
    )


def test_the_ssm_origins_pass_only_for_a_request_that_arrived_on_the_ssm_port() -> None:
    from kiro_crew.dashboard.origin import check_origin

    on_ssm = _checked_request(
        local_port=5481, host="localhost:5481", origin="http://localhost:5481"
    )
    assert check_origin(on_ssm)
    # A page served by whatever else holds that port (the other loopback family) and
    # posting to the dashboard's own port is the cross-port CSRF shape: refused.
    squatter = _checked_request(
        local_port=5470, host="localhost:5470", origin="http://localhost:5481"
    )
    assert not check_origin(squatter)


@pytest.mark.asyncio
async def test_the_invite_cookie_opens_nothing_on_the_dashboard_port(servers: Servers) -> None:
    _id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    status, _body, _ = await _get(servers.main, "/api/whoami", cookies=cookie)
    assert status == 403
    status, _body, _ = await _get(servers.main, "/api/whoami", params={"ssm_invite": credential})
    assert status == 403


@pytest.mark.asyncio
async def test_a_revoked_invite_stops_working_on_the_next_request(servers: Servers) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    cookie = await _join(servers.ssm, credential)
    assert (await _get(servers.ssm, "/api/whoami", cookies=cookie))[0] == 200
    ssm_invites.revoke(invite_id)
    assert (await _get(servers.ssm, "/api/whoami", cookies=cookie))[0] == 403


@pytest.mark.asyncio
async def test_a_revoked_invite_closes_a_request_that_is_still_open() -> None:
    """The re-check closes the guest's connection once the invite stops verifying.

    The handler waits on the transport's own close, so nothing here sleeps or races a
    timer: it returns exactly when the gate closes the connection.
    """
    invite_id, credential = ssm_invites.create("Dana")
    guest = ssm_invites.verify(credential)
    assert guest is not None
    closed = asyncio.Event()
    request = SimpleNamespace(transport=SimpleNamespace(close=closed.set))

    async def handler(_request: Any) -> str:
        await closed.wait()
        return "closed by the gate"

    ssm_invites.revoke(invite_id)
    result = await finished(
        ssm_gate._run_with_reverify(request, credential, guest, handler, interval=0),
        "the gate to close the revoked guest's connection (transport.close)",
    )
    assert result == "closed by the gate"


@pytest.mark.asyncio
async def test_redeeming_an_invite_is_audited_without_the_credential() -> None:
    """The redirect that mints the cookie is a grant, so it writes a SEL event.

    The event names the invite id and never the credential.
    """
    invite_id, credential = ssm_invites.create("Dana")
    logged: list[tuple[str, str, str]] = []

    def log_auth(_request: Any, subject: str, outcome: str, detail: str) -> None:
        logged.append((subject, outcome, detail))

    async def publish(_request: Any) -> None:
        raise AssertionError("a redemption publishes no actor")

    async def handler(_request: Any) -> None:
        raise AssertionError("a redemption never reaches the handler")

    from aiohttp.test_utils import make_mocked_request

    request = make_mocked_request("GET", f"/?{ssm_gate.INVITE_QUERY}={credential}")
    response = await ssm_gate.admit(
        request,
        handler,
        strict_internal=False,
        log_auth=log_auth,
        publish=publish,
    )
    assert response.status == 302
    assert logged == [(f"ssm:{invite_id}", "granted", "ssm invite redeemed")]
    assert credential not in repr(logged)


@pytest.mark.asyncio
async def test_the_loopback_host_redirect_never_carries_an_invite_off_the_ssm_port() -> None:
    """The redirect keeps the query, so on the SSM port it would move ``ssm_invite`` to another host.

    That layer runs before the SSM gate has checked anyone, and the invite is permanent:
    on a name that resolves to both loopback families, whoever holds the other family's
    port would receive it. Every redirect ahead of the gate is this one.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.server import build_host_canonical_redirect

    async def ok(_request: web.Request) -> web.Response:
        return web.Response(text="served here")

    ref = ssm_gate.GateRef()
    app = web.Application(middlewares=[build_host_canonical_redirect("kirocrew.localhost")])
    app["ssm_gate_ref"] = ref
    app.router.add_get("/", ok)
    headers = {"Host": "localhost:5477", "Sec-Fetch-Dest": "document"}
    async with TestClient(TestServer(app)) as client:
        control = await client.get(
            "/", params={"ssm_invite": "a.b"}, headers=headers, allow_redirects=False
        )
        assert control.status == 302, "the control: any other port is still canonicalized"

        ref.gate = ssm_gate.SsmGate(port=client.server.port)
        on_ssm = await client.get(
            "/", params={"ssm_invite": "a.b"}, headers=headers, allow_redirects=False
        )
        assert on_ssm.status == 200 and "Location" not in on_ssm.headers
        assert await on_ssm.text() == "served here"


def test_the_gate_owns_only_the_loopback_ssm_port() -> None:
    def request(sockname: Any) -> Any:
        transport = SimpleNamespace(get_extra_info=lambda name: sockname)
        return SimpleNamespace(transport=transport)

    gate = ssm_gate.SsmGate(port=5477)
    assert gate.owns(request(("127.0.0.1", 5477)))
    assert gate.owns(request(("::1", 5477, 0, 0)))
    assert not gate.owns(request(("127.0.0.1", 5476)))
    assert not gate.owns(request(("100.64.0.1", 5477)))
    assert not gate.owns(request(None))
    assert not gate.owns(SimpleNamespace(transport=None))


# ── owner routes ──


@pytest.mark.asyncio
async def test_the_owner_makes_and_revokes_invites(servers: Servers) -> None:
    token = _owner_token()
    async with _jar() as client:
        base = f"http://127.0.0.1:{servers.main}"
        async with client.get(f"{base}/api/shared-crew/ssm", params={"token": token}) as resp:
            status = await resp.json()
        assert status["state"] == "active" and status["invites"] == []
        assert status["command"].startswith("aws ssm start-session --target " + INSTANCE)
        assert status["local_url"] == f"http://localhost:{servers.ssm}/"
        access_set.acknowledge_first_share("owner")
        async with client.post(
            f"{base}/api/shared-crew/ssm/invites", params={"token": token}, json={"alias": "Dana"}
        ) as resp:
            made = await resp.json()
        assert made["invite"]["alias"] == "Dana"
        credential = made["url"].split("ssm_invite=", 1)[1]
        assert ssm_invites.verify(credential) is not None
        async with client.get(f"{base}/api/shared-crew/ssm", params={"token": token}) as resp:
            listed = await resp.json()
        assert credential not in json.dumps(listed)
        invite_id = made["invite"]["id"]
        async with client.delete(
            f"{base}/api/shared-crew/ssm/invites/{invite_id}", params={"token": token}
        ) as resp:
            assert (await resp.json())["revoked"] is True
        async with client.delete(
            f"{base}/api/shared-crew/ssm/invites/{invite_id}", params={"token": token}
        ) as resp:
            assert resp.status == 404
    assert ssm_invites.verify(credential) is None


@pytest.mark.asyncio
async def test_the_first_invite_waits_for_the_first_share_acknowledgement(
    servers: Servers,
) -> None:
    token = _owner_token()
    url = f"http://127.0.0.1:{servers.main}/api/shared-crew/ssm/invites"
    async with _jar() as client:
        async with client.post(url, params={"token": token}, json={"alias": "Dana"}) as resp:
            assert resp.status == 409
            assert (await resp.json())["code"] == "first_share_ack_required"
        assert ssm_invites.public_rows() == []
        access_set.acknowledge_first_share("owner")
        async with client.post(url, params={"token": token}, json={"alias": "Dana"}) as resp:
            assert resp.status == 200
    # An SSM invite is access: with one out, someone else can open the Crew.
    assert access_set.nobody_else_has_access() is False


def test_an_ssm_invite_counts_as_someone_else_having_access() -> None:
    assert access_set.nobody_else_has_access() is True
    ssm_invites.create("Dana")
    assert access_set.nobody_else_has_access() is False


@pytest.mark.asyncio
async def test_an_invite_reads_joined_once_someone_opens_it_and_names_their_person(
    servers: Servers,
) -> None:
    invite_id, credential = ssm_invites.create("Dana")
    [row] = ssm_invites.public_rows()
    assert row["joined"] is False
    assert row["principal_id"] == ssm_principal(invite_id).id
    await _join(servers.ssm, credential)
    [row] = ssm_invites.public_rows()
    assert row["joined"] is True
    ssm_invites._reset_for_tests()
    assert ssm_invites.public_rows()[0]["joined"] is True
    # A refused credential joins nobody.
    other, _ = ssm_invites.create("Lee")
    await _get(servers.ssm, "/", params={"ssm_invite": f"{other}.wrong"})
    assert [r["joined"] for r in ssm_invites.public_rows() if r["id"] == other] == [False]


@pytest.mark.asyncio
async def test_no_invite_is_made_while_the_listener_is_not_active(servers: Servers) -> None:
    ssm.note_listener(ssm.Listener(ssm.STATE_UNAVAILABLE, ssm.REASON_NO_TARGET, servers.ssm))
    async with _jar() as client:
        async with client.post(
            f"http://127.0.0.1:{servers.main}/api/shared-crew/ssm/invites",
            params={"token": _owner_token()},
            json={},
        ) as resp:
            assert resp.status == 409
            assert (await resp.json())["code"] == "ssm_not_active"
    assert ssm_invites.public_rows() == []


@pytest.mark.asyncio
async def test_the_routes_answer_404_without_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.routes import shared_crew as slice_

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "0")
    app = web.Application()
    slice_.register(app)
    handler = next(
        r.handler
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/shared-crew/ssm"
    )
    with pytest.raises(web.HTTPNotFound):
        await handler(SimpleNamespace())


@pytest.mark.asyncio
async def test_an_ssm_guest_is_listed_no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apps stay owner-only for a guest: their app frames load without the invite cookie.

    The list is what every app action (link preview, message and selection
    actions, composer actions) is built from, so an empty list hides them all.
    """
    from aiohttp.test_utils import make_mocked_request

    import kiro_crew.apps.routes as routes_mod
    from kiro_crew.shared_crew.contracts import SSM_GUEST_REQUEST_KEY, SsmGuest

    # Patched through the facade: these names are bound by more than one routes module.
    monkeypatch.setattr(routes_mod, "list_apps", lambda: [{"name": "demo", "enabled": True}])
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

    guest = make_mocked_request("GET", "/api/apps")
    guest[SSM_GUEST_REQUEST_KEY] = SsmGuest(invite_id="inv1", alias="Dana")
    assert json.loads((await routes_mod.handle_list_apps(guest)).text) == []

    owner = await routes_mod.handle_list_apps(make_mocked_request("GET", "/api/apps"))
    assert [row["name"] for row in json.loads(owner.text)] == ["demo"]
