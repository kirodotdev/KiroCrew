"""An MCP tool's named document body is not read as a shell command line.

The always-enforced tool_input scan in ``_resolve_permission`` hands every
string of an MCP tool's arguments to the command-text rules. The core server's
``knowledge_add_document`` carries a whole document in ``content``, so a page
that merely mentions the product CLI's dashboard-link subcommand on its own
line was refused by the credential-mint argv floor. These tests pin the
scoping in ``platform.tool_paths.MCP_DOCUMENT_BODY_FIELDS``: the listed body
field skips the command-text rules but keeps the path tier and the size
ceiling, every other field keeps the full scan, and the exemption is keyed to
the trusted core-server identity, so a same-named tool on another server, an
untrusted identity, or a shell tool keeps the full scan.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.platform.tool_paths import (
    MCP_DOCUMENT_BODY_FIELDS,
    mcp_document_body_keys,
)
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

# Built by concatenation so this file never carries the subcommand line itself.
_CLI = "kiro" + "crew"
_MINT_LINE = f"{_CLI} " + "token"
_PROSE = (
    "# Dashboard access\n\n"
    "To open the dashboard from another machine, mint a link on the host:\n\n"
    f"{_MINT_LINE}\n\n"
    "Paste the printed link into a browser.\n"
)
_CORE = "kirocrew-core"
_TOOL = "knowledge_add_document"


class _RecordingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _params(content: str = _PROSE, **extra: str) -> dict:
    params = {
        "title": "Dashboard access",
        "content": content,
        "source_uri": "https://example.com/docs/dashboard",
    }
    params.update(extra)
    return params


def _event(
    params: dict,
    *,
    server: str = _CORE,
    tool_name: str = _TOOL,
    title: str = f"@{_CORE}/{_TOOL}",
    is_shell: bool = False,
    trusted: bool = True,
    identity: bool = True,
) -> LLMEvent:
    """A permission event as the ACP client emits it after a tool_call frame."""
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        request_id="r1",
        tool_kind="other",
        tool_input=json.dumps(params, indent=2),
        raw_tool_params=params,
        raw_params_trusted=trusted,
        shell_classified=trusted,
        is_shell=is_shell,
        mcp_identity_trusted=identity,
        mcp_server_name=server,
        tool_name=tool_name,
    )


async def _resolve(event: LLMEvent) -> tuple[bool, list[dict]]:
    provider = _RecordingProvider()
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            provider,  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, rows


def _error(rows: list[dict]) -> str:
    assert len(rows) == 1, rows
    return str(rows[0].get("error") or "")


class TestTheListIsExplicit:
    def test_only_the_core_knowledge_document_body_is_listed(self) -> None:
        assert dict(MCP_DOCUMENT_BODY_FIELDS) == {(_CORE, _TOOL): frozenset({"content"})}

    @pytest.mark.parametrize(
        "tool_name",
        [_TOOL, f"{_CORE}___{_TOOL}", f"mcp__{_CORE}__{_TOOL}"],
    )
    def test_core_identity_resolves_in_each_qualified_spelling(self, tool_name: str) -> None:
        assert mcp_document_body_keys(tool_name, _CORE) == frozenset({"content"})

    @pytest.mark.parametrize(
        ("tool_name", "server"),
        [
            (_TOOL, ""),
            (_TOOL, "notes-server"),
            (f"mcp__notes-server__{_TOOL}", _CORE),
            (f"{_TOOL}_extra", _CORE),
            ("artifact_save", _CORE),
            ("", _CORE),
        ],
    )
    def test_any_other_identity_has_no_body(self, tool_name: str, server: str) -> None:
        assert mcp_document_body_keys(tool_name, server) == frozenset()


class TestTheDocumentBodyIsNotACommandLine:
    @pytest.mark.asyncio
    async def test_prose_naming_the_mint_subcommand_is_allowed(self) -> None:
        approved, rows = await _resolve(_event(_params()))
        assert approved is True, _error(rows)

    @pytest.mark.asyncio
    async def test_the_full_scan_refuses_the_same_prose(self) -> None:
        # The rule this exemption narrows still fires on the text: an
        # untrusted-provenance frame keeps the full scan and is refused.
        approved, rows = await _resolve(_event(_params(), trusted=False))
        assert approved is False
        assert "token" in _error(rows)


class TestEverythingElseKeepsTheFullScan:
    @pytest.mark.asyncio
    async def test_the_same_text_as_a_shell_command_is_denied(self) -> None:
        ev = _event(
            {"command": _MINT_LINE},
            server="",
            tool_name="execute_bash",
            title=_MINT_LINE,
            is_shell=True,
        )
        approved, rows = await _resolve(ev)
        assert approved is False
        assert "token" in _error(rows)

    @pytest.mark.asyncio
    async def test_a_same_named_tool_on_another_server_is_denied(self) -> None:
        ev = _event(_params(), server="notes-server", title=f"@notes-server/{_TOOL}")
        approved, _rows = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    async def test_another_core_tool_with_the_same_text_is_denied(self) -> None:
        ev = _event(
            {"name": "runbook", "content": _PROSE},
            tool_name="artifact_save",
            title=f"@{_CORE}/artifact_save",
        )
        approved, _rows = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    async def test_an_untrusted_identity_is_denied(self) -> None:
        approved, _rows = await _resolve(_event(_params(), identity=False))
        assert approved is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["title", "source_uri", "reason"])
    async def test_a_non_body_field_is_still_command_scanned(self, field: str) -> None:
        approved, _rows = await _resolve(_event(_params(**{field: _MINT_LINE})))
        assert approved is False


class TestTheBodyKeepsThePathTier:
    @pytest.mark.asyncio
    async def test_a_credential_path_as_the_body_is_denied(self) -> None:
        approved, rows = await _resolve(_event(_params(content="~/.aws/credentials")))
        assert approved is False
        assert "sensitive path" in _error(rows)

    @pytest.mark.asyncio
    async def test_an_oversized_body_is_still_refused(self) -> None:
        from kiro_crew.llm_helpers import _MAX_SCANNABLE_TOOL_INPUT_CHARS

        body = "a" * (_MAX_SCANNABLE_TOOL_INPUT_CHARS + 1)
        approved, rows = await _resolve(_event(_params(content=body)))
        assert approved is False
        assert "too large" in _error(rows)
