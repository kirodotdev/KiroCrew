"""Strands stays out of Crew Mode's member-facing sets, and why.

A member or crewmate-worker session's Crew servers (``kirocrew-core``, the dashboard
and panel mounts) are stdio children the Strands CLI starts inside the session's
sandbox, and the enforced credential mask hides the gateway credential from them, so
their gateway-backed calls would be refused (``internal_secret_missing``). Strands is
therefore refused for those sessions before spawn rather than run with tools that
cannot work.

The live round trip ``test/fixtures/acp_frames/strands/saved-spec-projection-live.jsonl``
(recorded with no mask) shows a saved spec confirmed and its own server reached through
the ``--mcp-config`` file, the evidence the harness can join on once a host-side
control-plane transport exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import acp_tool_gate, mate_worker
from kiro_crew.acp.types import (
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_MATE_WORKER_KIT,
    ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT,
    ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_MEMBER_TOOL_LIMITS,
)
from kiro_crew.agent_capabilities import member_backend_gap
from kiro_crew.config.paths import config_dir

agents_dir = deepseek_mount.agents_dir
MEMBER_KEY = deepseek_mount.MEMBER_KEY
CONTROL_PLANE_SERVER = deepseek_mount.CONTROL_PLANE_SERVER
FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "strands"

#: Every member-facing set whose sessions need a working in-sandbox Crew server.
MEMBER_FACING_SETS = {
    "ACP_BACKENDS_MEMBER_CAPABILITIES": ACP_BACKENDS_MEMBER_CAPABILITIES,
    "ACP_BACKENDS_MEMBER_TOOL_LIMITS": ACP_BACKENDS_MEMBER_TOOL_LIMITS,
    "ACP_BACKENDS_MEMBER_DISPATCH": ACP_BACKENDS_MEMBER_DISPATCH,
    "ACP_BACKENDS_MEMBER_PANEL": ACP_BACKENDS_MEMBER_PANEL,
    "ACP_BACKENDS_MATE_WORKER_KIT": ACP_BACKENDS_MATE_WORKER_KIT,
    "ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT": ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT,
    "ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS": ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
}


def _frames(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _note(name: str) -> str:
    first = (FIXTURES / name).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first)["_meta"]["note"]


def _names(out: list[dict]) -> list[str]:
    return [e["name"] for e in out]


class TestTheExclusion:
    @pytest.mark.parametrize("name", sorted(MEMBER_FACING_SETS))
    def test_strands_is_in_no_member_facing_set(self, name):
        assert ACP_BACKEND_STRANDS not in MEMBER_FACING_SETS[name]

    def test_the_mask_hides_the_gateway_credential_from_its_children(self):
        """Why the sets exclude it: an in-sandbox Crew server cannot authenticate."""
        assert acp_tool_gate.is_enforced(ACP_BACKEND_STRANDS) is True
        hidden = {
            os.path.normcase(p)
            for p in acp_tool_gate.adapter_hidden_credential_dirs(ACP_BACKEND_STRANDS)
        }
        home = config_dir()
        assert os.path.normcase(str(home / ".local_secret")) in hidden
        assert os.path.normcase(str(home / "run")) in hidden

    def test_a_member_session_is_refused_before_spawn(self):
        assert member_backend_gap(ACP_BACKEND_STRANDS, ()) == "harness_unsupported"

    def test_a_crewmate_worker_is_refused_with_its_remedy(self):
        refusal = mate_worker.worker_kit_backend_refusal(ACP_BACKEND_STRANDS)
        assert refusal is not None and "(strands)" in refusal

    def test_a_member_key_gets_no_member_mounts(self, agents_dir, tmp_path):
        client = deepseek_mount._client(tmp_path, MEMBER_KEY, backend=ACP_BACKEND_STRANDS)
        assert _names(client._resolve_session_mcp_servers()) == [CONTROL_PLANE_SERVER]
        assert client.member_dispatch_mounted is False


class TestTheCapture:
    def _calls(self, frames: list[dict]) -> dict[str, str]:
        """Each requested tool's title -> the text its completed update carried."""
        asked = {
            f["params"]["toolCall"]["toolCallId"].split(":")[2]: f["params"]["toolCall"]["title"]
            for f in frames
            if f.get("method") == "session/request_permission"
        }
        done = {}
        for f in frames:
            update = f.get("params", {}).get("update", {})
            if update.get("sessionUpdate") == "tool_call_update" and update.get("status") == (
                "completed"
            ):
                done[update["toolCallId"]] = update["content"][0]["content"]["text"]
        return {title: done[use_id] for use_id, title in asked.items()}

    def test_the_member_session_reached_its_saved_specs_own_server(self):
        calls = self._calls(_frames("saved-spec-projection-live.jsonl"))
        assert calls == {"mcp__probe__echo": "kumquat-7"}

    def test_it_did_not_run_under_the_mask(self):
        """Stated in the header so a reader does not take it for a masked run."""
        note = _note("saved-spec-projection-live.jsonl")
        assert "--mcp-config" in note and "empty array" in note
        assert "NOT under the enforced credential mask" in note
