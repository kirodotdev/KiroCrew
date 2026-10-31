"""The derived Tailscale access set: invite or approve only, owner by whois, removal stops work.

These drive the real auth middleware with a :class:`DirectGate` whose ``whois``
is a fake, the real access-set store under the test's data home, and a real
admission service over a ``TaskStore`` in ``tmp_path``. The socket addresses
that put a request on the direct listener are relabelled through
``tailnet_direct._sockaddr``, as in ``test_shared_crew_tailnet_direct``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import shared_crew_config_from
from kiro_crew.dashboard import tailnet, tailnet_direct, tailnet_live_allowlist, tailnet_localapi
from kiro_crew.dashboard.tailnet_direct import DirectGate, SelfNode
from kiro_crew.dashboard.tailnet_localapi import Answer
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import host_power, people, routes_access
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.admission import OperationState
from kiro_crew.shared_crew.containment import ContainmentReport
from kiro_crew.shared_crew.contracts import ActorContext, EnforcementLevel, Provenance, Transport
from kiro_crew.shared_crew.identity import tailnet_principal


@pytest.fixture(autouse=True)
def _shared_crew_preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover the preview's behaviour; the gate itself has its own tests."""
    _floor_monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")


S = OperationState
OWNER_IP = "100.64.0.1"
#: The owner's own phone: another device of the same Tailscale user.
OWNER_PHONE_IP = "100.64.0.3"
PORT = 5476
OWNER = SelfNode(
    addresses=(OWNER_IP,), login="owner@example.com", user_id="11", dns_name="crew.tail1.ts.net"
)


def _whois(login: str, uid: int, name: str, key_char: str, pic: str = "") -> dict[str, Any]:
    return {
        "Node": {"Name": f"{name}.tail1.ts.net.", "Key": "nodekey:" + key_char * 64},
        "UserProfile": {
            "ID": uid,
            "LoginName": login,
            "DisplayName": name.title(),
            "ProfilePicURL": pic,
        },
    }


PEERS: dict[str, dict[str, Any]] = {
    OWNER_PHONE_IP: _whois("owner@example.com", 11, "owner", "a"),
    "100.64.0.5": _whois("alice@example.com", 21, "alice", "b"),
    "100.64.0.7": _whois("mallory@example.com", 31, "mallory", "c", "https://pics.example/m.png"),
    "100.64.0.8": _whois("mallory@example.com", 32, "mallory-other", "e"),
}


def _fake_whois(addr: str) -> Answer:
    body = PEERS.get(addr.rsplit(":", 1)[0].strip("[]"))
    return Answer(body, tailnet_localapi.METHOD_LOCALAPI_UNIX) if body else Answer(None, "none")


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch):
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared._is_restricted_session", lambda *_a: False
    )
    ta._state.clear_all()
    people.reset_for_tests()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    real = tailnet_direct._sockaddr

    def relabelled(request: web.Request, name: str) -> tuple[str, int] | None:
        raw = request.headers.get("X-Test-Local" if name == "sockname" else "X-Test-Peer")
        if raw is None:
            return real(request, name)
        host, _, port = raw.rpartition(":")
        return host.strip("[]"), int(port)

    monkeypatch.setattr(tailnet_direct, "_sockaddr", relabelled)
    yield
    ta._state.clear_all()
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    ag.install_service(None)


def _install(seed: tuple[str, ...] = ()) -> None:
    access_set.install(seed, owner_login=OWNER.login, owner_user_id=OWNER.user_id)


async def _whoami(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    return web.json_response(
        {"user": request.get("user"), "owner": is_owner_dashboard_request(request)}
    )


async def _irreversible(request: web.Request) -> web.Response:
    return web.json_response({"ran": True})


async def _client(seed: tuple[str, ...] = ()) -> TestClient:
    gate = DirectGate(owner=OWNER, port=PORT, allowed_logins=seed, whois_fn=_fake_whois)
    app = web.Application(middlewares=[token_auth_middleware(tailnet_direct=gate)])
    app["state"] = SimpleNamespace(owner_id="", conversation_log=None, _ws_clients=[])
    app.router.add_get("/api/whoami", _whoami)
    app.router.add_delete("/api/shared-crew/tailscale/credential", _irreversible)
    routes_access.register(app)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client


def _direct(ip: str, **extra: str) -> dict[str, str]:
    return {"X-Test-Local": f"{OWNER_IP}:{PORT}", "X-Test-Peer": f"{ip}:50123", **extra}


async def _call(client: TestClient, method: str, path: str, ip: str, **headers: str) -> Any:
    resp = await client.request(method, path, headers=_direct(ip, **headers))
    text = await resp.text()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, text


# -- decision 1: the set is derived, a hand edit admits nobody ---------------- #


def test_derived_set_ignores_a_later_hand_edit_of_allowed_logins() -> None:
    _install(("alice@example.com",))
    assert tailnet.login_allowed("alice@example.com", ()) is True
    # The config list now also names mallory; the set was seeded once and ignores it.
    _install(("alice@example.com", "mallory@example.com"))
    assert tailnet.login_allowed("mallory@example.com", ("mallory@example.com",)) is False
    assert access_set.view()["seeded"] == ["alice@example.com"]


def test_without_a_derived_set_the_config_list_still_decides() -> None:
    assert tailnet.login_allowed("mallory@example.com", ("mallory@example.com",)) is True


def test_unreadable_set_admits_only_the_owner_through_serve() -> None:
    store.root().mkdir(parents=True, exist_ok=True)
    (store.root() / access_set.ACCESS_FILE).write_text("{not json", encoding="utf-8")
    _install(("alice@example.com",))
    assert tailnet.login_allowed(OWNER.login, ()) is True
    assert tailnet.login_allowed("alice@example.com", ("alice@example.com",)) is False


def test_an_accepted_invite_joins_the_set_by_login() -> None:
    _install()
    store.update_state(lambda s: s["members"].update({"u-a": {"login": "alice@example.com"}}))
    access_set.rebuild()
    assert tailnet.login_allowed("alice@example.com", ()) is True


# -- decision 2 and 3: a reachable stranger waits; approve admits by id ------- #


def test_a_failed_save_leaves_the_cache_as_the_disk_is(monkeypatch) -> None:
    _install()

    def _full(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(access_set, "atomic_write", _full)
        with pytest.raises(OSError):
            access_set.acknowledge_first_share("owner")
    assert access_set.view()["first_share_ack"] is False
    access_set.acknowledge_first_share("owner")
    assert access_set.view()["first_share_ack"] is True


# -- decision 4: the owner via whois has owner rights; irreversible confirms -- #


@pytest.mark.asyncio
async def test_owner_via_whois_gets_owner_routes_without_a_token() -> None:
    _install()
    client = await _client()
    try:
        who_status, who = await _call(client, "GET", "/api/whoami", OWNER_PHONE_IP)
        access_status, access = await _call(
            client, "GET", "/api/shared-crew/access", OWNER_PHONE_IP
        )
    finally:
        await client.close()
    assert who_status == 200 and who == {"user": "local-app", "owner": True}
    assert access_status == 200 and access["first_share"]["needs_ack"] is True


@pytest.mark.asyncio
async def test_irreversible_action_by_the_owner_needs_the_confirm_header() -> None:
    _install()
    client = await _client()
    try:
        bare, body = await _call(
            client, "DELETE", "/api/shared-crew/tailscale/credential", OWNER_PHONE_IP
        )
        confirmed, ran = await _call(
            client,
            "DELETE",
            "/api/shared-crew/tailscale/credential",
            OWNER_PHONE_IP,
            **{"X-Kirocrew-Confirm": "irreversible"},
        )
    finally:
        await client.close()
    assert bare == 428 and body["code"] == "confirm_required"
    assert confirmed == 200 and ran == {"ran": True}


# -- decision 5: removal stops the running turn now, capacity after the stop -- #


def _report() -> ContainmentReport:
    return ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_limits=False,
        hard_ready=True,
        slices_enabled=True,
    )


# -- decision 6: the first-share warning, with real counts, once ------------- #


@pytest.mark.asyncio
async def test_first_share_warning_counts_and_is_shown_once(monkeypatch) -> None:
    _install()

    class Log:
        def list_sessions(self) -> list[dict[str, Any]]:
            return [{"key": "dashboard:a"}, {"key": "dashboard:b"}, {"key": "dashboard:c"}]

    class Vector:
        def memory_stats(self) -> dict[str, int]:
            return {"semantic_active": 4, "episodic_active": 6}

    async def vector_store(_state: Any) -> Vector:
        return Vector()

    monkeypatch.setattr("kiro_crew.dashboard.handlers.memory._get_vector_store_async", vector_store)
    monkeypatch.setattr("kiro_crew.shared_crew.routes_identity._machine_only", lambda _k: False)
    client = await _client()
    client.server.app["state"].conversation_log = Log()
    try:
        _, before = await _call(client, "GET", "/api/shared-crew/access", OWNER_PHONE_IP)
        await _call(client, "POST", "/api/shared-crew/access/first-share/ack", OWNER_PHONE_IP)
        _, after = await _call(client, "GET", "/api/shared-crew/access", OWNER_PHONE_IP)
    finally:
        await client.close()
    share = before["first_share"]
    assert (share["needs_ack"], share["sessions"], share["memory_items"]) == (True, 3, 10)
    assert after["first_share"]["needs_ack"] is False
    access_set.reset_for_tests()
    assert access_set.first_share_acknowledged() is True  # remembered on disk


def test_first_share_warning_is_not_needed_once_someone_else_has_access() -> None:
    _install(("alice@example.com",))
    assert access_set.nobody_else_has_access() is False
    access_set.require_first_share_ack()  # does not raise


# -- decision 7: laptop detection and keep-awake ----------------------------- #


def test_linux_laptop_is_a_battery_that_is_not_a_peripheral(tmp_path) -> None:
    def supply(name: str, kind: str, scope: str = "") -> None:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "type").write_text(kind + "\n", encoding="utf-8")
        if scope:
            (folder / "scope").write_text(scope + "\n", encoding="utf-8")

    supply("AC", "Mains")
    supply("hidpp_battery_0", "Battery", "Device")
    desk = host_power.detect_laptop(system="linux", power_supply_root=tmp_path)
    assert desk == host_power.LaptopInfo(False, host_power.REASON_NO_BATTERY)
    supply("BAT0", "Battery", "System")
    laptop = host_power.detect_laptop(system="linux", power_supply_root=tmp_path)
    assert laptop == host_power.LaptopInfo(True, host_power.REASON_BATTERY)
    missing = host_power.detect_laptop(system="linux", power_supply_root=tmp_path / "none")
    assert missing.reason == host_power.REASON_UNKNOWN and not missing.laptop


def test_macos_and_windows_laptop_detection(monkeypatch) -> None:
    monkeypatch.setattr(host_power.os.path, "isfile", lambda p: p == host_power.PMSET_PATH)
    monkeypatch.setattr(host_power.os, "access", lambda p, mode: True)
    calls: list[list[str]] = []

    def pmset(argv: list[str], **kw: Any) -> Any:
        calls.append(argv)
        return SimpleNamespace(stdout=" -InternalBattery-0 (id=1)\t95%; charging\n")

    assert host_power.detect_laptop(system="darwin", run=pmset).laptop is True
    assert calls == [["/usr/bin/pmset", "-g", "batt"]]
    assert (
        host_power.detect_laptop(
            system="darwin", run=lambda *a, **k: SimpleNamespace(stdout="AC Power\n")
        ).laptop
        is False
    )
    assert host_power.detect_laptop(system="windows", windows_probe=lambda: True).laptop is True
    unknown = host_power.detect_laptop(system="windows", windows_probe=lambda: None)
    assert unknown.reason == host_power.REASON_UNKNOWN


def test_keep_awake_capability_per_platform(monkeypatch) -> None:
    from kiro_crew import power

    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: paths[0])
    assert host_power.keep_awake_capability(system="darwin")["backend"] == "caffeinate"
    assert host_power.keep_awake_capability(system="linux")["state"] == "available"
    assert host_power.keep_awake_capability(system="windows")["state"] == "available"
    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: None)
    linux = host_power.keep_awake_capability(system="linux")
    assert (linux["state"], linux["reason"]) == ("unavailable", "no_backend")
    assert host_power.keep_awake_capability(system="other")["reason"] == "unsupported_platform"


@pytest.mark.parametrize("platform_name", ["macos", "linux", "windows"])
def test_keep_awake_starts_while_a_collaborator_is_here_and_stops_when_idle(
    monkeypatch, platform_name
) -> None:
    from kiro_crew import platform_compat, power

    _install()
    for flag in ("IS_MACOS", "IS_LINUX", "IS_WINDOWS"):
        monkeypatch.setattr(platform_compat, flag, flag == f"IS_{platform_name.upper()}")
    monkeypatch.setattr(platform_compat, "IS_POSIX", platform_name != "windows")
    monkeypatch.setattr(power, "_resolve_trusted", lambda paths: paths[0])
    spawned: list[list[str]] = []
    killed: list[int] = []
    windows: list[bool] = []

    class Proc:
        pid = 4321

        def poll(self) -> None:
            return None

    def popen(argv: list[str], **kw: Any) -> Proc:
        spawned.append(argv)
        return Proc()

    monkeypatch.setattr(power.subprocess, "Popen", popen)
    monkeypatch.setattr(platform_compat, "kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(
        power, "_set_windows_execution_state", lambda keep_awake: windows.append(keep_awake) or True
    )

    class _Sock(dict):
        __hash__ = object.__hash__  # type: ignore[assignment]
        closed = False

    def _sock(login: str) -> Any:
        actor = ActorContext(
            principal=tailnet_principal(login, "", Provenance.TAILSCALE_LISTENER),
            request_id="r",
            transport=Transport.WEBSOCKET,
        )
        return _Sock({"_shared_crew_actor": actor, "_is_dashboard_user": True})

    collaborator = _sock("alice@example.com")
    owner_socket = _sock("owner@example.com")
    state = SimpleNamespace(_ws_clients=[owner_socket], _owner_ws_clients={owner_socket})
    inhibitor = power.SleepInhibitor()
    inhibitor.set_active(host_power.shared_use_wants_awake(state))
    assert not inhibitor.active  # the owner alone does not keep the host awake
    state._ws_clients.append(collaborator)
    inhibitor.set_active(host_power.shared_use_wants_awake(state))
    assert inhibitor.active
    state._ws_clients.remove(collaborator)
    inhibitor.set_active(host_power.shared_use_wants_awake(state))
    assert not inhibitor.active
    if platform_name == "macos":
        assert spawned[0][1:3] == ["-i", "-w"] and killed == [4321]
    elif platform_name == "linux":
        assert "--what=idle:sleep" in spawned[0] and killed == [4321]
    else:
        assert windows == [True, False] and spawned == []


def test_capabilities_report_keep_awake(monkeypatch) -> None:
    from kiro_crew.shared_crew.capabilities import aggregate_capabilities

    monkeypatch.setattr(
        host_power, "detect_laptop", lambda: host_power.LaptopInfo(True, "battery_present")
    )
    monkeypatch.setattr(
        host_power,
        "keep_awake_capability",
        lambda: {"state": "unavailable", "reason": "no_backend", "backend": "systemd-inhibit"},
    )
    host_power.note_host()
    body = aggregate_capabilities().to_dict()["keep_awake"]
    assert body == {
        "state": "unavailable",
        "reason": "no_backend",
        "backend": "systemd-inhibit",
        "laptop": True,
        "laptop_reason": "battery_present",
    }


# -- network trust: dashboard.shared_crew.local_authorization off ------------- #

TAGGED_IP = "100.64.0.9"


def _tagged_whois() -> dict[str, Any]:
    return {
        "Node": {"Name": "ci.tail1.ts.net.", "Key": "nodekey:" + "f" * 64, "Tags": ["tag:ci"]},
        "UserProfile": {"ID": 99, "LoginName": "tagged-devices", "DisplayName": "Tagged"},
    }


def test_the_listener_installs_the_set() -> None:
    from kiro_crew.dashboard import tailnet_direct_listener

    cfg = SimpleNamespace(
        degraded_sections=set(),
        dashboard=SimpleNamespace(
            tailscale=SimpleNamespace(allowed_logins=[]),
            shared_crew=shared_crew_config_from({}),
        ),
    )
    tailnet_direct_listener._install_access_set(cfg)  # type: ignore[arg-type]
    assert tailnet_live_allowlist.snapshot() is not None


@pytest.mark.asyncio
async def test_any_person_tailscale_names_is_admitted_on_the_direct_link() -> None:
    """Tailscale decides who reaches this machine; Crew approves nobody itself."""
    _install()
    client = await _client()
    try:
        status, me = await _call(client, "GET", "/api/whoami", "100.64.0.7")
    finally:
        await client.close()
    assert status == 200 and me == {"user": "tailnet:uid:31", "owner": False}
    # The tailscale serve path still admits only the owner, accepted invites and the seed.
    assert tailnet.login_allowed("mallory@example.com", ()) is False


@pytest.mark.asyncio
async def test_the_direct_link_still_refuses_tagged_nodes_and_forwarded_headers(
    monkeypatch,
) -> None:
    monkeypatch.setitem(PEERS, TAGGED_IP, _tagged_whois())
    _install()
    client = await _client()
    try:
        tagged, tagged_body = await _call(client, "GET", "/api/whoami", TAGGED_IP)
        forwarded, fwd_body = await _call(
            client, "GET", "/api/whoami", "100.64.0.7", **{"X-Forwarded-For": "100.64.0.5"}
        )
    finally:
        await client.close()
    assert tagged == 403 and tagged_body["reason"] == "tagged_node_not_allowed"
    assert forwarded == 403 and fwd_body["reason"] == "forwarded_header"
    assert tailnet.login_allowed("tagged-devices", ()) is False
