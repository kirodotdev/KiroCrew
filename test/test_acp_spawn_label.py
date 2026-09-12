"""The adapter log label names the program that ran, not the seam."""

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
        # node carries the entry script, so the full path distinguishes default packages.
        (
            ["/usr/bin/node", "/opt/acp/dist/index.js"],
            CLAUDE_ACP_BIN,
            "claude-agent-acp via /opt/acp/dist/index.js",
        ),
        # The Windows launcher name is recognised too; the path separator is
        # whatever the running platform's Path understands, so keep this POSIX.
        (
            ["node.exe", "/opt/acp/dist/index.js"],
            CLAUDE_ACP_BIN,
            "claude-agent-acp via /opt/acp/dist/index.js",
        ),
        # The launcher name is matched case-insensitively: Windows reports it
        # in whatever case the shim chose (node.EXE, Node.exe).
        (
            ["node.EXE", "/opt/acp/dist/index.js"],
            CLAUDE_ACP_BIN,
            "claude-agent-acp via /opt/acp/dist/index.js",
        ),
        # An empty argv cannot name anything; the seam constant stands alone.
        ([], CODEX_ACP_BIN, CODEX_ACP_BIN),
        # A bare interpreter still identifies the exact command.
        (["node"], CLAUDE_ACP_BIN, "claude-agent-acp via node"),
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


def test_resolved_adapter_argv_is_not_confused_with_a_sandbox_launcher():
    resolved_argv = ["/opt/acp/codex-acp"]
    wrapped_argv = ["env", "-u", "PYTHONPATH", *resolved_argv]

    assert _adapter_spawn_label(resolved_argv, CODEX_ACP_BIN) == "codex-acp via /opt/acp/codex-acp"
    assert _adapter_spawn_label(wrapped_argv, CODEX_ACP_BIN) == "codex-acp via env"


@pytest.mark.asyncio
async def test_kiro_stderr_uses_its_existing_prefix_explicitly():
    client = AcpClient()
    reader = AsyncMock(spec=["readline"])
    reader.readline = AsyncMock(side_effect=[b"adapter warning\\n", b""])

    with patch("kiro_crew.acp.client.logger") as logger:
        await client._drain_stderr(reader, label="kiro-cli")

    assert logger.warning.call_args.args[1] == "kiro-cli"


@pytest.mark.asyncio
async def test_adapter_stderr_uses_its_pre_wrap_label():
    client = AcpClient()
    reader = AsyncMock(spec=["readline"])
    reader.readline = AsyncMock(side_effect=[b"adapter warning\\n", b""])

    with patch("kiro_crew.acp.client.logger") as logger:
        await client._drain_stderr(reader, label="codex-acp via /opt/acp/codex-acp")

    assert logger.warning.call_args.args[1] == "codex-acp via /opt/acp/codex-acp"
