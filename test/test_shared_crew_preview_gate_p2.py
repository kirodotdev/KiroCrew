"""The Shared Crew preview gate on P2's surfaces: the direct listener and the access routes.

Without ``KIROCREW_SHARED_CREW_PREVIEW=1`` ``tailnet_direct`` reads as off whatever
config.json says, the direct listener never starts, and the invite, credential
and access routes answer 404. With it set, today's behaviour holds.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import tailnet_direct
from kiro_crew.dashboard import tailnet_direct_listener as listener
from kiro_crew.dashboard.routes import shared_crew as shared_crew_routes
from kiro_crew.shared_crew import access_set, routes_invites
from kiro_crew.shared_crew.preview import PREVIEW_ENV

GATED_ROUTES = [
    ("GET", "/api/shared-crew/tailscale/invites"),
    ("POST", "/api/shared-crew/tailscale/invites"),
    ("DELETE", "/api/shared-crew/tailscale/invites/inv-1"),
    ("POST", "/api/shared-crew/tailscale/invites/inv-1/resend"),
    ("POST", "/api/shared-crew/tailscale/credential"),
    ("DELETE", "/api/shared-crew/tailscale/credential"),
    ("POST", "/api/shared-crew/tailscale/avatars"),
    ("GET", "/api/shared-crew/tailscale/avatars/a1"),
    ("GET", "/api/shared-crew/access"),
    ("POST", "/api/shared-crew/access/first-share/ack"),
]


def _cfg_with_direct_on() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    shared = dataclasses.replace(cfg.dashboard.shared_crew, tailnet_direct=True)
    return dataclasses.replace(
        cfg, dashboard=dataclasses.replace(cfg.dashboard, shared_crew=shared)
    )


@pytest.fixture()
def preview_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PREVIEW_ENV, raising=False)


@pytest.fixture()
def preview_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREVIEW_ENV, "1")


def test_gate_off_reads_the_switch_off_whatever_config_says(preview_off) -> None:
    assert listener._shared(_cfg_with_direct_on()).tailnet_direct is False


def test_gate_on_reads_the_config_value(preview_on) -> None:
    assert listener._shared(_cfg_with_direct_on()).tailnet_direct is True


@pytest.mark.asyncio
async def test_gate_off_never_starts_the_listener_or_asks_the_daemon(
    preview_off, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tailnet_direct.tailnet_localapi, "status", pytest.fail)
    monkeypatch.setattr(tailnet_direct, "start_sites", pytest.fail)
    monkeypatch.setattr(access_set, "install_placeholder", pytest.fail)
    resolution = await listener.resolve(_cfg_with_direct_on(), 5476, True)
    assert (resolution.state, resolution.reason) == (
        tailnet_direct.STATE_OFF,
        tailnet_direct.REASON_DISABLED,
    )
    listener.prepare_access_set(_cfg_with_direct_on())
    assert listener._PENDING_INSTALL == []


def test_gate_off_keeps_allowed_logins_in_charge_with_tailscale_on(
    preview_off, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Tailscale on but no preview, no derived set replaces allowed_logins."""
    from kiro_crew.dashboard import tailnet

    monkeypatch.setattr(access_set, "install_placeholder", pytest.fail)
    cfg = KiroCrewConfig()
    ts = dataclasses.replace(cfg.dashboard.tailscale, enabled=True)
    cfg = dataclasses.replace(cfg, dashboard=dataclasses.replace(cfg.dashboard, tailscale=ts))
    listener.prepare_access_set(cfg)
    assert listener._PENDING_INSTALL == []
    assert tailnet.login_allowed("alice@example.com", ("alice@example.com",)) is True


@pytest.mark.asyncio
async def test_gate_on_resolves_the_listener_from_config(
    preview_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[bool] = []

    def resolved(**_kw: Any) -> tailnet_direct.DirectResolution:
        asked.append(True)
        return tailnet_direct.DirectResolution(
            tailnet_direct.STATE_UNAVAILABLE, tailnet_direct.REASON_TAILSCALE_NOT_RUNNING
        )

    monkeypatch.setattr(tailnet_direct, "resolve", resolved)
    monkeypatch.setattr(access_set, "install", lambda *_a, **_kw: None)
    monkeypatch.setattr(access_set, "install_placeholder", lambda: None)
    await listener.resolve(_cfg_with_direct_on(), 5476, True)
    assert asked == [True]
    listener._PENDING_INSTALL.clear()


async def _client() -> TestClient:
    app = web.Application()
    app["state"] = None
    shared_crew_routes.register(app)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_gate_off_hides_every_invite_credential_and_access_route(preview_off) -> None:
    client = await _client()
    try:
        statuses = [
            (method, path, (await client.request(method, path)).status)
            for method, path in GATED_ROUTES
        ]
    finally:
        await client.close()
    assert all(status == 404 for _m, _p, status in statuses), statuses
    # No poller reads Tailscale either.
    assert routes_invites._POLLER_KEY not in client.app


@pytest.mark.asyncio
async def test_gate_on_serves_the_routes_behind_their_own_checks(preview_on) -> None:
    client = await _client()
    try:
        statuses = [
            (method, path, (await client.request(method, path)).status)
            for method, path in GATED_ROUTES
        ]
    finally:
        await client.close()
    # Reached: refused by the handler's own owner check, not hidden.
    assert all(status != 404 for _m, _p, status in statuses), statuses


_BOOT_PROBE = """
import sys
from aiohttp import web
import kiro_crew.dashboard.server
import kiro_crew.dashboard.server_runtime.api_entrypoint
import kiro_crew.dashboard.token_auth
from kiro_crew.dashboard.routes import shared_crew
shared_crew.register(web.Application())
print(",".join(m for m in sys.argv[1:] if m in sys.modules))
"""

PREVIEW_ONLY_MODULES = [
    "kiro_crew.shared_crew.routes_invites",
    "kiro_crew.shared_crew.routes_access",
    "kiro_crew.shared_crew.tailscale_invites",
    "kiro_crew.shared_crew.tailscale_invite_store",
    "kiro_crew.shared_crew.tailscale_api",
    "kiro_crew.shared_crew.lifecycle",
    "kiro_crew.dashboard.tailnet_direct",
    "kiro_crew.dashboard.tailnet_direct_listener",
    "kiro_crew.dashboard.tailnet_localapi",
]


def test_without_the_preview_the_boot_path_imports_no_preview_only_module(tmp_path) -> None:
    """Importing the servers and registering routes loads none of P2's modules."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != PREVIEW_ENV}
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(  # noqa: S603 - this interpreter, fixed argv
        [sys.executable, "-c", _BOOT_PROBE, *PREVIEW_ONLY_MODULES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1:] in ([""], []), proc.stdout
