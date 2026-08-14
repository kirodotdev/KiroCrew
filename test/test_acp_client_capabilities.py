"""ACP `clientCapabilities` advertisement.

Locks in what KiroCrew declares during the ACP `initialize` handshake, and that
BOTH transports declare it. Before this, the key was omitted entirely, so the
agent assumed the all-false default.
"""

from pathlib import Path

from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES


def test_elicitation_is_declared() -> None:
    """kiro-cli gates `elicitation/create` on this capability being present."""
    assert ACP_CLIENT_CAPABILITIES["elicitation"] == {"form": {}, "url": {}}


def test_fs_and_terminal_stay_false() -> None:
    """We serve no `fs/*` or `terminal/*` handler, so we must not advertise them.

    Advertising either would invite inbound requests that
    `_reject_unknown_server_request` turns into errors.
    """
    assert ACP_CLIENT_CAPABILITIES["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }
    assert ACP_CLIENT_CAPABILITIES["terminal"] is False


def test_both_acp_transports_send_capabilities() -> None:
    """Both transports must advertise, not just one.

    `AcpClient` and `AcpRuntime` build their `initialize` params independently,
    so a capability added to one silently stays dark on the other. Asserted on
    source because neither params dict is reachable without spawning a real
    agent subprocess.
    """
    for rel in ("src/kiro_crew/acp/client.py", "src/kiro_crew/acp/runtime.py"):
        src = Path(__file__).resolve().parents[1] / rel
        # encoding is explicit: read_text() defaults to the locale codec, which
        # is cp1252 on the Windows CI shards, and these files contain non-ASCII
        # (em dashes / arrows) in their comments.
        assert "ACP_CLIENT_CAPABILITIES" in src.read_text(encoding="utf-8"), rel


def test_both_acp_transports_send_client_info_name() -> None:
    """Both transports must declare the client name under `clientInfo.name`.

    kiro-cli reads the driving ACP client name from the initialize request's
    `clientInfo.name` (agent/acp/acp_agent.rs: `if let Some(info) =
    request.client_info`). A flat top-level `clientName` key is ignored, which
    leaves the session unnamed in telemetry (bucketed as "(none)" instead of
    "kirocrew"). AcpRuntime previously sent the flat key; this locks in the
    nested form on BOTH transports. Asserted on source because neither params
    dict is reachable without spawning a real agent subprocess.
    """
    for rel in ("src/kiro_crew/acp/client.py", "src/kiro_crew/acp/runtime.py"):
        src = Path(__file__).resolve().parents[1] / rel
        text = src.read_text(encoding="utf-8")
        assert '"clientInfo": {"name": CLIENT_NAME' in text, rel
        # The flat key kiro-cli ignores must not come back.
        assert '"clientName": CLIENT_NAME' not in text, rel
