"""``POST /api/acp-backends/recheck`` over HTTP, with a real binary appearing on disk.

The unit tests in ``test_agent_sdk_backend_install.py`` stub the resolvers, which is
right for pinning the contract and wrong for answering "does the whole path work".
This file answers that: the real route table is mounted on an in-process aiohttp
server and driven over HTTP, so the router, the owner gate, the JSON body, the install
probe, the resolution ladder and the spawn path's own cache all take part.

The install is REAL -- an executable file that does not exist at the first probe and
does exist at the second. That is what makes the sequence evidence rather than a
restatement of the stubs: the first verdict is a miss because the file is absent.

What this does NOT cover: a human pressing the button in a browser against a live
gateway, because starting a listening gateway is refused by the sandbox this was written
in. That is a property of one environment rather than of this code, so it is stated in
the pull request and deliberately NOT pinned here -- an assertion about the host running
the suite would either never fail or fail for a reason that has nothing to do with the
subject.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.agent_sdk import INSTALLED, MISSING
from kiro_crew.agent_sdk import backend_install as probe
from kiro_crew.dashboard.routes import agent_config as agent_config_routes

pytestmark = pytest.mark.asyncio

_OWNER = "owner-e2e"

#: The harness under test, chosen because its binary is absent on the hosts this suite
#: runs on -- ``opencode`` and ``goose`` resolve through mise shims, so their "before"
#: state could never be a real miss. deepseek is also the build-excluded harness, so
#: this doubles as coverage that a described-but-not-offered row still re-probes.
#: If a host ever ships ``dsh``, the first assertion fails loudly rather than passing
#: for the wrong reason.


class _State:
    """The owner the dashboard gate compares against."""

    owner_id = _OWNER


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Both caches cleared on both sides, so no test inherits a verdict."""
    from kiro_crew.acp import client

    probe.clear_probe_cache()
    monkeypatch.setattr(client, "_self_served_bin_caches", {})
    yield
    probe.clear_probe_cache()


@pytest.fixture
def owner_middleware():
    """Stamp every request as the owner, the way the real auth layer would.

    The gate reads ``request["app"]`` present-and-EMPTY plus ``request["user"]``
    equal to ``state.owner_id``; a non-owner path is covered by the unit tests, so
    this fixture exists to get past the gate rather than to test it.
    """

    @web.middleware
    async def _mw(request, handler):
        request["app"] = ""
        request["user"] = _OWNER
        return await handler(request)

    return _mw


async def _client(owner_middleware) -> TestClient:
    app = web.Application(middlewares=[owner_middleware])
    app["state"] = _State()
    agent_config_routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    return d


def _binary_name() -> str:
    """The binary the ladder actually searches for, from the harness's own record.

    Read rather than written down: a literal here could drift from what
    ``_resolve_self_served_bin`` looks up, and the test would then install a file
    nothing resolves and still pass its first assertion for the wrong reason.
    """
    from kiro_crew.agent_sdk.backends import launch_for

    return launch_for(ACP_BACKEND_DEEPSEEK).binary


def _install(directory: Path, name: str) -> Path:
    """Write a real executable the PLATFORM'S OWN resolver will find.

    The ladder under test ends at ``shutil.which(name)``, and that call does not mean
    the same thing on both platforms. On POSIX it wants an execute bit. On Windows
    there is no execute bit and ``which`` only matches a name carrying one of the
    ``PATHEXT`` suffixes, so an extensionless shell script is invisible to it -- the
    probe then reads MISSING after an install this helper called successful, and the
    failure surfaces three assertions later as if the product had lost the binary.
    ``.cmd`` is in the default ``PATHEXT``, and ``which("dsh")`` resolves ``dsh.cmd``,
    which is the lookup this test exists to drive.
    """
    if os.name == "nt":
        target = directory / f"{name}.cmd"
        target.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        target = directory / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # The install is not done until the resolver can find it. Asserted HERE so a
    # platform whose rules this helper gets wrong fails on the helper's own line,
    # naming the file it wrote, instead of as a MISSING verdict further down.
    assert shutil.which(name, path=str(directory)), (
        f"{target.name} is not resolvable as {name!r} in {directory} -- the helper "
        f"wrote a file this platform does not treat as executable"
    )
    return target


async def _row(client: TestClient, backend: str) -> dict:
    resp = await client.get("/api/acp-backends")
    assert resp.status == 200
    body = await resp.json()
    rows = {r["id"]: r for r in body["backends"]}
    assert backend in rows
    return rows[backend]


class TestRecheckOverHttp:
    """The operator's sequence, driven through the real endpoints."""

    async def test_install_then_recheck_makes_the_harness_usable(
        self, tmp_path, monkeypatch, owner_middleware
    ):
        """missing -> a spawn caches it -> install -> re-check -> usable, no restart.

        Every step goes over HTTP. The only thing stubbed is PATH, and that is the
        subject: the binary genuinely is not there and then genuinely is.
        """
        from kiro_crew.acp import client as acp_client

        bindir = _bin_dir(tmp_path)
        monkeypatch.setenv("PATH", str(bindir))
        # A clean process cache: nothing has resolved this harness yet.
        monkeypatch.setattr(acp_client, "_self_served_bin_caches", {})

        client = await _client(owner_middleware)
        try:
            # 1. Absent on disk, and the GET says so with the component named.
            row = await _row(client, ACP_BACKEND_DEEPSEEK)
            assert row["installed"] == MISSING
            assert _binary_name() in row["missing_components"]
            assert row["install_command"]

            # 2. A spawn tried and cached the absence for the life of the process.
            #    This is the state that made a fresh install read as unusable.
            acp_client._self_served_bin_caches[ACP_BACKEND_DEEPSEEK] = (None, str(bindir))

            # 3. The operator runs the install command. The file now exists.
            _install(bindir, _binary_name())

            # The GET can only REPORT the divergence: installed on disk, and this
            # process still holds the miss.
            probe.clear_probe_cache()
            row = await _row(client, ACP_BACKEND_DEEPSEEK)
            assert row["installed"] == INSTALLED
            assert row["restart_required"] is True

            # 4. The re-check, which is allowed to clear.
            resp = await client.post(
                "/api/acp-backends/recheck",
                data=json.dumps({"backend": ACP_BACKEND_DEEPSEEK}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            rechecked = (await resp.json())["backend"]

            assert rechecked["id"] == ACP_BACKEND_DEEPSEEK
            assert rechecked["installed"] == INSTALLED
            # The claim the button exists to make good on: usable WITHOUT a restart.
            assert rechecked["restart_required"] is False
            # And the spawn path really was cleared, so the next session resolves the
            # binary the operator installed rather than the absence this process
            # remembered. Without this assertion the row above could be a re-read that
            # merely looks green.
            assert ACP_BACKEND_DEEPSEEK not in acp_client._self_served_bin_caches

            # 5. The answer survives a fresh GET -- it is the server's state now, not a
            #    value only the POST's response carried.
            probe.clear_probe_cache()
            row = await _row(client, ACP_BACKEND_DEEPSEEK)
            assert row["installed"] == INSTALLED
            assert row["restart_required"] is False
        finally:
            await client.close()

    async def test_a_recheck_on_a_still_absent_component_stays_missing(
        self, tmp_path, monkeypatch, owner_middleware
    ):
        """The same path, without the install: no parameter fakes a green."""
        from kiro_crew.acp import client as acp_client

        bindir = _bin_dir(tmp_path)
        monkeypatch.setenv("PATH", str(bindir))
        monkeypatch.setattr(acp_client, "_self_served_bin_caches", {})

        client = await _client(owner_middleware)
        try:
            resp = await client.post(
                "/api/acp-backends/recheck",
                data=json.dumps({"backend": ACP_BACKEND_DEEPSEEK}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            row = (await resp.json())["backend"]
            assert row["installed"] == MISSING
            assert _binary_name() in row["missing_components"]
            assert row["restart_required"] is False
        finally:
            await client.close()

    async def test_an_unknown_backend_is_refused_over_http(self, monkeypatch, owner_middleware):
        """The id validation, through the router rather than the handler alone."""
        client = await _client(owner_middleware)
        try:
            resp = await client.post(
                "/api/acp-backends/recheck",
                data=json.dumps({"backend": "not-a-backend"}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "unknown_agent_backend"
        finally:
            await client.close()

    async def test_the_recheck_route_rejects_a_GET(self, owner_middleware):
        """The verb is the contract: a replayable read may not clear the cache."""
        client = await _client(owner_middleware)
        try:
            resp = await client.get("/api/acp-backends/recheck")
            assert resp.status == 405
        finally:
            await client.close()


class TestGenerationFence:
    """A resolution already in flight cannot undo a re-check's clear.

    The hazard is structural rather than incidental: every publish site checks the
    sentinel, AWAITS its resolver, then assigns. So a resolve that began before the
    operator installed a component can complete after the clear and stamp its stale miss
    back over the cleared sentinel -- the panel having already reported the harness
    ready, and the next spawn failing on the revived miss.

    These cases drive the interleaving rather than waiting for it: the resolver blocks on
    an event, the clear lands while it is blocked, and only then is it released.
    """

    async def test_a_pre_clear_resolution_does_not_revive_the_stale_miss(self, monkeypatch):
        """The race, driven deterministically through the shipped publish shape.

        The resolver blocks until this test releases it, so the clear is guaranteed to
        land mid-flight rather than by luck.
        """
        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        monkeypatch.setattr(client, "_resolution_generation", {})

        release = threading.Event()
        started = threading.Event()

        def _pre_install_answer(backend: str):
            """The answer from before the install, held open by the test."""
            started.set()
            release.wait(5)
            return (None, "/usr/bin")

        monkeypatch.setattr(client, "_resolve_self_served_bin", _pre_install_answer)

        async def _publish_like_the_spawn_path() -> tuple:
            """The shipped fence: capture the epoch, await, publish only if current."""
            epoch = client._resolution_epoch(ACP_BACKEND_OPENCODE)
            resolved = await asyncio.to_thread(
                client._resolve_self_served_bin, ACP_BACKEND_OPENCODE
            )
            if client._resolution_epoch(ACP_BACKEND_OPENCODE) == epoch:
                client._self_served_bin_caches[ACP_BACKEND_OPENCODE] = resolved
            return resolved

        spawn = asyncio.create_task(_publish_like_the_spawn_path())
        await asyncio.to_thread(started.wait, 5)

        # The operator installs the component and presses Re-check WHILE that resolve
        # is still in flight.
        from kiro_crew.agent_sdk.drivers import acp as driver

        driver.forget_cached_resolution(ACP_BACKEND_OPENCODE)

        release.set()
        stale = await spawn

        # It used its own pre-install answer for its own session, and did NOT write it
        # back over the clear -- so the next spawn still resolves fresh.
        assert stale == (None, "/usr/bin")
        assert ACP_BACKEND_OPENCODE not in client._self_served_bin_caches

    async def test_removing_the_fence_would_revive_it(self, monkeypatch):
        """Mutation check: the same interleaving WITHOUT the generation comparison.

        Asserts the test above is load-bearing. If this did not revive the miss, the
        fence would be guarding nothing and the case above would pass either way.
        """
        from kiro_crew.acp import client

        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        monkeypatch.setattr(client, "_resolution_generation", {})

        async def _unfenced() -> tuple:
            resolved = (None, "/usr/bin")
            await asyncio.sleep(0)
            # No epoch comparison -- the pre-fence shape.
            client._self_served_bin_caches[ACP_BACKEND_OPENCODE] = resolved
            return resolved

        client.bump_resolution_generation(ACP_BACKEND_OPENCODE)
        await _unfenced()
        assert ACP_BACKEND_OPENCODE in client._self_served_bin_caches

    async def test_a_clear_bumps_the_generation(self, monkeypatch):
        """The wiring, so the fence cannot be left un-armed by the clear."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_resolution_generation", {})
        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        before = client._resolution_epoch(ACP_BACKEND_OPENCODE)
        driver.forget_cached_resolution(ACP_BACKEND_OPENCODE)
        assert client._resolution_epoch(ACP_BACKEND_OPENCODE) == before + 1

    async def test_both_pi_caches_share_one_generation(self, monkeypatch):
        """pi keeps two caches under one id, so one bump has to fence both."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_resolution_generation", {})
        monkeypatch.setattr(client, "_pi_acp_argv_cache", (None, "/usr/bin"), raising=False)
        monkeypatch.setattr(client, "_pi_bin_cache", (None, "/usr/bin"), raising=False)
        before = client._resolution_epoch(ACP_BACKEND_PI)
        driver.forget_cached_resolution(ACP_BACKEND_PI)
        assert client._resolution_epoch(ACP_BACKEND_PI) == before + 1
        assert client._pi_acp_argv_cache is client._UNRESOLVED
        assert client._pi_bin_cache is client._UNRESOLVED

    async def test_one_backends_bump_does_not_fence_another(self, monkeypatch):
        """A re-check of one harness must not make another re-resolve."""
        from kiro_crew.acp import client
        from kiro_crew.agent_sdk.drivers import acp as driver

        monkeypatch.setattr(client, "_resolution_generation", {})
        monkeypatch.setattr(client, "_self_served_bin_caches", {})
        driver.forget_cached_resolution(ACP_BACKEND_OPENCODE)
        assert client._resolution_epoch(ACP_BACKEND_CLAUDE) == 0
