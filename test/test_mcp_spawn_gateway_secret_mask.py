"""The gateway's internal-API secret files are hidden from two third-party-code spawns.

``.local_secret`` and ``run/gateway-<port>.secret`` hold the one credential of the
gateway's internal routes. The npm install the MCP resolver runs (third-party lifecycle
scripts) and the discovery probe of a third-party MCP server run code the gateway does
not control, and neither has any reason to read them. Both spawns hand the files to the
sandbox as ``extra_hidden_dirs``. A probe of the package's own managed server (the
first-party fixed argv) is left unmasked, as before.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache, probe_server
from kiro_crew.mcp_gateway import resolve_once as R
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import await_until

_posix_only = pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell stub")


def _plant_secret_files() -> tuple[str, str]:
    """Create the two files and return their paths (``.local_secret``, one run secret)."""
    root = str(config_dir())
    local = os.path.join(root, ".local_secret")
    run_dir = os.path.join(root, "run")
    os.makedirs(run_dir, exist_ok=True)
    run_secret = os.path.join(run_dir, "gateway-8765.secret")
    for path in (local, run_secret):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("s3cret")
    return os.path.normpath(local), os.path.normpath(run_secret)


@_posix_only
@pytest.mark.asyncio
async def test_npm_install_spawn_hides_the_gateway_secret_files(tmp_path, monkeypatch) -> None:
    local, run_secret = _plant_secret_files()
    seen: dict[str, tuple[str, ...]] = {}

    def _capture(argv, **kwargs):
        seen["hidden"] = tuple(kwargs.get("extra_hidden_dirs", ()))
        return list(argv), dict(os.environ), None

    monkeypatch.setattr(R, "sandboxed_spawn_argv", _capture)
    npm = tmp_path / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)

    await R.install(
        str(tmp_path / "home"), R.NpmSpec(package="foo@1.0.0", passthrough=()), npm=str(npm)
    )

    assert local in seen["hidden"]
    assert run_secret in seen["hidden"]


class TestDiscoveryProbeSecretMask:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        _probe_cache.clear()
        yield
        _probe_cache.clear()

    @staticmethod
    async def _hidden_for(server: McpServerInfo, which: str) -> tuple[str, ...]:
        seen: dict[str, tuple[str, ...]] = {}

        def _capture(argv, **kwargs):
            seen["hidden"] = tuple(kwargs.get("extra_hidden_dirs", ()))
            raise RuntimeError("stop at the wrap")

        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _capture),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value=which),
        ):
            await probe_server(server)
        return seen["hidden"]

    @pytest.mark.asyncio
    async def test_third_party_probe_hides_the_gateway_secret_files(self) -> None:
        local, run_secret = _plant_secret_files()

        hidden = await self._hidden_for(
            McpServerInfo(name="playwright-mcp", command="node"), "/usr/bin/node"
        )

        assert local in hidden
        assert run_secret in hidden

    @pytest.mark.asyncio
    async def test_first_party_fixed_argv_probe_is_not_masked(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        local, _ = _plant_secret_files()
        invocation = (sys.executable, ["mcp-core"])
        monkeypatch.setattr(md, "_resolved_managed_invocation", {"kirocrew-core": invocation})
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {})

        hidden = await self._hidden_for(
            McpServerInfo(name="kirocrew-core", command=invocation[0], args=list(invocation[1])),
            invocation[0],
        )

        assert local not in hidden
        assert hidden == ()


@pytest.mark.asyncio
async def test_first_prefetch_waits_until_the_listener_credentials_are_published() -> None:
    """The secret files exist only after the listener is bound and the process is ready,
    and the mask lists only files that exist at spawn, so a prefetch spawned earlier would
    mask nothing."""
    passes: list[dict[str, str]] = []
    ran = asyncio.Event()

    async def fake_pass(target_env):
        passes.append(target_env)
        ran.set()
        return {}

    ready = asyncio.Event()
    orch = SimpleNamespace(
        _prefetch_mcp_resolutions=fake_pass,
        _mcp_resolve_refresh_secs=lambda: 86400.0,
        _MCP_RESOLVE_MIN_SLEEP_SECS=1.0,
        _mcp_launch_approval_ready=ready,
    )
    task = asyncio.create_task(
        GatewayOrchestrator._mcp_resolve_prefetch_loop(orch, {"A": "b"})  # type: ignore[arg-type]
    )
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert passes == [], "the install must not start before the credentials are published"
        ready.set()
        await await_until(ran, what="the first prefetch pass")
        assert passes == [{"A": "b"}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
