"""The derived Tailscale access set: invite or approve only, owner by whois, removal stops work.

These drive the real auth middleware with a :class:`DirectGate` whose ``whois``
is a fake, the real access-set store under the test's data home, and a real
admission service over a ``TaskStore`` in ``tmp_path``. The socket addresses
that put a request on the direct listener are relabelled through
``tailnet_direct._sockaddr``, as in ``test_shared_crew_tailnet_direct``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.dashboard import tailnet, tailnet_direct, tailnet_live_allowlist, tailnet_localapi
from kiro_crew.dashboard.tailnet_direct import DirectGate, SelfNode
from kiro_crew.dashboard.tailnet_localapi import Answer
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import host_power, people, routes_access
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import ActorContext, EnforcementLevel, Provenance, Transport
from kiro_crew.shared_crew.identity import tailnet_principal
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

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


def _peer(ip: str) -> Any:
    peer = tailnet_direct.parse_whois(PEERS[ip])
    assert peer is not None
    return peer


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


def test_unreadable_set_admits_only_the_owner() -> None:
    store.root().mkdir(parents=True, exist_ok=True)
    (store.root() / access_set.ACCESS_FILE).write_text("{not json", encoding="utf-8")
    _install(("alice@example.com",))
    owner = tailnet_direct.admission_refusal(_peer(OWNER_PHONE_IP), allowed_logins=(), owner=OWNER)
    alice = tailnet_direct.admission_refusal(_peer("100.64.0.5"), allowed_logins=(), owner=OWNER)
    assert owner is None and alice == "allowlist_unreadable"


def test_an_accepted_invite_joins_the_set_by_login() -> None:
    _install()
    store.update_state(lambda s: s["members"].update({"u-a": {"login": "alice@example.com"}}))
    access_set.rebuild()
    assert tailnet.login_allowed("alice@example.com", ()) is True


# -- decision 2 and 3: a reachable stranger waits; approve admits by id ------- #


@pytest.mark.asyncio
async def test_acl_reachable_stranger_is_refused_and_listed_as_waiting() -> None:
    _install()
    client = await _client()
    try:
        status, body = await _call(client, "GET", "/api/whoami", "100.64.0.7")
        page_status, page = await _call(
            client, "GET", "/api/whoami", "100.64.0.7", Accept="text/html"
        )
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "awaiting_approval"
    assert page_status == 403 and "Waiting for the owner to approve you" in page
    waiting = access_set.view()["waiting"]
    assert [(w["user_id"], w["login"], w["avatar_url"]) for w in waiting] == [
        ("31", "mallory@example.com", "https://pics.example/m.png")
    ]
    assert waiting[0]["first_seen"] > 0


@pytest.mark.asyncio
async def test_approve_admits_by_stable_id_only() -> None:
    _install()
    client = await _client()
    try:
        await _call(client, "GET", "/api/whoami", "100.64.0.7")
        refused, body = await _call(
            client, "POST", "/api/shared-crew/access/waiting/31/approve", OWNER_PHONE_IP
        )
        assert refused == 409 and body["code"] == "first_share_ack_required"
        ack, _ = await _call(
            client, "POST", "/api/shared-crew/access/first-share/ack", OWNER_PHONE_IP
        )
        ok, _ = await _call(
            client, "POST", "/api/shared-crew/access/waiting/31/approve", OWNER_PHONE_IP
        )
        admitted, me = await _call(client, "GET", "/api/whoami", "100.64.0.7")
        same_login, _ = await _call(client, "GET", "/api/whoami", "100.64.0.8")
        teammate_owner_route, _ = await _call(
            client, "GET", "/api/shared-crew/access", "100.64.0.7"
        )
    finally:
        await client.close()
    assert (ack, ok, admitted) == (200, 200, 200)
    assert me == {"user": "tailnet:uid:31", "owner": False}
    # Another Tailscale user with the same login is a different person.
    assert same_login == 403
    assert teammate_owner_route == 403
    assert [a["user_id"] for a in access_set.view()["approved"]] == ["31"]


def test_a_failed_save_leaves_the_cache_as_the_disk_is(monkeypatch) -> None:
    _install()
    assert access_set.note_waiting(_peer("100.64.0.7")) is True
    access_set.acknowledge_first_share("owner")

    def _full(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(access_set, "atomic_write", _full)
        with pytest.raises(OSError):
            access_set.approve("31", "owner")
        assert "31" not in access_set.rebuild().approved_user_ids
    assert [w["user_id"] for w in access_set.view()["waiting"]] == ["31"]
    access_set.approve("31", "owner")
    assert [a["user_id"] for a in access_set.view()["approved"]] == ["31"]


def test_dismiss_drops_the_row_and_does_not_relist_for_a_day() -> None:
    _install()
    clock = [1000.0]
    access_set.clock = lambda: clock[0]
    assert access_set.note_waiting(_peer("100.64.0.7")) is True
    access_set.dismiss("31", "owner")
    assert access_set.view()["waiting"] == []
    clock[0] += 120
    assert access_set.note_waiting(_peer("100.64.0.7")) is False
    clock[0] += access_set.DISMISS_SECS
    assert access_set.note_waiting(_peer("100.64.0.7")) is True


def test_waiting_list_is_bounded_and_rate_limited() -> None:
    _install()
    clock = [1000.0]
    access_set.clock = lambda: clock[0]

    def stranger(n: int) -> Any:
        return tailnet_direct.DirectPeer(
            user_id=str(100 + n), login=f"p{n}@example.com", display_name="", node="n", node_key=""
        )

    burst = [access_set.note_waiting(stranger(n)) for n in range(12)]
    assert burst.count(True) == access_set.NEW_WAITING_PER_MINUTE
    for n in range(12, 80):
        clock[0] += 7.0
        access_set.note_waiting(stranger(n))
    waiting = access_set.view()["waiting"]
    assert len(waiting) == access_set.MAX_WAITING
    # The oldest rows went first.
    assert "100" not in {w["user_id"] for w in waiting}
    assert "179" in {w["user_id"] for w in waiting}
    # The refresh stamps are bounded by the same list: evicted people leave no stamp.
    assert set(access_set._TOUCHED) == {w["user_id"] for w in waiting}


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


def _service(tmp_path: Path) -> ag.AdmissionService:
    clock = ManualClock(start=10_000.0)
    work = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    limits = effective_limits(
        shared_crew_config_from(
            {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 3}
        ),
        auto_crew_concurrency=8,
    )
    runner = lambda cmd: subprocess.CompletedProcess(cmd, 0, "", "")  # noqa: E731
    return ag.AdmissionService(
        work,
        limits=limits,
        clock=clock.time,
        containment_probe=_report,
        slices=SliceManager(runner=runner, systemctl="/usr/bin/systemctl"),
        liveness=lambda ref: Liveness.STOPPED,
    )


@pytest.mark.asyncio
async def test_removing_a_person_stops_their_running_turn_and_frees_capacity_after_stop(
    tmp_path,
) -> None:
    _install()
    access_set.acknowledge_first_share("owner")
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    pid = tailnet_principal("mallory@example.com", "", Provenance.TAILSCALE_DIRECT).id
    svc = _service(tmp_path)
    ag.install_service(svc)
    running = await svc.admit(
        budget_key=pid,
        session_key="s1",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="k-run-000001",
        digest="a",
    )
    queued = await svc.admit(
        budget_key=pid,
        session_key="s2",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="k-queue-00001",
        digest="b",
    )
    assert running.admitted and queued.queued
    started, runtime_stopping, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def turn() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            runtime_stopping.set()
            await stopped.wait()  # the runtime takes a moment to stop
            raise
        return "never"

    task = asyncio.ensure_future(running.run(turn()))
    await asyncio.wait_for(started.wait(), 30)
    closed: list[tuple[frozenset[str], str]] = []

    async def closer(ids: frozenset[str], login: str) -> int:
        closed.append((ids, login))
        return 1

    access_set.socket_closer = closer
    result = await access_set.remove_approval("31", "owner")
    assert result["stopped_running"] == 1 and result["cancelled_queued"] == 1
    assert result["closed_connections"] == 1 and pid in closed[0][0]
    await asyncio.wait_for(runtime_stopping.wait(), 30)
    # Stopping, not stopped: the slot is still held until the runtime is gone.
    assert svc.live_work(running.work.work_id).state is S.CANCELLING
    assert svc._scheduler.is_active(running.work.work_id)
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    final = await svc.current_work(running.work.work_id)
    assert final.state is S.CANCELLED and final.reason is ReasonCode.ACCESS_REMOVED
    assert not svc._scheduler.is_active(running.work.work_id)
    gone = await svc.current_work(queued.work.work_id)
    assert gone.state is S.CANCELLED and gone.reason is ReasonCode.ACCESS_REMOVED
    # Refused from the next request, and any work of theirs still queued elsewhere.
    assert (
        tailnet_direct.admission_refusal(_peer("100.64.0.7"), allowed_logins=(), owner=OWNER)
        == "login_not_allowed"
    )
    assert access_set.principal_removed(pid)


@pytest.mark.asyncio
async def test_removing_a_person_stops_their_turn_and_queue_with_admission_off(
    monkeypatch,
) -> None:
    """Fair admission off (the default): no admitted record exists, the work still stops."""
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.shared_crew import turn_gate

    _install()
    access_set.acknowledge_first_share("owner")
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    mallory = tailnet_principal("mallory@example.com", "", Provenance.TAILSCALE_LISTENER)
    alice = tailnet_principal("alice@example.com", "", Provenance.TAILSCALE_LISTENER)
    ag.install_service(None)
    assert not await turn_gate.admission_on()

    running_slot, queue_slot = _ChatSlot("s1"), _ChatSlot("s2")
    queue_slot.queue_append("from mallory", meta={"author": mallory.to_public_dict()})
    queue_slot.queue_append("from alice", meta={"author": alice.to_public_dict()})
    state = SimpleNamespace(
        _slots={"s1": running_slot, "s2": queue_slot},
        _ws_clients=[],
        broadcast_ws=lambda *_a, **_k: None,
        push_slots_update=lambda: None,
    )
    app = web.Application()
    app["state"] = state
    await routes_access._install_closer(app)

    started = asyncio.Event()

    async def turn() -> None:
        started.set()
        await asyncio.Event().wait()

    with turn_gate.initiated_by(mallory.id):
        task = asyncio.ensure_future(turn_gate.gate_turn(state, running_slot, "hi", {}, turn))
    await asyncio.wait_for(started.wait(), 30)
    assert turn_gate.unadmitted_slots({mallory.id}) == ["s1"]

    stops: list[str] = []

    async def fake_stop(_state: Any, slot: Any, **_kw: Any) -> dict[str, Any]:
        stops.append(slot.key)
        task.cancel()
        return {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", fake_stop)
    monkeypatch.setattr(_ChatSlot, "running", property(lambda self: self.key == "s1"))
    result = await access_set.remove_approval("31", "owner")

    assert result["stopped_running"] == 1 and result["cancelled_queued"] == 1
    assert stops == ["s1"]
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [q["content"] for q in queue_slot._queue] == ["from alice"]
    assert turn_gate.unadmitted_slots({mallory.id}) == []


@pytest.mark.asyncio
async def test_a_removed_person_with_an_accepted_invite_stays_out_after_restart() -> None:
    from kiro_crew.shared_crew import tailscale_invites
    from kiro_crew.shared_crew.tailscale_api import AcceptedBy, DeviceInvite

    _install()
    access_set.acknowledge_first_share("owner")
    member = {"user_id": "31", "login": "mallory@example.com", "invite_id": "inv-1"}
    store.update_state(lambda s: s["members"].update({"31": member}))
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    await access_set.remove_approval("31", "owner")

    tailnet_live_allowlist.reset_for_tests()
    _install()
    assert tailnet.login_allowed("mallory@example.com", ()) is False

    def accepted(invite_id: str) -> DeviceInvite:
        by = AcceptedBy(user_id="31", login="mallory@example.com")
        return DeviceInvite(invite_id, "d", "", "", False, True, by)

    state = store.load_state()
    # The invite they already accepted does not bring them back; a new one does.
    assert tailscale_invites._new_accepts(state, [accepted("inv-1")]) == []
    assert len(tailscale_invites._new_accepts(state, [accepted("inv-2")])) == 1


def test_withdrawing_an_approval_ends_admission_by_user_id() -> None:
    _install()
    access_set.acknowledge_first_share("owner")
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    assert "31" in {a["user_id"] for a in access_set.view()["approved"]}
    access_set.withdraw_approval("31")
    assert "31" not in {a["user_id"] for a in access_set.view()["approved"]}
    assert tailnet_live_allowlist.user_id_revoked("31") is True


@pytest.mark.asyncio
async def test_a_failed_invite_store_write_leaves_the_removal_retryable(monkeypatch) -> None:
    _install()
    access_set.acknowledge_first_share("owner")
    member = {"user_id": "31", "login": "mallory@example.com", "invite_id": "inv-1"}
    store.update_state(lambda s: s["members"].update({"31": member}))
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    real_update = store.update_state

    def disk_full(_fn: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "update_state", disk_full)
    with pytest.raises(OSError):
        await access_set.remove_approval("31", "owner")
    monkeypatch.setattr(store, "update_state", real_update)
    result = await access_set.remove_approval("31", "owner")
    assert result["removed"] == "31"
    assert store.load_state()["members"]["31"]["removed"] is True


@pytest.mark.asyncio
async def test_removing_a_person_stops_their_thread_reply_with_admission_off() -> None:
    from kiro_crew.shared_crew import turn_gate

    _install()
    access_set.acknowledge_first_share("owner")
    assert access_set.note_waiting(_peer("100.64.0.7"))
    access_set.approve("31", "owner")
    mallory = tailnet_principal("mallory@example.com", "", Provenance.TAILSCALE_LISTENER)
    ag.install_service(None)
    state = SimpleNamespace(
        _slots={},
        _ws_clients=[],
        broadcast_ws=lambda *_a, **_k: None,
        push_slots_update=lambda: None,
    )
    app = web.Application()
    app["state"] = state
    await routes_access._install_closer(app)

    started = asyncio.Event()

    async def reply() -> None:
        started.set()
        await asyncio.Event().wait()

    with turn_gate.initiated_by(mallory.id):
        task = asyncio.ensure_future(turn_gate.run_admitted(reply(), session_key="thread:s1:m1"))
    await asyncio.wait_for(started.wait(), 30)
    result = await access_set.remove_approval("31", "owner")

    assert result["stopped_running"] == 1
    with pytest.raises(asyncio.CancelledError):
        await task
    assert turn_gate.unadmitted_slots({mallory.id}) == []


@pytest.mark.asyncio
async def test_a_removed_persons_thread_reply_does_not_start_with_admission_off() -> None:
    from kiro_crew.shared_crew import turn_gate

    ag.install_service(None)
    pid = tailnet_principal("mallory@example.com", "", Provenance.TAILSCALE_DIRECT).id
    access_set._note_removed(frozenset({pid}))
    ran: list[bool] = []
    told: list[object] = []

    async def reply() -> None:
        ran.append(True)

    with turn_gate.initiated_by(pid):
        await turn_gate.run_admitted(reply(), session_key="thread:s1:m1", refused=told.append)
    assert ran == [] and told == [ReasonCode.ACCESS_REMOVED]


@pytest.mark.asyncio
async def test_a_removed_persons_drained_turn_does_not_start_with_admission_off() -> None:
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.shared_crew import turn_gate

    ag.install_service(None)
    pid = tailnet_principal("mallory@example.com", "", Provenance.TAILSCALE_DIRECT).id
    access_set._note_removed(frozenset({pid}))
    slot = _ChatSlot("s1")
    ran: list[bool] = []

    async def turn() -> None:
        ran.append(True)

    with turn_gate.initiated_by(pid):
        await turn_gate.gate_turn(SimpleNamespace(), slot, "queued", {}, turn)
    assert ran == []
    assert "no longer has access" in slot.messages[-1]["content"]


@pytest.mark.asyncio
async def test_finished_work_is_kept_when_its_person_is_removed(tmp_path) -> None:
    pid = tailnet_principal("a@example.com", "", Provenance.TAILSCALE_SERVE).id
    svc = _service(tmp_path)
    adm = await svc.admit(
        budget_key=pid,
        session_key="s1",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="k-done-000001",
        digest="a",
    )
    assert await adm.run(asyncio.sleep(0, result="ok")) == "ok"
    from kiro_crew.shared_crew.admission_revoke import stop_principals

    assert await stop_principals(svc, frozenset({pid})) == {"queued": 0, "running": 0}
    assert (await svc.current_work(adm.work.work_id)).state is S.COMPLETED


@pytest.mark.asyncio
async def test_close_sockets_closes_the_removed_persons_direct_and_serve_sockets() -> None:
    class Sock(dict):
        def __init__(self, principal: Any) -> None:
            super().__init__(
                _shared_crew_actor=ActorContext(
                    principal=principal, request_id="r", transport=Transport.WEBSOCKET
                )
            )
            self.closed = False

        async def close(self, **_kw: Any) -> None:
            self.closed = True

    direct = Sock(tailnet_principal("ann@example.com", "phone", Provenance.TAILSCALE_DIRECT))
    serve = Sock(tailnet_principal("ann@example.com", "", Provenance.TAILSCALE_SERVE))
    bob = Sock(tailnet_principal("bob@example.com", "", Provenance.TAILSCALE_DIRECT))
    app = web.Application()
    app["state"] = SimpleNamespace(_ws_clients=[direct, serve, bob])
    ids = access_set.principal_ids_for("ann@example.com", frozenset({"44"}))
    assert await routes_access.close_sockets(app, ids, "ann@example.com") == 2
    assert direct.closed and serve.closed and not bob.closed


@pytest.mark.asyncio
async def test_close_sockets_also_closes_dashboard_sockets_no_person_was_named_for() -> None:
    """A forwarded socket the daemon could not attribute may be the removed person's."""
    from kiro_crew.shared_crew.contracts import Principal
    from kiro_crew.shared_crew.identity import local_operator_principal

    class Sock(dict):
        def __init__(self, principal: Any, *, dashboard: bool) -> None:
            super().__init__(
                _shared_crew_actor=ActorContext(
                    principal=principal, request_id="r", transport=Transport.WEBSOCKET
                ),
                _is_dashboard_user=dashboard,
            )
            self.closed = False

        async def close(self, **_kw: Any) -> None:
            self.closed = True

    unnamed = Sock(Principal.unknown(), dashboard=True)
    app_socket = Sock(Principal.unknown(), dashboard=False)
    local = Sock(local_operator_principal("owner"), dashboard=True)
    app = web.Application()
    app["state"] = SimpleNamespace(_ws_clients=[unnamed, app_socket, local])
    ids = access_set.principal_ids_for("ann@example.com", frozenset({"44"}))
    assert await routes_access.close_sockets(app, ids, "ann@example.com") == 1
    assert unnamed.closed and not app_socket.closed and not local.closed


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
    collaborator = SimpleNamespace(
        closed=False,
        get=lambda key: ActorContext(
            principal=tailnet_principal("alice@example.com", "", Provenance.TAILSCALE_DIRECT),
            request_id="r",
            transport=Transport.WEBSOCKET,
        ),
    )
    owner_socket = SimpleNamespace(
        closed=False,
        get=lambda key: ActorContext(
            principal=tailnet_principal("owner@example.com", "", Provenance.TAILSCALE_DIRECT),
            request_id="r",
            transport=Transport.WEBSOCKET,
        ),
    )
    state = SimpleNamespace(_ws_clients=[owner_socket])
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


def _install_network_trust() -> None:
    access_set.install(
        (), owner_login=OWNER.login, owner_user_id=OWNER.user_id, local_authorization=False
    )


def test_local_authorization_defaults_off_and_only_literal_false_turns_it_off() -> None:
    assert shared_crew_config_from({}).local_authorization is False
    assert shared_crew_config_from({"local_authorization": False}).local_authorization is False
    assert shared_crew_config_from({"local_authorization": True}).local_authorization is True
    assert shared_crew_config_from({"local_authorization": "no"}).local_authorization is True


def test_the_listener_installs_the_set_with_the_configured_mode() -> None:
    from kiro_crew.dashboard import tailnet_direct_listener

    cfg = SimpleNamespace(
        degraded_sections=set(),
        dashboard=SimpleNamespace(
            tailscale=SimpleNamespace(allowed_logins=[]),
            shared_crew=shared_crew_config_from({}),
        ),
    )
    tailnet_direct_listener._install_access_set(cfg)  # type: ignore[arg-type]
    assert access_set.local_authorization() is False
    snap = tailnet_live_allowlist.snapshot()
    assert snap is not None and snap.network_trust is True


@pytest.mark.asyncio
async def test_an_unapproved_tailnet_peer_is_admitted_by_default_and_never_waits() -> None:
    _install_network_trust()
    client = await _client()
    try:
        status, me = await _call(client, "GET", "/api/whoami", "100.64.0.7")
    finally:
        await client.close()
    assert status == 200 and me == {"user": "tailnet:uid:31", "owner": False}
    assert access_set.view()["waiting"] == []
    # Network trust widens only the direct listener: the tailscale serve path
    # still admits only the owner, accepted invites, approvals and the seed.
    assert tailnet.login_allowed("mallory@example.com", ()) is False


@pytest.mark.asyncio
async def test_the_same_peer_is_refused_with_local_authorization_on() -> None:
    access_set.install(
        (), owner_login=OWNER.login, owner_user_id=OWNER.user_id, local_authorization=True
    )
    client = await _client()
    try:
        status, body = await _call(client, "GET", "/api/whoami", "100.64.0.7")
    finally:
        await client.close()
    assert status == 403 and body["reason"] == "awaiting_approval"
    assert tailnet.login_allowed("mallory@example.com", ()) is False


@pytest.mark.asyncio
async def test_network_trust_still_refuses_tagged_nodes_and_forwarded_headers(
    monkeypatch,
) -> None:
    monkeypatch.setitem(PEERS, TAGGED_IP, _tagged_whois())
    _install_network_trust()
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


@pytest.mark.asyncio
async def test_approve_and_remove_answer_local_authorization_off() -> None:
    _install_network_trust()
    client = await _client()
    try:
        _, view = await _call(client, "GET", "/api/shared-crew/access", OWNER_PHONE_IP)
        approve, body = await _call(
            client, "POST", "/api/shared-crew/access/waiting/31/approve", OWNER_PHONE_IP
        )
        remove, _ = await _call(
            client, "DELETE", "/api/shared-crew/access/approved/31", OWNER_PHONE_IP
        )
    finally:
        await client.close()
    assert view["local_authorization"] is False
    assert approve == 409 and body["code"] == "local_authorization_off"
    assert remove == 409
