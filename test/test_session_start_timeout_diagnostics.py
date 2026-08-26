"""Session-start timeout diagnostics: name the MCP server that never reported.

A session/new or session/load that blows its budget used to report only the
budget. The runtime already holds both halves of the answer at that moment --
the roster it sent in ``mcpServers`` and the registration frames the reader loop
staged -- so these tests pin that the timeout carries them.

The ordering matters and is what most of these guard: ``_finish_session_init``
runs in the call site's ``finally`` and CLEARS the staging buffer, so the read
has to happen in the ``except`` ahead of it. Every test here therefore lets the
REAL ``_finish_session_init`` run; mocking it would make a read-after-clear
regression pass.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.runtime import (
    _MCP_PROGRESS_NAME_CAP,
    _MCP_PROGRESS_NAME_LEN_CAP,
    AcpRequestTimeout,
    AcpRuntime,
    AcpRuntimeError,
    _capped_names,
)
from kiro_crew.acp.types import (
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    JsonRpcMessage,
)


def _runtime() -> AcpRuntime:
    """An initialized runtime whose subprocess is faked (no kiro-cli spawned)."""
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt


def _roster(*names: str) -> list[dict]:
    """An ``mcpServers`` array shaped the way session_servers builds it."""
    return [{"name": n, "command": "/bin/true", "args": [], "env": []} for n in names]


def _stage(rt: AcpRuntime, method: str, name: str, **extra) -> None:
    """Stage one registration frame as the reader loop would during an init."""
    params = {"serverName": name}
    params.update(extra)
    rt._pending_init_notifications.append(JsonRpcMessage(method=method, params=params))


def _timeout(rt: AcpRuntime) -> None:
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpRequestTimeout("Request session/new timed out after 90s")
    )


@pytest.mark.asyncio
async def test_session_new_timeout_names_the_servers_that_never_reported():
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "beta")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha", "beta", "gamma", "delta"))

    text = str(caught.value)
    assert "timed out after 90s" in text  # the budget survives
    assert "2/4 MCP server(s) reported" in text
    assert "no report from gamma, delta" in text
    # The servers that DID report are not the ones to chase.
    assert "no report from alpha" not in text


@pytest.mark.asyncio
async def test_session_new_timeout_survives_the_staging_buffer_being_cleared():
    """The read must happen in the except, ahead of the finally that clears.

    Moving it after ``_finish_session_init`` empties the buffer and loses every
    name, so this is the mutation guard for the ordering.
    """
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha", "beta"))

    assert "no report from beta" in str(caught.value)
    # The real _finish_session_init ran and did its job: nothing staged, no
    # in-flight init left behind for the next attempt to misread.
    assert not rt._pending_init_notifications
    assert rt._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_session_new_timeout_scrubs_a_failed_servers_error_text():
    """A failed server's error can carry its own credentials -- scrub, don't leak."""
    rt = _runtime()
    _stage(
        rt,
        METHOD_MCP_SERVER_INIT_FAILURE,
        "gamma",
        error="connect failed: https://evil.example.com/?token=AKIAIOSFODNN7EXAMPLE",
    )
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("gamma"))

    text = str(caught.value)
    assert "failed: gamma" in text
    # The credential is gone. The host survives inside the redactor's own marker
    # by design -- an operator chasing an exfil attempt needs to know where it
    # pointed -- so assert on the marker rather than on the host's absence.
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "REDACTED" in text


@pytest.mark.asyncio
async def test_session_new_timeout_flags_a_server_awaiting_authorization():
    rt = _runtime()
    _stage(rt, METHOD_MCP_OAUTH_REQUEST, "drive", oauthUrl="https://example.com/auth")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("drive"))

    text = str(caught.value)
    assert "awaiting authorization: drive" in text
    # An OAuth request is not a report: the server is still missing.
    assert "no report from drive" in text


@pytest.mark.asyncio
async def test_session_new_timeout_counts_reports_when_the_roster_is_unknown():
    """An empty roster still yields a count -- never a bare budget."""
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=[])

    assert "1 MCP server(s) reported, roster unknown" in str(caught.value)


@pytest.mark.asyncio
async def test_session_new_timeout_notes_a_concurrent_init_is_runtime_wide():
    """Staged frames cannot be attributed to a session id that never returned."""
    rt = _runtime()
    rt._session_inits_in_flight = 1  # another init already open
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha", "beta"))

    assert "session inits in flight" in str(caught.value)
    assert "runtime-wide" in str(caught.value)


@pytest.mark.asyncio
async def test_session_load_timeout_names_the_servers_that_never_reported(monkeypatch):
    """session/load shares the budget and the staging, so it gets the same report.

    It resolves its own roster rather than taking one, so the resolver is what
    gets stubbed here.
    """
    rt = _runtime()
    rt._can_load_session = True
    monkeypatch.setattr(
        "kiro_crew.acp.runtime.pooled_session_servers",
        lambda *_a, **_k: _roster("alpha", "beta"),
    )
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    rt._send_and_await = AsyncMock(  # type: ignore[method-assign]
        side_effect=AcpRequestTimeout("Request session/load timed out after 90s")
    )

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.load_session(session_file="", resume_sid="s1")

    text = str(caught.value)
    assert "session/load timed out" in text
    assert "no report from beta" in text


@pytest.mark.asyncio
async def test_a_non_timeout_session_new_failure_gets_no_mcp_progress():
    """Only a stall is explained by MCP progress; a protocol fault is not."""
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    rt._send_and_await = AsyncMock(return_value={})  # no sessionId

    with pytest.raises(AcpRuntimeError) as caught:
        await rt.create_session(mcp_servers=_roster("alpha", "beta"))

    text = str(caught.value)
    assert "did not return sessionId" in text
    assert "MCP server(s) reported" not in text


def test_request_timeout_is_still_catchable_as_a_runtime_error():
    """Callers keying on AcpRuntimeError must keep catching timeouts."""
    assert issubclass(AcpRequestTimeout, AcpRuntimeError)


def test_capped_names_summarizes_the_tail_instead_of_listing_a_whole_fleet():
    names = [f"srv{i}" for i in range(_MCP_PROGRESS_NAME_CAP + 5)]
    text = _capped_names(names)
    assert text.count(",") == _MCP_PROGRESS_NAME_CAP - 1
    assert text.endswith("(+5 more)")
    assert _capped_names(["only"]) == "only"


# -- Names are config-derived, so an installed app chooses them --


@pytest.mark.asyncio
async def test_a_newline_in_a_server_name_cannot_forge_a_log_line():
    """The summary goes into a logger.warning; a name must not add a line to it."""
    rt = _runtime()
    forged = "beta\n2026-08-18 00:00:00 ERROR kiro_crew: injected"
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "alpha")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha", forged))

    text = str(caught.value)
    assert "\n" not in text
    assert "beta 2026-08-18" in text  # collapsed onto one line, not split


@pytest.mark.asyncio
async def test_an_overlong_server_name_is_length_capped():
    """The count cap alone does not bound the string; one huge name would."""
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INIT_FAILURE, "x" * 5000, error="nope")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha"))

    text = str(caught.value)
    assert "x" * (_MCP_PROGRESS_NAME_LEN_CAP + 1) not in text
    assert "x" * _MCP_PROGRESS_NAME_LEN_CAP in text


@pytest.mark.asyncio
async def test_a_credential_shaped_server_name_is_scrubbed():
    """Uses the failed bucket: a name only in `ready` is never printed at all,
    so asserting on it would pass whether or not the scrub runs."""
    rt = _runtime()
    _stage(
        rt, METHOD_MCP_SERVER_INIT_FAILURE, "svc-AKIAIOSFODNN7EXAMPLE", error="nope"
    )
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha"))

    text = str(caught.value)
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "svc-" in text  # scrubbed, not dropped -- the name stays identifiable


@pytest.mark.asyncio
async def test_reports_outside_the_roster_never_produce_an_impossible_ratio():
    """kiro-cli also initializes the agent spec's servers, so frames are a superset.

    Counting them against the injected roster yielded nonsense like "2/1 reported".
    """
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "in-roster")
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "from-agent-spec")
    _stage(rt, METHOD_MCP_SERVER_INITIALIZED, "also-agent-spec")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("in-roster"))

    text = str(caught.value)
    assert "1/1 MCP server(s) reported" in text
    assert "2/1" not in text and "3/1" not in text


@pytest.mark.asyncio
async def test_an_out_of_roster_failure_is_still_named():
    """Excluding them from the COUNT must not hide a server that actually failed."""
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INIT_FAILURE, "from-agent-spec", error="boom")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("in-roster"))

    text = str(caught.value)
    assert "0/1 MCP server(s) reported" in text
    assert "failed: from-agent-spec (boom)" in text


@pytest.mark.asyncio
async def test_a_terminal_escape_in_error_text_is_stripped():
    """`kirocrew logs` renders the gateway log in a terminal; an ESC in a failed
    server's error text would let it recolor or forge terminal output. ESC is
    not whitespace, so a whitespace collapse alone would keep it."""
    rt = _runtime()
    _stage(
        rt,
        METHOD_MCP_SERVER_INIT_FAILURE,
        "gamma",
        error="boom \x1b[31mFAKE ERROR\x1b[0m \x07\x08 done",
    )
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("gamma"))

    text = str(caught.value)
    assert "\x1b" not in text and "\x07" not in text and "\x08" not in text
    # The printable words survive the strip.
    assert "boom" in text and "FAKE ERROR" in text and "done" in text


@pytest.mark.asyncio
async def test_a_terminal_escape_in_a_server_name_is_stripped():
    """A name takes the same strip: it reaches the same logger.warning, and the
    failed bucket prints it verbatim next to its error text."""
    rt = _runtime()
    _stage(rt, METHOD_MCP_SERVER_INIT_FAILURE, "delta\x1b[2Jwiped", error="nope")
    _timeout(rt)

    with pytest.raises(AcpRequestTimeout) as caught:
        await rt.create_session(mcp_servers=_roster("alpha"))

    text = str(caught.value)
    assert "\x1b" not in text
    assert "delta[2Jwiped" in text
