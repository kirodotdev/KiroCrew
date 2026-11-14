"""A saved MCP sign-in the OAuth server stopped honouring, on the KAS backend.

KAS drops a remote MCP server's saved tokens, and reports ``failedAuthorization``,
only when the token endpoint refuses a refresh with ``invalid_grant``,
``invalid_client`` or ``unauthorized_client``. Any other refusal keeps the tokens
and reports the server ``failed`` without ``failedAuthorization``, so nothing
tracked it for a sign-in and a chat message naming it started none.

The error texts below use the shapes a KAS 3.2.1 engine reported against a local
OAuth server, with transient HTTP statuses added to exercise their classification.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.mcp_reattempt import is_refused_sign_in
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, JsonRpcMessage

_KEPT = " The sign-in is kept; retry the server when the token endpoint answers again."
_REFUSED_400 = "Sign-in for notes could not be refreshed (token endpoint answered 400)." + _KEPT
_REFUSED_401 = "Sign-in for notes could not be refreshed (token endpoint answered 401)." + _KEPT
_REFUSED_403 = "Sign-in for notes could not be refreshed (token endpoint answered 403)." + _KEPT
_TIMEOUT_408 = "Sign-in for notes could not be refreshed (token endpoint answered 408)." + _KEPT
_TOO_EARLY_425 = "Sign-in for notes could not be refreshed (token endpoint answered 425)." + _KEPT
_THROTTLED_429 = "Sign-in for notes could not be refreshed (token endpoint answered 429)." + _KEPT
_SERVER_ERROR = (
    "Sign-in for notes could not be refreshed (token endpoint answered 500 server_error)." + _KEPT
)
_UNREACHABLE = (
    "Sign-in for notes could not be refreshed (token endpoint could not be reached)." + _KEPT
)


@pytest.mark.parametrize(
    "message",
    [
        _REFUSED_400,
        _REFUSED_401,
        _REFUSED_403,
        "invalid_token",
        "unknown_error",
        "access_denied",
    ],
)
def test_a_refusal_from_the_token_endpoint_reads_as_a_refused_sign_in(message):
    assert is_refused_sign_in(message) is True


@pytest.mark.parametrize(
    "message",
    [
        # A later attempt can succeed with the same tokens, and a sign-in reset
        # would throw them away.
        _TIMEOUT_408,
        _TOO_EARLY_425,
        _THROTTLED_429,
        _SERVER_ERROR,
        _UNREACHABLE,
        "Sign-in for notes could not be refreshed (token endpoint answered 503).",
        # The engine names an error code here only for one it reads as passing.
        "Sign-in for notes could not be refreshed (token endpoint answered 400 server_error).",
        "server_error",
        "temporarily_unavailable",
        # Not an OAuth answer at all.
        "MCP server 'notes' connection timed out after 60000ms",
        "SSE error: Non-200 status code (405)",
        "spawn ENOENT",
        "fetch failed",
        "Unauthorized",
        "",
        None,
        401,
    ],
)
def test_any_other_failure_does_not(message):
    assert is_refused_sign_in(message) is False


def _handle(begun: list) -> AcpSessionHandle:
    def begin(session_id, name):
        begun.append((session_id, name))
        return True

    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = "s1"
    handle._runtime = SimpleNamespace(begin_mcp_sign_in=begin)
    handle._mcp_sign_in_needed = set()
    handle._mcp_sign_in_wanted = set()
    handle._mcp_sign_in_ask = ""
    handle._mcp_sign_in_asked = set()
    handle._mcp_sign_in_completed = set()
    handle._mcp_sign_in_dropped = 0
    handle._mcp_reattempt_waiting = set()
    handle._mcp_reattempt_counts = {}
    handle._oauth_emitted_servers = set()
    return handle


def _status(error: str, status: str = "failed") -> JsonRpcMessage:
    server = {"name": "notes", "status": status, "failedAuthorization": False}
    if error:
        server["errorMessage"] = error
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": "s1", "servers": [server]}
    )


@pytest.mark.parametrize(
    "error", [_REFUSED_400, _REFUSED_401, _REFUSED_403, "invalid_token", "unknown_error"]
)
def test_a_refused_saved_sign_in_is_tracked_and_naming_the_server_starts_one(error):
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(error), offer=True)
    assert handle._mcp_sign_in_needed == {"notes"}
    # Being tracked starts nothing: the user asks by naming the server.
    assert begun == []
    assert handle._mcp_reattempt_waiting == set()
    handle.want_mcp_sign_in_named("please sign in to notes")
    handle._offer_mcp_sign_in()
    assert begun == [("s1", "notes")]


@pytest.mark.parametrize(
    "error",
    [_TIMEOUT_408, _TOO_EARLY_425, _THROTTLED_429, _SERVER_ERROR, _UNREACHABLE, "spawn ENOENT"],
)
def test_a_failure_a_retry_can_cure_is_not_offered_a_sign_in(error):
    begun: list = []
    handle = _handle(begun)
    handle._note_mcp_sign_in_status(_status(error), offer=True)
    handle.want_mcp_sign_in_named("please sign in to notes")
    handle._offer_mcp_sign_in()
    assert handle._mcp_sign_in_needed == set()
    assert begun == []


def test_a_refused_server_that_connects_again_is_no_longer_tracked():
    handle = _handle([])
    handle._note_mcp_sign_in_status(_status(_REFUSED_401), offer=False)
    handle._note_mcp_sign_in_status(_status("", status="connected"), offer=False)
    assert handle._mcp_sign_in_needed == set()


@pytest.mark.parametrize("error", [_REFUSED_400, _REFUSED_401, _REFUSED_403])
def test_the_session_report_lists_a_refused_sign_in_as_waiting_for_sign_in(error):
    report = McpSessionReport()
    assert report.record_frame(_status(error), owned=True)
    payload = report.payload()
    assert payload["awaiting_auth"] == ["notes"]
    assert payload["failed"] == []
    assert payload["restart_to_load"] == []


@pytest.mark.parametrize("error", [_TIMEOUT_408, _TOO_EARLY_425, _THROTTLED_429, _SERVER_ERROR])
def test_the_session_report_keeps_a_curable_failure_as_failed(error):
    report = McpSessionReport()
    assert report.record_frame(_status(error), owned=True)
    payload = report.payload()
    assert payload["awaiting_auth"] == []
    assert payload["failed"] == ["notes"]
