"""Adapter log labels preserve the seam and identify the resolved program."""

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import (
    CLAUDE_ACP_BIN,
    CODEX_ACP_BIN,
    AcpClient,
    _adapter_spawn_label,
)


@pytest.mark.parametrize(
    ("argv", "seam", "expected"),
    [
        # The stable seam remains first, followed by the actual program.
        (
            ["/usr/local/bin/claude-agent-acp"],
            CLAUDE_ACP_BIN,
            "claude-agent-acp via /usr/local/bin/claude-agent-acp",
        ),
        (["/usr/local/bin/codex-acp"], CODEX_ACP_BIN, "codex-acp via /usr/local/bin/codex-acp"),
        # Both vendored adapters resolve to dist/index.js through node. The
        # stable seam identifies the adapter more usefully than that basename.
        (
            [
                "/usr/bin/node",
                "/opt/acp/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js",
            ],
            CLAUDE_ACP_BIN,
            "claude-agent-acp",
        ),
        # Launcher matching stays case-insensitive on Windows too.
        (
            [
                "node.EXE",
                "/opt/acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js",
            ],
            CODEX_ACP_BIN,
            "codex-acp",
        ),
        # An empty argv cannot name anything; the seam constant stands alone.
        ([], CODEX_ACP_BIN, CODEX_ACP_BIN),
        # A bare interpreter cannot identify an adapter.
        (["node"], CLAUDE_ACP_BIN, "claude-agent-acp"),
    ],
)
def test_label_names_the_seam_and_resolved_adapter(argv, seam, expected):
    assert _adapter_spawn_label(argv, seam) == expected


def test_override_to_a_dispatch_shim_keeps_the_seam_and_names_the_shim():
    """CLAUDE_AGENT_ACP_BIN may point at a shim that execs a different adapter.

    Keeping the seam alone told operators a Codex session was running on
    claude-agent-acp. The suffix records the program that actually launched.
    """
    label = _adapter_spawn_label(["/home/u/.local/bin/acp-dispatch"], CLAUDE_ACP_BIN)
    assert label == "claude-agent-acp via /home/u/.local/bin/acp-dispatch"


def test_kiro_client_carries_no_adapter_label():
    """Only the claude and codex spawn branches assign these.

    The kiro path leaves both unset, so its spawn and stderr labels resolve
    through the seam constants exactly as they did before.
    """
    client = AcpClient()

    assert client._adapter_label is None
    assert client._adapter_stderr_label is None


def test_resolved_adapter_argv_is_not_confused_with_a_sandbox_launcher():
    resolved_argv = ["/opt/acp/codex-acp"]
    wrapped_argv = ["env", "-u", "PYTHONPATH", *resolved_argv]

    assert _adapter_spawn_label(resolved_argv, CODEX_ACP_BIN) == "codex-acp via /opt/acp/codex-acp"
    assert _adapter_spawn_label(wrapped_argv, CODEX_ACP_BIN) == "codex-acp via env"


@pytest.mark.asyncio
async def test_kiro_stderr_keeps_its_existing_prefix():
    client = AcpClient()
    reader = AsyncMock(spec=["readline"])
    reader.readline = AsyncMock(side_effect=[b"adapter warning\\n", b""])

    with patch("kiro_crew.acp.client.logger") as logger:
        await client._drain_stderr(reader)

    assert logger.warning.call_args.args[1] == "kiro-cli"


@pytest.mark.asyncio
async def test_adapter_stderr_uses_its_resolved_label():
    client = AcpClient()
    client._adapter_stderr_label = "codex-acp via /opt/acp/codex-acp"
    reader = AsyncMock(spec=["readline"])
    reader.readline = AsyncMock(side_effect=[b"adapter warning\\n", b""])

    with patch("kiro_crew.acp.client.logger") as logger:
        await client._drain_stderr(reader)

    assert logger.warning.call_args.args[1] == "codex-acp via /opt/acp/codex-acp"
