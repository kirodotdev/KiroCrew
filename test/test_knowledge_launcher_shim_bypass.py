"""The knowledge pool's zero-tool worker skips the kiro-cli launcher shim.

The installed ``kiro-cli`` can be a shim that starts a sandbox tool before kiro-cli.
That tool adds its own MCP server to every spec in the agents directories on each
launch, the alias the worker is about to load included, so the harness mounts the
server and the post-start zero-tool check refuses the worker on every start. The
worker therefore runs the bundle binary the shim's own fallback runs, and Kiro Crew's
own sandbox wraps it instead of the shim's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from acp_launch_capture import capture

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import client_spawn_env
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_spawn_env import apply_pod_bundle_spawn
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO


def _bundle_layout(tmp_path: Path) -> tuple[str, str]:
    """A shim at ``<root>/sandbox/kiro-cli`` beside the bundle binary ``<root>/kiro-cli``."""
    root = tmp_path / "tools" / "kiro-cli" / "9.9.9"
    (root / "sandbox").mkdir(parents=True)
    shim = root / "sandbox" / "kiro-cli"
    bundle = root / "kiro-cli"
    for path in (shim, bundle):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return str(shim), str(bundle)


@pytest.fixture
def no_pinned_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shim honours a preset KIRO_CLI_PATH; the host's own must not answer here.
    monkeypatch.delenv("KIRO_CLI_PATH", raising=False)


# ── The helper ────────────────────────────────────────────────────────────────


def test_the_shim_is_swapped_for_its_bundle_binary(tmp_path: Path, no_pinned_bundle: None) -> None:
    shim, bundle = _bundle_layout(tmp_path)
    argv = [shim, "acp", "--agent", "kirocrew-knowledge"]
    assert client_spawn_env.bypass_launcher_shim(argv, sandbox_mode="auto") == [
        bundle,
        "acp",
        "--agent",
        "kirocrew-knowledge",
    ]


def test_sandbox_off_keeps_the_shim(tmp_path: Path, no_pinned_bundle: None) -> None:
    """With Crew's sandbox off the shim's sandbox is the only one left: keep it."""
    shim, _bundle = _bundle_layout(tmp_path)
    assert client_spawn_env.bypass_launcher_shim([shim, "acp"], sandbox_mode="off") is None


def test_no_bundle_binary_keeps_what_was_resolved(tmp_path: Path, no_pinned_bundle: None) -> None:
    plain = tmp_path / "bin" / "kiro-cli"
    plain.parent.mkdir()
    plain.write_text("#!/bin/sh\nexit 0\n")
    plain.chmod(0o755)
    assert client_spawn_env.bypass_launcher_shim([str(plain), "acp"], sandbox_mode="auto") is None
    assert client_spawn_env.bypass_launcher_shim([], sandbox_mode="auto") is None


# ── The client spawn ──────────────────────────────────────────────────────────


def _capture_kiro(tmp_path: Path, *, bypass: bool) -> tuple[list[str], dict[str, Any], str, str]:
    """Spawn a kiro-cli client resolved to a shim; return argv, wrap kwargs, shim, bundle."""
    shim, bundle = _bundle_layout(tmp_path)
    wrap_kwargs: dict[str, Any] = {}

    async def _wrap(argv: list[str], **kwargs: Any) -> tuple[list[str], None]:
        wrap_kwargs.update(kwargs)
        return list(argv), None

    real_init = AcpClient.__init__

    def _init(self: AcpClient, *args: Any, **kwargs: Any) -> None:
        kwargs["bypass_launcher_shim"] = bypass
        real_init(self, *args, **kwargs)

    answers = capture(
        ACP_BACKEND_KIRO,
        tmp_path,
        extra_patches=[
            patch.object(
                client_mod, "_resolve_kiro_bin_for_spawn", new=AsyncMock(return_value=shim)
            ),
            patch.object(client_mod, "wrap_argv_async", side_effect=_wrap),
            # The capture pins this step to "no delegation"; the real one is under test.
            patch.object(client_mod, "apply_pod_bundle_spawn", side_effect=apply_pod_bundle_spawn),
            patch("kiro_crew.kiro_cli.kiro_acp_engine_args", return_value=[]),
            patch.object(AcpClient, "__init__", _init),
        ],
    )
    return answers["argv"], wrap_kwargs, shim, bundle


def test_a_client_that_asks_runs_the_bundle_under_crews_sandbox(
    tmp_path: Path, no_pinned_bundle: None
) -> None:
    """Red before the fix: the shim ran, and Crew skipped its own wrap for it."""
    argv, wrap_kwargs, _shim, bundle = _capture_kiro(tmp_path, bypass=True)
    assert argv[0] == bundle
    assert wrap_kwargs["is_kiro_cli"] is False


def test_every_other_client_keeps_the_shim_and_its_sandbox(
    tmp_path: Path, no_pinned_bundle: None
) -> None:
    argv, wrap_kwargs, shim, _bundle = _capture_kiro(tmp_path, bypass=False)
    assert argv[0] == shim
    assert wrap_kwargs["is_kiro_cli"] is True


# ── The knowledge pool asks for it ────────────────────────────────────────────


def test_the_knowledge_pool_kiro_client_asks_to_bypass_the_shim() -> None:
    import asyncio

    from kiro_crew.knowledge import llm_pool

    made: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            made.append(kwargs)

        def authored_spec_declares_zero_tools(self) -> bool:
            return True

        async def ensure_ready(self) -> None:
            return None

    worker = llm_pool.AcpWorker(sandbox_mode="auto", acp_backend=ACP_BACKEND_KIRO)
    with (
        patch.object(llm_pool, "AcpClient", _Client),
        patch.object(llm_pool, "_repair_agent_spec", return_value=None),
    ):
        asyncio.run(worker._spawn_client(ACP_BACKEND_KIRO, "auto"))
    assert made and made[0]["bypass_launcher_shim"] is True
