"""The relay viewer token route does not wait on unrelated blocking work.

The route cases fill the event loop's default executor before minting a Crew token or
calling the control plane. The refresh case fills every worker of the provider's pool after
its initial vault resolve, then proves the refresh lock and HTTP request can finish
without that pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from collections.abc import Awaitable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request

from kiro_crew.auth.provider import KasAuthProvider
from kiro_crew.auth.store import KasToken, TokenStore
from kiro_crew.dashboard.handlers import relay_viewer as rv
from kiro_crew.member_memory_auth import LocalOwnerProvenance
from kiro_crew.relay_viewer import cps
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SECRET = "local-secret-value"
MINT_DEADLINE_SECS = 10
PROVIDER_WORKERS = 4
EP = "a" * 24
CREW = cps.CrewEndpoint(
    endpoint_id=EP,
    crew_url=f"https://{EP}.relay.example.test",
    name="Build box",
    crew_type="PERSONAL",
    presence="CONNECTED",
    relay_connection_status="CONNECTED",
)
_T = TypeVar("_T")


def _token(access: str, expires_in: timedelta) -> KasToken:
    return KasToken(
        access_token=access,
        expires_at=datetime.now(timezone.utc) + expires_in,
        provider="BuilderId",
        identity="social",
    )


class _Store:
    """The vault holds a fresh sign-in."""

    def resolve(self) -> KasToken:
        return _token("fresh-sign-in", timedelta(hours=1))


class _GatedResolveStore(TokenStore):
    def __init__(self, root: Path, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(root)
        self._loop = loop
        self.resolve_started = asyncio.Event()
        self.release_resolve = threading.Event()

    def resolve(self) -> KasToken | None:
        token = super().resolve()
        self._loop.call_soon_threadsafe(self.resolve_started.set)
        assert self.release_resolve.wait(FALLBACK_TIMEOUT_SECS)
        return token


@pytest.fixture
def viewer(monkeypatch) -> SimpleNamespace:
    """The viewer on, CPS faked, the host-process gate passing."""
    tunnel = SimpleNamespace(
        relay_viewer_enabled=True,
        relay_viewer_cps_url="https://cps.example.test",
        relay_viewer_credential="",
        relay_viewer_relay_domain="relay.example.test",
    )
    state = SimpleNamespace(tunnel=tunnel, bearers=[])

    async def _list(url, bearer, *, crew_domain, token_type=""):
        state.bearers.append(bearer)
        return (CREW,)

    async def _mint(url, bearer, endpoint_id, *, token_type=""):
        return cps.CrewAccessToken("access-token-value", 2000.0, 240.0, 1700.0)

    monkeypatch.setattr(rv, "_load_tunnel", lambda: tunnel)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    monkeypatch.setattr(cps, "create_crew_access_token", _mint)
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", _Store)
    monkeypatch.setattr("kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: "fp")
    monkeypatch.setattr(
        "kiro_crew.relay_client.lease.read_lease_credential", lambda _ref: "test-credential"
    )
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.local_owner_bootstrap_provenance",
        lambda _request: LocalOwnerProvenance(),
    )
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
    return state


def _mint_request() -> web.Request:
    sock = MagicMock()
    sock.family = socket.AF_INET
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda key, default=None: {
        "socket": sock,
        "peername": ("127.0.0.1", 43123),
    }.get(key, default)
    app = web.Application()
    app["local_secret"] = SECRET
    request = make_mocked_request(
        "POST",
        "/api/relay-viewer/token",
        headers={"X-Local-Secret": SECRET, "Content-Type": "application/json"},
        app=app,
        transport=transport,
    )
    # A mocked request has no body stream; aiohttp answers read() from this cache.
    request._read_bytes = json.dumps({"endpointId": EP}).encode()
    return request


@contextlib.contextmanager
def _default_executor_held() -> Iterator[None]:
    """Make the event loop's default executor one thread, and hold that thread."""
    pool = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(pool)
    release = threading.Event()
    started = threading.Event()

    def _hold() -> None:
        started.set()
        assert release.wait(FALLBACK_TIMEOUT_SECS)

    try:
        pool.submit(_hold)
        assert started.wait(MINT_DEADLINE_SECS)
        yield
    finally:
        release.set()


async def _finished_within_deadline(call: Awaitable[_T]) -> _T:
    task = asyncio.ensure_future(call)
    done, _pending = await asyncio.wait({task}, timeout=MINT_DEADLINE_SECS)
    if task not in done:
        task.cancel()
    assert task in done, "the call did not finish while an unrelated executor was saturated"
    return task.result()


@pytest.mark.asyncio
async def test_mints_with_the_vault_sign_in_while_the_default_executor_is_full(
    viewer,
) -> None:
    with _default_executor_held():
        response = await _finished_within_deadline(rv.api_relay_viewer_token(_mint_request()))

    assert response.status == 200
    assert viewer.bearers == ["fresh-sign-in"]


@pytest.mark.asyncio
async def test_refresh_does_not_need_the_providers_pool(monkeypatch, tmp_path: Path) -> None:
    async def _refresh(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "accessToken": "fresh-sign-in",
                "refreshToken": "fresh-refresh-token",
                "expiresIn": 3600,
                "profileArn": "arn:fresh",
            }
        )

    app = web.Application()
    app.router.add_post("/refreshToken", _refresh)
    loop = asyncio.get_running_loop()
    store = _GatedResolveStore(tmp_path, loop)
    await asyncio.to_thread(
        store.save,
        KasToken(
            access_token="stale-sign-in",
            expires_at=datetime.now(timezone.utc),
            provider="BuilderId",
            identity="social",
            refresh_token="stale-refresh-token",
            profile_arn="arn:stale",
        ),
    )
    pool = ThreadPoolExecutor(max_workers=PROVIDER_WORKERS)
    release_workers = threading.Event()
    all_workers_started = asyncio.Event()
    started = 0
    started_lock = threading.Lock()

    def _hold_worker() -> None:
        nonlocal started
        with started_lock:
            started += 1
            if started == PROVIDER_WORKERS:
                loop.call_soon_threadsafe(all_workers_started.set)
        assert release_workers.wait(FALLBACK_TIMEOUT_SECS)

    blockers: list[Future[None]] = []
    async with TestServer(app, host="127.0.0.1") as server:
        monkeypatch.setattr(
            "kiro_crew.auth.refresh.social_service_url",
            lambda: f"http://localhost:{server.port}",
        )
        provider = KasAuthProvider(store, allow_env_api_key=False, executor=pool)
        current = asyncio.create_task(provider.current())
        try:
            await _finished_within_deadline(store.resolve_started.wait())
            blockers = [pool.submit(_hold_worker) for _ in range(PROVIDER_WORKERS)]
            store.release_resolve.set()
            await _finished_within_deadline(all_workers_started.wait())
            refreshed = await _finished_within_deadline(current)
        finally:
            store.release_resolve.set()
            release_workers.set()
            if not current.done():
                current.cancel()
            await asyncio.gather(current, return_exceptions=True)
            if blockers:
                await _finished_within_deadline(
                    asyncio.gather(*(asyncio.wrap_future(future) for future in blockers))
                )
            pool.shutdown(wait=False)

    assert refreshed.access_token == "fresh-sign-in"


@pytest.mark.asyncio
async def test_mints_with_the_test_credential_while_the_default_executor_is_full(
    viewer,
) -> None:
    viewer.tunnel.relay_viewer_credential = "~/relay-viewer-credential"

    with _default_executor_held():
        response = await _finished_within_deadline(rv.api_relay_viewer_token(_mint_request()))

    assert response.status == 200
    assert viewer.bearers == ["test-credential"]


@pytest.mark.asyncio
async def test_a_control_plane_call_resolves_its_host_while_the_default_executor_is_full(
    monkeypatch,
) -> None:
    async def _answer(_request: web.Request) -> web.Response:
        return web.json_response({"crewEndpoints": []})

    app = web.Application()
    app.router.add_post("/", _answer)
    monkeypatch.setattr(cps, "env_proxy_for", lambda _url: None)

    async with TestServer(app, host="127.0.0.1") as server:
        url = f"http://localhost:{server.port}/"
        with _default_executor_held():
            answer = await _finished_within_deadline(
                cps._post(url, "bearer-value", cps.LIST_TARGET, {}, "")
            )

    assert answer.status == 200


_LOOKUP_ANSWERS = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443)),
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", 443, 0, 0)),
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 443, 0, 1)),
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::11",)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_the_pool_resolver_answers_like_aiohttps_threaded_resolver(
    monkeypatch, windows: bool
) -> None:
    import aiohttp.resolver

    from kiro_crew import executor_resolver

    def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if flags and host.casefold().rstrip(".") == "localhost":
            raise socket.gaierror(socket.EAI_NONAME, "refused under AI_ADDRCONFIG")
        return list(_LOOKUP_ANSWERS)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(executor_resolver, "_IS_WINDOWS", windows)
    monkeypatch.setattr(aiohttp.resolver, "_IS_WINDOWS", windows)
    pool = ThreadPoolExecutor(max_workers=1)
    ours = executor_resolver.ExecutorResolver(pool)
    theirs = aiohttp.resolver.ThreadedResolver()
    try:
        for host in ("crew.example.test", "LocalHost."):
            expected = await _outcome(theirs.resolve(host, 443, socket.AF_UNSPEC))
            assert await _outcome(ours.resolve(host, 443, socket.AF_UNSPEC)) == expected
    finally:
        await ours.close()
        pool.shutdown(wait=False)


async def _outcome(call: Awaitable[list]) -> object:
    try:
        return [dict(entry) for entry in await call]
    except socket.gaierror as exc:
        return type(exc)
