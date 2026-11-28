"""A spec's ``toolsSettings`` shell and write allowlists on opencode.

opencode reads neither allowlist. Its seed puts ``"*": "ask"`` last in its resolved
``permission`` rules, so every ``bash``, ``write``, ``edit`` and ``apply_patch`` call
raises ``session/request_permission``; the direct client stamps the spec's
allowlists on those requests and the gate's ``spec-shell-allowlist`` and
``spec-write-allowlist`` rows refuse what they do not cover, a scheduled job's
``AUTO_APPROVE`` included (``permission_floor.refusal_for``).

The shell request is replayed from the committed live capture. The write captures
leave their permission frames out, so the frame here is opencode 1.18.30's measured
shape for one: ``kind: "edit"``, ``rawInput`` with a lowercase ``filepath`` and the
diff text, and a ``diff`` content block naming the absolute path, which is the field
the write row reads.

opencode's own allow-always stops it asking about the calls the approved pattern
covers, so the client answers it once only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401

from kiro_crew import permission_floor
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "opencode"
_AGENT = "probe"
_LIVE_COMMAND = "echo kirocrew-opencode-live"
_SHELL = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
_WRITE = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["docs/**"]}}


@pytest.fixture(autouse=True)
def _ungoverned(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """No governance profile on the host decides any of these calls."""
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _write_spec(agents_dir: Path, tools_settings: dict[str, Any] | None) -> None:
    spec: dict[str, Any] = {"name": _AGENT, "prompt": "p", "tools": ["*"], "mcpServers": {}}
    if tools_settings is not None:
        spec["toolsSettings"] = tools_settings
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _client(work_dir: Path) -> AcpClient:
    """An opencode client whose array resolution ran, as the spawn path runs it."""
    client = AcpClient(
        work_dir=work_dir, agent=_AGENT, acp_backend=ACP_BACKEND_OPENCODE, session_key="cron:probe"
    )
    client._session_mcp_servers()
    client._session_id = "opencode-session-1"
    return client


def _frames(name: str, cwd: Path) -> list[dict[str, Any]]:
    # Substituted as a JSON string body, so a Windows path's backslashes stay escaped.
    text = (_FRAMES / name).read_text(encoding="utf-8")
    text = text.replace("<cwd>", json.dumps(str(cwd))[1:-1])
    return [json.loads(line) for line in text.splitlines()[1:] if line.strip()]


def _feed_update(client: AcpClient, params: dict[str, Any]) -> None:
    """One ``session/update`` through both parsers the turn loop runs on it."""
    msg = JsonRpcMessage(method="session/update", params=params)
    client._extract_tool_event(msg)
    client._extract_tool_call_refinement(msg)


def _live_shell_request(client: AcpClient, cwd: Path) -> AcpEvent:
    """The live capture's tool_call frames, then its permission request, through
    the direct client's own parsers."""
    for frame in _frames("permission-request-live.jsonl", cwd):
        method = frame.get("method")
        if method == "session/update":
            _feed_update(client, frame["params"])
        elif method == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=method, params=frame["params"])
            event = client._build_permission_event(msg)
            assert event is not None
            return event
    raise AssertionError("no permission request in the capture")


def _write_request(client: AcpClient, cwd: Path, tool: str, target: Path) -> AcpEvent:
    """One file-changing call: its tool_call frame, then opencode's request for it."""
    call_id = f"call-{tool}"
    raw: dict[str, Any] = {"filePath": str(target), "content": "x\n"}
    if tool == "apply_patch":
        raw = {"patchText": f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch\n"}
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "in_progress",
        "kind": "edit",
        "title": tool,
        "locations": [{"path": str(target)}],
        "rawInput": raw,
    }
    _feed_update(client, {"sessionId": "opencode-session-1", "update": update})
    tool_call = {
        "toolCallId": call_id,
        "title": str(target),
        "kind": "edit",
        "status": "pending",
        "locations": [{"path": str(target)}],
        "rawInput": {"filepath": str(target), "diff": f"--- {target}\n+++ {target}\n+x\n"},
        "content": [{"type": "diff", "path": str(target), "oldText": None, "newText": "x\n"}],
    }
    options = [
        {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
        {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
        {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
    ]
    msg = JsonRpcMessage(
        id=f"req-{call_id}",
        method="session/request_permission",
        params={"sessionId": "opencode-session-1", "toolCall": tool_call, "options": options},
    )
    event = client._build_permission_event(msg)
    assert event is not None
    return event


def _auto_refusal(event: AcpEvent) -> str | None:
    """What an ``AUTO_APPROVE`` job's gate answers: a reason refuses, ``None`` runs it."""
    return permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)


def test_opencode_is_a_member() -> None:
    assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_CREW_ENFORCES_SPEC_TOOLS_SETTINGS


def test_an_auto_mode_job_refuses_a_command_the_spec_never_allowed(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, _SHELL)
    reason = _auto_refusal(_live_shell_request(_client(tmp_path), tmp_path))
    assert reason is not None and "denyByDefault" in reason


def test_an_auto_mode_job_still_runs_a_listed_command(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, {"shell": {"denyByDefault": True, "allowedCommands": [_LIVE_COMMAND]}})
    assert _auto_refusal(_live_shell_request(_client(tmp_path), tmp_path)) is None


@pytest.mark.parametrize("tool", ["write", "edit", "apply_patch"])
def test_an_auto_mode_job_refuses_a_write_outside_the_allowed_paths(
    tmp_path, agents_dir, tool
) -> None:
    _write_spec(agents_dir, _WRITE)
    event = _write_request(_client(tmp_path), tmp_path, tool, tmp_path / "src" / "a.py")
    reason = _auto_refusal(event)
    assert reason is not None and "fallbackAction" in reason


@pytest.mark.parametrize("tool", ["write", "edit", "apply_patch"])
def test_an_auto_mode_job_still_writes_inside_the_allowed_paths(tmp_path, agents_dir, tool) -> None:
    _write_spec(agents_dir, _WRITE)
    event = _write_request(_client(tmp_path), tmp_path, tool, tmp_path / "docs" / "a.md")
    assert _auto_refusal(event) is None


def test_a_spec_with_no_allowlist_stamps_nothing(tmp_path, agents_dir) -> None:
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    event = _live_shell_request(client, tmp_path)
    assert event.spec_shell_allowlist is None and event.spec_write_allowlist is None
    assert _auto_refusal(event) is None


def test_an_always_answer_is_sent_as_once(tmp_path, agents_dir, monkeypatch) -> None:
    """The live request offers ``always``; the client answers ``once``."""
    _write_spec(agents_dir, None)
    client = _client(tmp_path)
    event = _live_shell_request(client, tmp_path)
    monkeypatch.setattr(permission_floor, "refusal_for", lambda *_a, **_k: None)
    sent: list[str] = []

    async def _send(request_id, result, **kwargs):
        sent.append(result["outcome"]["optionId"])

    monkeypatch.setattr(client, "_send_response", _send)
    assert asyncio.run(client.approve_tool(event.request_id, always=True)) is True
    assert sent == ["once"]
