"""Strands in Crew Mode: a member's saved spec and a crewmate's worker kit.

The Strands mirror composes the session's array (``providers/mirrors/strands.py``) and
the launch writes it to the ``--mcp-config`` file the CLI mounts at start. These drive
the REAL resolver over a real spec on disk and read what comes out; the live round
trips are ``test/fixtures/acp_frames/strands/saved-spec-projection-live.jsonl`` (a
member DM on a saved spec) and ``worker-kit-live.jsonl`` (a crewmate's worker).

Member session control and the member panel are NOT carried: each is a stdio Crew
child inside the session's sandbox, and the enforced credential mask hides the
gateway credential from it, so every call would be refused.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import test_mate_worker_harnesses as worker_harness
import test_member_mount_deepseek as deepseek_mount

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew import acp_tool_gate, agent_state, mate_worker
from kiro_crew.acp.transport_errors import AcpError
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
from kiro_crew.agent_capabilities import _digest, member_backend_gap
from kiro_crew.config.paths import config_dir
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.providers.mirrors import mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition

agents_dir = deepseek_mount.agents_dir
pixel_worker = worker_harness.pixel_worker
MEMBER_KEY = deepseek_mount.MEMBER_KEY
CONTROL_PLANE_SERVER = deepseek_mount.CONTROL_PLANE_SERVER
FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "strands"


def _frames(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _note(name: str) -> str:
    first = (FIXTURES / name).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first)["_meta"]["note"]


def _client(tmp_path: Path, session_key: str = MEMBER_KEY):
    return deepseek_mount._client(tmp_path, session_key, backend=ACP_BACKEND_STRANDS)


def _names(out: list[dict]) -> list[str]:
    return [e["name"] for e in out]


class TestTheDecision:
    def test_a_saved_spec_loads_but_its_tool_limits_do_not(self):
        assert ACP_BACKEND_STRANDS in ACP_BACKENDS_MEMBER_CAPABILITIES
        assert member_backend_gap(ACP_BACKEND_STRANDS, ()) is None
        assert ACP_BACKEND_STRANDS not in ACP_BACKENDS_MEMBER_TOOL_LIMITS
        assert member_backend_gap(ACP_BACKEND_STRANDS, ("docs",)) == "tool_limits_unsupported"

    def test_a_worker_runs_with_its_prompt_in_the_block_and_no_tool_limits(self):
        assert ACP_BACKEND_STRANDS in ACP_BACKENDS_MATE_WORKER_KIT
        assert mate_worker.worker_kit_backend_refusal(ACP_BACKEND_STRANDS) is None
        # The mirror projects no spec prompt, so the block must carry it.
        rulings = mirror_for(ACP_BACKEND_STRANDS).rulings()
        assert rulings[Concern.PROMPT].disposition is Disposition.WITHHELD
        assert ACP_BACKEND_STRANDS in ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT
        assert ACP_BACKEND_STRANDS not in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS

    def test_session_control_and_the_panel_stay_off_because_the_mask_hides_the_credential(
        self,
    ):
        """The member mounts would run inside the sandbox, where the gateway
        credential is hidden, so every call would be refused."""
        assert ACP_BACKEND_STRANDS not in ACP_BACKENDS_MEMBER_DISPATCH
        assert ACP_BACKEND_STRANDS not in ACP_BACKENDS_MEMBER_PANEL
        assert acp_tool_gate.is_enforced(ACP_BACKEND_STRANDS) is True
        hidden = {
            os.path.normcase(p)
            for p in acp_tool_gate.adapter_hidden_credential_dirs(ACP_BACKEND_STRANDS)
        }
        home = config_dir()
        assert os.path.normcase(str(home / ".local_secret")) in hidden
        assert os.path.normcase(str(home / "run")) in hidden


class TestTheMemberArray:
    def test_a_member_session_gets_no_member_mounts(self, agents_dir, tmp_path):
        client = _client(tmp_path)
        assert _names(client._resolve_session_mcp_servers()) == [CONTROL_PLANE_SERVER]
        assert client.member_dispatch_mounted is False


class TestTheSavedSpec:
    SAVED = {"name": "kirocrew", "description": "a saved member", "tools": ["*"]}

    def _member(self, tmp_path):
        client = _client(tmp_path)
        client.member_context = True
        client._session_id = "strands-session-1"
        client._confirmed_capability_session = None
        client._loaded_capability_template = ""
        client._capability_projection_gaps = ()
        return client

    def _save(self, agents_dir, monkeypatch, spec):
        (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
        intent = {"materialized": _digest(spec)}
        monkeypatch.setattr(agent_state, "get_capabilities", lambda _template: intent)

    def test_a_saved_spec_is_confirmed(self, agents_dir, tmp_path, monkeypatch):
        self._save(agents_dir, monkeypatch, self.SAVED)
        client = self._member(tmp_path)
        client._resolve_session_mcp_servers()
        asyncio.run(client.confirm_member_projection())
        assert client._loaded_capability_template == "kirocrew"
        assert client._confirmed_capability_session == "strands-session-1"

    def test_a_spec_edited_after_its_save_is_refused(self, agents_dir, tmp_path, monkeypatch):
        self._save(agents_dir, monkeypatch, self.SAVED)
        (agents_dir / "kirocrew.json").write_text(
            json.dumps(dict(self.SAVED, description="edited after it was saved")),
            encoding="utf-8",
        )
        client = self._member(tmp_path)
        client._resolve_session_mcp_servers()
        with pytest.raises(AcpError, match="capability_runtime_unverified"):
            asyncio.run(client.confirm_member_projection())
        assert client._loaded_capability_template == ""


class TestTheWorkerKit:
    def test_the_mirror_reads_the_workers_spec_by_its_session(self, pixel_worker):
        """Without the session key a worker would run on the bare base."""
        mirror = mirror_for(ACP_BACKEND_STRANDS)
        worker = mirror.session_projection(MATE_WORKER_AGENT, session_key="dashboard:chat-9")
        assert "jira" in _names(worker.params["mcpServers"])
        bare = mirror.session_projection(MATE_WORKER_AGENT)
        assert "jira" not in _names(bare.params["mcpServers"])

    def test_a_tool_limited_worker_is_refused(self, pixel_worker):
        limited = {
            **worker_harness.PIXEL,
            "mcpServers": {"jira": {"command": "jira-mcp", "disabledTools": ["x"]}},
        }
        (pixel_worker / "pixel.json").write_text(json.dumps(limited), encoding="utf-8")
        with pytest.raises(Exception, match="tool_limits_unsupported"):
            mirror_for(ACP_BACKEND_STRANDS).session_projection(
                MATE_WORKER_AGENT, session_key="dashboard:chat-9"
            )


class TestTheCaptures:
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

    def test_the_tools_arrived_through_the_launch_file_and_not_under_the_mask(self):
        for name in ("saved-spec-projection-live.jsonl", "worker-kit-live.jsonl"):
            note = _note(name)
            assert "--mcp-config" in note and "empty array" in note
            # Stated in the header so a reader does not take it for a masked run.
            assert "NOT under the enforced credential mask" in note

    def test_the_worker_reached_its_crewmates_own_server(self):
        assert self._calls(_frames("worker-kit-live.jsonl")) == {"mcp__probe__echo": "kumquat-7"}
