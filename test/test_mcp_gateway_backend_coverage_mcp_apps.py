"""How the MCP Apps helpers gate a tool result, fetch its ``ui://`` resource and parse it.

These tests pin ``_parse_ui_contents`` on inline text with its CSP and permissions, on a base64 blob
and on the entries it rejects (invalid base64, a non-object, a wrong MIME type, neither text nor
blob), with a non-object ``_meta.ui`` yielding no CSP or permissions;
``_maybe_intercept_ui_result``, which intercepts nothing with the feature flag off, on another
method, on a non-object result or on a tool with no UI association, and records the ``ui://``
declarations a ``tools/list`` result carries while a non-object one leaves the recorded map alone;
and ``_read_ui_resource``, whose ``resources/read`` raises on an error reply (clearing its pending
entry), a malformed result or empty contents, and returns the contents on success. A topic of the
``test_mcp_gateway_backend_coverage`` family: ``test_mcp_gateway_backend_coverage.py`` keeps the
client-extension injection, the other siblings are
``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``, and the shared fixture and helpers live
in ``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import _frames, _line, _make_backend

from kiro_crew.mcp_gateway.backend import (
    MCP_APPS_ENV_FLAG,
    MCP_APPS_MIME_TYPE,
    Backend,
    _PendingRequest,
)

# --- MCP Apps helpers -------------------------------------------------------


class TestParseUiContents:
    def test_inline_text_with_csp_and_permissions(self) -> None:
        backend = _make_backend()
        html, csp, perms = backend._parse_ui_contents(
            [
                {
                    "mimeType": MCP_APPS_MIME_TYPE,
                    "text": "<h1>hi</h1>",
                    "_meta": {"ui": {"csp": "default-src 'none'", "permissions": ["clipboard"]}},
                }
            ]
        )
        assert html == "<h1>hi</h1>"
        assert csp == "default-src 'none'"
        assert perms == ["clipboard"]

    def test_base64_blob_decoded(self) -> None:
        backend = _make_backend()
        blob = base64.b64encode(b"<p>b</p>").decode("ascii")
        html, csp, perms = backend._parse_ui_contents(
            [{"mimeType": MCP_APPS_MIME_TYPE, "blob": blob}]
        )
        assert (html, csp, perms) == ("<p>b</p>", None, None)

    def test_invalid_base64_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="invalid base64 blob"):
            backend._parse_ui_contents(
                [{"mimeType": MCP_APPS_MIME_TYPE, "blob": "!!!not-base64!!!"}]
            )

    def test_non_object_entry_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="is not an object"):
            backend._parse_ui_contents(["nope"])

    def test_wrong_mime_type_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="unexpected mimeType"):
            backend._parse_ui_contents([{"mimeType": "text/plain", "text": "x"}])

    def test_neither_text_nor_blob_rejected(self) -> None:
        backend = _make_backend()
        with pytest.raises(RuntimeError, match="neither text nor blob"):
            backend._parse_ui_contents([{"mimeType": MCP_APPS_MIME_TYPE}])

    def test_non_dict_meta_yields_no_csp(self) -> None:
        backend = _make_backend()
        _, csp, perms = backend._parse_ui_contents(
            [{"mimeType": MCP_APPS_MIME_TYPE, "text": "x", "_meta": {"ui": "bad"}}]
        )
        assert csp is None and perms is None


class TestInterceptionGating:
    @pytest.mark.asyncio
    async def test_flag_off_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://x/y.html"}
        pending = _PendingRequest("s1", 1, "tools/call", tool_name="draw")
        assert await backend._maybe_intercept_ui_result(pending, {"result": {}}) is False

    @pytest.mark.asyncio
    async def test_other_methods_and_shapes_never_intercept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        # Not a tools/call.
        assert (
            await backend._maybe_intercept_ui_result(
                _PendingRequest("s1", 1, "resources/list"), {"result": {}}
            )
            is False
        )
        # tools/call whose result is not an object.
        assert (
            await backend._maybe_intercept_ui_result(
                _PendingRequest("s1", 1, "tools/call"), {"result": "text"}
            )
            is False
        )
        # tools/call with no ui association anywhere.
        assert (
            await backend._maybe_intercept_ui_result(
                _PendingRequest("s1", 1, "tools/call", tool_name="draw"),
                {"result": {"content": []}},
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_tools_list_harvest_records_declarations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        msg = {
            "result": {
                "tools": [
                    {
                        "name": "draw",
                        "inputSchema": {"type": "object", "properties": {}},
                        "_meta": {"ui": {"resourceUri": "ui://draw/app.html"}},
                    }
                ]
            }
        }
        assert (
            await backend._maybe_intercept_ui_result(_PendingRequest("s1", 1, "tools/list"), msg)
            is False
        )
        assert backend._apps_declared_uris == {"draw": "ui://draw/app.html"}

    @pytest.mark.asyncio
    async def test_tools_list_with_non_dict_result_leaves_map_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://keep"}
        assert (
            await backend._maybe_intercept_ui_result(
                _PendingRequest("s1", 1, "tools/list"), {"result": None}
            )
            is False
        )
        assert backend._apps_declared_uris == {"draw": "ui://keep"}


class TestReadUiResource:
    @pytest.mark.asyncio
    async def test_error_reply_raises_and_clears_pending(self) -> None:
        backend = _make_backend()

        async def _answer(payload: dict[str, Any]) -> None:
            # flake-ok: pre-existing one-tick yield, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            await backend._route_backend_line(_line(payload))

        task = asyncio.create_task(
            _answer({"id": "gw-4242-1", "error": {"code": -1, "message": "nope"}})
        )
        with pytest.raises(RuntimeError, match="resources/read error"):
            await backend._read_ui_resource("ui://x/y.html")
        # flake-ok: pre-existing await of the reply task, which the read above already consumed, moved verbatim
        await task
        assert backend._pending_requests == {}
        assert _frames(backend)[0]["method"] == "resources/read"

    @pytest.mark.asyncio
    async def test_malformed_result_raises(self) -> None:
        backend = _make_backend()
        task = asyncio.create_task(_deferred_route(backend, {"id": "gw-4242-1", "result": "text"}))
        with pytest.raises(RuntimeError, match="malformed result"):
            await backend._read_ui_resource("ui://x/y.html")
        # flake-ok: pre-existing await of the reply task, which the read above already consumed, moved verbatim
        await task

    @pytest.mark.asyncio
    async def test_empty_contents_raises(self) -> None:
        backend = _make_backend()
        task = asyncio.create_task(
            _deferred_route(backend, {"id": "gw-4242-1", "result": {"contents": []}})
        )
        with pytest.raises(RuntimeError, match="no contents"):
            await backend._read_ui_resource("ui://x/y.html")
        # flake-ok: pre-existing await of the reply task, which the read above already consumed, moved verbatim
        await task

    @pytest.mark.asyncio
    async def test_contents_returned_on_success(self) -> None:
        backend = _make_backend()
        entry = {"mimeType": MCP_APPS_MIME_TYPE, "text": "<b>ok</b>"}
        task = asyncio.create_task(
            _deferred_route(backend, {"id": "gw-4242-1", "result": {"contents": [entry]}})
        )
        assert await backend._read_ui_resource("ui://x/y.html") == [entry]
        # flake-ok: pre-existing await of the reply task, which the read above already consumed, moved verbatim
        await task


async def _deferred_route(backend: Backend, payload: dict[str, Any]) -> None:
    # flake-ok: pre-existing one-tick yield, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0)
    await backend._route_backend_line(_line(payload))
