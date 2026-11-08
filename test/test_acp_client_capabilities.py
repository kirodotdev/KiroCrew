"""ACP `clientCapabilities` advertisement.

Locks in what KiroCrew declares during the ACP `initialize` handshake, and that
BOTH transports declare it. Before this, the key was omitted entirely, so the
agent assumed the all-false default.
"""

from pathlib import Path

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.client_turn import _process_message
from kiro_crew.acp.types import (
    ACP_CLIENT_CAPABILITIES,
    METHOD_ELICITATION_CREATE,
    METHOD_KAS_MCP_ELICITATION,
    JsonRpcMessage,
)


def test_elicitation_is_advertised_only_with_a_handler_on_both_drivers() -> None:
    """`elicitation` is declared, and every driver classifies the method it invites.

    The capability makes harnesses forward `elicitation/create`; a `-32601` from
    `_reject_unknown_server_request` would cancel the tool call. So both classifiers must
    route it to `elicitation`.
    """
    assert ACP_CLIENT_CAPABILITIES["elicitation"] == {"form": {}, "url": {}}
    for method in (METHOD_ELICITATION_CREATE, METHOD_KAS_MCP_ELICITATION):
        msg = JsonRpcMessage(id=7, method=method, params={"sessionId": "s"})
        assert classify_notification(msg) == "elicitation", method
        assert _process_message(_ClassifierHost(), msg, req_id=1) == "elicitation", method


class _ClassifierHost:
    """The slice of ``AcpClient`` ``_process_message`` reads before its method checks."""

    _steer_turn = None

    def _note_active_run(self, msg: JsonRpcMessage) -> None:
        raise AssertionError("an elicitation request is not a session update")

    def _settle_steer_answer(self, msg: JsonRpcMessage) -> bool:
        return False


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

    Each builds `initialize` params on its own; asserted on source because neither is
    reachable without a real subprocess. The runtime's half lives in each kiro harness.
    """
    for rel in (
        "src/kiro_crew/acp/client.py",
        "src/kiro_crew/acp/harness/kiro.py",
        "src/kiro_crew/acp/harness/kas.py",
    ):
        src = Path(__file__).resolve().parents[1] / rel
        # encoding is explicit: read_text() defaults to the locale codec, which
        # is cp1252 on the Windows CI shards, and these files contain non-ASCII
        # (em dashes / arrows) in their comments.
        assert "CLIENT_CAPABILITIES" in src.read_text(encoding="utf-8"), rel


def _runtime_family_text() -> str:
    """runtime.py and its owner modules (runtime_*.py), which hold code that was its own."""
    import sys

    from kiro_crew.acp import runtime

    return "".join(
        Path(sys.modules[name].__file__).read_text(encoding="utf-8")
        for name in (runtime.__name__, *runtime._RUNTIME_OWNERS)
    )


def test_the_shared_process_transport_reads_capabilities_from_its_host() -> None:
    """The runtime must not carry a capability literal of its own.

    A second copy beside the harness's would be free to drift from the one
    actually sent, and the drift is invisible: both spellings compile and only a
    live session shows which set went out.
    """
    text = _runtime_family_text()
    # The declaration is read from the harness; the runtime only fills the
    # settings channel a host declares (``client_meta_settings``) before sending.
    assert "base = self._harness.client_capabilities" in text
    assert '"clientCapabilities": client_capabilities' in text
    assert "ACP_CLIENT_CAPABILITIES" not in text
    assert "KAS_CLIENT_CAPABILITIES" not in text


def test_both_acp_transports_send_client_info_name() -> None:
    """Both transports must declare the client name under `clientInfo.name`.

    kiro-cli ignores a flat `clientName` (agent/acp/acp_agent.rs), leaving telemetry at
    "(none)". Asserted on source because neither params dict is reachable without a spawn.
    """
    for rel in ("src/kiro_crew/acp/client.py", "src/kiro_crew/acp/runtime.py"):
        src = Path(__file__).resolve().parents[1] / rel
        # client.py's owner modules (client_*.py) hold code that was its own.
        paths = [src, *sorted(src.parent.glob("client_*.py"))] if src.name == "client.py" else [src]
        text = "".join(p.read_text(encoding="utf-8") for p in paths)
        if src.name == "runtime.py":
            text = _runtime_family_text()
        assert '"clientInfo": {"name": CLIENT_NAME' in text, rel
        # The flat key kiro-cli ignores must not come back.
        assert '"clientName": CLIENT_NAME' not in text, rel


def test_client_version_is_the_package_version() -> None:
    """The version in `clientInfo` must be the package's, not a literal of its own.

    A literal has no bumper, so `acp_client_version` telemetry could not split releases.
    `__version__` tracks both the release lane and a repackager's `BUILD_VERSION` stamp.
    """
    import kiro_crew
    from kiro_crew.acp import client

    assert client.CLIENT_VERSION == kiro_crew.__version__


def test_client_identity_is_declared_once_for_both_transports() -> None:
    """`runtime` must re-export the identity pair, never re-declare it.

    A second pair of literals in `runtime.py` could report a different client. Checked on
    source too: an equal but separate literal passes today and drifts later.
    """
    from kiro_crew.acp import client, runtime

    assert runtime.CLIENT_NAME is client.CLIENT_NAME
    assert runtime.CLIENT_VERSION is client.CLIENT_VERSION

    text = _runtime_family_text()
    assert "CLIENT_NAME = " not in text
    assert "CLIENT_VERSION = " not in text
