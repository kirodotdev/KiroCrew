"""Every MCP stdio server must NAME ITSELF on loopback gateway requests.

#3503: the gateway's folder-audit path used to infer ``source=mcp`` from the
mere presence of ``X-Internal-Secret`` — correct only while exactly one
internal caller existed. The fix has two halves, and this file pins the
sending half: ``run_mcp_stdio_loop`` declares the server's component name
(``mcp_shared.set_internal_caller``) before any tool is dispatched, and the
``mcp_core`` request helpers attach it to every request as
``X-Internal-Caller``. The receiving half (validation against a known set,
``unknown-internal`` fallback) is pinned in ``test_chat_folder_audit_origin.py``.

A process that never declared an identity (CLI, tests) must send NO caller
header rather than a guessed one — absence is what the receiving side turns
into a loud ``unknown-internal`` audit, while a wrong guess would be trusted.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from kiro_crew import mcp_core, mcp_shared


@pytest.fixture(autouse=True)
def _isolated_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts undeclared, however earlier tests left the global."""
    monkeypatch.setattr(mcp_shared, "_internal_caller_name", None)


class _CapturedRequest:
    def __init__(self) -> None:
        self.req: Any = None


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturedRequest:
    """Route the mcp_core helpers' urlopen into a recorder returning ``{}``."""
    cap = _CapturedRequest()

    class _Resp(io.BytesIO):
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *a: Any) -> None:
            self.close()

    def _fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        cap.req = req
        return _Resp(json.dumps({}).encode())

    monkeypatch.setattr(mcp_core, "_api_urlopen", _fake_urlopen)
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "sekrit")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
    monkeypatch.setattr(mcp_core, "_api_base", lambda: "http://127.0.0.1:1")
    return cap


def _headers(cap: _CapturedRequest) -> dict[str, str]:
    return {k.lower(): v for k, v in cap.req.header_items()}


class TestCallerHeaderAttachment:
    def test_post_carries_declared_caller(self, captured: _CapturedRequest) -> None:
        mcp_shared.set_internal_caller("kirocrew-dashboard")
        mcp_core._post("/api/chat/folders", {"name": "x"})
        headers = _headers(captured)
        assert headers["x-internal-caller"] == "kirocrew-dashboard"
        assert headers["x-internal-secret"] == "sekrit"

    def test_get_carries_declared_caller(self, captured: _CapturedRequest) -> None:
        mcp_shared.set_internal_caller("kirocrew-dashboard")
        mcp_core._get("/api/chat/folders")
        assert _headers(captured)["x-internal-caller"] == "kirocrew-dashboard"

    def test_patch_carries_declared_caller(self, captured: _CapturedRequest) -> None:
        mcp_shared.set_internal_caller("kirocrew-dashboard")
        mcp_core._patch("/api/chat/folders/f1", {"parent_id": ""})
        assert _headers(captured)["x-internal-caller"] == "kirocrew-dashboard"

    def test_put_carries_declared_caller(self, captured: _CapturedRequest) -> None:
        mcp_shared.set_internal_caller("kirocrew-dashboard")
        mcp_core._put("/api/thing", {})
        assert _headers(captured)["x-internal-caller"] == "kirocrew-dashboard"

    def test_delete_carries_declared_caller(self, captured: _CapturedRequest) -> None:
        """DELETE is the one verb that destroys data — the case the audit
        exists for — so it must be attributable like the other four."""
        mcp_shared.set_internal_caller("kirocrew-dashboard")
        mcp_core._delete("/api/thing")
        assert _headers(captured)["x-internal-caller"] == "kirocrew-dashboard"

    def test_undeclared_process_sends_no_caller_header(
        self, captured: _CapturedRequest
    ) -> None:
        """No identity declared → no header. Absence is the honest signal the
        receiving side converts to ``unknown-internal``; a fabricated default
        would instead be trusted as a real component name."""
        mcp_core._post("/api/chat/folders", {"name": "x"})
        headers = _headers(captured)
        assert "x-internal-caller" not in headers
        assert headers["x-internal-secret"] == "sekrit"


class TestStdioLoopDeclaresIdentity:
    def test_loop_declares_server_name_before_dispatch_and_restores_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The declaration is centralized in ``run_mcp_stdio_loop`` — every
        current and FUTURE stdio server self-identifies without per-server
        wiring — and it must land before the dispatch loop serves anything,
        or the first tool call of a session would go out anonymous. On exit
        the prior value is restored (like the fd snapshot), so repeated loops
        in one process cannot leak one server's identity into later requests."""
        seen_at_dispatch: list[str | None] = []

        def _fake_dispatch(*a: Any, **k: Any) -> None:
            seen_at_dispatch.append(mcp_shared.internal_caller())

        monkeypatch.setattr(mcp_shared, "_run_stdio_dispatch_loop", _fake_dispatch)
        mcp_shared.run_mcp_stdio_loop(
            "test-server", "0.0", lambda: [], lambda _n, _a: ""
        )
        assert seen_at_dispatch == ["test-server"]
        assert mcp_shared.internal_caller() is None

    def test_loop_restores_identity_even_when_dispatch_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("dispatch died")

        monkeypatch.setattr(mcp_shared, "_run_stdio_dispatch_loop", _boom)
        with pytest.raises(RuntimeError):
            mcp_shared.run_mcp_stdio_loop(
                "test-server", "0.0", lambda: [], lambda _n, _a: ""
            )
        assert mcp_shared.internal_caller() is None
