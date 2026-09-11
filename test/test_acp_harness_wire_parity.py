"""Byte-level wire parity for the harness-adapter extraction.

The ``HarnessAdapter`` layer moves every kiro/KAS-specific decision out of
``AcpRuntime`` and into a strategy object. The whole point of that move is that
nothing on the wire changes, so the proof has to be about BYTES, not about the
code being shaped nicely.

This module drives the real ``AcpRuntime`` request-construction paths against a
fake subprocess, captures every outbound JSON-RPC payload, and compares the
serialized bytes to a golden file committed from the pre-extraction tree. A
golden mismatch means a session on a real backend would see a different frame
than it saw before the refactor.

This module NEVER writes. A gate that can rewrite its own expectations is not
a gate, and an environment variable is a poor guard for that -- it turns "the
wire changed" into one export away from "the wire is fine". On a mismatch the
captured document is printed in full, so a deliberate update is a copy from the
failure output into the golden file, by hand, with the diff visible.

The captures are pinned to explicit inputs (fixed cwd, fixed MCP roster, fixed
custom-agent payload) so the bytes do not move with the host's filesystem. Two
kinds of fact deliberately stay OUT of the golden. ``initialize``'s
``protocolVersion`` and ``clientCapabilities`` are chosen by module constants
rather than by a request, and a golden of a constant only proves the golden was
written from the constant, so they are pinned against those constants directly.
And the KAS auth callback's SUCCESS response carries a live credential, so only
its failure frame is committed; the success shape is asserted without storing it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.harness import kas as kas_harness_mod
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError
from kiro_crew.acp.kas_transport import METHOD_KAS_AUTH_GET_ACCESS_TOKEN
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_CLIENT_CAPABILITIES,
    KAS_CLIENT_CAPABILITIES,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "acp_harness_wire"

# Fixed inputs. Every value here is arbitrary but PINNED: the golden's bytes are
# only meaningful if the request that produced them cannot drift with the host.
_CWD = "/pinned/work/dir"
_AGENT = "pinned-agent"
_RESUME_SID = "pinned-resume-sid"
_SESSION_FILE = "/pinned/transcript.jsonl"
_MCP_ROSTER: list[dict[str, Any]] = [
    {"name": "kirocrew-core", "command": "pinned-core"},
    {"name": "kirocrew-cron", "command": "pinned-cron"},
]
_KAS_AGENTS = [{"name": _AGENT, "prompt": "pinned"}]
# Advertised so set_mode is reached on both paths rather than skipped: the mode
# activation request is part of what session start puts on the wire.
_MODES = {"currentModeId": "other-mode", "availableModes": [{"id": _AGENT}]}
_INBOUND_REQUEST_ID = 7
_AUTH_FAILURE = "pinned auth failure"

BACKENDS = {"kiro": ACP_BACKEND_KIRO, "kas": ACP_BACKEND_KAS}


@pytest.fixture(autouse=True)
def _fast_no_report_ceiling(monkeypatch):
    """Shrink drain_init()'s no-report ceiling, as test_acp_runtime does.

    The captures drive the real create_session()/load_session() against a fake
    backend that emits no MCP registration frames; at the production ceiling
    each capture stalls for seconds and buys nothing -- the ceiling is not what
    this module measures.
    """
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)


def _runtime(backend: str) -> AcpRuntime:
    """An initialized runtime on ``backend``, wired to a fake subprocess.

    Mirrors ``test_acp_runtime._make_runtime`` but pins the backend so nothing
    reads the host filesystem.
    """
    rt = AcpRuntime(work_dir=_CWD, acp_backend=backend)
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    rt._can_load_session = True
    return rt


def _stdin_frames(rt: AcpRuntime) -> list[dict[str, Any]]:
    """Every JSON object the runtime wrote to the child's stdin, in order."""
    return [json.loads(call.args[0].decode()) for call in rt._process.stdin.write.call_args_list]


async def _capture(backend: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Drive the request-construction seams and return one capture document."""
    rt = _runtime(backend)
    sent: list[dict[str, Any]] = []

    async def _fake_send(method, params, timeout=None):
        sent.append({"method": method, "params": params})
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "pinned-new-sid", "modes": _MODES}
        if method == METHOD_SESSION_LOAD:
            return {"modes": _MODES}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    async def _fake_work_dir(cwd=None):
        # PurePosixPath, not Path: the request carries str(work_dir), and a
        # platform-native Path renders the separator of whichever host runs the
        # test. That would make the golden host-dependent, so the gate would fail
        # on Windows for a reason that has nothing to do with the wire -- and a
        # byte gate that cannot hold everywhere is not a gate there. What the
        # capture is for is the SHAPE of each request; how a real cwd renders is
        # the caller's business and is covered where that decision is made.
        return PurePosixPath(_CWD)

    monkeypatch.setattr(rt, "_session_work_dir", _fake_work_dir)

    # session/load resolves its own roster from the gateway overlay, which stats
    # files. Pin it to the same roster session/new is given so the two requests
    # are comparable and neither moves with the host's gateway configuration.
    monkeypatch.setattr(runtime_mod, "pooled_session_servers", lambda overlay, agent: _MCP_ROSTER)

    async def _fake_kas_agents(agent, *, member_dispatch=False):
        # The real projection reads ~/.kiro/agents; the GATE it is behind is what
        # this capture is about, so the payload is pinned and the gate is not.
        return _KAS_AGENTS if backend == ACP_BACKEND_KAS else None

    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_kas_agents)

    # ── session/new -> set_mode -> teardown ──
    handle = await rt.create_session(cwd=_CWD, agent=_AGENT, mcp_servers=_MCP_ROSTER)
    await handle.destroy()

    # ── session/load -> set_mode -> teardown ──
    load_handle = await rt.load_session(
        session_file=_SESSION_FILE,
        resume_sid=_RESUME_SID,
        cwd=_CWD,
        agent=_AGENT,
    )
    await load_handle.destroy()

    # ── the inbound request this backend answers itself ──
    # KAS: the auth callback, captured on its FAILURE frame so no credential is
    # committed. kiro: nothing is host-answered, so the same inbound method takes
    # the ownerless -32601 path every non-KAS backend gets.
    if backend == ACP_BACKEND_KAS:

        async def _fail_auth():
            raise HostAuthCallbackError(_AUTH_FAILURE)

        monkeypatch.setattr(kas_harness_mod, "answer_get_access_token", _fail_auth)
        await rt._answer_get_access_token(_INBOUND_REQUEST_ID)
    else:
        await rt._answer_ownerless_request(_INBOUND_REQUEST_ID, METHOD_KAS_AUTH_GET_ACCESS_TOKEN)

    return {
        "backend": backend,
        "requests": sent,
        "teardown_method": rt._session_teardown_method(),
        # Frames written straight to the child rather than through
        # _send_and_await: responses and errors the host originates.
        "stdin_frames": _stdin_frames(rt),
    }


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def _serialize(capture: dict[str, Any]) -> str:
    return json.dumps(capture, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("name,backend", sorted(BACKENDS.items()))
@pytest.mark.asyncio
async def test_outbound_wire_matches_golden(
    name: str, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every frame the runtime builds is byte-identical to the pre-refactor tree.

    This is the extraction's acceptance gate. A diff here is a product change,
    whatever the diff to the source looked like.
    """
    capture = await _capture(backend, monkeypatch)
    payload = _serialize(capture)
    path = _golden_path(name)

    # Refuse a host separator before comparing, so a golden that drifted into
    # platform dependence fails with THAT as the reason rather than as an opaque
    # byte mismatch on one OS.
    assert "\\" not in payload, (
        "the capture carries a Windows path separator, so this golden would be "
        "host-dependent -- pin the work dir as a PurePosixPath"
    )
    assert path.exists(), f"missing golden {path}"
    assert path.read_text(encoding="utf-8") == payload, (
        f"outbound wire for backend {backend!r} changed. The harness layer is "
        f"behaviour-preserving by contract, so this is a real product delta.\n\n"
        f"If the change is deliberate, copy the capture below into {path} by hand "
        f"and say in the PR what moved on the wire and why.\n\n{payload}"
    )


@pytest.mark.parametrize("name,backend", sorted(BACKENDS.items()))
def test_golden_declares_its_own_backend(name: str, backend: str) -> None:
    """A golden cannot be silently swapped for the other backend's file.

    Without this, copying kiro.json over kas.json makes both parametrizations
    pass and the KAS-specific ``_meta`` envelope stops being checked at all.
    """
    stored = json.loads(_golden_path(name).read_text(encoding="utf-8"))
    assert stored["backend"] == backend


@pytest.mark.parametrize("name,backend", sorted(BACKENDS.items()))
def test_golden_covers_the_whole_session_lifecycle(name: str, backend: str) -> None:
    """The capture reached every frame a session start and teardown put on the wire.

    A capture that silently stopped after session/new would still byte-compare
    clean forever, so the gate has to assert its own coverage. set_mode is the
    one most easily lost: it is skipped when the response advertises no modes.
    """
    stored = json.loads(_golden_path(name).read_text(encoding="utf-8"))
    methods = [entry["method"] for entry in stored["requests"]]
    assert methods.count(METHOD_SESSION_NEW) == 1
    assert methods.count(METHOD_SESSION_LOAD) == 1
    assert methods.count(METHOD_SET_MODE) == 2
    assert methods.count(stored["teardown_method"]) == 2
    assert len(stored["stdin_frames"]) == 1


def test_kas_golden_carries_the_custom_agent_envelope() -> None:
    """The KAS ``_meta.kiro.customAgents`` envelope is present on BOTH requests.

    Pinned positively rather than left to the byte compare: an extraction that
    dropped the envelope from session/load would still produce a self-consistent
    golden if someone regenerated it, and this is the field whose absence made a
    resumed KAS session advertise the wrong mode set.
    """
    stored = json.loads(_golden_path("kas").read_text(encoding="utf-8"))
    by_method = {entry["method"]: entry["params"] for entry in stored["requests"]}
    for method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
        meta = by_method[method]["_meta"]
        assert meta["kiro"]["customAgents"] == _KAS_AGENTS, method


def test_kiro_golden_carries_no_custom_agent_envelope() -> None:
    """The kiro path is untouched by the KAS projection.

    The other half of the gate above: the envelope must not leak onto a backend
    that has an ``--agent`` spawn flag and would then advertise the agent twice.
    """
    stored = json.loads(_golden_path("kiro").read_text(encoding="utf-8"))
    for entry in stored["requests"]:
        assert "kiro" not in (entry["params"].get("_meta") or {}), entry["method"]


def test_kiro_golden_carries_the_session_file_meta() -> None:
    """Only kiro-cli is handed a transcript path on session/load."""
    stored = json.loads(_golden_path("kiro").read_text(encoding="utf-8"))
    by_method = {entry["method"]: entry["params"] for entry in stored["requests"]}
    assert by_method[METHOD_SESSION_LOAD]["_meta"]["_kiro.dev/session_file"] == _SESSION_FILE


def test_only_kas_answers_the_host_auth_callback() -> None:
    """The same inbound method is served by KAS and refused by kiro.

    The two goldens' single stdin frame is what proves it: KAS answers with the
    callback's own error code, kiro with ``-32601 Method not found``. A harness
    that started answering the callback on a backend that owns no credential
    would flip this pair.
    """
    from kiro_crew.acp.kas_transport import KAS_AUTH_CALLBACK_ERROR_CODE

    kas_frame = json.loads(_golden_path("kas").read_text(encoding="utf-8"))["stdin_frames"][0]
    kiro_frame = json.loads(_golden_path("kiro").read_text(encoding="utf-8"))["stdin_frames"][0]
    assert kas_frame["error"]["code"] == KAS_AUTH_CALLBACK_ERROR_CODE
    assert kas_frame["error"]["message"] == _AUTH_FAILURE
    assert kiro_frame["error"]["code"] == -32601
    # No credential field reaches the committed fixture on either side.
    assert "result" not in kas_frame and "result" not in kiro_frame


def test_handshake_params_come_from_the_shared_constants() -> None:
    """``initialize``'s two backend-varying fields are pinned to their constants.

    A golden would only prove the golden was generated from the constant. What
    matters is that the harness keeps answering with the SAME object the
    pre-extraction inline conditional read, including KAS's integer
    ``protocolVersion`` -- KAS rejects the date-string spelling outright.
    """
    from kiro_crew.acp.runtime import PROTOCOL_VERSION, PROTOCOL_VERSION_KAS

    assert PROTOCOL_VERSION == "2025-08-22"
    assert PROTOCOL_VERSION_KAS == 1
    assert isinstance(PROTOCOL_VERSION_KAS, int)
    assert KAS_CLIENT_CAPABILITIES == {
        **ACP_CLIENT_CAPABILITIES,
        "_meta": {"kiro": {"settings": {}}},
    }


def test_this_gate_cannot_write_to_the_repository() -> None:
    """The gate has no path that writes a golden, under any environment.

    A test that can regenerate its own expectations turns "the wire changed" into
    one export away from "the wire is fine", and the repository refuses that shape
    elsewhere too -- ``test_acp_frame_replay`` pins that replay mutates no
    committed file.

    Read as a SYNTAX TREE, not as text. The property is the absence of a call,
    which running the module cannot demonstrate, and a text scan for the call
    shapes would match its own list of them: green either way, proving nothing.
    Walking the tree asks the real question, and the forbidden names reach it as
    identifiers rather than as source a scan can trip over.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    writers = {"write_text", "write_bytes", "mkdir", "unlink", "rename", "touch", "open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in writers, f"line {node.lineno}: {node.func.attr}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open", f"line {node.lineno}: open()"


def test_host_auth_callback_membership_is_kas_only() -> None:
    """Which backend the host answers for is a membership fact, not a branch."""
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HOST_AUTH_CALLBACK

    assert ACP_BACKEND_KAS in ACP_BACKENDS_HOST_AUTH_CALLBACK
    assert ACP_BACKEND_KIRO not in ACP_BACKENDS_HOST_AUTH_CALLBACK
    assert METHOD_KAS_AUTH_GET_ACCESS_TOKEN == "_kiro/auth/getAccessToken"
