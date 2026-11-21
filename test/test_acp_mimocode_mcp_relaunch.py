"""An MCP change reaching a live MiMo Code chat by relaunch + ``session/load``.

``mimo acp`` mounts the ``mcpServers`` array of each ``session/new`` and
``session/load``. The captures in
``test/fixtures/acp_frames/mimocode/mcp-relaunch-*-live.jsonl`` show a second process
loading a first process's session with a changed array: an added server's tool is
called and the earlier turns are kept, and a withdrawn server's tool is not offered.
These tests pin that evidence, the membership, the client and provider gates, and the
launch clearing the mark right before it reads the spec. The turn-boundary mechanics
are the shared ``acp/relaunch_reload.py`` ones, pinned in
``test/test_acp_strands_mcp_relaunch.py``. No process is spawned.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from kiro_crew.acp import relaunch_reload
from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_MIMOCODE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD,
    ACP_BACKENDS_MCP_RELAUNCH_RELOAD,
    ACP_BACKENDS_MCP_WIRE_REINJECT,
)
from kiro_crew.mcp_hot_reload import (
    live_sessions_apply_mcp_live,
    live_sessions_hot_reload,
    mark_reinjecting_providers,
    provider_relaunches_for_mcp,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "mimocode"


def _frames(name: str) -> list[dict]:
    lines = (_FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _after_load(name: str) -> tuple[dict, list[dict], list[dict]]:
    """The ``session/load`` answer, the frames replayed before it, and those after it."""
    frames = _frames(name)
    load_at = next(i for i, f in enumerate(frames) if f.get("id") == 2 and "method" not in f)
    return frames[load_at], frames[:load_at], frames[load_at + 1 :]


def _updates(frames: list[dict], kind: str) -> list[dict]:
    return [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == kind
    ]


def _text(frames: list[dict]) -> str:
    return "".join(u["content"]["text"] for u in _updates(frames, "agent_message_chunk"))


class TestCapturedEvidence:
    def test_a_relaunch_with_an_added_server_calls_it_and_keeps_the_history(self) -> None:
        load, replayed, after = _after_load("mcp-relaunch-add-live.jsonl")
        assert "modes" in load["result"]
        # The first process's call of alpha comes back as replayed history.
        assert [u["title"] for u in _updates(replayed, "tool_call")] == ["alpha_alpha_codeword"]
        titles = [u["title"] for u in _updates(after, "tool_call")]
        assert titles == ["beta_beta_codeword"]
        text = _text(after)
        assert "plum-17" in text
        assert "kiwi-31" in text

    def test_a_relaunch_without_a_server_no_longer_offers_it(self) -> None:
        load, replayed, after = _after_load("mcp-relaunch-remove-live.jsonl")
        assert "modes" in load["result"]
        assert [u["title"] for u in _updates(replayed, "tool_call")] == ["alpha_alpha_codeword"]
        assert _updates(after, "tool_call") == []
        assert "NO-BETA" in _text(after)


class TestMembership:
    def test_mimocode_is_a_member(self) -> None:
        assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MCP_RELAUNCH_RELOAD

    def test_mimocode_takes_no_other_live_mechanism(self) -> None:
        assert ACP_BACKEND_MIMOCODE not in ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD
        assert ACP_BACKEND_MIMOCODE not in ACP_BACKENDS_MCP_WIRE_REINJECT


class TestGate:
    def test_a_mimocode_client_declares_it_and_its_parent_does_not(self, tmp_path) -> None:
        mimo = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        opencode = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert relaunch_reload.mcp_config_relaunch_reload(mimo)
        assert not relaunch_reload.mcp_config_relaunch_reload(opencode)

    def test_a_relaunching_provider_skips_the_reset_and_is_marked(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_MIMOCODE)
        assert provider.mcp_config_relaunch_reload is True
        assert provider_relaunches_for_mcp(provider)
        assert live_sessions_apply_mcp_live([provider])
        assert not live_sessions_hot_reload([provider])
        assert mark_reinjecting_providers([provider]) == 1
        assert provider._client._mcp_relaunch_pending is True


def test_the_mimocode_launch_clears_the_mark_right_before_it_reads_the_spec() -> None:
    """A mark set before this read is applied by this launch; one set after it survives."""
    from kiro_crew.acp.harness import mimocode as mimocode_mod

    source = inspect.getsource(mimocode_mod.MimoCodeLaunch.resolve_spawn)
    clear = source.index("session._mcp_relaunch_pending = False")
    read = source.index("await session._prepare_session_mcp()")
    assert clear < read
    assert "await" not in source[clear:read].replace("await session._prepare_session_mcp()", "")
