"""In-Crew Tailscale invites against a fake Tailscale API.

No test reaches the real API: every client is pointed at an in-process fake
that implements the device-invite endpoints Tailscale documents, checks basic
auth, and can be told to rate-limit. Covers connect (device found by node key,
then tailnet address), email and link invites, accept -> allowlist and alias by
stable user id (including an accept from a different account than the invited
email), revoke (Tailscale delete, allowlist removal, live refusal, socket close),
auth errors, 429 backoff, the owner gate, the no-key state, avatars, and that
the key never appears in a response, the state file, the config or the log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import tailnet, tailnet_live_allowlist
from kiro_crew.shared_crew import access_set, routes_invites, tailscale_allowlist
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew import tailscale_invites as invites
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Provenance, Transport
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.tailscale_api import (
    TailscaleApi,
    TailscaleApiError,
    api_key_id,
    basic_auth,
)


@pytest.fixture(autouse=True)
def _shared_crew_preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover the preview's behaviour; the gate itself has its own tests."""
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


KEY = "tskey-api-kAbC123CNTRL-Zz9yYxXwWvV"
OTHER_KEY = "tskey-api-kOther1CNTRL-Qq1"
NODE_KEY = "nodekey:abc123"
OWNER_LOGIN = "owner@example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeTailscale:
    """The device-invite endpoints, as Tailscale's OpenAPI schema describes them."""

    def __init__(self) -> None:
        self.invites: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.rate_limit = 0
        self.next_id = 100
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
def fake(monkeypatch: pytest.MonkeyPatch):
    api = FakeTailscale()
    url, stop = _serve_in_thread(api.app())
    base = url + "/api/v2"
    monkeypatch.setattr(invites, "api_factory", lambda key: TailscaleApi(key, base_url=base))
    monkeypatch.setattr(
        invites,
        "daemon_self_reader",
        lambda: invites.DaemonSelf(NODE_KEY, ("100.64.0.1",), "example.ts.net", OWNER_LOGIN),
    )
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    yield api
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    stop()


@pytest.fixture
def config_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dashboard": {"tailscale": {"allowed_logins": []}}}))
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(loader, "config_path", lambda: path)
    return path


def _logins(path) -> list[str]:
    return json.loads(path.read_text())["dashboard"]["tailscale"]["allowed_logins"]


def _active_members() -> dict[str, Any]:
    """Members still admitted; a revoked one stays on file, marked removed."""
    return {uid: m for uid, m in store.load_state()["members"].items() if not m.get("removed")}


# ── client ──


def test_api_key_id_parses_only_api_keys() -> None:
    assert api_key_id(KEY) == "kAbC123CNTRL"
    assert api_key_id("tskey-auth-abc-def") == ""
    assert api_key_id("not a key") == ""
    assert "Zz9" not in repr(TailscaleApi(KEY))


@pytest.mark.asyncio
async def test_wrong_key_is_auth_failed(fake: FakeTailscale) -> None:
    with pytest.raises(TailscaleApiError) as err:
        await invites.api_factory(OTHER_KEY).list_devices()
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
    from kiro_crew.shared_crew import tailscale_api

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


# ── connect ──


@pytest.mark.asyncio
async def test_connect_finds_device_by_node_key(fake: FakeTailscale) -> None:
    view = await invites.connect(KEY, "owner")
    assert view["connected"] is True
    assert view["tailnet"] == "example.ts.net"
    assert view["key_expires"] == "2099-01-01T00:00:00Z"
    assert store.load_state()["device_id"] == "nSelf"


@pytest.mark.asyncio
async def test_connect_falls_back_to_tailnet_address(fake: FakeTailscale, monkeypatch) -> None:
    fake.devices[1]["nodeKey"] = "nodekey:rotated"
    await invites.connect(KEY, "owner")
    assert store.load_state()["device_id"] == "nSelf"


@pytest.mark.asyncio
async def test_connect_never_matches_by_name(fake: FakeTailscale) -> None:
    fake.devices = [
        {
            "id": "1",
            "nodeId": "nX",
            "nodeKey": "nodekey:q",
            "addresses": ["100.1.1.1"],
            "hostname": "example",
        }
    ]
    with pytest.raises(invites.InviteError) as err:
        await invites.connect(KEY, "owner")
    assert err.value.code == "device_not_found"
    assert store.credential_present() is False


@pytest.mark.asyncio
async def test_connect_rejects_non_api_key(fake: FakeTailscale) -> None:
    with pytest.raises(invites.InviteError) as err:
        await invites.connect("tskey-client-abc-def", "owner")
    assert err.value.code == "invalid_key_format"


@pytest.mark.asyncio
async def test_key_never_leaves_the_vault(fake: FakeTailscale, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    view = await invites.connect(KEY, "owner")
    assert KEY not in json.dumps(view)
    state_text = (store.root() / store.STATE_FILE).read_text()
    assert KEY not in state_text and "Zz9yYxXwWvV" not in state_text
    for path in store.root().rglob("*"):
        if path.is_file():
            assert b"Zz9yYxXwWvV" not in path.read_bytes(), path
    assert "Zz9yYxXwWvV" not in caplog.text
    assert store.load_credential() == KEY


def test_vault_root_is_masked_from_sandbox_and_tools() -> None:
    from kiro_crew.sandbox_leaves import _RELOCATED_CREW_HIDDEN_LEAVES
    from kiro_crew.security import is_sensitive_path

    root = store.root()
    assert root.parent.name == ".vault"
    assert ".vault" in _RELOCATED_CREW_HIDDEN_LEAVES
    assert is_sensitive_path(str(root / ".vault" / "secrets.enc"))
    assert is_sensitive_path(str(root / store.STATE_FILE))


# ── invite, accept ──


@pytest.mark.asyncio
async def test_email_invites_never_allow_exit_node(fake: FakeTailscale) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com", "b@example.com"], multi_use=True, alias="Ann", avatar_id="", actor="owner"
    )
    posted = [b for m, p, b in fake.requests if m == "POST" and p.endswith("/device-invites")][0]
    assert posted == [
        {"email": "a@example.com", "multiUse": False, "allowExitNode": False},
        {"email": "b@example.com", "multiUse": False, "allowExitNode": False},
    ]
    assert [m["kind"] for m in made] == ["email", "email"]
    assert all("invite_url" not in m for m in made)


@pytest.mark.asyncio
async def test_link_invite_single_use_by_default_and_reusable_on_request(
    fake: FakeTailscale,
) -> None:
    await invites.connect(KEY, "owner")
    single = (await invites.create_invites([], multi_use=False, alias="", avatar_id="", actor="o"))[
        0
    ]
    multi = (await invites.create_invites([], multi_use=True, alias="", avatar_id="", actor="o"))[0]
    bodies = [b for m, p, b in fake.requests if m == "POST" and p.endswith("/device-invites")]
    assert bodies == [
        [{"multiUse": False, "allowExitNode": False}],
        [{"multiUse": True, "allowExitNode": False}],
    ]
    assert single["kind"] == "link" and single["invite_url"].startswith(
        "https://login.tailscale.com/"
    )
    assert multi["multi_use"] is True


@pytest.mark.asyncio
async def test_accept_adds_login_to_allowlist_and_binds_alias_by_user_id(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    avatar = store.save_avatar(PNG)
    made = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="Ann", avatar_id=avatar, actor="o"
    )
    assert tailnet.login_allowed("ann@example.com", ()) is False
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "https://pic/a.png")
    result = await invites.sync(force=True)
    assert result["admitted"] == ["ann@example.com"]
    assert result["restart_required"] is True
    member = store.load_state()["members"]["u-42"]
    assert (member["login"], member["alias"], member["avatar_id"]) == (
        "ann@example.com",
        "Ann",
        avatar,
    )
    assert _logins(config_file) == [OWNER_LOGIN, "ann@example.com"]
    cfg = json.loads(config_file.read_text())["dashboard"]["tailscale"]
    assert cfg["trust_identity"] is True and cfg["enabled"] is True
    assert tailnet.login_allowed("ann@example.com", (OWNER_LOGIN,)) is True
    view = await invites.status_view()
    assert view["invites"][0]["status"] == "accepted"
    assert view["invites"][0]["accepted_login"] == "ann@example.com"


@pytest.mark.asyncio
async def test_a_removed_person_stays_out_after_their_login_changes(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)

    def remove(s: dict[str, Any]) -> None:
        s["members"]["u-42"]["removed"] = True

    store.update_state(remove)
    fake.accept(made[0]["id"], "u-42", "ann.renamed@example.com", "")
    result = await invites.sync(force=True)
    assert result["admitted"] == []
    assert store.load_state()["members"]["u-42"]["removed"] is True
    assert tailnet.login_allowed("ann.renamed@example.com", (OWNER_LOGIN,)) is False


@pytest.mark.asyncio
async def test_an_older_accepted_invite_never_brings_a_removed_person_back(
    fake, config_file
) -> None:
    await invites.connect(KEY, "owner")

    def remove(s: dict[str, Any]) -> None:
        s["members"]["u-42"]["removed"] = True

    first = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(first[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    store.update_state(remove)
    second = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(second[0]["id"], "u-42", "ann@example.com", "")
    assert (await invites.sync(force=True))["admitted"] == ["ann@example.com"]
    store.update_state(remove)
    # Both accepted invites are still listed at Tailscale.
    assert (await invites.sync(force=True))["admitted"] == []
    assert store.load_state()["members"]["u-42"]["removed"] is True


@pytest.mark.asyncio
async def test_a_second_accept_by_a_member_is_recorded_and_refused_after_removal(
    fake, config_file
) -> None:
    """A member who accepts a second invite under the same login cannot use it after removal."""
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@example.com", "ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    fake.accept(made[1]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    member = store.load_state()["members"]["u-42"]
    assert set(member["invite_ids"]) == {made[0]["id"], made[1]["id"]}
    store.update_state(lambda s: s["members"]["u-42"].update(removed=True))
    assert (await invites.sync(force=True))["admitted"] == []
    assert store.load_state()["members"]["u-42"]["removed"] is True


@pytest.mark.asyncio
async def test_revoking_a_members_second_accepted_invite_removes_them(
    fake, config_file, monkeypatch
) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@example.com", "ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    fake.accept(made[1]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    assert store.load_state()["invites"][made[1]["id"]]["accepted_user_id"] == "u-42"
    monkeypatch.setattr(invites.access_set, "drop_from_seed", lambda _login: None)
    result = await invites.revoke(made[1]["id"], "owner")
    assert result["member_removed"] is True
    assert "u-42" not in _active_members()
    # Their other accepted invite, still listed at Tailscale, does not bring them back.
    assert (await invites.sync(force=True))["admitted"] == []
    assert "u-42" not in _active_members()


@pytest.mark.asyncio
async def test_a_rename_keeps_the_old_login_reachable_for_revoke(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    fake.accept(made[0]["id"], "u-42", "ann.renamed@example.com", "")
    await invites.sync(force=True)
    assert tailnet_live_allowlist.logins_for("u-42") == {
        "ann@example.com",
        "ann.renamed@example.com",
    }
    await invites.revoke(made[0]["id"], "owner")
    assert tailnet_live_allowlist.decision("ann@example.com") is False
    assert tailnet_live_allowlist.decision("ann.renamed@example.com") is False


@pytest.mark.asyncio
async def test_a_stale_sync_keeps_an_invite_created_after_its_fetch(fake, config_file) -> None:
    from kiro_crew.shared_crew.tailscale_api import parse_invite

    await invites.connect(KEY, "owner")
    old = await invites.create_invites(
        ["old@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    known = invites._known_invite_ids(store.load_state())
    # The list was fetched while old was already gone (expired) at Tailscale.
    stale = [parse_invite(row) for iid, row in fake.invites.items() if iid != old[0]["id"]]
    new = await invites.create_invites(
        ["new@example.com"], multi_use=False, alias="New", avatar_id="", actor="o"
    )
    async with invites._commit_lock():
        await invites._commit_accepts(stale, known, "crew")
    rows = store.load_state()["invites"]
    assert new[0]["id"] in rows and rows[new[0]["id"]]["alias"] == "New"
    assert old[0]["id"] not in rows


@pytest.mark.asyncio
async def test_a_stale_sync_does_not_restore_a_revoked_console_invite(fake, config_file) -> None:
    from kiro_crew.shared_crew.tailscale_api import parse_invite

    await invites.connect(KEY, "owner")
    fake.invites["c1"] = {"id": "c1", "deviceId": 222, "multiUse": False, "accepted": False}
    fake.accept("c1", "u-9", "cy@example.com")
    await invites.sync(force=True)
    assert "u-9" in store.load_state()["members"]
    # A sync that fetched the list before the revoke commits after it.
    stale = [parse_invite(fake.invites["c1"])]
    known = invites._known_invite_ids(store.load_state())
    await invites.revoke("c1", "owner")
    async with invites._commit_lock():
        result = await invites._commit_accepts(stale, known, "crew")
    assert result["admitted"] == []
    assert "u-9" not in _active_members()


@pytest.mark.asyncio
async def test_a_reusable_link_revoked_during_a_sync_admits_nobody_new(fake, config_file) -> None:
    # B is already a member by link I. A sync fetches I as accepted by C, then
    # the owner revokes I before the sync commits. B's removed row still names I,
    # so only the durable revoked set keeps C out.
    from kiro_crew.shared_crew.tailscale_api import parse_invite

    await invites.connect(KEY, "owner")
    made = await invites.create_invites([], multi_use=True, alias="", avatar_id="", actor="o")
    link = made[0]["id"]
    fake.accept(link, "u-b", "b@example.com")
    await invites.sync(force=True)
    assert "u-b" in _active_members()
    fake.accept(link, "u-c", "c@example.com")
    stale = [parse_invite(fake.invites[link])]
    known = invites._known_invite_ids(store.load_state())
    await invites.revoke(link, "owner")
    async with invites._commit_lock():
        result = await invites._commit_accepts(stale, known, "crew")
    assert result["admitted"] == []
    assert "u-c" not in _active_members()
    assert "c@example.com" not in _logins(config_file)
    assert link in store.load_state()["revoked_invite_ids"]


@pytest.mark.asyncio
async def test_accept_from_another_account_uses_the_accepting_login(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@work.example"], multi_use=False, alias="Ann", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-7", "ann.personal@gmail.example")
    await invites.sync(force=True)
    assert "ann.personal@gmail.example" in _logins(config_file)
    assert "ann@work.example" not in _logins(config_file)
    assert tailnet.login_allowed("ann@work.example", (OWNER_LOGIN,)) is False
    assert invites.members_by_login()["ann.personal@gmail.example"]["alias"] == "Ann"


@pytest.mark.asyncio
async def test_sync_is_idempotent(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    await invites.sync(force=True)
    again = await invites.sync(force=True)
    assert again["admitted"] == []
    assert _logins(config_file).count("a@example.com") == 1


@pytest.mark.asyncio
async def test_a_reusable_link_keeps_syncing_after_its_first_accept(
    fake, config_file, not_restricted, monkeypatch
) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites([], multi_use=True, alias="", avatar_id="", actor="o")
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    await invites.sync(force=True)
    assert invites.pending_count() == 1
    fake.accept(made[0]["id"], "u-2", "b@example.com")
    monkeypatch.setattr(invites, "SYNC_MIN_INTERVAL_SECS", 0.0)
    async with TestClient(TestServer(_route_app(local_operator_principal("local-app")))) as client:
        assert (await client.get(routes_invites.STATUS_PATH)).status == 200
    assert "b@example.com" in _logins(config_file)


@pytest.mark.asyncio
async def test_a_failed_membership_write_grants_no_live_access(
    fake, config_file, monkeypatch
) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")

    def _full(_mutate: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(store, "update_state", _full)
        with pytest.raises(OSError):
            await invites.sync(force=True)
    assert tailnet_live_allowlist.granted("a@example.com") is False
    # The config mirror is written only after the membership row: a seed a
    # later restart would read never holds a person revoke cannot find.
    assert "a@example.com" not in _logins(config_file)


@pytest.mark.asyncio
async def test_a_failed_mirror_write_is_retried_and_revoke_still_withdraws(
    fake, config_file, monkeypatch
) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    real_writer = invites.allowlist_writer

    async def _full(login: str, add: bool) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(invites, "allowlist_writer", _full)
    await invites.sync(force=True)
    assert "a@example.com" not in _logins(config_file)
    assert store.load_state()["members"]["u-1"]["mirror_pending"] is True
    monkeypatch.setattr(invites, "allowlist_writer", real_writer)
    await invites.sync(force=True)
    assert "a@example.com" in _logins(config_file)
    assert "mirror_pending" not in store.load_state()["members"]["u-1"]
    await invites.revoke(made[0]["id"], "owner")
    assert "a@example.com" not in _logins(config_file)


@pytest.mark.asyncio
async def test_revoking_a_reusable_link_removes_everyone_it_admitted(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites([], multi_use=True, alias="", avatar_id="", actor="o")
    for uid, login in (("u-1", "a@example.com"), ("u-2", "b@example.com")):
        fake.accept(made[0]["id"], uid, login)
        await invites.sync(force=True)
    assert {"a@example.com", "b@example.com"} <= set(_logins(config_file))
    result = await invites.revoke(made[0]["id"], "owner")
    assert result["member_removed"] is True
    assert not {"a@example.com", "b@example.com"} & set(_logins(config_file))
    assert _active_members() == {}


@pytest.mark.asyncio
async def test_rate_limit_backs_off_with_retry_after(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    fake.rate_limit = 1
    with pytest.raises(invites.InviteError) as err:
        await invites.sync()
    assert err.value.code == "rate_limited" and err.value.retry_after >= 42
    calls = len(fake.requests)
    with pytest.raises(invites.InviteError) as err:
        await invites.sync()
    assert err.value.code == "backing_off"
    assert len(fake.requests) == calls, "a backed-off sync must not call the API"


@pytest.mark.asyncio
async def test_poller_waits_longer_after_failure(fake, monkeypatch) -> None:
    await invites.connect(KEY, "owner")
    await invites.create_invites([], multi_use=False, alias="", avatar_id="", actor="o")
    fake.rate_limit = 5
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 3:
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        await invites.poll_forever(sleep)
    assert delays[0] == invites.POLL_BASE_SECS
    assert delays[1] >= 42


@pytest.mark.asyncio
async def test_expired_invite_disappears_but_accepted_stays(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    a, b = await invites.create_invites(
        ["a@x.example", "b@x.example"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(a["id"], "u-a", "a@x.example")
    fake.invites.pop(b["id"])
    await invites.sync(force=True)
    ids = {r["id"] for r in (await invites.status_view())["invites"]}
    assert ids == {a["id"]}


# ── revoke ──


@pytest.mark.asyncio
async def test_revoke_deletes_share_and_removes_allowlist(fake, config_file, monkeypatch) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="A", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    await invites.sync(force=True)
    dropped: list[str] = []
    monkeypatch.setattr(invites.access_set, "drop_from_seed", dropped.append)
    result = await invites.revoke(made[0]["id"], "owner")
    assert result == {"revoked": made[0]["id"], "member_removed": True}
    assert made[0]["id"] not in fake.invites
    assert dropped == ["a@example.com"]
    assert _logins(config_file) == [OWNER_LOGIN]
    assert tailnet.login_allowed("a@example.com", (OWNER_LOGIN, "a@example.com")) is False
    assert _active_members() == {}


@pytest.mark.asyncio
async def test_revoke_during_the_list_read_is_not_undone_by_sync(
    fake, config_file, monkeypatch
) -> None:
    """A revoke that lands after sync read the list wins: the stale list admits nobody."""
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    real_factory = invites.api_factory

    def factory(key: str) -> TailscaleApi:
        api = real_factory(key)
        real_list = api.list_invites

        async def list_then_revoke(device_id: str) -> Any:
            listed = await real_list(device_id)
            await invites.revoke(made[0]["id"], "owner")
            return listed

        api.list_invites = list_then_revoke  # type: ignore[method-assign]
        return api

    monkeypatch.setattr(invites, "api_factory", factory)
    result = await invites.sync(force=True)
    assert result["admitted"] == []
    assert _active_members() == {}
    assert "a@example.com" not in _logins(config_file)
    assert tailnet.login_allowed("a@example.com", (OWNER_LOGIN,)) is False


@pytest.mark.asyncio
async def test_revoke_while_sync_admits_waits_and_then_removes(
    fake, config_file, monkeypatch
) -> None:
    """A revoke started while sync is admitting runs after the admit, so it removes them."""
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-1", "a@example.com")
    real_writer = invites.allowlist_writer
    real_locked = invites._revoke_locked
    revoking: list[asyncio.Task[Any]] = []
    order: list[str] = []

    async def revoke_locked(*args: Any, **kwargs: Any) -> Any:
        order.append("revoke")
        return await real_locked(*args, **kwargs)

    async def writer(login: str, add: bool) -> Any:
        if add and not revoking:
            revoking.append(asyncio.ensure_future(invites.revoke(made[0]["id"], "owner")))
        return await real_writer(login, add)

    monkeypatch.setattr(invites, "_revoke_locked", revoke_locked)
    monkeypatch.setattr(invites, "allowlist_writer", writer)
    await invites.sync(force=True)
    order.append("sync committed")
    await revoking[0]
    assert order == ["sync committed", "revoke"]
    assert _active_members() == {}
    assert made[0]["id"] not in fake.invites
    assert tailnet.login_allowed("a@example.com", (OWNER_LOGIN, "a@example.com")) is False


@pytest.mark.asyncio
async def test_revoke_never_live_revokes_the_current_owner(fake, config_file) -> None:
    """The host switched to an account that once accepted this invite: it stays the owner."""
    await invites.connect(KEY, "owner")
    made = await invites.create_invites([], multi_use=False, alias="", avatar_id="", actor="o")
    fake.accept(made[0]["id"], "u-own", "Old.Name@example.com")
    await invites.sync(force=True)
    tailnet_live_allowlist.note_login("u-own", "old.name@example.com")
    tailnet_live_allowlist.note_login("u-own", OWNER_LOGIN)
    access_set.install((), owner_login=OWNER_LOGIN, owner_user_id="u-own")
    await invites.revoke(made[0]["id"], "owner")
    assert tailnet_live_allowlist.decision(OWNER_LOGIN) is not False
    assert tailnet_live_allowlist.decision("old.name@example.com") is not False
    assert tailnet.login_allowed(OWNER_LOGIN, (OWNER_LOGIN,)) is True


@pytest.mark.asyncio
async def test_revoke_withdraws_a_renamed_members_old_login_after_a_restart(
    fake, config_file, monkeypatch
) -> None:
    """The renamed-away login is on disk, so a revoke after a restart withdraws it too."""
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["ann@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.accept(made[0]["id"], "u-42", "ann@example.com", "")
    await invites.sync(force=True)
    fake.accept(made[0]["id"], "u-42", "ann.renamed@example.com", "")
    await invites.sync(force=True)
    tailnet_live_allowlist.reset_for_tests()  # a restart forgets the daemon's aliases
    dropped: list[str] = []
    monkeypatch.setattr(invites.access_set, "drop_from_seed", dropped.append)
    _result, withdrawn = await invites.revoke_with_logins(made[0]["id"], "owner")
    assert {"ann@example.com", "ann.renamed@example.com"} <= set(dropped)
    assert {"ann@example.com", "ann.renamed@example.com"} <= withdrawn
    assert tailnet_live_allowlist.decision("ann@example.com") is False


@pytest.mark.asyncio
async def test_revoke_closes_the_revoked_persons_open_sockets() -> None:
    from kiro_crew.shared_crew.contracts import Principal

    class Sock(dict):
        def __init__(self, principal: Any, *, dashboard: bool = True) -> None:
            actor = ActorContext(principal=principal, request_id="r", transport=Transport.WEBSOCKET)
            super().__init__(_shared_crew_actor=actor, _is_dashboard_user=dashboard)
            self.closed = False

        async def close(self, **_kw: Any) -> None:
            self.closed = True

    ann = Sock(tailnet_principal("Ann@Example.com", "phone", Provenance.TAILSCALE_SERVE))
    bob = Sock(tailnet_principal("bob@example.com", "phone", Provenance.TAILSCALE_SERVE))
    unnamed = Sock(Principal.unknown())
    local = Sock(local_operator_principal("owner"))
    app = web.Application()
    app["state"] = SimpleNamespace(_ws_clients=[ann, bob, unnamed, local])
    closed = await routes_invites.close_revoked_sockets(app, frozenset({"ann@example.com"}))
    assert closed == 2
    assert ann.closed and unnamed.closed and not bob.closed and not local.closed


@pytest.mark.asyncio
async def test_revoke_never_removes_the_owner_login(fake, config_file) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites([], multi_use=False, alias="", avatar_id="", actor="o")
    fake.accept(made[0]["id"], "u-own", OWNER_LOGIN)
    await invites.sync(force=True)
    await invites.revoke(made[0]["id"], "owner")
    assert _logins(config_file) == [OWNER_LOGIN]


@pytest.mark.asyncio
async def test_revoke_tolerates_an_invite_already_gone(fake) -> None:
    await invites.connect(KEY, "owner")
    made = await invites.create_invites(
        ["a@example.com"], multi_use=False, alias="", avatar_id="", actor="o"
    )
    fake.invites.clear()
    assert (await invites.revoke(made[0]["id"], "owner"))["revoked"] == made[0]["id"]


# ── allowlist writer ──


def test_allowlist_writer_keeps_owner_and_skips_duplicates() -> None:
    data: dict[str, Any] = {
        "dashboard": {
            "tailscale": {
                "allowed_logins": ["Owner@Example.com"],
                "enabled": True,
                "trust_identity": True,
            }
        }
    }
    out: dict[str, bool] = {}
    tailscale_allowlist._mutate(data, "a@x.example", True, OWNER_LOGIN, out)
    assert data["dashboard"]["tailscale"]["allowed_logins"] == ["Owner@Example.com", "a@x.example"]
    assert out["restart"] is False
    assert tailscale_allowlist._mutate(data, "a@x.example", True, OWNER_LOGIN, {}) is None
    tailscale_allowlist._mutate(data, "owner@example.com", False, OWNER_LOGIN, {})
    assert "Owner@Example.com" in data["dashboard"]["tailscale"]["allowed_logins"]


def test_allowlist_writer_refuses_a_malformed_list() -> None:
    with pytest.raises(ValueError):
        tailscale_allowlist._mutate(
            {"dashboard": {"tailscale": {"allowed_logins": "x"}}}, "a@x", True, "", {}
        )


# ── avatars, alias ──


def test_avatar_upload_is_bounded_and_typed() -> None:
    avatar_id = store.save_avatar(PNG)
    assert store.read_avatar(avatar_id) == (PNG, "image/png")
    with pytest.raises(ValueError, match="avatar_type"):
        store.save_avatar(b"<svg onload=alert(1)>")
    with pytest.raises(ValueError, match="avatar_too_large"):
        store.save_avatar(PNG + b"\x00" * store.MAX_AVATAR_BYTES)
    assert store.read_avatar("../../config.json") is None


def test_alias_is_display_text_only() -> None:
    assert store.clean_alias("  Ann\n\x00 ") == "Ann"
    assert len(store.clean_alias("x" * 500)) == store.MAX_ALIAS_CHARS
    assert store.clean_alias(42) == ""


def test_emails_are_validated_and_deduplicated() -> None:
    assert invites.clean_emails(["a@x.example", "A@x.example"]) == ["a@x.example"]
    for bad in (["nope"], "a@x", [1], ["a@x.example"] * (invites.MAX_EMAILS + 1)):
        with pytest.raises(invites.InviteError):
            invites.clean_emails(bad)


# ── routes ──


def _route_app(principal: Any, *, user: str = "local-app") -> web.Application:
    @web.middleware
    async def as_viewer(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = ""
        request["user"] = user
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[as_viewer])
    app["state"] = SimpleNamespace(owner_id="", _ws_clients=[])
    for path, method, handler in (
        (routes_invites.STATUS_PATH, "GET", routes_invites.api_status),
        (routes_invites.STATUS_PATH, "POST", routes_invites.api_create),
        (routes_invites.CREDENTIAL_PATH, "POST", routes_invites.api_connect),
        (routes_invites.CREDENTIAL_PATH, "DELETE", routes_invites.api_disconnect),
        (routes_invites.INVITE_PATH, "DELETE", routes_invites.api_revoke),
        (routes_invites.AVATARS_PATH, "POST", routes_invites.api_upload_avatar),
        (routes_invites.AVATAR_PATH, "GET", routes_invites.api_avatar),
    ):
        app.router.add_route(method, path, handler)
    return app


@pytest.fixture
def not_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.dashboard.handlers._shared as shared

    monkeypatch.setattr(shared, "_is_restricted_session", lambda *_a: False)


@pytest.mark.asyncio
async def test_routes_without_key_show_admin_fallback(fake, not_restricted) -> None:
    async with TestClient(TestServer(_route_app(local_operator_principal("local-app")))) as client:
        resp = await client.get(routes_invites.STATUS_PATH)
        body = await resp.json()
        assert resp.status == 200
        assert body["connected"] is False
        assert body["admin_share_url"] == "https://login.tailscale.com/admin/machines"
        resp = await client.post(routes_invites.STATUS_PATH, json={"emails": ["a@x.example"]})
        assert resp.status == 409 and (await resp.json())["code"] == "not_connected"


@pytest.mark.asyncio
async def test_routes_connect_invite_and_never_echo_key(fake, not_restricted) -> None:
    async with TestClient(TestServer(_route_app(local_operator_principal("local-app")))) as client:
        resp = await client.post(routes_invites.CREDENTIAL_PATH, json={"key": KEY})
        text = await resp.text()
        assert resp.status == 200 and KEY not in text and "Zz9" not in text
        invite = {"emails": [], "multi_use": False, "alias": "Ann"}
        resp = await client.post(routes_invites.STATUS_PATH, json=invite)
        # The first share waits for the owner to see what teammates will see.
        assert resp.status == 409
        assert (await resp.json())["code"] == "first_share_ack_required"
        access_set.acknowledge_first_share("owner")
        resp = await client.post(routes_invites.STATUS_PATH, json=invite)
        assert resp.status == 200
        resp = await client.get(routes_invites.STATUS_PATH)
        text = await resp.text()
        assert "Zz9" not in text
        assert json.loads(text)["invites"][0]["alias"] == "Ann"


@pytest.mark.asyncio
async def test_routes_wrong_key_is_502_not_401(fake, not_restricted) -> None:
    async with TestClient(TestServer(_route_app(local_operator_principal("local-app")))) as client:
        resp = await client.post(routes_invites.CREDENTIAL_PATH, json={"key": OTHER_KEY})
        assert resp.status == 502
        assert (await resp.json())["code"] == "tailscale_auth_failed"


@pytest.mark.asyncio
async def test_routes_refuse_a_teammate(fake, not_restricted) -> None:
    teammate = tailnet_principal("ann@example.com", "phone", Provenance.TAILSCALE_DIRECT)
    async with TestClient(TestServer(_route_app(teammate))) as client:
        for method, path in (
            ("GET", routes_invites.STATUS_PATH),
            ("POST", routes_invites.CREDENTIAL_PATH),
            ("DELETE", "/api/shared-crew/tailscale/invites/1"),
        ):
            resp = await client.request(method, path, json={"key": KEY})
            assert resp.status == 403, (method, path)
    assert store.credential_present() is False


@pytest.mark.asyncio
async def test_routes_admit_the_owner_own_tailnet_login(fake, not_restricted) -> None:
    owner_phone = tailnet_principal(OWNER_LOGIN, "phone", Provenance.TAILSCALE_DIRECT)
    async with TestClient(TestServer(_route_app(owner_phone))) as client:
        assert (await client.get(routes_invites.STATUS_PATH)).status == 200


@pytest.mark.asyncio
async def test_routes_refuse_a_non_owner_token(fake, not_restricted) -> None:
    app = _route_app(local_operator_principal("someone"), user="someone")
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(routes_invites.STATUS_PATH)).status == 403


@pytest.mark.asyncio
async def test_routes_avatar_upload_and_safe_serving(fake, not_restricted) -> None:
    async with TestClient(TestServer(_route_app(local_operator_principal("local-app")))) as client:
        resp = await client.post(
            routes_invites.AVATARS_PATH, json={"data_base64": base64.b64encode(PNG).decode()}
        )
        avatar_id = (await resp.json())["avatar_id"]
        resp = await client.get(f"/api/shared-crew/tailscale/avatars/{avatar_id}")
        assert resp.status == 200 and resp.content_type == "image/png"
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in resp.headers["Content-Security-Policy"]
        resp = await client.post(
            routes_invites.AVATARS_PATH, json={"data_base64": base64.b64encode(b"<html>").decode()}
        )
        assert resp.status == 400
        resp = await client.post(routes_invites.AVATARS_PATH, json={"data_base64": "!!"})
        assert resp.status == 400


def test_people_rows_show_alias_by_verified_login(fake) -> None:
    store.update_state(
        lambda s: s["members"].update(
            {"u-1": {"user_id": "u-1", "login": "ann@example.com", "alias": "Ann", "avatar_id": ""}}
        )
    )
    ann = tailnet_principal("ann@example.com", "phone", Provenance.TAILSCALE_SERVE).to_public_dict()
    bob = tailnet_principal("bob@example.com", "phone", Provenance.TAILSCALE_SERVE).to_public_dict()
    rows = [
        {"principal": ann, "provenance": "tailscale_serve"},
        {"principal": bob, "provenance": "tailscale_serve"},
        {"principal": ann, "provenance": "local_operator"},
    ]
    body = routes_invites.decorate_people({"people": rows})
    assert body["people"][0]["member"]["alias"] == "Ann"
    assert "member" not in body["people"][1]
    assert "member" not in body["people"][2], "only a verified tailnet row borrows an alias"
