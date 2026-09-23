"""Adapter log labels preserve the seam and identify the resolved program."""

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import (
    _CLAUDE_ACP_PKG_ENTRY,
    _CODEX_ACP_PKG_ENTRY,
    _PI_ACP_PKG_ENTRY,
    CLAUDE_ACP_BIN,
    CODEX_ACP_BIN,
    PI_ACP_BIN,
    AcpClient,
    _adapter_spawn_label,
)


@pytest.mark.parametrize(
    ("argv", "seam", "pkg_entry", "expected"),
    [
        # The stable seam remains first, followed by the actual program.
        (
            ["/usr/local/bin/claude-agent-acp"],
            CLAUDE_ACP_BIN,
            _CLAUDE_ACP_PKG_ENTRY,
            "claude-agent-acp via /usr/local/bin/claude-agent-acp",
        ),
        (
            ["/usr/local/bin/codex-acp"],
            CODEX_ACP_BIN,
            _CODEX_ACP_PKG_ENTRY,
            "codex-acp via /usr/local/bin/codex-acp",
        ),
        # An installed adapter resolves to its own packaged `dist/index.js`,
        # whose basename names the packaging rather than the adapter, so the
        # seam alone is the useful identity there.
        (
            [
                "/usr/bin/node",
                "/opt/acp/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js",
            ],
            CLAUDE_ACP_BIN,
            _CLAUDE_ACP_PKG_ENTRY,
            "claude-agent-acp",
        ),
        # Launcher matching stays case-insensitive on Windows too.
        (
            [
                "node.EXE",
                "/opt/acp/node_modules/@agentclientprotocol/codex-acp/dist/index.js",
            ],
            CODEX_ACP_BIN,
            _CODEX_ACP_PKG_ENTRY,
            "codex-acp",
        ),
        (
            ["/usr/bin/node", "/opt/acp/node_modules/pi-acp/dist/index.js"],
            PI_ACP_BIN,
            _PI_ACP_PKG_ENTRY,
            "pi-acp",
        ),
        # Any other `index.js` is not the adapter's own entry, and its path is
        # the only record of which build actually launched.
        (
            ["/usr/bin/node", "/home/u/.acp/vendor/acme-acp/dist/index.js"],
            CLAUDE_ACP_BIN,
            _CLAUDE_ACP_PKG_ENTRY,
            "claude-agent-acp via /home/u/.acp/vendor/acme-acp/dist/index.js",
        ),
        # Nothing to compare against means nothing is suppressed.
        (
            ["/usr/bin/node", "/opt/x/dist/index.js"],
            CLAUDE_ACP_BIN,
            None,
            "claude-agent-acp via /opt/x/dist/index.js",
        ),
        # An empty argv cannot name anything; the seam constant stands alone.
        ([], CODEX_ACP_BIN, _CODEX_ACP_PKG_ENTRY, CODEX_ACP_BIN),
        # A bare interpreter cannot identify an adapter.
        (["node"], CLAUDE_ACP_BIN, _CLAUDE_ACP_PKG_ENTRY, "claude-agent-acp"),
    ],
)
def test_label_names_the_seam_and_resolved_adapter(argv, seam, pkg_entry, expected):
    assert _adapter_spawn_label(argv, seam, pkg_entry=pkg_entry) == expected


def test_override_to_a_dispatch_shim_keeps_the_seam_and_names_the_shim():
    """CLAUDE_AGENT_ACP_BIN may point at a shim that execs a different adapter.

    Keeping the seam alone told operators a Codex session was running on
    claude-agent-acp. The suffix records the program that actually launched.
    """
    label = _adapter_spawn_label(["/home/u/.local/bin/acp-dispatch"], CLAUDE_ACP_BIN)
    assert label == "claude-agent-acp via /home/u/.local/bin/acp-dispatch"


def test_override_supplying_the_package_entry_keeps_its_path(monkeypatch):
    """A deliberate override is never collapsed back to the bare seam.

    Suppressing the path here would erase the only record of which build the
    operator pointed the seam at, which is what the suffix exists to record.
    """
    script = "/home/u/.acp/override/@agentclientprotocol/claude-agent-acp/dist/index.js"
    monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", script)

    label = _adapter_spawn_label(
        ["/usr/bin/node", script],
        CLAUDE_ACP_BIN,
        pkg_entry=_CLAUDE_ACP_PKG_ENTRY,
        override_env="CLAUDE_AGENT_ACP_BIN",
    )

    assert label == f"claude-agent-acp via {script}"


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
async def test_adapter_stderr_uses_the_label_its_call_site_resolved():
    """The spawn passes the resolved adapter label; nothing re-derives it here."""
    client = AcpClient()
    reader = AsyncMock(spec=["readline"])
    reader.readline = AsyncMock(side_effect=[b"adapter warning\\n", b""])

    with patch("kiro_crew.acp.client.logger") as logger:
        await client._drain_stderr(reader, label="codex-acp via /opt/acp/codex-acp")

    assert logger.warning.call_args.args[1] == "codex-acp via /opt/acp/codex-acp"
