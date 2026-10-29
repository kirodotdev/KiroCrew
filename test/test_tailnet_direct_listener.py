"""The direct tailnet listener resolves and starts without ever raising, and binds only when on.

The daemon, the gate resolver and the site binder are fakes; what is real is the
module's own decision of what to record and when to bind.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import tailnet, tailnet_direct
from kiro_crew.dashboard import tailnet_direct_listener as listener
from kiro_crew.dashboard import tailnet_live_allowlist
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.tailnet_direct import DirectGate, DirectResolution, SelfNode
from kiro_crew.shared_crew import access, access_set

PORT = 5476
OWNER = SelfNode(
    addresses=("100.64.0.1", "fd7a:115c:a1e0::1"),
    login="owner@example.com",
    user_id="11",
    dns_name="crew.tail1234.ts.net",
)


def _cfg(direct: bool, *, tailscale: bool = False) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    shared = dataclasses.replace(cfg.dashboard.shared_crew, tailnet_direct=direct)
    ts = dataclasses.replace(cfg.dashboard.tailscale, enabled=tailscale)
    dashboard = dataclasses.replace(cfg.dashboard, shared_crew=shared, tailscale=ts)
    return dataclasses.replace(cfg, dashboard=dashboard)


@pytest.fixture(autouse=True)
def _no_daemon(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _floor_monkeypatch.setattr(
        tailnet_direct.tailnet_localapi,
        "status",
        lambda: tailnet_direct.tailnet_localapi.Answer(None, "none"),
    )
    _floor_monkeypatch.setattr(access_set, "install", lambda *_a, **_kw: None)
    _floor_monkeypatch.setattr(access_set, "set_owner", lambda *_a: None)
    previous = access.tailnet_direct()
    listener._PENDING_INSTALL.clear()
    yield
    # A test that prepared without finishing must not leave a pending install behind.
    listener._PENDING_INSTALL.clear()
    access.note_tailnet_direct(previous)


@pytest.mark.asyncio
async def test_the_default_config_never_asks_the_daemon_or_installs_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", pytest.fail)
    monkeypatch.setattr(access_set, "install", pytest.fail)
    resolution = await listener.resolve(KiroCrewConfig(), PORT, True)
    assert (resolution.state, resolution.reason) == (
        tailnet_direct.STATE_OFF,
        tailnet_direct.REASON_DISABLED,
    )


@pytest.mark.asyncio
async def test_tailscale_on_with_the_listener_off_still_learns_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners: list[tuple[str, str]] = []
    monkeypatch.setattr(access_set, "set_owner", lambda login, uid: owners.append((login, uid)))
    monkeypatch.setattr(tailnet_direct, "parse_status", lambda _data: OWNER)
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: object())
    resolution = await listener.resolve(_cfg(False, tailscale=True), PORT, True)
    assert (resolution.state, resolution.reason) == (
        tailnet_direct.STATE_OFF,
        tailnet_direct.REASON_DISABLED,
    )
    assert resolution.gate is None
    # The boot path never waits on the daemon: the owner is asked after the bind.
    assert owners == []
    assert await listener.start(web.Application(), object(), PORT, resolution) == []  # type: ignore[arg-type]
    assert owners == [("owner@example.com", "11")]


@pytest.mark.asyncio
async def test_the_store_is_read_after_the_bind_and_a_placeholder_holds_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(access_set, "install", lambda *_a, **_kw: calls.append("install"))
    monkeypatch.setattr(
        access_set, "install_placeholder", lambda **_kw: calls.append("placeholder")
    )
    resolution = await listener.resolve(_cfg(False, tailscale=True), PORT, True)
    assert calls == ["placeholder"]
    await listener.start(web.Application(), object(), PORT, resolution)  # type: ignore[arg-type]
    assert calls == ["placeholder", "install"]


@pytest.mark.asyncio
async def test_start_skips_the_owner_lookup_when_no_tailnet_path_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", pytest.fail)
    monkeypatch.setattr(tailnet_live_allowlist, "snapshot", lambda: None)
    off = DirectResolution(tailnet_direct.STATE_OFF, tailnet_direct.REASON_DISABLED)
    before = access.tailnet_direct()
    assert await listener.start(web.Application(), object(), PORT, off) == []  # type: ignore[arg-type]
    # An off gate binds and records nothing: ``resolve`` already recorded it.
    assert access.tailnet_direct() == before


@pytest.mark.asyncio
async def test_an_access_set_that_cannot_install_does_not_stop_the_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(access_set, "install", broken)
    resolution = await listener.resolve(_cfg(False, tailscale=True), PORT, True)
    assert resolution.reason == tailnet_direct.REASON_DISABLED


@pytest.mark.asyncio
async def test_on_hands_the_config_to_the_gate_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    gate = DirectGate(owner=OWNER, port=PORT)

    def fake_resolve(**kw: Any) -> DirectResolution:
        seen.update(kw)
        return DirectResolution(tailnet_direct.STATE_ACTIVE, gate=gate)

    monkeypatch.setattr(tailnet_direct, "resolve", fake_resolve)
    monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda **_kw: False)
    resolution = await listener.resolve(_cfg(True), PORT, True)
    assert resolution.state == tailnet_direct.STATE_ACTIVE
    assert seen == {
        "port": PORT,
        "allowed_logins": (),
        "identity_unknown": False,
        "governance_pinned": False,
    }
    assert access.tailnet_direct().owner_login == "owner@example.com"


@pytest.mark.asyncio
async def test_a_resolver_that_raises_reports_tailscale_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_a: Any) -> DirectResolution:
        raise RuntimeError("daemon gone")

    monkeypatch.setattr(listener, "_resolve_blocking", broken)
    resolution = await listener.resolve(_cfg(True), PORT, True)
    assert (resolution.state, resolution.reason) == (
        tailnet_direct.STATE_UNAVAILABLE,
        tailnet_direct.REASON_TAILSCALE_NOT_RUNNING,
    )


def test_prepare_at_start_records_the_tailnet_path_and_reads_no_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the bind nothing waits on the daemon, even with the listener on."""
    noted: list[dict[str, Any]] = []
    monkeypatch.setattr(access, "note_tailnet", lambda **kw: noted.append(kw))
    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", pytest.fail)
    monkeypatch.setattr(access_set, "install", pytest.fail)
    placeholders: list[bool] = []
    monkeypatch.setattr(access_set, "install_placeholder", lambda **kw: placeholders.append(True))
    trust = TailnetTrust(trust_identity=True, allowed_logins=("alice@example.com",))
    ref = listener.prepare_at_start(
        _cfg(True), host="crew.tail1234.ts.net", trust=trust, local_only=True
    )
    assert ref == tailnet_direct.GateRef(None)
    assert placeholders == [True]
    assert noted == [
        {
            "enabled": False,
            "host": "crew.tail1234.ts.net",
            "identity_trust": True,
            "loopback_only": True,
            "proof_gap": None,
            "identity_config_unreadable": False,
        }
    ]


@pytest.mark.asyncio
async def test_start_after_bind_resolves_and_fills_the_chain_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = DirectGate(owner=OWNER, port=0)
    ref = tailnet_direct.GateRef()
    seen: list[Any] = []

    async def resolved(cfg: Any, port: int, local_only: bool) -> DirectResolution:
        seen.append((port, local_only))
        return DirectResolution(tailnet_direct.STATE_ACTIVE, gate=gate)

    async def bound(_runner: Any, _port: int, g: DirectGate) -> list[Any]:
        # The chain can decide a request the moment a direct site accepts one.
        seen.append(ref.gate is g)
        return [SimpleNamespace(name="http://100.64.0.1:5476")]

    monkeypatch.setattr(listener, "resolve", resolved)
    monkeypatch.setattr(tailnet_direct, "start_sites", bound)
    app = web.Application()
    app["allowed_origins"] = set()
    sites = await listener.start_after_bind(
        app, object(), PORT, _cfg(True), ref, local_only=True  # type: ignore[arg-type]
    )
    assert len(sites) == 1
    assert seen == [(PORT, True), True]
    assert ref.gate is gate


@pytest.mark.asyncio
async def test_a_bind_failure_leaves_the_chain_holder_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sites(*_a: Any) -> list[Any]:
        return []

    monkeypatch.setattr(tailnet_direct, "start_sites", no_sites)
    ref = tailnet_direct.GateRef()
    active = DirectResolution(tailnet_direct.STATE_ACTIVE, gate=DirectGate(owner=OWNER, port=0))
    assert await listener.start(web.Application(), object(), PORT, active, ref) == []  # type: ignore[arg-type]
    assert ref.gate is None


@pytest.mark.asyncio
async def test_start_binds_nothing_while_the_gate_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailnet_direct, "start_sites", pytest.fail)
    off = DirectResolution(tailnet_direct.STATE_OFF, tailnet_direct.REASON_DISABLED)
    assert await listener.start(web.Application(), object(), PORT, off) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_start_records_a_bind_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sites(*_a: Any) -> list[Any]:
        return []

    monkeypatch.setattr(tailnet_direct, "start_sites", no_sites)
    gate = DirectGate(owner=OWNER, port=0)
    active = DirectResolution(tailnet_direct.STATE_ACTIVE, gate=gate)
    assert await listener.start(web.Application(), object(), PORT, active) == []  # type: ignore[arg-type]
    assert gate.port == PORT
    recorded = access.tailnet_direct()
    assert (recorded.state, recorded.reason) == (
        tailnet_direct.STATE_UNAVAILABLE,
        tailnet_direct.REASON_BIND_FAILED,
    )


@pytest.mark.asyncio
async def test_start_adds_the_listener_origins_and_publishes_its_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = [SimpleNamespace(name="http://100.64.0.1:5476")]

    async def bound(*_a: Any) -> list[Any]:
        return sites

    monkeypatch.setattr(tailnet_direct, "start_sites", bound)
    app = web.Application()
    app["allowed_origins"] = {"http://127.0.0.1:5476"}
    gate = DirectGate(owner=OWNER, port=0)
    active = DirectResolution(tailnet_direct.STATE_ACTIVE, gate=gate)
    assert await listener.start(app, object(), PORT, active) == sites  # type: ignore[arg-type]
    assert tailnet_direct.direct_origins(gate, PORT) <= app["allowed_origins"]
    recorded = access.tailnet_direct()
    assert recorded.state == tailnet_direct.STATE_ACTIVE
    assert recorded.urls == tuple(tailnet_direct.direct_urls(gate, PORT))
