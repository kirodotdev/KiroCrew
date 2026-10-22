"""Constants, fakes and the launch rig the ``test_acp_pi_backend*.py`` suites share.

The adapter's two environment overrides and the autouse ``_no_ambient_pi_env`` that
keeps them out of every test (each suite binds it by name so pytest registers it for
that module), pi's ``get_commands`` registry and response builders, the permission
frame and envelope builders, and ``_PiLaunchRig``, which drives
``PiLaunch.resolve_spawn`` against fakes at the adapter's own seams.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp._dispatch import GATE_ENVELOPE_MARKER
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_PI, gate_probe_command_for
from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

_ENV_PI_ACP_BIN = "PI_ACP_BIN"
_ENV_PI_COMMAND = "PI_ACP_PI_COMMAND"
PROBE = gate_probe_command_for(ACP_BACKEND_PI)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_acp_pi_backend.py and the suites split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _no_ambient_pi_env(monkeypatch):
    """Neither override may leak in from the developer's own shell."""
    monkeypatch.delenv(_ENV_PI_ACP_BIN, raising=False)
    monkeypatch.delenv(_ENV_PI_COMMAND, raising=False)


def _registry(*entries: tuple[str, str | None]) -> list:
    """A pi ``get_commands`` list: ``(name, source path)`` pairs, ``None`` for built-ins."""
    out = []
    for name, path in entries:
        entry: dict = {"name": name, "description": ""}
        if path is not None:
            entry["source"] = "extension"
            entry["sourceInfo"] = {"path": path, "source": "cli"}
        out.append(entry)
    return out


def _response(commands: list, request_id: str = "kiro-crew-gate-readback") -> str:
    return json.dumps(
        {
            "id": request_id,
            "type": "response",
            "command": "get_commands",
            "success": True,
            "data": {"commands": commands},
        }
    )


class _PiLaunchRig:
    """``PiLaunch.resolve_spawn`` driven against fakes at the adapter's own seams.

    Every collaborator that would touch the host -- the two resolvers, the version
    read, the sandbox preflight, the seal, the launcher write, the sandbox wrap, the
    read-back child and its cleanup -- answers a fixed value and records, in order,
    that it ran, on which thread, and with what. A test then asserts what the launch
    DID: the order of its steps, what each was handed, whether a blocking step ran
    off the loop, and the plan or the refusal it produced.
    """

    PI_ACP_ARGV = ["/opt/bin/node", "/opt/lib/pi-acp/dist/index.js"]
    PI_BIN = "/opt/bin/pi"
    SEALED = "/opt/run/kirocrew_pi_gate_1.ts"
    LAUNCHER = "/opt/run/kirocrew_pi_gate_1_x.sh"
    HIDDEN = ("/opt/creds/.aws",)
    SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
    CLEANUP = "/opt/run/kirocrew_sandbox_readback"
    MODE = "standard"

    def __init__(self, monkeypatch, tmp_path, *, routing=("", ""), node_choice=None):
        self.events: list[tuple] = []
        self.loop_thread: int | None = None
        self.plan = None
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        self.adapter = pi_mod.PiLaunch()
        self._tmp_path = tmp_path
        forget_cached_resolution(ACP_BACKEND_PI)

        def _record(name, *args):
            self.events.append((name, threading.get_ident(), args))

        def _resolve_pi_acp_bin():
            _record("resolve_pi_acp")
            return list(self.PI_ACP_ARGV), "/opt/bin"

        def _resolve_pi_bin():
            _record("resolve_pi")
            return self.PI_BIN, "/opt/bin"

        def _version_issue(pi_bin):
            _record("version", pi_bin)
            return ""

        def _node_choice(session, pi_bin, wrap):
            _record("node", pi_bin, wrap)
            return node_choice or pi_mod.PiNodeChoice()

        async def _preflight(preflight, backend, mode):
            _record("preflight", preflight, backend, mode)
            return self.HIDDEN

        def _seal():
            _record("seal")
            return self.SEALED

        def _launcher(pi_bin, extension_path, program=()):
            _record("launcher", pi_bin, extension_path, *((program,) if program else ()))
            return self.LAUNCHER

        async def _wrap(argv, **kwargs):
            _record("wrap", list(argv), kwargs)
            return [*self.SANDBOX, *argv], self.CLEANUP

        def _verify(session, backend, argv, extension_path):
            _record("verify", list(argv), extension_path, session, backend)
            return routing

        def _unlink(path):
            _record("unlink", path)

        monkeypatch.setattr(pi_mod, "_resolve_pi_acp_bin", _resolve_pi_acp_bin)
        monkeypatch.setattr(pi_mod, "_resolve_pi_bin", _resolve_pi_bin)
        monkeypatch.setattr(pi_mod, "_pi_version_issue", _version_issue)
        monkeypatch.setattr(pi_mod, "_pi_node_choice", _node_choice)
        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
        monkeypatch.setattr(pi_mod, "_seal_pi_gate_extension", _seal)
        monkeypatch.setattr(pi_mod, "_ensure_pi_gate_launcher", _launcher)
        monkeypatch.setattr(pi_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(pi_mod, "_verify_pi_gate", _verify)
        monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", _unlink)

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

        try:
            self.plan = asyncio.run(_go())
        finally:
            forget_cached_resolution(ACP_BACKEND_PI)
        return self.plan

    def names(self) -> list[str]:
        return [name for name, _thread, _args in self.events]

    def _event(self, name: str) -> tuple:
        hits = [event for event in self.events if event[0] == name]
        assert len(hits) == 1, f"{name} ran {len(hits)} times: {self.names()}"
        return hits[0]

    def args_of(self, name: str) -> tuple:
        return self._event(name)[2]

    def ran_off_the_loop(self, name: str) -> bool:
        assert self.loop_thread is not None
        return self._event(name)[1] != self.loop_thread


def _permission_frame(message: str, title: str = "bash") -> JsonRpcMessage:
    """pi-acp's rendering of a confirm dialog, as captured live."""
    return JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {
                "toolCallId": "pi-ui-5646e0bd",
                "title": title,
                "kind": "other",
                "status": "pending",
                "rawInput": {"method": "confirm", "title": title, "message": message},
            },
            "options": [
                {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        },
    )


NONCE = "f00dfeed" * 4


def _envelope(**overrides) -> str:
    body = {
        GATE_ENVELOPE_MARKER: 1,
        "nonce": NONCE,
        "toolCallId": "call_v0prcvqi",
        "tool": "bash",
        "kind": "execute",
        "input": {"command": "echo gate-check"},
    }
    body.update(overrides)
    return json.dumps(body)
