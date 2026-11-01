"""The app proxy forwards only to the tracked backend generation it signs for.

A CLI reinstall rotates ``.app_secret`` in another process. These tests pin the two
halves that let a live gateway recover without a restart:

* ``acquire_app_backend_target`` leases a gateway-spawned child only when the digest
  recorded at spawn matches the current secret, and an adopted backend only while its
  live listener identity still matches the adopted record; teardown waits for leases.
* ``handle_app_api_proxy`` reaches a gateway-managed (``backend.entryPoint``) app only
  through such a lease. Without one it answers a retryable ``503
  app_backend_unavailable`` and never falls back to the manifest port.

The process doubles come from ``apps_backend_coverage_helpers.py`` and the route
harness from ``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_backend_coverage_helpers import _fake_proc
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.backend as bmod
import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.backend import AppProcess
from kiro_crew.apps.manager import enable_app
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _clean_process_table(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    """Each test starts and ends with an empty tracked-process table."""
    _floor_monkeypatch.setattr(
        bmod, "_pidfile_path", lambda: tmp_path / "app_backends.pids.json"
    )
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()
    yield
    with bmod._lock:
        bmod._processes.clear()
        bmod._allocated_ports.clear()

# ---------------------------------------------------------------------------
# Target leases
# ---------------------------------------------------------------------------


def _adopted(name: str = "adopted", port: int = 9200, **extra: Any) -> AppProcess:
    fields: dict[str, Any] = {
        "app_name": name,
        "port": port,
        "pid": 0,
        "proc": None,
        "healthy": True,
        "adopted_pids": [888],
        "adopted_start_times": {888: "start-888"},
    }
    fields.update(extra)
    return AppProcess(**fields)


def _present_listener(
    monkeypatch: pytest.MonkeyPatch, owners: list[int], start_time: str
) -> None:
    monkeypatch.setattr(bmod.platform_compat, "find_port_listeners", lambda port: ["l"])
    monkeypatch.setattr(bmod.platform_compat, "loopback_owner_pids", lambda listeners: owners)
    monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: start_time)


class TestBackendTargetLease:
    def _track(self, secret: str = "current") -> AppProcess:
        ap = AppProcess(
            app_name="leased",
            port=9100,
            pid=777,
            proc=_fake_proc(pid=777),
            healthy=True,
            gateway_started=True,
            proxy_secret_digest=hashlib.sha256(secret.encode("utf-8")).digest(),
        )
        with bmod._lock:
            bmod._processes[ap.app_name] = ap
            bmod._allocated_ports[ap.app_name] = ap.port
        return ap

    def test_only_current_live_spawned_generation_is_acquired(self) -> None:
        ap = self._track()
        lease = bmod.acquire_app_backend_target("leased", "current")
        assert lease is not None
        assert (lease.port, lease.pid) == (ap.port, ap.pid)
        assert ap.forward_leases == 1
        bmod.release_app_backend_target(lease)
        assert ap.forward_leases == 0

    def test_adopted_record_uses_a_separate_tracked_target_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adopted = _adopted()
        with bmod._lock:
            bmod._processes[adopted.app_name] = adopted
            bmod._allocated_ports[adopted.app_name] = adopted.port
        _present_listener(monkeypatch, [888], "start-888")

        assert bmod.acquire_app_backend_target("adopted", "current") is None
        lease = bmod.acquire_adopted_app_backend_target("adopted")
        assert lease is not None
        assert (lease.port, lease.pid) == (adopted.port, 888)
        assert adopted.forward_leases == 1
        bmod.release_app_backend_target(lease)
        assert adopted.forward_leases == 0

    def test_adopted_target_requires_complete_owner_identities(self) -> None:
        adopted = _adopted(adopted_start_times={})
        with bmod._lock:
            bmod._processes[adopted.app_name] = adopted
        assert bmod.acquire_adopted_app_backend_target("adopted") is None

    @pytest.mark.parametrize("drift", ["new_owner", "recycled_pid", "unreadable"])
    def test_adopted_target_fails_closed_on_listener_identity_drift(
        self, drift: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebound port, a recycled pid or an unreadable owner gets no lease."""
        adopted = _adopted()
        with bmod._lock:
            bmod._processes[adopted.app_name] = adopted
            bmod._allocated_ports[adopted.app_name] = adopted.port
        if drift == "new_owner":
            _present_listener(monkeypatch, [999], "start-999")
        elif drift == "recycled_pid":
            _present_listener(monkeypatch, [888], "start-RECYCLED")
        else:
            _present_listener(monkeypatch, [], "start-888")
        assert bmod.acquire_adopted_app_backend_target("adopted") is None
        assert adopted.forward_leases == 0

    @pytest.mark.parametrize("state", ["stale", "unhealthy", "retiring", "adopted", "dead"])
    def test_untrusted_target_states_fail_closed(self, state: str) -> None:
        ap = self._track()
        secret = "current"
        if state == "stale":
            secret = "rotated"
        elif state == "unhealthy":
            ap.healthy = False
        elif state == "retiring":
            ap.retiring = True
        elif state == "adopted":
            ap.proc = None
        elif state == "dead":
            ap.proc.returncode = 1
        assert bmod.acquire_app_backend_target("leased", secret) is None
        assert ap.forward_leases == 0

    def test_reconciler_generation_check_observes_disk_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ap = self._track("first")
        secret_file = tmp_path / ".app_secret"
        secret_file.write_text("first", encoding="utf-8")
        monkeypatch.setattr(bmod, "app_dir", lambda name: tmp_path)
        assert bmod.app_backend_matches_current_secret("leased") is True
        secret_file.write_text("second", encoding="utf-8")
        assert bmod.app_backend_matches_current_secret("leased") is False
        ap.proc = None
        assert bmod.app_backend_matches_current_secret("leased") is False
        secret_file.unlink()
        assert bmod.app_backend_matches_current_secret("leased") is None

    def test_stop_waits_for_the_exact_process_lease(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ap = self._track()
        lease = bmod.acquire_app_backend_target("leased", "current")
        assert lease is not None
        monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", lambda *_args: None)
        stopped: list[bool] = []
        worker = threading.Thread(
            target=lambda: stopped.append(bmod.stop_app_backend("leased")),
            daemon=True,
        )
        worker.start()

        def _retiring() -> bool:
            with bmod._lock:
                return ap.retiring

        wait_until(_retiring, describe=lambda: "stop never marked the record retiring")
        assert ap.retiring is True
        assert worker.is_alive(), "teardown must wait while a signed request owns the target"
        assert bmod.acquire_app_backend_target("leased", "current") is None
        bmod.release_app_backend_target(lease)
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert stopped == [True]
        assert "leased" not in bmod._processes


# ---------------------------------------------------------------------------
# Proxy routing for gateway-managed apps
# ---------------------------------------------------------------------------


def _no_manifest_fallback(reason: str) -> Any:
    def _fail(name: str, m: Any = None) -> str:
        pytest.fail(reason)

    return _fail


def _echo_backend(seen: list[bytes]) -> web.Application:
    async def _handler(request: web.Request) -> web.Response:
        seen.append(await request.read())
        return web.json_response({"ok": True})

    backend = web.Application()
    backend.router.add_post("/api/run", _handler)
    return backend


@pytest.mark.asyncio
async def test_managed_backend_without_matching_target_never_uses_manifest_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, backend={"entryPoint": "server.py", "port": "7801"})
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("rotated", encoding="utf-8")
    monkeypatch.setattr(
        routes_mod,
        "_resolve_app_backend_url",
        _no_manifest_fallback("managed apps must not use manifest fallback"),
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"/apps/{APP}/api/run", data=b"signed body")
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "1"
        assert (await resp.json())["code"] == "app_backend_unavailable"


@pytest.mark.asyncio
async def test_managed_backend_forwards_only_to_matching_tracked_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, backend={"entryPoint": "server.py", "port": "auto"})
    enable_app(APP)
    secret = "current-generation"
    (home / "apps" / APP / ".app_secret").write_text(secret, encoding="utf-8")
    seen: list[bytes] = []
    async with TestServer(_echo_backend(seen)) as backend_server:
        proc = MagicMock()
        proc.poll.return_value = None
        tracked = AppProcess(
            app_name=APP,
            port=backend_server.port,
            pid=777,
            proc=proc,
            healthy=True,
            proxy_secret_digest=hashlib.sha256(secret.encode("utf-8")).digest(),
        )
        with bmod._lock:
            bmod._processes[APP] = tracked
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/apps/{APP}/api/run", data=b"signed body")
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
        assert seen == [b"signed body"]
        assert tracked.forward_leases == 0


@pytest.mark.asyncio
async def test_a_rotated_secret_is_refused_by_the_old_generation_then_served_by_the_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reinstall case: the old child is never signed for, the new one is."""
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, backend={"entryPoint": "server.py", "port": "auto"})
    enable_app(APP)
    secret_file = home / "apps" / APP / ".app_secret"
    secret_file.write_text("old", encoding="utf-8")
    seen: list[bytes] = []
    async with TestServer(_echo_backend(seen)) as backend_server:
        proc = MagicMock()
        proc.poll.return_value = None
        old = AppProcess(
            app_name=APP,
            port=backend_server.port,
            pid=777,
            proc=proc,
            healthy=True,
            proxy_secret_digest=hashlib.sha256(b"old").digest(),
        )
        with bmod._lock:
            bmod._processes[APP] = old
        async with TestClient(TestServer(_make_app())) as client:
            # Another process rotates the secret: no gateway-side invalidation.
            secret_file.write_text("new", encoding="utf-8")
            resp = await client.post(f"/apps/{APP}/api/run", data=b"one")
            assert resp.status == 503
            assert (await resp.json())["code"] == "app_backend_unavailable"
            assert seen == []
            # The reconciler's replacement records the new generation.
            with bmod._lock:
                old.proxy_secret_digest = hashlib.sha256(b"new").digest()
            resp = await client.post(f"/apps/{APP}/api/run", data=b"two")
            assert resp.status == 200
        assert seen == [b"two"]
        assert old.forward_leases == 0


@pytest.mark.asyncio
async def test_adopted_backend_forwards_through_its_tracked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, backend={"entryPoint": "server.py", "port": "7801"})
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("adopted", encoding="utf-8")
    seen: list[bytes] = []
    async with TestServer(_echo_backend(seen)) as backend_server:
        adopted = _adopted(APP, backend_server.port)
        with bmod._lock:
            bmod._processes[APP] = adopted
        _present_listener(monkeypatch, [888], "start-888")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/apps/{APP}/api/run", data=b"signed body")
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
        assert seen == [b"signed body"]
        assert adopted.forward_leases == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owners", "start_time"),
    [([999], "start-999"), ([888], "start-RECYCLED")],
    ids=["new-owner", "recycled-pid"],
)
async def test_adopted_backend_with_drifted_listener_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owners: list[int], start_time: str
) -> None:
    """A port rebound under a process the record never adopted gets 503, not the body."""
    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, backend={"entryPoint": "server.py", "port": "7801"})
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("adopted", encoding="utf-8")
    adopted = _adopted(APP, 7801)
    with bmod._lock:
        bmod._processes[APP] = adopted
    _present_listener(monkeypatch, owners, start_time)
    monkeypatch.setattr(
        routes_mod,
        "_resolve_app_backend_url",
        _no_manifest_fallback("a drifted adopted record must not use the manifest port"),
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"/apps/{APP}/api/run", data=b"signed body")
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "1"
        assert (await resp.json())["code"] == "app_backend_unavailable"
    assert adopted.forward_leases == 0
