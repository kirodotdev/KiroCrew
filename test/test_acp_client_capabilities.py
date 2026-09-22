"""ACP `clientCapabilities` advertisement.

Locks in what KiroCrew declares during the ACP `initialize` handshake, and that
BOTH transports declare it. Before this, the key was omitted entirely, so the
agent assumed the all-false default.
"""

from pathlib import Path

from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES


def test_elicitation_is_not_advertised_without_a_handler() -> None:
    """Declaring `elicitation` with no handler is worse than declaring nothing.

    It reads like a free forward-bet and is not one. A client that sees the
    capability sends its human-in-the-loop prompts as `elicitation/create`
    INSTEAD of falling back to `session/request_permission`; codex-acp gates on
    `clientCapabilities.elicitation.form` exactly that way. We answer that method
    with `-32601`, and the client turns the error into a cancellation of the tool
    call the human was approving -- so the declaration does not wait quietly for
    a handler, it breaks approval on every client that believes it.

    Re-add the key in the same change that registers the handler.
    """
    assert "elicitation" not in ACP_CLIENT_CAPABILITIES


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

    The two build their `initialize` params independently, so a capability added
    to one silently stays dark on the other. Asserted on source because neither
    params dict is reachable without spawning a real agent subprocess.

    The shared-process transport reads the set from its host's harness, so that
    is where its half of the proof lives; every harness that serves a kiro-family
    host must name the constant itself.
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


def test_the_shared_process_transport_reads_capabilities_from_its_host() -> None:
    """The runtime must not carry a capability literal of its own.

    A second copy beside the harness's would be free to drift from the one
    actually sent, and the drift is invisible: both spellings compile and only a
    live session shows which set went out.
    """
    src = Path(__file__).resolve().parents[1] / "src/kiro_crew/acp/runtime.py"
    text = src.read_text(encoding="utf-8")
    # The declaration is read from the harness; the runtime only fills the
    # settings channel a host declares (``client_meta_settings``) before sending.
    assert "base = self._harness.client_capabilities" in text
    assert '"clientCapabilities": client_capabilities' in text
    assert "ACP_CLIENT_CAPABILITIES" not in text
    assert "KAS_CLIENT_CAPABILITIES" not in text


def test_both_acp_transports_send_client_info_name() -> None:
    """Both transports must declare the client name under `clientInfo.name`.

    kiro-cli reads the driving ACP client name from the initialize request's
    `clientInfo.name` (agent/acp/acp_agent.rs: `if let Some(info) =
    request.client_info`). A flat top-level `clientName` key is ignored, which
    leaves the session unnamed in telemetry (bucketed as "(none)" instead of
    "kirocrew"). AcpRuntime sends the nested form on BOTH transports. Asserted on source because neither params
    dict is reachable without spawning a real agent subprocess.
    """
    for rel in ("src/kiro_crew/acp/client.py", "src/kiro_crew/acp/runtime.py"):
        src = Path(__file__).resolve().parents[1] / rel
        text = src.read_text(encoding="utf-8")
        assert '"clientInfo": {"name": CLIENT_NAME' in text, rel
        # The flat key kiro-cli ignores must not come back.
        assert '"clientName": CLIENT_NAME' not in text, rel


def test_client_version_is_the_package_version() -> None:
    """The version in `clientInfo` must be the package's, not a literal of its own.

    A hand-maintained literal here is a second version number with no bumper.
    It had one: `CLIENT_VERSION` sat at `"0.1.2"` from the commit that introduced
    it while the product shipped 0.2.0 through 0.8.0, so every Crew-driven
    session of every release reported the same version to the agent host and its
    `acp_client_version` telemetry could not split Crew traffic by release at all.

    Binding it to `__version__` is also what makes it track the two places the
    version really moves: a release lane rewriting the literal in
    `kiro_crew/__init__.py`, and a repackager's `BUILD_VERSION` stamp, which that
    module resolves at import before any reader copies the attribute.
    """
    import kiro_crew
    from kiro_crew.acp import client

    assert client.CLIENT_VERSION == kiro_crew.__version__


def test_client_identity_is_declared_once_for_both_transports() -> None:
    """`runtime` must re-export the identity pair, never re-declare it.

    Both transports send ONE `clientInfo` object built from these two names, so a
    second pair of literals in `runtime.py` is free to report a different client
    to the same host. That is not hypothetical: the flat-`clientName` regression
    survived precisely because the two transports each owned a copy of the
    handshake, so one was correct while the other was silently unnamed.

    Checked on source as well as by identity because an equal-but-separate
    literal passes the value check on the day it is written and drifts later.
    """
    from kiro_crew.acp import client, runtime

    assert runtime.CLIENT_NAME is client.CLIENT_NAME
    assert runtime.CLIENT_VERSION is client.CLIENT_VERSION

    text = (Path(__file__).resolve().parents[1] / "src/kiro_crew/acp/runtime.py").read_text(
        encoding="utf-8"
    )
    assert "CLIENT_NAME = " not in text
    assert "CLIENT_VERSION = " not in text
