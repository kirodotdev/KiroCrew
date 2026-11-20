"""``permissions.kiroOperations``: the gateway calls an app's approved Kiro operations.

Pinned here: what an operation name may be; that ``kiroOperations`` is a staged,
explicit-only family (install consent approves what it shows, an update that adds a
name holds it, a dropped name needs nothing); that only a declared AND approved name
is forwarded, and a refusal sends nothing; that only the app's own token or the host
owner may call the route; that a signed-out or refused sign-in answers its own code;
that the endpoint comes from the profile's region and an unknown region is refused;
that input and reply sizes are bounded; and that the token reaches no answer, error,
log line or audit row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import execution, kiro_calls, kiro_operations
from kiro_crew.apps.context import AppContext
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    disable_app,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest import CORE_APP_ROUTE_SEGMENTS, AppManifest
from kiro_crew.apps.point_grants import explicit_only_grant_families, point_grant_families
from kiro_crew.apps.routes_kiro import register_kiro_call_routes
from kiro_crew.apps.rpc_cbor import cbor_decode, cbor_encode
from kiro_crew.auth.provider import NotAuthenticated
from kiro_crew.testing.synthetic_credentials import synth

APP = "widgets"
OPS = ["GetWidget", "ListWidgets"]
OWNER = "U_OWNER"
TOKEN = synth(48, seed=7)
RENEWED = synth(48, seed=8)
EU_ARN = "arn:aws:codewhisperer:eu-central-1:123456789012:profile/ABCDEF"


# --- what a name may be --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["", "getWidget", "Get-Widget", "Get Widget", "Get/Widget", "../X", "GetWidgét", "G" * 65, 3],
)
def test_a_value_that_is_not_an_operation_name_is_refused(name: Any) -> None:
    assert not kiro_operations.is_operation_name(name)
    assert kiro_operations.declared_operations([name]) == []


def test_names_validate_round_trip_and_limits() -> None:
    assert kiro_operations.kiro_operation_errors(OPS) == []
    assert kiro_operations.kiro_operation_errors([OPS[0], OPS[0]]) == [
        "permissions.kiroOperations repeats an entry"
    ]
    many = [f"Op{i}" for i in range(65)]
    assert any("exceeds the limit" in e for e in kiro_operations.kiro_operation_errors(many))
    manifest = AppManifest.from_dict(
        {
            "name": APP,
            "version": "1.0.0",
            "displayName": "W",
            "description": "d",
            "permissions": {"kiroOperations": OPS},
        }
    )
    assert manifest.validate() == []
    assert manifest.to_dict()["permissions"]["kiroOperations"] == OPS
    bad = AppManifest.from_dict(
        {"name": APP, "version": "1.0.0", "permissions": {"kiroOperations": "GetWidget"}}
    )
    assert "permissions.kiroOperations must be an array of strings" in bad.validate()
    assert "kiro" in CORE_APP_ROUTE_SEGMENTS


# --- the grant family -----------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda _name: None)
    yield home


def _source(tmp_path: Path, ops: list[str], version: str = "1.0.0") -> Path:
    source = tmp_path / f"source-{version}" / APP
    source.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Widgets",
        "description": "calls Kiro",
        "permissions": {"kiroOperations": ops},
    }
    (source / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return source


def _install(tmp_path: Path, ops: list[str]) -> None:
    assert install_app(_source(tmp_path, ops)).ok
    meta = _read_installed(APP)
    assert meta is not None
    meta.enabled = True
    _write_installed(APP, meta)


def test_kiro_operations_is_an_explicit_only_staged_family() -> None:
    assert kiro_operations.GRANT_FAMILY in point_grant_families()
    assert kiro_operations.GRANT_FAMILY in explicit_only_grant_families()


@pytest.mark.usefixtures("home")
def test_install_approves_the_shown_names_and_an_added_one_waits(tmp_path: Path) -> None:
    _install(tmp_path, OPS[:1])
    assert kiro_operations.app_kiro_operations(APP) == {OPS[0]}
    assert get_app(APP)["kiroOperationsGrant"] == {"approved": OPS[:1], "staged": []}

    assert update_app(_source(tmp_path, OPS, "1.1.0")).ok
    # The old name keeps working; the added one is held for the owner.
    assert kiro_operations.app_kiro_operations(APP) == {OPS[0]}
    assert get_app(APP)["kiroOperationsGrant"] == {"approved": OPS[:1], "staged": OPS[1:]}

    assert enable_app(APP, grants_consent={"kiroOperations": OPS[1:]}).ok
    assert kiro_operations.app_kiro_operations(APP) == set(OPS)


@pytest.mark.usefixtures("home")
def test_a_dropped_name_and_a_disable_end_the_grant_without_approval(tmp_path: Path) -> None:
    _install(tmp_path, OPS)
    assert kiro_operations.app_kiro_operations(APP) == set(OPS)
    assert update_app(_source(tmp_path, OPS[:1], "1.1.0")).ok
    assert kiro_operations.app_kiro_operations(APP) == {OPS[0]}
    assert get_app(APP)["kiroOperationsGrant"]["staged"] == []
    assert disable_app(APP).ok
    assert kiro_operations.app_kiro_operations(APP) == frozenset()


def test_an_untrusted_or_unknown_app_holds_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, OPS)
    monkeypatch.setattr(execution, "third_party_ceiling_closed", lambda _n: "untrusted")
    assert kiro_operations.app_kiro_operations(APP) == frozenset()
    assert kiro_operations.app_kiro_operations("not-installed") == frozenset()
    assert kiro_operations.app_kiro_operations("") == frozenset()


# --- the call path, against a fake BFF ---------------------------------------------------


class FakeBff:
    """Records each request; answers from ``replies`` in turn, the last one repeating."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.replies: list[tuple[int, Any, dict[str, str]]] = [
            (200, {"widget": {"id": "w-1", "blob": b"\x00\x01"}}, {})
        ]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.seen.append(
            {
                "path": request.path,
                "headers": dict(request.headers),
                "input": cbor_decode(body) if body else None,
            }
        )
        status, payload, extra = self.replies[min(len(self.seen), len(self.replies)) - 1]
        raw = payload if isinstance(payload, bytes) else cbor_encode(payload)
        headers = {"smithy-protocol": "rpc-v2-cbor", "content-type": "application/cbor", **extra}
        return web.Response(status=status, body=raw, headers=headers)


class FakeProvider:
    def __init__(self, *, arn: str | None = None, signed_in: bool = True) -> None:
        self.arn = arn
        self.signed_in = signed_in
        self.rejected: list[str | None] = []

    async def current(self, *, rejected_access_token: str | None = None) -> Any:
        self.rejected.append(rejected_access_token)
        if not self.signed_in:
            raise NotAuthenticated("no stored Kiro credential")
        access = RENEWED if rejected_access_token else TOKEN
        return SimpleNamespace(
            access_token=access,
            provider="Google",
            identity="social",
            profile_arn=self.arn,
            auth_method=None,
        )


@pytest_asyncio.fixture
async def bff(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[FakeBff, str]]:
    fake = FakeBff()
    app = web.Application()
    app.router.add_post("/service/{service}/operation/{op}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(kiro_calls, "resolve_proxy", lambda _url: None)
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


@pytest.fixture
def approved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kiro_calls, "app_kiro_operations", lambda app: frozenset(OPS) if app == APP else frozenset()
    )
    monkeypatch.setattr(
        kiro_calls,
        "declared_kiro_operations",
        lambda app: frozenset([*OPS, "DeleteWidget"]) if app == APP else frozenset(),
    )


def _upstream(origin: str, provider: FakeProvider, region: str = "us-east-1") -> Any:
    return kiro_calls.KiroSignIn(provider=lambda: provider, origins={region: origin})


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_an_approved_operation_returns_its_output_and_the_bff_saw_the_sign_in(
    bff: tuple[FakeBff, str],
) -> None:
    fake, origin = bff
    out = await kiro_calls.call_kiro_operation(
        APP, "GetWidget", {"id": "w-1"}, caller="hook", upstream=_upstream(origin, FakeProvider())
    )
    assert out == {"widget": {"id": "w-1", "blob": "AAE="}}
    [seen] = fake.seen
    assert seen["path"] == "/service/KiroWebBearerService/operation/GetWidget"
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert seen["headers"]["X-Kiro-Idp"] == "Google"
    assert seen["headers"]["smithy-protocol"] == "rpc-v2-cbor"
    assert seen["input"] == {"id": "w-1"}


@pytest.mark.usefixtures("approved")
@pytest.mark.parametrize(
    ("operation", "status", "code"),
    [("DeleteWidget", 403, "operation_not_approved"), ("Nope", 404, "unknown_operation")],
)
@pytest.mark.asyncio
async def test_an_undeclared_or_unapproved_name_sends_nothing(
    bff: tuple[FakeBff, str], operation: str, status: int, code: str
) -> None:
    fake, origin = bff
    provider = FakeProvider()
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, operation, {}, caller="hook", upstream=_upstream(origin, provider)
        )
    assert (err.value.status, err.value.code) == (status, code)
    assert fake.seen == [] and provider.rejected == []


@pytest.mark.asyncio
async def test_another_apps_approval_reaches_nothing(
    bff: tuple[FakeBff, str], approved: None
) -> None:
    fake, origin = bff
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            "other", "GetWidget", {}, caller="hook", upstream=_upstream(origin, FakeProvider())
        )
    assert err.value.code == "unknown_operation" and fake.seen == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_signed_out_answers_its_own_code_and_sends_nothing(
    bff: tuple[FakeBff, str],
) -> None:
    fake, origin = bff
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP,
            "GetWidget",
            {},
            caller="hook",
            upstream=_upstream(origin, FakeProvider(signed_in=False)),
        )
    assert (err.value.status, err.value.code) == (409, "kiro_sign_in_required")
    assert "Sign in to Kiro in Settings" in err.value.message
    assert fake.seen == []


class _Broken:
    async def current(self, **_kw: Any) -> Any:
        raise RuntimeError(f"refresh failed: {TOKEN}")


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_a_failed_refresh_names_only_its_type(bff: tuple[FakeBff, str]) -> None:
    fake, origin = bff
    upstream = kiro_calls.KiroSignIn(provider=_Broken, origins={"us-east-1": origin})
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(APP, "GetWidget", {}, caller="hook", upstream=upstream)
    assert (err.value.status, err.value.code) == (503, "kiro_sign_in_unavailable")
    assert TOKEN not in json.dumps(err.value.body()) and fake.seen == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_a_refused_token_is_renewed_once_then_reported(bff: tuple[FakeBff, str]) -> None:
    fake, origin = bff
    provider = FakeProvider()
    fake.replies = [(401, {"__type": "x#AccessDenied", "message": "no"}, {})]
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, provider)
        )
    assert (err.value.status, err.value.code) == (409, "kiro_sign_in_refused")
    assert provider.rejected == [None, TOKEN]
    assert [s["headers"]["Authorization"] for s in fake.seen] == [
        f"Bearer {TOKEN}",
        f"Bearer {RENEWED}",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_the_profile_region_picks_the_origin_and_an_unknown_one_is_refused(
    bff: tuple[FakeBff, str],
) -> None:
    fake, origin = bff
    eu = FakeProvider(arn=EU_ARN)
    await kiro_calls.call_kiro_operation(
        APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, eu, "eu-central-1")
    )
    assert fake.seen[-1]["headers"]["X-Kiro-Profile-Arn"] == EU_ARN
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, eu, "us-east-1")
        )
    assert (err.value.status, err.value.code) == (503, "kiro_region_unsupported")
    assert len(fake.seen) == 1
    assert set(kiro_calls.KIRO_ORIGINS) == {"us-east-1", "eu-central-1"}
    assert all(o.startswith("https://") for o in kiro_calls.KIRO_ORIGINS.values())


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_a_bff_error_passes_its_type_and_a_redacted_message(
    bff: tuple[FakeBff, str],
) -> None:
    fake, origin = bff
    fake.replies = [(409, {"__type": "a.b#ConflictException", "message": "taken"}, {})]
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, FakeProvider())
        )
    assert err.value.code == "kiro_operation_failed" and err.value.status == 502
    assert err.value.kiro == {"status": 409, "type": "ConflictException", "message": "taken"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_inputs_and_replies_are_bounded(
    bff: tuple[FakeBff, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, origin = bff
    upstream = _upstream(origin, FakeProvider())
    big = {"x": "a" * (kiro_calls.MAX_REQUEST_BYTES + 1)}
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(kiro_calls.MAX_INPUT_DEPTH + 1):
        node["n"] = {}
        node = node["n"]
    for payload, code in ((big, "body_too_large"), (deep, "body_too_large"), ([1], "invalid_json")):
        with pytest.raises(kiro_calls.KiroCallError) as err:
            await kiro_calls.call_kiro_operation(
                APP, "GetWidget", payload, caller="hook", upstream=upstream
            )
        assert err.value.code == code
    assert fake.seen == []

    monkeypatch.setattr(kiro_calls, "MAX_RESPONSE_BYTES", 16)
    fake.replies = [(200, {"blob": b"x" * 64}, {})]
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(APP, "GetWidget", {}, caller="hook", upstream=upstream)
    assert err.value.code == "kiro_bad_reply"


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_a_reply_that_is_not_rpc_v2_cbor_is_refused(bff: tuple[FakeBff, str]) -> None:
    fake, origin = bff
    fake.replies = [(200, {"ok": True}, {"smithy-protocol": "awsJson1_0"})]
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, FakeProvider())
        )
    assert err.value.code == "kiro_bad_reply"


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_more_than_the_in_flight_cap_is_refused(
    bff: tuple[FakeBff, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake, origin = bff
    monkeypatch.setattr(kiro_calls, "_in_flight", {APP: kiro_calls.MAX_IN_FLIGHT})
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(
            APP, "GetWidget", {}, caller="hook", upstream=_upstream(origin, FakeProvider())
        )
    assert (err.value.status, err.value.code) == (429, "too_many_calls")
    assert kiro_calls._in_flight == {APP: kiro_calls.MAX_IN_FLIGHT}


@pytest.mark.asyncio
@pytest.mark.usefixtures("approved")
async def test_the_token_reaches_no_answer_error_log_or_audit(
    bff: tuple[FakeBff, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake, origin = bff
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw))
    )
    upstream = _upstream(origin, FakeProvider())
    caplog.set_level(logging.DEBUG)
    fake.replies = [
        (200, {"echo": "ok"}, {}),
        (500, {"__type": "Boom", "message": f"leaked Bearer {TOKEN}"}, {}),
    ]
    out = await kiro_calls.call_kiro_operation(
        APP, "GetWidget", {}, caller="hook", upstream=upstream
    )
    with pytest.raises(kiro_calls.KiroCallError) as err:
        await kiro_calls.call_kiro_operation(APP, "GetWidget", {}, caller="hook", upstream=upstream)
    signed = await upstream.sign()
    assert TOKEN not in repr(signed)
    for text in (json.dumps(out), json.dumps(err.value.body()), caplog.text, json.dumps(rows)):
        assert TOKEN not in text
    assert [r["outcome"] for r in rows] == ["success", "error"]
    assert rows[0]["resources"].endswith("grant=kiroOperations:GetWidget")


# --- the route and ctx.kiro_call ---------------------------------------------------------


@web.middleware
async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
    kind = request.headers.get("X-Test-Caller", "")
    if kind == "app":
        request["app"] = request.headers["X-Test-App"]
        request["user"] = "app"
        request["auth_token"] = "signed-app-token"
    elif kind == "internal":
        # The internal secret with the app's cookie: a token is present, and the
        # transport alone must refuse it.
        request["internal_auth"] = True
        request["app"] = request.headers["X-Test-App"]
        request["auth_token"] = "signed-app-token"
    elif kind in ("owner", "teammate"):
        request["app"] = ""
        request["user"] = OWNER if kind == "owner" else "U_TEAMMATE"
        request["auth_token"] = "signed-dashboard-token"
    return await handler(request)


@pytest_asyncio.fixture
async def gateway(
    bff: tuple[FakeBff, str], approved: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[TestClient, FakeBff]]:
    fake, origin = bff
    monkeypatch.setattr(kiro_calls, "KIRO_UPSTREAM", _upstream(origin, FakeProvider()))
    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    register_kiro_call_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, fake
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"X-Test-Caller": "app", "X-Test-App": APP}, 200),
        ({"X-Test-Caller": "owner"}, 200),
        ({"X-Test-Caller": "app", "X-Test-App": "other"}, 403),
        ({"X-Test-Caller": "internal", "X-Test-App": APP}, 403),
        ({"X-Test-Caller": "teammate"}, 403),
        ({}, 403),
    ],
)
@pytest.mark.asyncio
async def test_only_the_apps_own_token_or_the_owner_may_call(
    gateway: tuple[TestClient, FakeBff], headers: dict[str, str], status: int
) -> None:
    client, fake = gateway
    resp = await client.post(f"/api/apps/{APP}/kiro/GetWidget", json={"id": "w"}, headers=headers)
    body = await resp.json()
    assert resp.status == status, body
    if status == 403:
        assert body["error"]["code"] == "kiro_caller_refused" and fake.seen == []
    else:
        assert body == {"widget": {"id": "w-1", "blob": "AAE="}}


@pytest.mark.asyncio
async def test_the_route_refuses_a_large_or_bad_body_before_sending(
    gateway: tuple[TestClient, FakeBff],
) -> None:
    client, fake = gateway
    headers = {"X-Test-Caller": "app", "X-Test-App": APP}
    big = b"{" + b" " * kiro_calls.MAX_REQUEST_BYTES + b"}"
    resp = await client.post(f"/api/apps/{APP}/kiro/GetWidget", data=big, headers=headers)
    assert resp.status == 413
    resp = await client.post(f"/api/apps/{APP}/kiro/GetWidget", data=b"[1]", headers=headers)
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_json"
    resp = await client.post(f"/api/apps/{APP}/kiro/Nope", json={}, headers=headers)
    assert resp.status == 404
    assert fake.seen == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("gateway")
async def test_ctx_kiro_call_runs_the_same_path_for_its_own_app() -> None:
    ctx = AppContext(name=APP, data_dir=Path("."))
    assert await ctx.kiro_call("GetWidget", {}) == {"widget": {"id": "w-1", "blob": "AAE="}}
    other = AppContext(name="other", data_dir=Path("."))
    with pytest.raises(kiro_calls.KiroCallError):
        await other.kiro_call("GetWidget", {})


@pytest.mark.asyncio
async def test_concurrent_calls_release_their_in_flight_slot(
    gateway: tuple[TestClient, FakeBff],
) -> None:
    client, _fake = gateway
    headers = {"X-Test-Caller": "app", "X-Test-App": APP}
    resps = await asyncio.gather(
        *(
            client.post(f"/api/apps/{APP}/kiro/GetWidget", json={}, headers=headers)
            for _ in range(3)
        )
    )
    assert [r.status for r in resps] == [200, 200, 200]
    assert kiro_calls._in_flight == {}
