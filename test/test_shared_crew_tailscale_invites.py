"""The Tailscale device-invite API client against a fake Tailscale API.

No test reaches the real API: every client is pointed at an in-process fake
that implements the device-invite endpoints Tailscale documents, checks basic
auth, and can be told to rate-limit. Covers device and key reads, email and
link invite creation (never an exit node), list, delete and resend, every
error code, 429 backoff, a body split across reads, the response size cap,
refused redirects, and that the key never appears in the client's repr.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.shared_crew import tailscale_api
from kiro_crew.shared_crew.tailscale_api import (
    TailscaleApi,
    TailscaleApiError,
    api_key_id,
    basic_auth,
    looks_like_api_key,
    parse_invite,
)

KEY = "tskey-api-kAbC123CNTRL-Zz9yYxXwWvV"
OTHER_KEY = "tskey-api-kOther1CNTRL-Qq1"
NODE_KEY = "nodekey:abc123"


class FakeTailscale:
    """The device-invite endpoints, as Tailscale's OpenAPI schema describes them."""

    def __init__(self) -> None:
        self.invites: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.rate_limit = 0
        self.next_id = 100
        self.base = ""
        self.devices = [
            {
                "id": "111",
                "nodeId": "nOther",
                "nodeKey": "nodekey:zzz",
                "addresses": ["100.64.0.9"],
            },
            {"id": "222", "nodeId": "nSelf", "nodeKey": NODE_KEY, "addresses": ["100.64.0.1"]},
        ]

    def accept(self, invite_id: str, user_id: str, login: str, pic: str = "") -> None:
        self.invites[invite_id]["accepted"] = True
        self.invites[invite_id]["acceptedBy"] = {
            "id": user_id,
            "loginName": login,
            "profilePicUrl": pic,
        }

    def app(self) -> web.Application:
        app = web.Application()

        @web.middleware
        async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
            body = await request.text()
            self.requests.append((request.method, request.path, json.loads(body) if body else None))
            if request.headers.get("Authorization") != basic_auth(KEY):
                return web.json_response({"message": "unauthorized"}, status=401)
            if self.rate_limit:
                self.rate_limit -= 1
                return web.json_response(
                    {"message": "slow down"}, status=429, headers={"Retry-After": "42"}
                )
            return await handler(request)

        app.middlewares.append(auth)

        async def devices(_r: web.Request) -> web.Response:
            return web.json_response({"devices": self.devices})

        async def key(r: web.Request) -> web.Response:
            return web.json_response({"id": r.match_info["kid"], "expires": "2099-01-01T00:00:00Z"})

        async def create(r: web.Request) -> web.Response:
            if r.match_info["dev"] != "nSelf":
                return web.json_response({}, status=404)
            out = []
            for entry in await r.json():
                self.next_id += 1
                row = {
                    "id": str(self.next_id),
                    "deviceId": 222,
                    "inviteUrl": f"https://login.tailscale.com/admin/invite/code{self.next_id}",
                    "multiUse": entry.get("multiUse", False),
                    "allowExitNode": entry.get("allowExitNode"),
                    "accepted": False,
                }
                if entry.get("email"):
                    row["email"] = entry["email"]
                self.invites[row["id"]] = row
                out.append(row)
            return web.json_response(out)

        async def list_(r: web.Request) -> web.Response:
            return web.json_response(list(self.invites.values()))

        async def delete(r: web.Request) -> web.Response:
            if self.invites.pop(r.match_info["iid"], None) is None:
                return web.json_response({}, status=404)
            return web.Response(status=200)

        async def resend(r: web.Request) -> web.Response:
            return web.Response(status=200)

        app.router.add_get("/api/v2/tailnet/-/devices", devices)
        app.router.add_get("/api/v2/tailnet/-/keys/{kid}", key)
        app.router.add_post("/api/v2/device/{dev}/device-invites", create)
        app.router.add_get("/api/v2/device/{dev}/device-invites", list_)
        app.router.add_delete("/api/v2/device-invites/{iid}", delete)
        app.router.add_post("/api/v2/device-invites/{iid}/resend", resend)
        return app

    def client(self, key: str = KEY) -> TailscaleApi:
        return TailscaleApi(key, base_url=self.base)


def _serve_in_thread(app: web.Application) -> tuple[str, Any]:
    """Run *app* on a loopback port in its own thread and loop; returns (url, stop)."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def run() -> None:
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["port"] = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        holder["runner"] = runner
        ready.set()
        loop.run_forever()
        loop.run_until_complete(runner.cleanup())
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(10)

    def stop() -> None:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)

    return f"http://127.0.0.1:{holder['port']}", stop


@pytest.fixture
def fake():
    api = FakeTailscale()
    url, stop = _serve_in_thread(api.app())
    api.base = url + "/api/v2"
    yield api
    stop()


# ── client ──


def test_api_key_id_parses_only_api_keys() -> None:
    assert api_key_id(KEY) == "kAbC123CNTRL"
    assert api_key_id("tskey-auth-abc-def") == ""
    assert api_key_id("not a key") == ""
    assert looks_like_api_key(KEY) and not looks_like_api_key("tskey-auth-abc-def")
    assert "Zz9" not in repr(TailscaleApi(KEY))


@pytest.mark.asyncio
async def test_wrong_key_is_auth_failed(fake: FakeTailscale) -> None:
    with pytest.raises(TailscaleApiError) as err:
        await fake.client(OTHER_KEY).list_devices()
    assert err.value.code == "auth_failed"


class _SplitStream:
    """A body that arrives in pieces: each ``read(n)`` returns at most one piece."""

    def __init__(self, pieces: list[bytes]) -> None:
        self._pieces = list(pieces)

    async def read(self, n: int = -1) -> bytes:
        if not self._pieces:
            return b""
        piece = self._pieces.pop(0)
        if 0 <= n < len(piece):
            self._pieces.insert(0, piece[n:])
            piece = piece[:n]
        return piece

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        while True:
            piece = await self.read(n)
            if not piece:
                return
            yield piece


class _SplitSession:
    def __init__(self, pieces: list[bytes]) -> None:
        self._pieces = pieces

    async def __aenter__(self) -> "_SplitSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def request(self, *_a: Any, **_kw: Any) -> Any:
        pieces = self._pieces

        class _Resp:
            status = 200
            headers: dict[str, str] = {}
            content = _SplitStream(pieces)

            async def __aenter__(self) -> "_Resp":
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Resp()


@pytest.mark.asyncio
async def test_a_body_split_across_reads_is_read_to_the_end() -> None:
    body = json.dumps([{"id": "1", "accepted": False, "deviceId": 2}]).encode()
    split = [body[:7], body[7:20], body[20:]]
    api = TailscaleApi(KEY, session_factory=lambda: _SplitSession(split))  # type: ignore[arg-type,return-value]
    listed = await api.list_invites("nSelf")
    assert [i.id for i in listed] == ["1"]


@pytest.mark.asyncio
async def test_a_split_body_over_the_cap_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(tailscale_api, "MAX_RESPONSE_BYTES", 16)
    pieces = [b"[" + b" " * 10, b" " * 10, b"]"]
    api = TailscaleApi(KEY, session_factory=lambda: _SplitSession(pieces))  # type: ignore[arg-type,return-value]
    with pytest.raises(TailscaleApiError) as err:
        await api.list_invites("nSelf")
    assert err.value.code == "bad_response"


@pytest.mark.asyncio
async def test_redirect_is_refused_and_never_followed() -> None:
    app = web.Application()
    hits: list[str] = []

    async def bounce(_r: web.Request) -> web.Response:
        raise web.HTTPTemporaryRedirect("/elsewhere")

    async def elsewhere(r: web.Request) -> web.Response:
        hits.append(r.headers.get("Authorization", ""))
        return web.json_response({"devices": []})

    app.router.add_get("/api/v2/tailnet/-/devices", bounce)
    app.router.add_get("/elsewhere", elsewhere)
    async with TestServer(app) as server:
        api = TailscaleApi(KEY, base_url=str(server.make_url("/api/v2")))
        with pytest.raises(TailscaleApiError) as err:
            await api.list_devices()
    assert err.value.code == "bad_response"
    assert hits == []


@pytest.mark.asyncio
async def test_devices_and_key_expiry_are_read(fake: FakeTailscale) -> None:
    devices = await fake.client().list_devices()
    assert [(d.node_id, d.legacy_id, d.node_key) for d in devices] == [
        ("nOther", "111", "nodekey:zzz"),
        ("nSelf", "222", NODE_KEY),
    ]
    assert devices[1].addresses == ("100.64.0.1",)
    assert await fake.client().key_expiry(api_key_id(KEY)) == "2099-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_email_invites_never_allow_exit_node(fake: FakeTailscale) -> None:
    made = await fake.client().create_invites(
        "nSelf", ["a@example.com", "b@example.com"], multi_use=True
    )
    posted = [b for m, p, b in fake.requests if m == "POST" and p.endswith("/device-invites")][0]
    assert posted == [
        {"email": "a@example.com", "multiUse": False, "allowExitNode": False},
        {"email": "b@example.com", "multiUse": False, "allowExitNode": False},
    ]
    assert [i.email for i in made] == ["a@example.com", "b@example.com"]


@pytest.mark.asyncio
async def test_link_invite_single_use_by_default_and_reusable_on_request(
    fake: FakeTailscale,
) -> None:
    single = (await fake.client().create_invites("nSelf", []))[0]
    multi = (await fake.client().create_invites("nSelf", [], multi_use=True))[0]
    bodies = [b for m, p, b in fake.requests if m == "POST" and p.endswith("/device-invites")]
    assert bodies == [
        [{"multiUse": False, "allowExitNode": False}],
        [{"multiUse": True, "allowExitNode": False}],
    ]
    assert single.invite_url.startswith("https://login.tailscale.com/")
    assert single.multi_use is False and multi.multi_use is True


@pytest.mark.asyncio
async def test_list_reports_who_accepted(fake: FakeTailscale) -> None:
    made = (await fake.client().create_invites("nSelf", ["a@example.com"]))[0]
    fake.accept(made.id, "u1", " ann@example.com ", "https://pic.example/a.png")
    listed = await fake.client().list_invites("nSelf")
    assert len(listed) == 1 and listed[0].accepted is True
    assert listed[0].accepted_by == tailscale_api.AcceptedBy(
        "u1", "ann@example.com", "https://pic.example/a.png"
    )


@pytest.mark.asyncio
async def test_delete_and_resend(fake: FakeTailscale) -> None:
    made = (await fake.client().create_invites("nSelf", ["a@example.com"]))[0]
    await fake.client().resend_invite(made.id)
    await fake.client().delete_invite(made.id)
    assert fake.invites == {}
    with pytest.raises(TailscaleApiError) as err:
        await fake.client().delete_invite(made.id)
    assert err.value.code == "not_found" and err.value.status == 404


@pytest.mark.asyncio
async def test_rate_limit_carries_retry_after(fake: FakeTailscale) -> None:
    fake.rate_limit = 1
    with pytest.raises(TailscaleApiError) as err:
        await fake.client().list_invites("nSelf")
    assert err.value.code == "rate_limited" and err.value.retry_after == 42.0
    assert await fake.client().list_invites("nSelf") == []


@pytest.mark.asyncio
async def test_an_unsafe_id_is_refused_before_any_call(fake: FakeTailscale) -> None:
    for bad in ("", "../keys", "a/b", "x" * 129):
        with pytest.raises(TailscaleApiError) as err:
            await fake.client().delete_invite(bad)
        assert err.value.code == "bad_request"
    assert fake.requests == []


def _status_app(status: int, body: bytes = b"", headers: dict[str, str] | None = None):
    app = web.Application()

    async def handler(_r: web.Request) -> web.Response:
        return web.Response(status=status, body=body, headers=headers or {})

    app.router.add_route("*", "/api/v2/{tail:.*}", handler)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retry_after"),
    [
        (403, "forbidden", 0.0),
        (400, "bad_request", 0.0),
        (429, "rate_limited", 60.0),
        (503, "unavailable", 7.0),
    ],
)
async def test_status_codes_map_to_error_codes(status: int, code: str, retry_after: float) -> None:
    headers = {"Retry-After": "7"} if status == 503 else None
    async with TestServer(_status_app(status, headers=headers)) as server:
        api = TailscaleApi(KEY, base_url=str(server.make_url("/api/v2")))
        with pytest.raises(TailscaleApiError) as err:
            await api.list_devices()
    assert (err.value.code, err.value.status, err.value.retry_after) == (code, status, retry_after)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b'{"devices": "nope"}', b"[]"])
async def test_a_malformed_body_is_bad_response(body: bytes) -> None:
    async with TestServer(_status_app(200, body)) as server:
        api = TailscaleApi(KEY, base_url=str(server.make_url("/api/v2")))
        with pytest.raises(TailscaleApiError) as err:
            await api.list_devices()
    assert err.value.code == "bad_response"


@pytest.mark.asyncio
async def test_an_empty_body_is_none_and_a_bad_list_is_refused() -> None:
    async with TestServer(_status_app(200)) as server:
        api = TailscaleApi(KEY, base_url=str(server.make_url("/api/v2")))
        assert await api.key_expiry("k1") == ""
        await api.delete_invite("1")
        with pytest.raises(TailscaleApiError):
            await api.list_invites("nSelf")
        with pytest.raises(TailscaleApiError):
            await api.create_invites("nSelf", [])


@pytest.mark.asyncio
async def test_an_unreachable_server_is_unavailable() -> None:
    async with TestServer(web.Application()) as server:
        base = str(server.make_url("/api/v2"))
    with pytest.raises(TailscaleApiError) as err:
        await TailscaleApi(KEY, base_url=base).list_devices()
    assert err.value.code == "unavailable"


def test_parse_invite_refuses_malformed_rows() -> None:
    for raw in (None, [], {"id": ""}, {"id": "../x"}, {"id": True}):
        with pytest.raises(TailscaleApiError):
            parse_invite(raw)
    row = parse_invite({"id": 5, "acceptedBy": {"id": "u", "loginName": ""}, "multiUse": "yes"})
    assert row.id == "5" and row.accepted_by is None and row.multi_use is False


def test_device_rows_without_an_id_are_skipped() -> None:
    assert tailscale_api._parse_device("x") is None
    assert tailscale_api._parse_device({"hostname": "h"}) is None
    dev = tailscale_api._parse_device({"id": 9, "addresses": "100.64.0.1"})
    assert dev is not None and dev.node_id == "9" and dev.addresses == ()


def test_retry_after_is_bounded() -> None:
    assert tailscale_api._retry_after(None) == 0.0
    assert tailscale_api._retry_after("soon") == 0.0
    assert tailscale_api._retry_after("-5") == 0.0
    assert tailscale_api._retry_after("99999") == 3600.0
