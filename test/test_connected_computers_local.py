"""Connected computers: the local machine's consent, grants and connector.

* B01 -- no grant (or a revoked one): no read, write or command; a clear refusal.
* B02 -- only the confirmed gateway + root pair is served; a cancelled consent
  leaves nothing, and a socket that answers as another gateway is refused.
* B05 -- a root that holds or sits in a protected folder is refused, symlinks
  resolved.
* B07 -- revoking one of two grants leaves the other.
* B13 -- a remote page cannot enable or widen a grant: consent needs a direct
  local request, redemption needs the desktop shell's internal secret.
* B14 -- only ticked stdio MCP servers are exposed, as confirmed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.connected_computers import local_routes
from kiro_crew.connected_computers.connector import Connector, ConnectorRefused
from kiro_crew.connected_computers.local_mcp import McpExposureRefused, exposable, snapshot
from kiro_crew.connected_computers.local_store import Grant, LocalGrantStore, new_grant_id
from kiro_crew.connected_computers.root_policy import RootRefused, resolve_root
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.asyncio


class _MemoryProtector:
    """Stands in for the OS keyring (``protected_store``) in these tests."""

    backend = "test"

    def __init__(self) -> None:
        self.key = None

    def load(self):
        return self.key

    def save(self, key: bytes) -> None:
        self.key = bytes(key)


@pytest.fixture
def store(tmp_path):
    return LocalGrantStore(SecretVault(tmp_path / "local-vault"), protector=_MemoryProtector())


def _grant(instance: str = "inst-1", root: str = "/work/proj", **kw) -> Grant:
    return Grant(
        grant_id=new_grant_id(),
        instance_id=instance,
        gateway_id=f"gw-{instance}",
        gateway_label="remote",
        device_id="laptop",
        root=root,
        credential="cred-" + instance + ".signature",
        credential_expires=4_000_000_000.0,
        peer_key=PEER_KEY,
        **kw,
    )


PEER_KEY = "ab" * 32


# -- B05: which folder may be shared ------------------------------------------------


class TestRootPolicy:
    @pytest.fixture
    def home(self, tmp_path):
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / "proj").mkdir()
        return home

    def _resolve(self, raw, home):
        return resolve_root(str(raw), home=home, protected=[home / ".ssh"])

    def test_a_project_folder_resolves_to_its_real_path(self, home):
        assert self._resolve(home / "proj", home) == os.path.realpath(home / "proj")

    @pytest.mark.parametrize("pick", ["home", "parent", "ssh", "inside_ssh"])
    def test_b05_protected_or_too_wide_roots_are_refused(self, home, pick):
        (home / ".ssh" / "keys").mkdir()
        target = {
            "home": home,
            "parent": home.parent,
            "ssh": home / ".ssh",
            "inside_ssh": home / ".ssh" / "keys",
        }[pick]
        with pytest.raises(RootRefused):
            self._resolve(target, home)

    def test_b05_a_symlink_into_a_protected_folder_is_refused(self, home):
        link = home / "proj" / "innocent"
        link.symlink_to(home / ".ssh", target_is_directory=True)
        with pytest.raises(RootRefused):
            self._resolve(link, home)

    @pytest.mark.parametrize("raw", ["", "relative/path", "/no/such/dir", "/a/../b"])
    def test_malformed_roots_are_refused(self, home, raw):
        with pytest.raises(RootRefused):
            resolve_root(raw, home=home, protected=[home / ".ssh"])

    def test_the_disk_root_is_refused(self, home):
        with pytest.raises(RootRefused):
            resolve_root("/", home=home, protected=[home / ".ssh"])


# -- grants: B01, B02, B07, re-enable ----------------------------------------------


class TestGrantStore:
    def test_b01_a_fresh_computer_has_no_grant(self, store):
        assert store.active() == []

    def test_b07_revoking_one_grant_leaves_the_other(self, store):
        a = store.add(_grant("inst-a"))
        b = store.add(_grant("inst-b"))
        store.revoke(a.grant_id)
        assert [g.grant_id for g in store.active()] == [b.grant_id]
        assert store.get(a.grant_id).credential == "", "a revoked grant keeps no credential"

    def test_b02_a_new_consent_for_one_gateway_replaces_the_old(self, store):
        old = store.add(_grant("inst-a", root="/work/old"))
        new = store.add(_grant("inst-a", root="/work/new"))
        assert [g.grant_id for g in store.active()] == [new.grant_id]
        assert store.get(old.grant_id).active is False

    def test_reenable_never_revives_a_revoked_grant(self, store):
        grant = store.add(_grant())
        store.revoke_all()
        assert store.set_credential(grant.grant_id, "new-cred", 1.0) is None
        assert store.active() == []

    def test_the_install_key_is_stable(self, store):
        assert store.install_key() == store.install_key()
        assert len(store.install_key()) == 32

    def test_public_rows_never_carry_the_credential(self, store):
        grant = store.add(_grant())
        assert "cred" not in json.dumps(grant.public())


# -- B14: which local MCP servers may be exposed -------------------------------------


class TestMcpExposure:
    SERVERS = [
        McpServerInfo(name="notes", command="/usr/bin/notes-mcp", args=["--stdio"]),
        McpServerInfo(name="web", url="https://mcp.example.test/sse"),
        McpServerInfo(name="vaulted", command="tool", env={"TOKEN": "secret://tok"}),
        McpServerInfo(name="kirocrew-core", command="kirocrew"),
    ]

    def test_b14_a_ticked_stdio_server_is_exposed_as_confirmed(self):
        exposed = snapshot(["notes"], self.SERVERS)
        assert [(e.name, e.command, e.args) for e in exposed] == [
            ("notes", "/usr/bin/notes-mcp", ("--stdio",))
        ]

    @pytest.mark.parametrize("name", ["web", "vaulted", "kirocrew-core", "missing"])
    def test_b14_unsupported_servers_are_refused_with_a_reason(self, name):
        with pytest.raises(McpExposureRefused, match=name):
            snapshot([name], self.SERVERS)

    def test_b14_the_consent_lists_every_server_with_its_verdict(self):
        verdicts = {row["name"]: row["refused"] for row in exposable(self.SERVERS)}
        assert verdicts["notes"] == ""
        assert all(verdicts[n] for n in ("web", "vaulted", "kirocrew-core"))

    def test_b14_nothing_ticked_exposes_nothing(self):
        assert snapshot([], self.SERVERS) == ()

    def test_b14_a_server_with_tools_turned_off_locally_keeps_them_off(self):
        # Shared, not refused: the connector enforces the local limit
        # (test_connected_computers_mcp_filter.py).
        limited = McpServerInfo(name="limited", command="/usr/bin/x", disabled_tools=["rm"])
        [exposed] = snapshot(["limited"], [limited])
        assert exposed.disabled_tools == ("rm",)
        assert exposable([limited])[0]["refused"] == ""
        open_ = McpServerInfo(name="open", command="/usr/bin/x", disabled_tools=[])
        assert [(e.name, e.disabled_tools) for e in snapshot(["open"], [open_])] == [("open", ())]


class TestLinuxEnvNotInArgv:
    def test_b05_child_env_values_never_reach_the_bwrap_argv(self, tmp_path):
        from kiro_crew.connected_computers.confinement import LinuxPlan

        root = tmp_path / "root"
        scratch = tmp_path / "scratch"
        root.mkdir()
        scratch.mkdir()
        # An explicit machine: the plan is built (not run) on every CI OS.
        confined = LinuxPlan("/usr/bin/bwrap", machine="x86_64").wrap(
            ["/usr/bin/tool"],
            root=str(root),
            scratch=str(scratch),
            env={"API_KEY": "very-private-value"},
        )
        try:
            assert "very-private-value" not in " ".join(confined.argv)
            assert "--setenv" not in confined.argv
            assert confined.env["API_KEY"] == "very-private-value"
        finally:
            confined.close()


# -- the connector: B01, B02 ----------------------------------------------------------


#: Queued after ``hello`` by :class:`FakeWs`: the gateway's authenticated ``admitted``.
ADMITTED = object()


class FakeWs:
    def __init__(
        self,
        incoming: list,
        *,
        protect: bool = True,
        close: tuple[int, str] | None = None,
        admit: bool = True,
    ) -> None:
        self._incoming = list(incoming)
        #: ``(code, message)`` of the gateway's close once ``incoming`` runs out.
        #: Unauthenticated: the connector must never act on it.
        self._close = close
        #: Answer ``hello`` with an authenticated ``admitted`` (the real gateway's
        #: first frame after it verified auth and ready).
        self._admit = admit
        self.close_code: int | None = None
        self.sent: list[dict] = []
        self.wire_sent: list[str] = []
        self.closed = False
        self.headers: dict = {}
        self.frames = None
        self.protect = protect

    def _next(self) -> str:
        from kiro_crew.connected_computers import protocol
        from kiro_crew.connected_computers.authenticated import SessionFrames

        item = self._incoming.pop(0)
        if item is ADMITTED:
            assert self.frames is not None
            return self.frames.encode({"op": "admitted"})
        text = item(self.headers) if callable(item) else item
        frame = json.loads(text)
        if frame.get("op") == "hello":
            bound = (
                frame["gateway"],
                frame["device"],
                self.headers[protocol.NONCE_HEADER],
                frame["nonce"],
            )
            self.frames = SessionFrames(PEER_KEY, bound, gateway=True)
            if self._admit:
                self._incoming.insert(0, ADMITTED)
        elif self.protect and self.frames is not None:
            return self.frames.encode(frame)
        return text

    async def receive(self, timeout=None):
        if not self._incoming:
            if self._close is not None:
                self.close_code, message = self._close
                self._close = None
                return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, self.close_code, message)
            return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, self._next(), None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, self._next(), None)

    async def send_str(self, text: str) -> None:
        from kiro_crew.connected_computers import protocol

        self.wire_sent.append(text)
        # Every connector frame after hello, auth included, is authenticated.
        assert self.frames is not None
        frame = self.frames.decode(text, allowed=protocol.FROM_DEVICE | protocol.AUTH_ONLY)
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, ws: FakeWs) -> None:
        self.ws = ws
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def ws_connect(self, url, *, headers, **_kw):
        self.headers = headers
        self.ws.headers = dict(headers)
        return self.ws


class RefusingPlan:
    name = "test-plan"

    def wrap(self, *a, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("a refused open must never start a process")


def _hello(grant: Grant, gateway: str | None = None):
    """A ``hello`` proving the grant's peer key over the connector's nonce (R6)."""
    from kiro_crew.connected_computers import protocol
    from kiro_crew.connected_computers.credentials import gateway_proof

    def build(headers: dict) -> str:
        gw = gateway or grant.gateway_id
        g_nonce = "c" * 64
        proof = gateway_proof(
            PEER_KEY, gw, grant.device_id, headers[protocol.NONCE_HEADER], g_nonce
        )
        return json.dumps(
            {
                "op": "hello",
                "v": 1,
                "gateway": gw,
                "device": grant.device_id,
                "nonce": g_nonce,
                "proof": proof,
            }
        )

    return build


async def test_b01_a_revoked_grant_refuses_the_open_and_starts_nothing(monkeypatch):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    grant = _grant()
    ws = FakeWs([_hello(grant), json.dumps({"op": "open", "ch": 1, "kind": "workspace"})])
    session = FakeSession(ws)
    connector = Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=RefusingPlan(),
        grant_still_active=lambda: False,
        session_factory=lambda: session,
    )
    await asyncio.wait_for(connector.run(), 30)
    ready = [f for f in ws.sent if f["op"] == "ready"]
    refused = [f for f in ws.sent if f["op"] == "refused"]
    assert ready and ready[0]["root"] == grant.root
    assert refused and refused[0]["ch"] == 1 and "turned off" in refused[0]["reason"]
    assert "Authorization" not in session.headers, "R6: no credential before the proof"
    assert ws.sent[0]["op"] == "auth" and "credential" not in ws.sent[0]
    assert grant.credential not in "".join(ws.wire_sent)


async def test_b01_the_local_switch_off_refuses_even_with_a_live_grant(monkeypatch):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: False)
    grant = _grant()
    ws = FakeWs([_hello(grant), json.dumps({"op": "open", "ch": 2, "kind": "workspace"})])
    connector = Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=RefusingPlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: FakeSession(ws),
    )
    await asyncio.wait_for(connector.run(), 30)
    assert [f["op"] for f in ws.sent if f["op"] == "refused"] == ["refused"]


async def test_b02_a_socket_answering_as_another_gateway_is_refused():
    grant = _grant()
    ws = FakeWs([_hello(grant, gateway="gw-impostor")])
    connector = Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=RefusingPlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: FakeSession(ws),
    )
    with pytest.raises(ConnectorRefused):
        await asyncio.wait_for(connector.run(), 30)
    assert ws.sent == [], "nothing, not even ready, goes to a gateway the user did not approve"


async def test_b14_an_mcp_server_that_was_not_exposed_is_refused(monkeypatch):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    grant = _grant()
    ws = FakeWs([_hello(grant), json.dumps({"op": "open", "ch": 3, "kind": "mcp:browser"})])
    connector = Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=RefusingPlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: FakeSession(ws),
    )
    await asyncio.wait_for(connector.run(), 30)
    refused = [f for f in ws.sent if f["op"] == "refused"]
    assert refused and "not shared" in refused[0]["reason"]


# -- B13: consent needs this computer's own native path ------------------------------


@web.middleware
async def _owner(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    internal = request.headers.get("X-Test-Internal")
    if internal:
        request["internal_auth"] = True
    if request.headers.get("X-Test-Agent-Grant"):
        from kiro_crew.dashboard.agent_ipc import GRANT_KEY

        request[GRANT_KEY] = object()
    return await handler(request)


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path, store):
    local_routes.clear_consents()
    monkeypatch.setattr(local_routes, "local_enabled", lambda cfg=None: True)
    instance = SimpleNamespace(ssh_host="remote-host", name="Remote", local_port=0)
    monkeypatch.setattr(local_routes, "_instance", lambda _id: instance)
    from kiro_crew.connected_computers import local_mcp

    monkeypatch.setattr(
        local_mcp,
        "configured_servers",
        lambda: [McpServerInfo(name="notes", command="/usr/bin/notes-mcp")],
    )
    from kiro_crew.connected_computers import controller as controller_mod

    # Minting a consent reads this computer's Off generation from the store.
    owner = controller_mod.LocalController(store, instance_lookup=lambda _id: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: owner)
    app = web.Application(middlewares=[_owner])
    app["state"] = MagicMock()
    local_routes.register_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def _consent(client) -> str:
    resp = await client.post(local_routes.CONSENT_PATH, json={"instance_id": "inst-1"})
    assert resp.status == 200, await resp.text()
    return (await resp.json())["consent"]


async def test_b13_a_forwarded_request_cannot_start_consent(client):
    resp = await client.post(
        local_routes.CONSENT_PATH,
        json={"instance_id": "inst-1"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert resp.status == 403
    assert (await resp.json())["code"] == "not_local"


async def test_b13_redeeming_needs_the_desktop_shells_internal_secret(client):
    consent = await _consent(client)
    resp = await client.post(
        local_routes.REDEEM_PATH, json={"consent": consent, "root": "/tmp", "mcp": []}
    )
    assert resp.status == 403


async def test_b13_an_agent_capability_never_redeems_consent(client):
    consent = await _consent(client)
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": consent, "root": "/tmp", "mcp": []},
        headers={"X-Test-Internal": "1", "X-Test-Agent-Grant": "1"},
    )
    assert resp.status == 403


async def test_b02_cancelled_consent_writes_nothing(client, store):
    await _consent(client)  # the native dialog was cancelled: nothing is redeemed
    assert store.list() == []
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": "made-up", "root": "/tmp", "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    assert resp.status == 404
    assert store.list() == []


async def test_b02_a_consent_id_is_single_use(client, monkeypatch, tmp_path, store):
    _patch_enrollment(monkeypatch, store)
    root = tmp_path / "proj"
    root.mkdir()
    consent = await _consent(client)
    body = {"consent": consent, "root": str(root), "mcp": ["notes"]}
    first = await client.post(local_routes.REDEEM_PATH, json=body, headers={"X-Test-Internal": "1"})
    assert first.status == 200, await first.text()
    again = await client.post(local_routes.REDEEM_PATH, json=body, headers={"X-Test-Internal": "1"})
    assert again.status == 404
    [grant] = store.active()
    assert grant.root == os.path.realpath(root)
    assert [m.name for m in grant.mcp] == ["notes"]
    assert grant.gateway_id == "gw-remote" and grant.device_id == "laptop"
    assert "cred-from-ssh" not in await first.text()


def _patch_enrollment(monkeypatch, store) -> None:
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    async def fake_enroll(instance, *, base, fingerprint, label):
        return enroll_mod.Enrolled(
            device_id="laptop",
            gateway_id="gw-remote",
            gateway_label="remote",
            credential="cred-from-ssh",
            expires=4_000_000_000.0,
        )

    monkeypatch.setattr(enroll_mod, "enroll", fake_enroll)
    fake = controller_mod.LocalController(store, instance_lookup=lambda _id: None)

    async def no_reconcile() -> None:
        return None

    monkeypatch.setattr(fake, "start", lambda: None)
    monkeypatch.setattr(fake, "reconcile", no_reconcile)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: fake)


async def test_b13_turning_off_needs_no_native_path_and_works_while_disabled(
    client, monkeypatch, store
):
    from kiro_crew.connected_computers import controller as controller_mod

    grant = store.add(_grant())
    fake = controller_mod.LocalController(store, instance_lookup=lambda _id: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: fake)
    monkeypatch.setattr(local_routes, "local_enabled", lambda cfg=None: False)
    resp = await client.post(local_routes.OFF_PATH)
    assert resp.status == 200
    assert (await resp.json())["revoked"] == [grant.grant_id]
    assert store.active() == []


def test_the_tool_server_files_are_where_the_connector_reads_them():
    from kiro_crew.connected_computers.connector import tools_dir

    assert (Path(tools_dir()) / "workspace_tools.py").is_file()


# -- B02 / B13: the native dialog's cancel, and an Off racing a redemption -----------


async def test_b02_the_native_cancel_drops_the_consent_and_writes_nothing(client, store):
    consent = await _consent(client)
    cancel = await client.post(
        local_routes.CANCEL_PATH, json={"consent": consent}, headers={"X-Test-Internal": "1"}
    )
    assert cancel.status == 200
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": consent, "root": "/tmp", "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    assert resp.status == 404
    assert store.list() == []


async def test_b02_describe_shows_the_native_dialog_the_minted_crew_without_using_the_consent(
    client, store
):
    consent = await _consent(client)
    resp = await client.post(
        local_routes.DESCRIBE_PATH, json={"consent": consent}, headers={"X-Test-Internal": "1"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["instance"]["name"], str) and body["instance"]["id"]
    assert isinstance(body["mcp"], list)
    # Describing does not spend the consent: cancel still finds it once.
    again = await client.post(
        local_routes.DESCRIBE_PATH, json={"consent": consent}, headers={"X-Test-Internal": "1"}
    )
    assert again.status == 200
    assert store.list() == []


async def test_b13_a_page_cannot_describe_a_consent_without_the_internal_secret(client):
    consent = await _consent(client)
    resp = await client.post(local_routes.DESCRIBE_PATH, json={"consent": consent})
    assert resp.status == 403
    unknown = await client.post(
        local_routes.DESCRIBE_PATH, json={"consent": "x" * 32}, headers={"X-Test-Internal": "1"}
    )
    assert unknown.status == 404


async def test_b13_a_page_cannot_cancel_or_redeem_without_the_internal_secret(client):
    consent = await _consent(client)
    resp = await client.post(local_routes.CANCEL_PATH, json={"consent": consent})
    assert resp.status == 403


async def test_b01_a_local_off_during_enrollment_leaves_no_grant(
    client, monkeypatch, tmp_path, store
):
    from kiro_crew.connected_computers import controller as controller_mod
    from kiro_crew.connected_computers import enroll as enroll_mod

    _patch_enrollment(monkeypatch, store)
    fake = controller_mod.get_controller()

    async def enroll_then_off(instance, *, base, fingerprint, label):
        await fake.off()  # the user pressed Off while the SSH enrollment ran
        return enroll_mod.Enrolled(
            device_id="laptop",
            gateway_id="gw-remote",
            gateway_label="remote",
            credential="cred-from-ssh",
            expires=4_000_000_000.0,
        )

    monkeypatch.setattr(enroll_mod, "enroll", enroll_then_off)
    forgotten = []

    async def fake_forget(instance, *, device_id, fingerprint):
        forgotten.append(device_id)

    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    root = tmp_path / "proj"
    root.mkdir()
    consent = await _consent(client)
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": consent, "root": str(root), "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    assert resp.status == 409
    assert (await resp.json())["code"] == "turned_off"
    assert store.active() == []
    assert forgotten == ["laptop"]  # the remote enrollment is undone when reachable


async def test_b01_a_local_off_drops_every_pending_consent(client, monkeypatch, store):
    from kiro_crew.connected_computers import controller as controller_mod

    consent = await _consent(client)
    fake = controller_mod.LocalController(store, instance_lookup=lambda _id: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: fake)
    assert (await client.post(local_routes.OFF_PATH)).status == 200
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": consent, "root": "/tmp", "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    assert resp.status == 404


async def test_b01_an_off_from_another_process_while_the_dialog_is_open_refuses_the_grant(
    client, monkeypatch, tmp_path, store
):
    """The CLI Off cannot clear this gateway's consents: the generation read at mint does."""
    from kiro_crew.connected_computers import enroll as enroll_mod

    _patch_enrollment(monkeypatch, store)
    forgotten = []

    async def fake_forget(instance, *, device_id, fingerprint):
        forgotten.append(device_id)

    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    root = tmp_path / "proj"
    root.mkdir()
    consent = await _consent(client)  # the native dialog is now open
    store.revoke_all()  # `kirocrew connected-computer off` in another terminal
    resp = await client.post(
        local_routes.REDEEM_PATH,
        json={"consent": consent, "root": str(root), "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    assert resp.status == 409
    assert (await resp.json())["code"] == "turned_off"
    assert store.active() == []
    assert forgotten == ["laptop"]


@pytest.mark.parametrize("operation", ["renew", "run", "forget"])
async def test_controller_registry_lookups_run_off_the_event_loop(monkeypatch, operation):
    import threading

    from kiro_crew.connected_computers import controller as controller_mod

    class LookupObserved(BaseException):
        """End the controller iteration without entering its reconnect backoff."""

    loop_thread = threading.get_ident()
    lookups = []
    grant = _grant()
    pending = SimpleNamespace(key="inst-1/laptop", instance_id="inst-1", next_try=0)
    store = SimpleNamespace(
        get=lambda _id: grant,
        unsealed=lambda _id: grant,
        pending_forgets=lambda: [pending],
    )

    def lookup(instance_id):
        lookups.append((instance_id, threading.get_ident()))
        raise LookupObserved

    async def checked(_plan, _root):
        return None

    monkeypatch.setattr(controller_mod, "self_test", checked)
    controller = controller_mod.LocalController(
        store,
        instance_lookup=lookup,
        plan_factory=lambda: object(),
        enabled=lambda: True,
        clock=lambda: 0,
    )
    if operation == "renew":
        action = controller._renew(grant)
    elif operation == "run":
        action = controller._run_grant(grant)
    else:
        action = controller_mod.drain_pending_forgets(store, lookup, clock=lambda: 0)
    with pytest.raises(LookupObserved):
        await asyncio.wait_for(action, timeout=FALLBACK_TIMEOUT_SECS)
    assert len(lookups) == 1
    assert lookups[0][0] == grant.instance_id
    assert lookups[0][1] != loop_thread, "the registry can wait on a file lock"


@pytest.mark.parametrize("operation", ["stop", "open_failure"])
async def test_channel_scratch_cleanup_runs_off_the_event_loop(monkeypatch, tmp_path, operation):
    import threading

    from kiro_crew.connected_computers import connector as connector_mod
    from kiro_crew.connected_computers import flag

    loop_thread = threading.get_ident()
    removals = []
    scratch = tmp_path / "channel-scratch"
    scratch.mkdir()
    (scratch / "child-output").write_text("temporary", encoding="utf-8")
    real_rmtree = connector_mod.shutil.rmtree

    def remove(path, *, ignore_errors):
        removals.append((Path(path), threading.get_ident()))
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(connector_mod, "shutil", SimpleNamespace(rmtree=remove))
    if operation == "stop":
        proc = SimpleNamespace(returncode=0)
        channel = connector_mod._LocalChannel(1, proc, str(scratch))
        await asyncio.wait_for(channel.stop(), timeout=FALLBACK_TIMEOUT_SECS)
        assert channel.ended
    else:
        monkeypatch.setattr(flag, "local_enabled", lambda: True)
        monkeypatch.setattr(
            connector_mod, "tempfile", SimpleNamespace(mkdtemp=lambda **_kw: str(scratch))
        )
        connector = Connector(
            _grant(),
            url="ws://127.0.0.1:1/x",
            plan=RefusingPlan(),
            grant_still_active=lambda: True,
        )
        from kiro_crew.connected_computers.authenticated import SessionFrames

        socket = FakeWs([])
        # The matching directional pair a real handshake leaves on both endpoints.
        bound = ("gw-inst-1", "laptop", "0" * 64, "1" * 64)
        socket.frames = SessionFrames(PEER_KEY, bound, gateway=True)
        connector._frames = SessionFrames(PEER_KEY, bound, gateway=False)
        connector._ws = socket
        await asyncio.wait_for(connector._open(1, "workspace"), timeout=FALLBACK_TIMEOUT_SECS)
        assert [frame["op"] for frame in socket.sent] == ["refused"]
        assert connector._channels == {}
    assert len(removals) == 1
    assert removals[0][0] == scratch
    assert removals[0][1] != loop_thread, "scratch may contain a large tool-server tree"
    assert not scratch.exists()


async def test_channel_preparation_runs_off_the_event_loop(monkeypatch, tmp_path):
    """The scratch folder and the plan's wrap (probes, realpath walks, seccomp pipe)."""
    import threading

    from kiro_crew.connected_computers import connector as connector_mod
    from kiro_crew.connected_computers import flag
    from kiro_crew.connected_computers.authenticated import SessionFrames

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    scratch = tmp_path / "channel-scratch"

    def mkdtemp(**_kw):
        seen["mkdtemp"] = threading.get_ident()
        scratch.mkdir()
        return str(scratch)

    class _ObservedPlan:
        name = "observed"

        def wrap(self, argv, **_kw):
            seen["wrap"] = threading.get_ident()
            raise OSError("the plan could not be built here")

    def local_enabled() -> bool:
        # Without a live snapshot it reads the config files.
        seen["local_enabled"] = threading.get_ident()
        return True

    def tools_dir() -> str:
        # A realpath walk of the install, which slow storage can stall.
        seen["tools_dir"] = threading.get_ident()
        return str(tmp_path)

    monkeypatch.setattr(flag, "local_enabled", local_enabled)
    monkeypatch.setattr(connector_mod, "tools_dir", tools_dir)
    monkeypatch.setattr(connector_mod, "tempfile", SimpleNamespace(mkdtemp=mkdtemp))
    connector = Connector(
        _grant(), url="ws://127.0.0.1:1/x", plan=_ObservedPlan(), grant_still_active=lambda: True
    )
    socket = FakeWs([])
    bound = ("gw-inst-1", "laptop", "0" * 64, "1" * 64)
    socket.frames = SessionFrames(PEER_KEY, bound, gateway=True)
    connector._frames = SessionFrames(PEER_KEY, bound, gateway=False)
    connector._ws = socket
    await asyncio.wait_for(connector._open(1, "workspace"), timeout=FALLBACK_TIMEOUT_SECS)
    assert [frame["op"] for frame in socket.sent] == ["refused"]
    assert set(seen) == {"local_enabled", "tools_dir", "mkdtemp", "wrap"}
    assert loop_thread not in seen.values(), "slow storage must not hold the gateway loop"
    assert not scratch.exists(), "a failed wrap leaves no scratch"


async def test_the_self_test_wraps_its_probe_off_the_event_loop(tmp_path):
    import threading

    from kiro_crew.connected_computers import confinement

    loop_thread = threading.get_ident()
    seen: list[int] = []

    class _ObservedPlan:
        def wrap(self, argv, **_kw):
            seen.append(threading.get_ident())
            raise confinement.ConfinementUnavailable("observed")

    witnesses = SimpleNamespace(spec={}, scratch=str(tmp_path))
    with pytest.raises(confinement.ConfinementUnavailable, match="observed"):
        await asyncio.wait_for(
            confinement._run_probe(_ObservedPlan(), witnesses, str(tmp_path), 1.0),
            timeout=FALLBACK_TIMEOUT_SECS,
        )
    assert len(seen) == 1 and seen[0] != loop_thread


async def test_the_plan_factory_runs_off_the_event_loop(monkeypatch):
    import threading

    from kiro_crew.connected_computers import controller as controller_mod

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def factory():
        seen.append(threading.get_ident())
        return SimpleNamespace(name="test-plan")

    async def checked(_plan, _root):
        return None

    monkeypatch.setattr(controller_mod, "self_test", checked)
    controller = controller_mod.LocalController(
        SimpleNamespace(), instance_lookup=lambda _id: None, plan_factory=factory
    )
    plan = await asyncio.wait_for(controller._checked_plan(_grant()), FALLBACK_TIMEOUT_SECS)
    assert plan.name == "test-plan"
    assert len(seen) == 1 and seen[0] != loop_thread, "finding bwrap walks PATH"


@pytest.mark.skipif(os.name == "nt", reason="EPIPE on a pipe without a reader is POSIX")
async def test_a_wrap_finished_after_cancellation_closes_its_fds():
    """Cancelling the await does not stop the thread: its fds are closed, never leaked."""
    import threading

    from kiro_crew.connected_computers import confinement
    from kiro_crew.testing.wait import async_wait_until

    started = threading.Event()
    release = threading.Event()
    read_fd, write_fd = os.pipe()

    class _SlowPlan:
        def wrap(self, argv, **_kw):
            started.set()
            release.wait(FALLBACK_TIMEOUT_SECS)
            return confinement.Confined(list(argv), {}, "/", pass_fds=(read_fd,))

    def reader_closed() -> bool:
        # Asked through the write end: a recycled fd number cannot answer for it.
        try:
            os.write(write_fd, b"x")
        except BrokenPipeError:
            return True
        return False

    task = asyncio.create_task(confinement.wrap_off_loop(_SlowPlan(), ["/bin/true"]))
    try:
        await async_wait_until(started.is_set, off_loop=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
        assert not reader_closed(), "still the worker's until its wrap returns"
        release.set()
        await async_wait_until(reader_closed, off_loop=True)
    finally:
        # Never read_fd: the wrap's release owns it, and its number may be reused.
        release.set()
        os.close(write_fd)
