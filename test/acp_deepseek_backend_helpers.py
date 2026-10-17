"""Shared helpers for the ``test_acp_deepseek_backend*`` tests.

The autouse kiro-cli version pin every part runs under, the captured ACP fixture frames, the
launch rig that drives a real DeepSeek launch, and the bare ACP client builder.
"""

from __future__ import annotations

import asyncio
import pathlib
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_acp_deepseek_backend*.py modules calls monkeypatch.undo()
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts first pays it against the HOST's install
    with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


def _fixture_frames(name: str) -> list[dict]:
    """Every frame of one committed fixture, header excluded."""
    import json

    path = pathlib.Path(__file__).parent / "fixtures" / "acp_frames" / "deepseek" / name
    return [json.loads(line) for line in path.read_text().splitlines()[1:]]


class _DeepseekLaunchRig:
    """``DeepseekLaunch.resolve_spawn`` driven against fakes at the adapter's own seams.

    The resolution, the preflight, the seal and patch writers, the provider-key name
    read, the probe's scratch window, the sandbox wrap, the probe itself and the
    launcher cleanup each answer a fixed value and record, in order, that they ran
    and with what. The provider KEY resolver and the vault are booby-trapped: the
    launch's probe half must never reach either.
    """

    ARGV = ["/opt/bin/dsh", "--profile", "acp"]
    SEALED = "/opt/run/kirocrew_dsh_gate_1.mjs"
    PATCH = "/opt/run/kirocrew_dsh_gate_1.patch.yml"
    HIDDEN = ("/opt/creds/.aws",)
    SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
    CLEANUP = "/opt/run/kirocrew_sandbox_readback"
    MODE = "standard"

    def __init__(self, monkeypatch, tmp_path, *, routing=("", ""), names=()):
        from kiro_crew import agent_scratch

        self.events: list[tuple] = []
        self.loop_thread: int | None = None
        self.plan = None
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
        self.adapter = deepseek_mod.DeepseekLaunch()
        self.probe_dir = tmp_path / "probe-window"
        self._tmp_path = tmp_path

        def _record(name, *args):
            self.events.append((name, threading.get_ident(), args))

        async def _launch(backend):
            _record("resolve", backend)
            return self.ARGV[0], list(self.ARGV), "dsh --profile acp", "dsh"

        async def _preflight(preflight, backend, mode):
            _record("preflight", preflight, backend, mode)
            return self.HIDDEN

        def _seal():
            _record("seal")
            return self.SEALED

        def _patch(extension_path):
            _record("patch", extension_path)
            return self.PATCH

        def _names():
            _record("names")
            return tuple(names)

        def _allocate(label):
            _record("allocate", label)
            self.probe_dir.mkdir()
            return self.probe_dir

        async def _wrap(argv, **kwargs):
            _record("wrap", list(argv), kwargs)
            return [*self.SANDBOX, *argv], self.CLEANUP

        def _verify(session, backend, argv, extension_path, marker_path, nonce, **kwargs):
            _record("verify", list(argv), extension_path, marker_path, nonce, kwargs)
            return routing

        def _unlink(path):
            _record("unlink", path)

        def _no_key(*_a, **_kw):
            raise AssertionError("the launch reached the provider key; the probe needs none")

        monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _launch)
        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
        monkeypatch.setattr(deepseek_mod, "_seal_deepseek_gate_extension", _seal)
        monkeypatch.setattr(deepseek_mod, "_write_deepseek_gate_patch", _patch)
        monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env_names", _names)
        monkeypatch.setattr(agent_scratch, "allocate_scratch", _allocate)
        monkeypatch.setattr(deepseek_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(deepseek_mod, "_verify_deepseek_gate", _verify)
        monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", _unlink)
        monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env", _no_key)
        monkeypatch.setattr(deepseek_mod, "resolve_secret_uris", _no_key)

    def run(self):
        """Resolve the plan on a fresh loop, noting the thread the loop runs on."""

        async def _go():
            self.loop_thread = threading.get_ident()
            return await self.adapter.resolve_spawn(
                SpawnContext(
                    agent="kirocrew",
                    work_dir=self._tmp_path,
                    model=None,
                    environ={},
                    home=self._tmp_path,
                    sandbox_mode=self.MODE,
                    session=self.client,
                )
            )

        self.plan = asyncio.run(_go())
        return self.plan

    def names(self) -> list[str]:
        return [name for name, _thread, _args in self.events]

    def args_of(self, name: str) -> tuple:
        hits = [event for event in self.events if event[0] == name]
        assert len(hits) == 1, f"{name} ran {len(hits)} times: {self.names()}"
        return hits[0][2]


def _client(tmp_path: pathlib.Path, backend: str) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    return client
