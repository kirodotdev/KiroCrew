"""Tests for MCP Apps result interception + disk spool (SEP-1865).

Two layers:

* Pure-function unit tests for :mod:`kiro_crew.mcp_gateway.apps`
  (:func:`extract_ui_resource_uri`, :func:`append_marker`,
  :func:`write_spool`, :func:`sweep_spool`) — no event loop needed.
* An async end-to-end test of the backend's parking/injection seam: a fake
  (mock-stdin) :class:`Backend` is driven through a tools/call response that
  carries a ui:// resource, the gateway-originated ``resources/read`` is
  observed on stdin, its reply is fed back through the routing path, and the
  stub inbox is asserted to receive the MARKED response while a spool file is
  written to disk.

Interception is gated by ``KIROCREW_MCP_APPS`` and MUST be a no-op when off.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_gateway import apps
from kiro_crew.mcp_gateway.apps import (
    MARKER_PREFIX,
    SCHEMA_VERSION,
    append_marker,
    extract_ui_resource_uri,
    spool_dir,
    strip_model_hidden_tools,
    sweep_spool,
    write_spool,
)
from kiro_crew.mcp_gateway.backend import (
    MCP_APPS_ENV_FLAG,
    MCP_APPS_MIME_TYPE,
    Backend,
    _PendingRequest,
)
from kiro_crew.mcp_gateway.pool import PoolKey


@pytest.fixture
def spool_tmp(tmp_path, monkeypatch):
    """Point the spool at an isolated tmp dir for the duration of a test."""
    d = tmp_path / "mcp-apps"
    monkeypatch.setenv(apps.SPOOL_ENV, str(d))
    return d


# --------------------------------------------------------------------------
# extract_ui_resource_uri
# --------------------------------------------------------------------------

class TestExtractUiResourceUri:
    def test_nested_form(self):
        result = {"_meta": {"ui": {"resourceUri": "ui://excalidraw/app.html"}}}
        assert extract_ui_resource_uri(result) == "ui://excalidraw/app.html"

    def test_deprecated_flat_form(self):
        result = {"_meta": {"ui/resourceUri": "ui://x/y.html"}}
        assert extract_ui_resource_uri(result) == "ui://x/y.html"

    def test_nested_takes_precedence_over_flat(self):
        result = {"_meta": {
            "ui": {"resourceUri": "ui://nested"},
            "ui/resourceUri": "ui://flat",
        }}
        assert extract_ui_resource_uri(result) == "ui://nested"

    def test_non_ui_scheme_rejected(self):
        for uri in ("http://evil/x", "file:///etc/passwd", "https://a", "ftp://a"):
            result = {"_meta": {"ui": {"resourceUri": uri}}}
            assert extract_ui_resource_uri(result) is None

    def test_missing_meta(self):
        assert extract_ui_resource_uri({}) is None
        assert extract_ui_resource_uri({"content": []}) is None

    def test_missing_ui(self):
        assert extract_ui_resource_uri({"_meta": {"other": 1}}) is None

    def test_non_dict_inputs(self):
        assert extract_ui_resource_uri(None) is None  # type: ignore[arg-type]
        assert extract_ui_resource_uri("x") is None  # type: ignore[arg-type]
        assert extract_ui_resource_uri({"_meta": "bad"}) is None

    def test_non_string_uri_rejected(self):
        assert extract_ui_resource_uri({"_meta": {"ui": {"resourceUri": 42}}}) is None


# --------------------------------------------------------------------------
# append_marker
# --------------------------------------------------------------------------

class TestAppendMarker:
    def test_appends_to_first_text_item(self):
        result = {"content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "second"},
        ]}
        out = append_marker(result, "abc123")
        assert out["content"][0]["text"] == "hello [kirocrew-mcp-app:abc123]"
        # Only the FIRST text item is marked.
        assert out["content"][1]["text"] == "second"

    def test_skips_non_text_finds_first_text(self):
        result = {"content": [
            {"type": "image", "data": "..."},
            {"type": "text", "text": "caption"},
        ]}
        out = append_marker(result, "id9")
        assert out["content"][1]["text"] == "caption [kirocrew-mcp-app:id9]"

    def test_no_text_item_appends_new_item(self):
        result = {"content": [{"type": "image", "data": "..."}]}
        out = append_marker(result, "zz")
        assert out["content"][-1] == {"type": "text", "text": "[kirocrew-mcp-app:zz]"}

    def test_empty_content_appends_new_item(self):
        out = append_marker({"content": []}, "q")
        assert out["content"] == [{"type": "text", "text": "[kirocrew-mcp-app:q]"}]

    def test_missing_content_key_appends_new_item(self):
        out = append_marker({}, "m")
        assert out["content"] == [{"type": "text", "text": "[kirocrew-mcp-app:m]"}]

    def test_input_not_mutated(self):
        result = {"content": [{"type": "text", "text": "orig"}], "isError": False}
        import copy
        before = copy.deepcopy(result)
        out = append_marker(result, "x")
        assert result == before
        assert out is not result
        assert out["content"] is not result["content"]
        assert out["content"][0] is not result["content"][0]

    def test_marker_prefix_constant_used(self):
        out = append_marker({"content": [{"type": "text", "text": "a"}]}, "sid")
        assert MARKER_PREFIX in out["content"][0]["text"]


# --------------------------------------------------------------------------
# write_spool / sweep_spool
# --------------------------------------------------------------------------

class TestWriteSpool:
    def test_writes_file_with_schema_fields(self, spool_tmp):
        sid = write_spool({
            "server": "excalidraw-mcp",
            "tool": "draw",
            "session_key": "dashboard:abc",
            "html": "<html>hi</html>",
            "csp": "default-src 'self'",
            "permissions": ["clipboard"],
            "structured_content": {"k": "v"},
        })
        assert isinstance(sid, str) and len(sid) == 32  # uuid4().hex
        path = spool_tmp / f"{sid}.json"
        assert path.exists()
        record = json.loads(path.read_text())
        assert record["schema"] == SCHEMA_VERSION
        assert record["server"] == "excalidraw-mcp"
        assert record["tool"] == "draw"
        assert record["session_key"] == "dashboard:abc"
        assert record["html"] == "<html>hi</html>"
        assert record["csp"] == "default-src 'self'"
        assert record["permissions"] == ["clipboard"]
        assert record["structured_content"] == {"k": "v"}
        assert record["created_at"]  # auto-populated

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX permission bits are a no-op on Windows"
    )
    def test_file_mode_0600_dir_0700(self, spool_tmp):
        sid = write_spool({"html": "x"})
        path = spool_tmp / f"{sid}.json"
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        dir_mode = stat.S_IMODE(os.stat(spool_tmp).st_mode)
        assert file_mode == 0o600, oct(file_mode)
        assert dir_mode == 0o700, oct(dir_mode)

    def test_defaults_for_absent_keys(self, spool_tmp):
        sid = write_spool({"html": "only"})
        record = json.loads((spool_tmp / f"{sid}.json").read_text())
        assert record["server"] == ""
        assert record["tool"] == ""
        assert record["session_key"] == ""
        assert record["csp"] is None
        assert record["permissions"] is None
        assert record["structured_content"] is None

    def test_created_at_honored_if_supplied(self, spool_tmp):
        sid = write_spool({"html": "x", "created_at": "2020-01-01T00:00:00+00:00"})
        record = json.loads((spool_tmp / f"{sid}.json").read_text())
        assert record["created_at"] == "2020-01-01T00:00:00+00:00"

    def test_tool_input_and_result_content_persisted(self, spool_tmp):
        """Additive v1 fields: the originating tools/call arguments and result
        content ride in the record so the app initializes from real state."""
        sid = write_spool({
            "html": "x",
            "tool_input": {"url": "https://example.com/a.pdf"},
            "result_content": [{"type": "text", "text": "opened"}],
        })
        record = json.loads((spool_tmp / f"{sid}.json").read_text())
        assert record["tool_input"] == {"url": "https://example.com/a.pdf"}
        assert record["result_content"] == [{"type": "text", "text": "opened"}]
        # Absent -> None (readers .get() them; schema stays v1).
        sid2 = write_spool({"html": "y"})
        record2 = json.loads((spool_tmp / f"{sid2}.json").read_text())
        assert record2["tool_input"] is None
        assert record2["result_content"] is None

    def test_unique_ids(self, spool_tmp):
        ids = {write_spool({"html": "x"}) for _ in range(20)}
        assert len(ids) == 20

    def test_spool_dir_env_override(self, spool_tmp):
        assert spool_dir() == spool_tmp


class TestSweepSpool:
    def test_missing_dir_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv(apps.SPOOL_ENV, str(tmp_path / "does-not-exist"))
        assert sweep_spool() == 0

    def test_removes_only_stale(self, spool_tmp):
        fresh = write_spool({"html": "fresh"})
        stale = write_spool({"html": "stale"})
        stale_path = spool_tmp / f"{stale}.json"
        old = time.time() - 48 * 3600
        os.utime(stale_path, (old, old))

        removed = sweep_spool(max_age_hours=24.0)
        assert removed == 1
        assert not stale_path.exists()
        assert (spool_tmp / f"{fresh}.json").exists()

    def test_all_fresh_removes_none(self, spool_tmp):
        write_spool({"html": "a"})
        write_spool({"html": "b"})
        assert sweep_spool(max_age_hours=24.0) == 0

    def test_reaps_rendered_sidecar_with_record(self, spool_tmp):
        stale = write_spool({"html": "stale"})
        stale_path = spool_tmp / f"{stale}.json"
        sidecar = spool_tmp / f"{stale}.rendered"
        sidecar.touch()
        old = time.time() - 48 * 3600
        os.utime(stale_path, (old, old))

        assert sweep_spool(max_age_hours=24.0) == 1
        assert not stale_path.exists()
        assert not sidecar.exists()

    def test_reaps_orphaned_stale_sidecar(self, spool_tmp):
        spool_tmp.mkdir(parents=True, exist_ok=True)
        orphan = spool_tmp / f"{'a' * 32}.rendered"
        orphan.touch()
        old = time.time() - 48 * 3600
        os.utime(orphan, (old, old))
        sweep_spool(max_age_hours=24.0)
        assert not orphan.exists()

    def test_write_spool_sweeps_opportunistically(self, spool_tmp):
        """Every write reaps expired records so a long-running flag-on
        gateway stays bounded without relying on restarts (the arbiter's
        'dead code' finding: sweep_spool must actually be invoked)."""
        stale = write_spool({"html": "stale"})
        stale_path = spool_tmp / f"{stale}.json"
        old = time.time() - 48 * 3600
        os.utime(stale_path, (old, old))

        fresh = write_spool({"html": "fresh"})
        assert not stale_path.exists()
        assert (spool_tmp / f"{fresh}.json").exists()


class TestSpoolDirAgreement:
    def test_writer_and_reader_agree_under_kirocrew_home(self, tmp_path, monkeypatch):
        """Regression (found in live pod testing): the gateway writer once
        hardcoded ``Path.home()`` while the dashboard reader used
        ``config_dir()`` — under KIROCREW_HOME (pods) the writer spooled into
        the LIVE plane's home and the reader found nothing. Both sides MUST
        resolve identically with only KIROCREW_HOME set (no spool override)."""
        from kiro_crew import mcp_apps_render
        monkeypatch.delenv(apps.SPOOL_ENV, raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "isolated-home"))
        assert apps.spool_dir() == mcp_apps_render._spool_dir()
        assert str(tmp_path / "isolated-home") in str(apps.spool_dir())


class TestExtractDeclaredUiUris:
    def test_harvests_declared_tools(self):
        result = {"tools": [
            {"name": "display_pdf", "_meta": {"ui": {"resourceUri": "ui://pdf/app.html"}}},
            {"name": "list_pdfs"},
            {"name": "legacy", "_meta": {"ui/resourceUri": "ui://legacy/app.html"}},
        ]}
        assert apps.extract_declared_ui_uris(result) == {
            "display_pdf": "ui://pdf/app.html",
            "legacy": "ui://legacy/app.html",
        }

    def test_rejects_non_ui_schemes(self):
        result = {"tools": [
            {"name": "evil", "_meta": {"ui": {"resourceUri": "file:///etc/passwd"}}},
            {"name": "web", "_meta": {"ui": {"resourceUri": "https://x/app.html"}}},
        ]}
        assert apps.extract_declared_ui_uris(result) == {}

    def test_malformed_inputs(self):
        assert apps.extract_declared_ui_uris({}) == {}
        assert apps.extract_declared_ui_uris({"tools": "nope"}) == {}
        assert apps.extract_declared_ui_uris({"tools": [None, {}, {"name": 3}]}) == {}
        assert apps.extract_declared_ui_uris(None) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# strip_model_hidden_tools
# --------------------------------------------------------------------------

def _tool(name: str, visibility=...) -> dict:
    """A tools/list entry, with ``_meta.ui.visibility`` only when given.

    ``visibility=...`` (the default) omits the key entirely, which is the shape
    the SEP-1865 default applies to.
    """
    tool: dict = {"name": name, "inputSchema": {"type": "object"}}
    if visibility is not ...:
        tool["_meta"] = {"ui": {"visibility": visibility}}
    return tool


class TestStripModelHiddenTools:
    def test_drops_app_only_tool(self):
        result = {"tools": [_tool("refresh", ["app"]), _tool("get_weather", ["model", "app"])]}
        withheld = strip_model_hidden_tools(result)
        assert withheld.declared == ["refresh"]
        assert withheld.unreadable == []
        assert [t["name"] for t in result["tools"]] == ["get_weather"]

    def test_keeps_tool_with_no_visibility(self):
        """Absent visibility defaults to ["model", "app"] per SEP-1865."""
        result = {"tools": [_tool("plain")]}
        assert strip_model_hidden_tools(result).names == []
        assert [t["name"] for t in result["tools"]] == ["plain"]

    def test_keeps_model_only_tool(self):
        result = {"tools": [_tool("hidden_from_app", ["model"])]}
        assert strip_model_hidden_tools(result).names == []
        assert [t["name"] for t in result["tools"]] == ["hidden_from_app"]

    @pytest.mark.parametrize(
        "vis",
        ["app", []],
        ids=["bare-app", "empty-list"],
    )
    def test_readable_declaration_without_model_drops_as_declared(self, vis):
        """A parseable declaration that omits "model" is the server doing what
        the spec provides for — routine, so it lands in ``declared``."""
        result = {"tools": [_tool("t", vis)]}
        withheld = strip_model_hidden_tools(result)
        assert withheld.declared == ["t"]
        assert withheld.unreadable == []
        assert result["tools"] == []

    @pytest.mark.parametrize(
        "vis",
        [None, {}, 42, {"app": True}],
        ids=["explicit-null", "empty-dict", "int", "dict"],
    )
    def test_unreadable_visibility_drops_and_is_reported_separately(self, vis):
        """A PRESENT visibility this host cannot parse drops, but is bucketed
        apart so the caller can warn: the tool vanished on OUR judgement, not
        the server's instruction.

        Only absence gets the permissive default. A leak is silent and lets the
        model execute a tool the server withheld; an over-drop is logged.
        """
        result = {"tools": [_tool("t", vis)]}
        withheld = strip_model_hidden_tools(result)
        assert withheld.declared == []
        assert withheld.unreadable == ["t"]
        assert result["tools"] == []

    @pytest.mark.parametrize(
        "vis",
        ["model", ["model"], ["model", "app"], ["app", "model"]],
        ids=["bare-model", "list-model", "both", "both-reversed"],
    )
    def test_visibility_including_model_keeps(self, vis):
        """A bare ``"model"`` string is coerced, not treated as unreadable —
        blanket-dropping malformed values would hide a tool meant to be seen."""
        result = {"tools": [_tool("t", vis)]}
        assert strip_model_hidden_tools(result).names == []
        assert [t["name"] for t in result["tools"]] == ["t"]

    def test_unnamed_app_only_tool_reported_as_placeholder(self):
        result = {"tools": [{"_meta": {"ui": {"visibility": ["app"]}}}]}
        assert strip_model_hidden_tools(result).declared == ["<unnamed>"]
        assert result["tools"] == []

    def test_non_dict_entries_preserved(self):
        result = {"tools": ["junk", 7, _tool("ok")]}
        assert strip_model_hidden_tools(result).names == []
        assert len(result["tools"]) == 3

    def test_missing_or_malformed_tools_key(self):
        assert strip_model_hidden_tools({}).names == []
        assert strip_model_hidden_tools({"tools": "nope"}).names == []
        assert strip_model_hidden_tools({"tools": None}).names == []

    def test_untouched_when_nothing_dropped(self):
        """The list object is left as-is when no tool is withheld, so a caller
        holding a reference to it does not see a surprise rebind."""
        tools = [_tool("a"), _tool("b", ["model"])]
        result = {"tools": tools}
        assert strip_model_hidden_tools(result).names == []
        assert result["tools"] is tools

    def test_both_buckets_populated_together(self):
        result = {"tools": [_tool("declared_hidden", ["app"]), _tool("bad_shape", 42)]}
        withheld = strip_model_hidden_tools(result)
        assert withheld.declared == ["declared_hidden"]
        assert withheld.unreadable == ["bad_shape"]
        assert sorted(withheld.names) == ["bad_shape", "declared_hidden"]
        assert result["tools"] == []


# --------------------------------------------------------------------------
# Backend interception seam (async)
# --------------------------------------------------------------------------

def _pool_key(server: str = "excalidraw-mcp") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name="test-agent",
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="jkl012",
    )


def _make_backend() -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    stdin = MagicMock()
    stdin.close = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdout = MagicMock()
    now = time.monotonic()
    return Backend(
        pool_key=_pool_key(),
        process=proc,
        stdin=stdin,
        stdout=stdout,
        created_at=now,
        last_used_at=now,
    )


def _written_frames(backend: Backend) -> list[dict]:
    """Decode every JSON-RPC frame written to the backend's mock stdin."""
    frames = []
    for call in backend.stdin.write.call_args_list:
        payload = call.args[0]
        for line in payload.decode("utf-8").splitlines():
            if line.strip():
                frames.append(json.loads(line))
    return frames


async def _drain_inbox(inbox: "asyncio.Queue[bytes]", timeout: float = 2.0) -> dict:
    data = await asyncio.wait_for(inbox.get(), timeout=timeout)
    return json.loads(data.decode("utf-8"))


@pytest.fixture
def apps_flag_on(monkeypatch):
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")


def _listing_pending(stub_uuid: str) -> _PendingRequest:
    return _PendingRequest(
        stub_uuid=stub_uuid, original_id=1, method="tools/list", t_start_ms=0.0
    )


def _listing_msg() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [_tool("refresh", ["app"]), _tool("draw")]},
    }


@pytest.mark.asyncio
async def test_model_listing_withholds_app_only_tools(apps_flag_on):
    """SEP-1865 MUST: the agent's tools/list omits a tool whose visibility
    does not include "model"."""
    backend = _make_backend()
    msg = _listing_msg()
    # False = "caller delivers normally"; the filter mutates in place.
    assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    assert [t["name"] for t in msg["result"]["tools"]] == ["draw"]


@pytest.mark.asyncio
async def test_app_call_listing_keeps_app_only_tools(apps_flag_on):
    """The app-call authorization snapshot MUST keep app-only tools.

    ``app_call`` authorizes an app's tools/call against a fresh tools/list. If
    the filter reached that listing, every app-only tool would look
    non-existent and the interactive-refresh pattern the visibility split
    exists for would be dead. Pins the ordering of the ``__app_call__`` guard
    ahead of the tools/list branch in ``_maybe_intercept_ui_result``.
    """
    backend = _make_backend()
    msg = _listing_msg()
    pending = _listing_pending("__app_call__deadbeef")
    assert await backend._maybe_intercept_ui_result(pending, msg) is False
    assert [t["name"] for t in msg["result"]["tools"]] == ["refresh", "draw"]


@pytest.mark.asyncio
async def test_listing_filtered_even_when_feature_disabled(monkeypatch):
    """The visibility filter is NOT gated on the MCP Apps feature flag.

    ``visibility`` is the server's statement about who may call a tool, not a
    property of our renderer. With apps disabled there is no app to call an
    app-only tool, so filtering makes it unreachable — the server's own
    consequence, and strictly better than handing the model a tool the server
    withheld from it. The stdout pump calls the interception hook
    unconditionally, so without this the flag-off path leaked app-only tools.
    """
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    backend = _make_backend()
    msg = _listing_msg()
    assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    assert [t["name"] for t in msg["result"]["tools"]] == ["draw"]


@pytest.mark.asyncio
async def test_app_call_listing_unfiltered_when_feature_disabled(monkeypatch):
    """The app-call exemption still wins with the flag off, since its guard is
    checked ahead of both the filter and the gate."""
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    backend = _make_backend()
    msg = _listing_msg()
    pending = _listing_pending("__app_call__off01")
    assert await backend._maybe_intercept_ui_result(pending, msg) is False
    assert [t["name"] for t in msg["result"]["tools"]] == ["refresh", "draw"]


@pytest.mark.asyncio
async def test_unreadable_visibility_drop_warns_declared_drop_informs(apps_flag_on, caplog):
    """The two drop causes must not log at the same level.

    A well-formed ``["app"]`` is routine (INFO). An unparseable declaration
    means a tool vanished on THIS host's judgement, which is the case an
    operator has to be able to find — so it warns.
    """
    backend = _make_backend()
    msg = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": [_tool("routine", ["app"]), _tool("bad_shape", 42)]},
    }
    with caplog.at_level(logging.INFO, logger="kiro_crew.mcp_gateway.backend"):
        assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    informed = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("bad_shape" in r.getMessage() for r in warned)
    assert not any("bad_shape" in r.getMessage() for r in informed)
    assert any("routine" in r.getMessage() for r in informed)
    assert not any("routine" in r.getMessage() for r in warned)


@pytest.mark.asyncio
async def test_disabled_listing_still_refreshes_declarations(monkeypatch):
    """A listing that arrives while apps are DISABLED must still refresh the
    declaration map, or a withdrawn ui:// stays cached and a later re-enable
    renders it.

    Sequence: enabled listing caches ui://old -> apps disabled -> server
    withdraws the declaration -> apps re-enabled -> a tools/call falling back
    to the map must NOT find ui://old.
    """
    backend = _make_backend()
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
    declared = _tool("draw")
    declared["_meta"] = {"ui": {"resourceUri": "ui://srv/old.html"}}
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [declared]}}
    await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg)
    assert backend._apps_declared_uris == {"draw": "ui://srv/old.html"}

    # Apps off; the server's new listing no longer declares the resource.
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    msg2 = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [_tool("draw")]}}
    await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg2)
    assert backend._apps_declared_uris == {}


@pytest.mark.asyncio
async def test_visibility_withhold_is_sel_audited(apps_flag_on, monkeypatch):
    """Withholding a tool is an authorization decision and must reach the SEL
    chain — the sibling app-call direction audits every outcome, so the
    direction that takes capability AWAY from the model cannot be the
    unaudited one.

    Exactly ONE event per listing that withheld something: a server with a
    permanent app-only tool would otherwise mint an event on every listing.
    """
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.backend.SecurityEventLog", lambda: _FakeSel()
    )
    backend = _make_backend()
    pending = _PendingRequest(
        stub_uuid="s1", original_id=1, method="tools/list",
        t_start_ms=0.0, session_key="dashboard:sess-9",
    )
    msg = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": [_tool("routine", ["app"]), _tool("bad", 42), _tool("ok")]},
    }
    assert await backend._maybe_intercept_ui_result(pending, msg) is False
    assert len(calls) == 1
    ev = calls[0]
    assert ev["caller"] == "dashboard:sess-9"
    assert ev["operation"] == "mcp-gateway.tools-list-visibility"
    assert ev["outcome"] == "denied"
    assert "declared=routine" in ev["resources"]
    assert "unreadable=bad" in ev["resources"]
    assert "excalidraw-mcp" in ev["resources"]


@pytest.mark.asyncio
async def test_no_sel_event_when_nothing_withheld(apps_flag_on, monkeypatch):
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.backend.SecurityEventLog", lambda: _FakeSel()
    )
    backend = _make_backend()
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [_tool("ok")]}}
    assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    assert calls == []


@pytest.mark.asyncio
async def test_sel_failure_does_not_break_the_filter(apps_flag_on, monkeypatch):
    """A broken audit sink must not cost the user their tools/list response, and
    must not undo the withhold."""
    class _ExplodingSel:
        def log_api_access(self, **kw):
            raise RuntimeError("sel disk full")

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.backend.SecurityEventLog", lambda: _ExplodingSel()
    )
    backend = _make_backend()
    msg = _listing_msg()
    assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    assert [t["name"] for t in msg["result"]["tools"]] == ["draw"]


@pytest.mark.asyncio
async def test_hidden_tool_ui_uri_is_not_harvested(apps_flag_on):
    """A withheld tool's declared ui:// must never enter _apps_declared_uris.

    The strip runs before the harvest, so a model-originated call naming an
    app-only tool cannot find a declared resource to render. Pins that
    ordering — reversing it would resurrect hidden-tool rendering.
    """
    backend = _make_backend()
    hidden = _tool("refresh", ["app"])
    hidden["_meta"]["ui"]["resourceUri"] = "ui://srv/hidden.html"
    shown = _tool("draw")
    shown["_meta"] = {"ui": {"resourceUri": "ui://srv/draw.html"}}
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [hidden, shown]}}
    assert await backend._maybe_intercept_ui_result(_listing_pending("s1"), msg) is False
    assert backend._apps_declared_uris == {"draw": "ui://srv/draw.html"}


@pytest.mark.asyncio
async def test_interception_end_to_end(apps_flag_on, spool_tmp):
    """A tools/call result carrying a ui:// resource triggers a gateway
    resources/read; the fetched html is spooled and the delivered response is
    marked. Exercises forward -> park -> resources/read -> resolve -> deliver."""
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")

    caller = CallerContext(session_key="dashboard:sess-1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 42, "method": "tools/call",
         "params": {"name": "draw", "arguments": {}}},
        caller=caller,
    )
    # Find the gateway forward id assigned to the tools/call.
    tc_fid = next(
        f for f, p in backend._pending_requests.items() if p.method == "tools/call"
    )
    assert backend._pending_requests[tc_fid].session_key == "dashboard:sess-1"
    assert backend._pending_requests[tc_fid].tool_name == "draw"

    # Backend answers the tools/call with a ui:// resource reference.
    tools_call_response = {
        "jsonrpc": "2.0",
        "id": tc_fid,
        "result": {
            "content": [{"type": "text", "text": "drawn"}],
            "structuredContent": {"nodes": 3},
            "_meta": {"ui": {"resourceUri": "ui://excalidraw/app.html"}},
        },
    }
    await backend._route_backend_line(
        (json.dumps(tools_call_response) + "\n").encode("utf-8")
    )

    # The parked fetch task should now have written a resources/read upstream.
    for _ in range(50):
        await asyncio.sleep(0)
        rr = [f for f in _written_frames(backend) if f.get("method") == "resources/read"]
        if rr:
            break
    assert rr, "expected a gateway-originated resources/read"
    assert rr[0]["params"]["uri"] == "ui://excalidraw/app.html"
    rr_fid = rr[0]["id"]
    assert backend._pending_requests[str(rr_fid)].stub_uuid == "__apps__"

    # Feed the resources/read reply (html as inline text).
    read_response = {
        "jsonrpc": "2.0",
        "id": rr_fid,
        "result": {"contents": [{
            "uri": "ui://excalidraw/app.html",
            "mimeType": MCP_APPS_MIME_TYPE,
            "text": "<html>app</html>",
            "_meta": {"ui": {"csp": "default-src 'self'", "permissions": ["clipboard"]}},
        }]},
    }
    await backend._route_backend_line(
        (json.dumps(read_response) + "\n").encode("utf-8")
    )

    delivered = await _drain_inbox(inbox)
    # Original stub id restored, marker injected on the first text item.
    assert delivered["id"] == 42
    text = delivered["result"]["content"][0]["text"]
    assert text.startswith("drawn ")
    assert text.startswith("drawn [kirocrew-mcp-app:")
    # Structured content preserved.
    assert delivered["result"]["structuredContent"] == {"nodes": 3}

    # Exactly one spool file, carrying the fetched html + policy.
    spooled = list(spool_tmp.glob("*.json"))
    assert len(spooled) == 1
    record = json.loads(spooled[0].read_text())
    assert record["html"] == "<html>app</html>"
    assert record["csp"] == "default-src 'self'"
    assert record["permissions"] == ["clipboard"]
    assert record["server"] == "excalidraw-mcp"
    assert record["tool"] == "draw"
    assert record["session_key"] == "dashboard:sess-1"
    assert record["structured_content"] == {"nodes": 3}
    # The spool id in the marker matches the file on disk.
    marker_id = text.split("[kirocrew-mcp-app:")[1].rstrip("]").strip()
    assert (spool_tmp / f"{marker_id}.json").exists()


@pytest.mark.asyncio
async def test_interception_blob_base64(apps_flag_on, spool_tmp):
    """resources/read returning a base64 blob is decoded to html."""
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "x"}],
                   "_meta": {"ui": {"resourceUri": "ui://a/b.html"}}},
    }) + "\n").encode("utf-8"))
    for _ in range(50):
        await asyncio.sleep(0)
        rr = [f for f in _written_frames(backend) if f.get("method") == "resources/read"]
        if rr:
            break
    blob = base64.b64encode(b"<html>blob</html>").decode("ascii")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": rr[0]["id"],
        "result": {"contents": [{"mimeType": MCP_APPS_MIME_TYPE, "blob": blob}]},
    }) + "\n").encode("utf-8"))
    delivered = await _drain_inbox(inbox)
    marker_id = delivered["result"]["content"][0]["text"].split(
        "[kirocrew-mcp-app:")[1].rstrip("]").strip()
    record = json.loads((spool_tmp / f"{marker_id}.json").read_text())
    assert record["html"] == "<html>blob</html>"


@pytest.mark.asyncio
async def test_no_ui_resource_delivers_unmodified(apps_flag_on, spool_tmp):
    """A tools/call result without a ui:// resource is delivered unchanged and
    no spool file is written (flag on, but nothing to intercept)."""
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "plain"}]},
    }) + "\n").encode("utf-8"))
    delivered = await _drain_inbox(inbox)
    assert delivered["id"] == 9
    assert delivered["result"]["content"][0]["text"] == "plain"
    assert list(spool_tmp.glob("*.json")) == []


@pytest.mark.asyncio
async def test_flag_off_no_interception(monkeypatch, spool_tmp):
    """With the kill-switch set, a ui:// result is delivered verbatim (no
    marker, no resources/read, no spool) — byte-identical to pre-feature
    behavior. The gate defaults to ON, so "off" is set explicitly."""
    monkeypatch.setenv(MCP_APPS_ENV_FLAG, "0")
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "z"}],
                   "_meta": {"ui": {"resourceUri": "ui://a/b.html"}}},
    }) + "\n").encode("utf-8"))
    delivered = await _drain_inbox(inbox)
    assert delivered["id"] == 5
    assert delivered["result"]["content"][0]["text"] == "z"  # no marker
    assert not any(
        f.get("method") == "resources/read" for f in _written_frames(backend)
    )
    assert list(spool_tmp.glob("*.json")) == []


@pytest.mark.asyncio
async def test_resources_read_timeout_delivers_original(apps_flag_on, spool_tmp, monkeypatch):
    """If the resources/read never returns, the original tools/call response is
    delivered unmodified after the timeout (best-effort — never wedge/drop)."""
    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.backend._APPS_RESOURCE_READ_TIMEOUT_SECS", 0.05
    )
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "orig"}],
                   "_meta": {"ui": {"resourceUri": "ui://a/b.html"}}},
    }) + "\n").encode("utf-8"))
    # Do NOT feed a resources/read reply — let it time out.
    delivered = await _drain_inbox(inbox, timeout=2.0)
    assert delivered["id"] == 11
    assert delivered["result"]["content"][0]["text"] == "orig"  # unmarked
    assert list(spool_tmp.glob("*.json")) == []


@pytest.mark.asyncio
async def test_resources_read_error_delivers_original(apps_flag_on, spool_tmp):
    """A JSON-RPC error to the resources/read yields the original response."""
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "keep"}],
                   "_meta": {"ui": {"resourceUri": "ui://a/b.html"}}},
    }) + "\n").encode("utf-8"))
    for _ in range(50):
        await asyncio.sleep(0)
        rr = [f for f in _written_frames(backend) if f.get("method") == "resources/read"]
        if rr:
            break
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": rr[0]["id"],
        "error": {"code": -32601, "message": "no such resource"},
    }) + "\n").encode("utf-8"))
    delivered = await _drain_inbox(inbox)
    assert delivered["id"] == 13
    assert delivered["result"]["content"][0]["text"] == "keep"
    assert list(spool_tmp.glob("*.json")) == []


@pytest.mark.asyncio
async def test_wrong_mimetype_delivers_original(apps_flag_on, spool_tmp):
    """resources/read contents with an unexpected mimeType is rejected; the
    original response is delivered unmodified."""
    backend = _make_backend()
    inbox = await backend.attach_stub("s1")
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "t"}},
        caller=CallerContext(session_key="k"),
    )
    tc_fid = next(f for f, p in backend._pending_requests.items() if p.method == "tools/call")
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": tc_fid,
        "result": {"content": [{"type": "text", "text": "orig"}],
                   "_meta": {"ui": {"resourceUri": "ui://a/b.html"}}},
    }) + "\n").encode("utf-8"))
    for _ in range(50):
        await asyncio.sleep(0)
        rr = [f for f in _written_frames(backend) if f.get("method") == "resources/read"]
        if rr:
            break
    await backend._route_backend_line((json.dumps({
        "jsonrpc": "2.0", "id": rr[0]["id"],
        "result": {"contents": [{"mimeType": "text/plain", "text": "nope"}]},
    }) + "\n").encode("utf-8"))
    delivered = await _drain_inbox(inbox)
    assert delivered["id"] == 15
    assert delivered["result"]["content"][0]["text"] == "orig"
    assert list(spool_tmp.glob("*.json")) == []


# --------------------------------------------------------------------------
# _maybe_intercept_ui_result decision logic (isError ordering + withdrawal)
# --------------------------------------------------------------------------

class TestInterceptDecision:
    """Unit tests on the interception seam with a mock backend."""

    def _pending(self, method: str = "tools/call", tool: str = "draw"):
        from kiro_crew.mcp_gateway.backend import _PendingRequest
        return _PendingRequest(
            stub_uuid="s1", original_id=1, method=method,
            session_key="dashboard:x", tool_name=tool,
        )

    @pytest.mark.asyncio
    async def test_is_error_result_never_intercepts_either_form(self, spool_tmp, monkeypatch):
        """A FAILED tool call must never spawn a render — checked before the
        result-side _meta.ui form is even read (was previously only guarding
        the declared-uri fallback)."""
        from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://fake/app.html"}
        msg = {
            "jsonrpc": "2.0", "id": 9,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "boom"}],
                # Result-side association present — must still be ignored.
                "_meta": {"ui": {"resourceUri": "ui://fake/app.html"}},
            },
        }
        assert await backend._maybe_intercept_ui_result(self._pending(), msg) is False

    @pytest.mark.asyncio
    async def test_tools_list_replaces_declaration_map(self, spool_tmp, monkeypatch):
        """Each tools/list is the server's COMPLETE current declaration set:
        a withdrawn tool→ui association must not survive the refresh."""
        from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://fake/app.html"}
        # Fresh listing WITHOUT the draw declaration → association withdrawn.
        msg = {
            "jsonrpc": "2.0", "id": 5,
            "result": {"tools": [{"name": "other",
                                  "inputSchema": {"type": "object", "properties": {}}}]},
        }
        assert await backend._maybe_intercept_ui_result(
            self._pending(method="tools/list", tool=""), msg
        ) is False
        assert backend._apps_declared_uris == {}

    @pytest.mark.asyncio
    async def test_app_call_forward_never_reintercepted(self, spool_tmp, monkeypatch):
        """#13: an app-originated callback (forwarded on a ``__app_call__*``
        stub) whose called tool ALSO declares a ui:// resource must return
        verbatim — never re-spooled with a marker, which would replace the
        app's real result and mint a stray record."""
        from kiro_crew.mcp_gateway.backend import MCP_APPS_ENV_FLAG, _PendingRequest
        monkeypatch.setenv(MCP_APPS_ENV_FLAG, "1")
        backend = _make_backend()
        backend._apps_declared_uris = {"draw": "ui://fake/app.html"}
        pending = _PendingRequest(
            stub_uuid="__app_call__deadbeef", original_id=1, method="tools/call",
            session_key="dashboard:x", tool_name="draw",
        )
        msg = {
            "jsonrpc": "2.0", "id": 9,
            "result": {
                "content": [{"type": "text", "text": "ok"}],
                "_meta": {"ui": {"resourceUri": "ui://fake/app.html"}},
            },
        }
        assert await backend._maybe_intercept_ui_result(pending, msg) is False
