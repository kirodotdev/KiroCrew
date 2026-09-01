"""Phase 3 black-box ACP v1 conformance gate for ``kirocrew acp``.

This suite treats ``kirocrew acp`` as an EXTERNAL binary: it spawns the real
entrypoint as a subprocess and speaks newline-delimited JSON-RPC 2.0 over its
stdio, exactly as an ACP editor (Zed, VS Code) would. It imports no server
internals (``AcpAgentServer`` / ``AgentTransport`` are never imported here); the
only coupling to the implementation is the pinned wire surface in
:mod:`acp_bb_schema`, through which the harness validates **every** emitted
frame automatically (see :meth:`AcpEditor.assert_conformant`).

Architecture (see ``PLANS/acp-baseline-conformance.md`` Phase 3):

* ``kirocrew acp`` runs in its default gateway-proxy mode.
* Its gateway HTTP backend is stubbed at its real HTTP seam by
  :class:`acp_bb_gateway.FakeGateway` (127.0.0.1 only) — deterministic, offline,
  fast, no daemon/user state. This is the "isolated test gateway" the plan asks
  for, minus the 5-15s real-gateway startup that gates ``test_e2e_smoke``.
* :class:`acp_bb_editor.AcpEditor` is the black-box client.

Dependency decision (requirement 1):
  The validator is built from the official ACP v1 schema vendored at
  ``test/conformance/vendor/acp-v1`` and pinned to ``schema-v1.21.0`` commit
  ``272bf799f35a258c6a4107a0410ed361e83683d3``. :mod:`acp_bb_schema` loads that
  surface through :mod:`acp_v1_vendor` without importing Kiro Crew ACP types.
  Full Draft 2020-12 validation remains unavailable because ``jsonschema`` is
  absent; the official-SDK-driven smoke tests below are skipped-with-reason
  until an SDK is vendored.

Run this gate:
  ``python -m pytest test/test_acp_conformance_blackbox.py \\
      --override-ini="addopts=-p no:cacheprovider -n0" -q``
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import time
from typing import Any, Callable

import pytest
from acp_bb_editor import AcpEditor, agent_message_text, echo_mcp_server
from acp_bb_gateway import FakeGateway

from kiro_crew.sandbox import detect_backend

# All tests spawn a subprocess; pin them to one xdist group so a ``-n auto`` run
# does not cold-start ~20 adapter processes across workers at once (the same
# starvation the test_mcp_gateway_* modules avoid). Harmless under ``-n0``.
pytestmark = pytest.mark.xdist_group(name="acp_blackbox")


@pytest.fixture
def gateway():
    with FakeGateway() as gw:
        yield gw


@pytest.fixture
def make_editor(tmp_path):
    """Factory that starts AcpEditors against a gateway and cleans them all up."""
    editors: list[AcpEditor] = []

    def _make(gw: FakeGateway, **kwargs: Any) -> AcpEditor:
        home = tempfile.mkdtemp(dir=str(tmp_path))
        editor = AcpEditor(gw.url, home=home, **kwargs)
        editor.__enter__()
        editors.append(editor)
        return editor

    yield _make
    for editor in editors:
        editor.close()


def _wait_frame(
    editor: AcpEditor, pred: Callable[[dict[str, Any]], bool], timeout: float = 15.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in list(editor.all_frames):
            if pred(frame):
                return frame
        time.sleep(0.02)
    raise AssertionError(
        f"no frame matched within {timeout}s; frames={editor.all_frames!r}\n{editor.stderr_tail()}"
    )


def _error(frame: dict[str, Any]) -> dict[str, Any]:
    return frame.get("error") or {}


# ─────────────────── initialize / version negotiation / caps ────────────────


class TestInitialize:
    def test_negotiates_v1_and_advertises_capabilities(self, gateway, make_editor):
        editor = make_editor(gateway)
        result = editor.initialize()["result"]
        assert result["protocolVersion"] == 1
        caps = result["agentCapabilities"]
        assert caps["loadSession"] is True
        assert set(caps["sessionCapabilities"]) == {"list", "resume"}
        assert result["agentInfo"]["name"] == "kirocrew"
        editor.assert_conformant()

    def test_never_echoes_unsupported_offered_version(self, gateway, make_editor):
        editor = make_editor(gateway)
        result = editor.initialize(protocol_version=999)["result"]
        # Strict negotiation: the agent answers with the version it will speak,
        # never the unsupported value the client offered.
        assert result["protocolVersion"] == 1
        editor.assert_conformant()


# ───────────────────────── prompt streaming / stop reason ───────────────────


class TestPromptTurn:
    def test_new_prompt_streams_reply_and_ends_turn(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        assert sid
        result = editor.prompt(sid, "ping")["result"]
        assert result["stopReason"] == "end_turn"
        chunk = editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        )
        assert "pong from fake gateway :: ping" in agent_message_text(chunk)
        assert chunk["params"]["sessionId"] == sid
        # The adapter drove the gateway's real HTTP surface.
        assert gateway.state.created_slots, "adapter never created a backing slot"
        assert gateway.state.projects.get(sid) == "/tmp"
        assert gateway.state.chat_posts[-1]["message"] == "ping"
        editor.assert_conformant()

    def test_thinking_chunk_is_thought_update(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        editor.prompt(sid, "[[THINK]] reflect")
        editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "agent_thought_chunk"
        )
        editor.assert_conformant()

    def test_resource_link_block_preserved_across_boundary(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        uri = "file:///repo/src/main.py"
        editor.prompt(
            sid,
            [{"type": "resource_link", "uri": uri}, {"type": "text", "text": " please review"}],
        )
        # The structured resource_link's uri must survive the ACP->text boundary.
        assert uri in gateway.state.chat_posts[-1]["message"]
        chunk = editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
            and uri in agent_message_text(f)
        )
        assert uri in agent_message_text(chunk)
        editor.assert_conformant()


# ──────────────────────── JSON-RPC error discipline ─────────────────────────


class TestJsonRpcErrors:
    def test_malformed_json_is_parse_error(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        editor.send_raw(b"{ this is not valid json \n")
        frame = _wait_frame(editor, lambda f: _error(f).get("code") == -32700)
        assert frame["id"] is None  # no id recoverable from unparseable bytes
        editor.assert_conformant()

    def test_non_object_frame_is_invalid_request(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        editor.send_raw(b"[1, 2, 3]\n")
        _wait_frame(editor, lambda f: _error(f).get("code") == -32600)
        editor.assert_conformant()

    def test_bad_jsonrpc_version_is_invalid_request(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        editor.send_raw(b'{"jsonrpc": "1.0", "id": 4242, "method": "initialize", "params": {}}\n')
        frame = _wait_frame(
            editor, lambda f: _error(f).get("code") == -32600 and f.get("id") == 4242
        )
        assert frame["id"] == 4242  # a recoverable id is echoed back
        editor.assert_conformant()

    def test_missing_absolute_cwd_is_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        # cwd omitted.
        resp = editor.request("session/new", {})
        assert _error(resp).get("code") == -32602
        # relative cwd is equally invalid.
        resp2 = editor.request("session/new", {"cwd": "relative/path"})
        assert _error(resp2).get("code") == -32602
        editor.assert_conformant()

    def test_bad_prompt_shape_is_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        resp = editor.request("session/prompt", {"sessionId": sid, "prompt": "not-an-array"})
        assert _error(resp).get("code") == -32602
        editor.assert_conformant()

    def test_prompt_unknown_session_is_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.request(
            "session/prompt",
            {"sessionId": "no-such-session", "prompt": [{"type": "text", "text": "hi"}]},
        )
        # session/prompt IS implemented; only the sessionId parameter is
        # unknown/stale -> -32602 Invalid params, never -32601 Method not found
        # (a strict client branching on the code must not read a bad session id
        # as "this agent does not implement session/prompt").
        assert _error(resp).get("code") == -32602
        editor.assert_conformant()

    def test_nonstandard_set_model_is_method_not_found(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.request("session/set_model", {})
        assert _error(resp).get("code") == -32601
        editor.assert_conformant()


# ──────────────────────── permission (allow / deny) ─────────────────────────


class TestPermission:
    def test_permission_allow_flows_to_gateway(self, gateway, make_editor):
        editor = make_editor(gateway, permission_mode="allow")
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        result = editor.prompt(sid, "[[PERMISSION]] do it")["result"]
        assert result["stopReason"] == "end_turn"
        req = editor.wait_permission()
        options = {o["optionId"] for o in req["params"]["options"]}
        assert options == {"allow_once", "reject_once"}
        assert gateway.state.approvals, "adapter never answered the gateway's approval"
        assert gateway.state.approvals[-1]["action"] == "approved"
        editor.assert_conformant()

    def test_permission_deny_flows_to_gateway(self, gateway, make_editor):
        editor = make_editor(gateway, permission_mode="deny")
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        editor.prompt(sid, "[[PERMISSION]] do it")
        editor.wait_permission()
        assert gateway.state.approvals[-1]["action"] == "rejected"
        editor.assert_conformant()


# ─────────────────────────── cancellation races ─────────────────────────────


class TestCancellation:
    def test_cancel_while_tool_running(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        req_id = editor.prompt_async(sid, "[[SLOW]] long tool")
        # Wait until the turn is streaming, then cancel mid-flight.
        editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "agent_message_chunk"
        )
        editor.cancel(sid)
        result = editor.wait_response(req_id, timeout=20.0)["result"]
        assert result["stopReason"] == "cancelled"
        assert sid in gateway.state.stops
        editor.assert_conformant()

    def test_cancel_while_permission_pending(self, gateway, make_editor):
        editor = make_editor(gateway, permission_mode="manual")
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        req_id = editor.prompt_async(sid, "[[PERMISSION]] risky")
        perm = editor.wait_permission()
        # Cancel while the agent is blocked awaiting our permission answer.
        editor.cancel(sid)
        editor.answer_permission(perm["id"], allow=False)
        result = editor.wait_response(req_id, timeout=20.0)["result"]
        assert result["stopReason"] == "cancelled"
        assert sid in gateway.state.stops
        editor.assert_conformant()


# ───────────────── gateway disconnect / EOF / process cleanup ────────────────


class TestTransportLifecycle:
    def test_gateway_error_maps_to_internal_error(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        # The backend gateway 500s the turn; the adapter must map the non-schema
        # failure to a JSON-RPC internal error, never an out-of-schema stopReason.
        resp = editor.request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": "[[GWERROR]] boom"}]},
        )
        assert _error(resp).get("code") == -32603
        editor.assert_conformant()

    def test_adapter_eof_clean_shutdown(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        editor.prompt(sid, "ping")
        code = editor.close(timeout=10.0)
        assert (
            code == 0
        ), f"adapter did not exit cleanly on EOF (code={code})\n{editor.stderr_tail()}"
        editor.assert_conformant()


# ─────────────────── two clients / capability end-to-end ────────────────────


class TestConcurrencyAndCapabilities:
    def test_two_clients_get_independent_sessions(self, gateway, make_editor):
        a = make_editor(gateway)
        b = make_editor(gateway)
        a.initialize()
        b.initialize()
        sid_a = a.session_new()["result"]["sessionId"]
        sid_b = b.session_new()["result"]["sessionId"]
        assert sid_a != sid_b
        # Overlap the two turns.
        ra = a.prompt_async(sid_a, "from A")
        rb = b.prompt_async(sid_b, "from B")
        assert a.wait_response(ra)["result"]["stopReason"] == "end_turn"
        assert b.wait_response(rb)["result"]["stopReason"] == "end_turn"
        # Neither client saw the other's session id in its updates.
        assert all(f["params"]["sessionId"] == sid_a for f in a.updates)
        assert all(f["params"]["sessionId"] == sid_b for f in b.updates)
        a.assert_conformant()
        b.assert_conformant()

    def test_advertised_optional_methods_all_work_end_to_end(self, gateway, make_editor):
        gateway.state.slot_messages["load-slot"] = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        gateway.state.list_slots = [
            {
                "key": "s-old",
                "project": "/tmp",
                "title": "Old",
                "last_activity_ts": "2026-01-01T00:00:00Z",
            },
            {
                "key": "s-new",
                "project": "/tmp",
                "title": "New",
                "last_activity_ts": "2026-02-01T00:00:00Z",
            },
        ]
        editor = make_editor(gateway)
        caps = editor.initialize()["result"]["agentCapabilities"]
        assert caps["loadSession"] is True and set(caps["sessionCapabilities"]) == {
            "list",
            "resume",
        }
        # Each advertised optional method must have a working end-to-end path.
        assert "result" in editor.session_list()
        load_result = editor.session_load("load-slot")["result"]
        assert "modes" in load_result and "configOptions" in load_result
        resume_result = editor.session_resume("s-new")["result"]
        assert "modes" in resume_result and "configOptions" in resume_result
        editor.assert_conformant()


# ──────────────────── history replay ordering / list paging ─────────────────


class TestHistoryAndListing:
    def test_session_load_replays_history_in_order(self, gateway, make_editor):
        n = 40
        gateway.state.slot_messages["hist-slot"] = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"} for i in range(n)
        ]
        editor = make_editor(gateway)
        editor.initialize()
        result = editor.session_load("hist-slot")["result"]
        assert "modes" in result and "configOptions" in result
        replay = [f for f in editor.updates if f["params"]["sessionId"] == "hist-slot"]
        assert len(replay) == n
        for i, frame in enumerate(replay):
            update = frame["params"]["update"]
            assert update.get("messageId") == f"history-{i}", "history replayed out of order"
            expected = "user_message_chunk" if i % 2 == 0 else "agent_message_chunk"
            assert update["sessionUpdate"] == expected
            assert update["content"]["text"] == f"msg-{i}"
        editor.assert_conformant()

    def test_session_list_sorted_and_bounded_first_release(self, gateway, make_editor):
        gateway.state.list_slots = [
            {
                "key": "s1",
                "project": "/tmp",
                "title": "One",
                "last_activity_ts": "2026-01-01T00:00:00Z",
            },
            {
                "key": "s2",
                "project": "/tmp",
                "title": "Two",
                "last_activity_ts": "2026-03-01T00:00:00Z",
            },
            {
                "key": "s3",
                "project": "/tmp",
                "title": "Three",
                "last_activity_ts": "2026-02-01T00:00:00Z",
            },
        ]
        editor = make_editor(gateway)
        editor.initialize()
        sessions = editor.session_list()["result"]["sessions"]
        assert [s["sessionId"] for s in sessions] == ["s2", "s3", "s1"]  # updatedAt desc
        # Documented bounded first release: no cursor pagination — a cursor still
        # returns the full page rather than erroring or partial-paging.
        paged = editor.session_list(cursor="anything")["result"]["sessions"]
        assert [s["sessionId"] for s in paged] == ["s2", "s3", "s1"]
        editor.assert_conformant()


# ─────────────────────────── stdio MCP baseline ─────────────────────────────

_SANDBOX_INFRA_HINTS = ("sandbox", "unshare", "seccomp", "bwrap", "namespace")


class TestStdioMcp:
    def test_session_new_with_stdio_mcp_preflights_and_registers(self, gateway, make_editor):
        if detect_backend() == "none":
            pytest.skip("host has no OS sandbox backend for untrusted MCP children")
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.session_new(cwd="/tmp", mcp_servers=[echo_mcp_server("echo")])
        if "error" in resp:
            msg = resp["error"].get("message", "").lower()
            if any(h in msg for h in _SANDBOX_INFRA_HINTS):
                pytest.skip(f"sandbox spawn infra unavailable in this env: {msg}")
            raise AssertionError(f"valid MCP server was rejected: {resp['error']}")
        sid = resp["result"]["sessionId"]
        # Registration only happens AFTER a successful real spawn + `initialize`
        # preflight, so its presence proves the handshake actually ran.
        assert sid in gateway.state.mcp
        assert [s["name"] for s in gateway.state.mcp[sid]] == ["echo"]
        editor.assert_conformant()

    def test_unsupported_transport_rejected_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.session_new(
            cwd="/tmp", mcp_servers=[{"name": "remote", "url": "https://mcp.example"}]
        )
        assert _error(resp).get("code") == -32602
        assert "transport" in _error(resp).get("message", "")
        assert not gateway.state.mcp  # nothing registered on rejection
        editor.assert_conformant()

    def test_bad_mcp_command_rejected_client_safe(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.session_new(
            cwd="/tmp", mcp_servers=[{"name": "b", "command": "/no/such/binary-xyz"}]
        )
        assert _error(resp).get("code") == -32603
        assert "failed to start" in _error(resp).get("message", "")
        assert not gateway.state.mcp
        editor.assert_conformant()

    def test_duplicate_mcp_names_rejected_secret_safe(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        resp = editor.session_new(
            cwd="/tmp",
            mcp_servers=[
                {"name": "dup", "command": "/a", "env": [{"name": "SECRET", "value": "tok"}]},
                {"name": "dup", "command": "/b"},
            ],
        )
        assert _error(resp).get("code") == -32602
        assert "duplicate" in _error(resp).get("message", "")
        assert "tok" not in _error(resp).get("message", "")  # never leaks a secret value
        editor.assert_conformant()


# ─────────────────── reply-options extension (namespaced _meta) ──────────────


class TestReplyOptions:
    def test_reply_options_surface_as_namespaced_meta(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new()["result"]["sessionId"]
        editor.prompt(sid, "[[OPTIONS]] pick one")
        frame = editor.wait_update(
            lambda f: f["params"]["update"].get("_meta", {}).get("kirocrew", {}).get("options")
        )
        assert frame["params"]["update"]["_meta"]["kirocrew"]["options"] == ["Yes", "No", "Maybe"]
        editor.assert_conformant()


# ───────────── official SDK smoke tests (ready, skipped until vendored) ──────


def _python_acp_sdk_present() -> bool:
    return any(
        importlib.util.find_spec(name) is not None
        for name in ("acp", "agent_client_protocol", "agentclientprotocol")
    )


@pytest.mark.skipif(
    not _python_acp_sdk_present(),
    reason=(
        "No official ACP v1 Python SDK is installable in this offline test "
        "workspace; the raw-framing black-box gate above is authoritative. This "
        "test is ready to drive normal flows through the SDK once it is vendored."
    ),
)
def test_official_python_sdk_smoke(gateway, make_editor):  # pragma: no cover - skipped
    # When the official SDK is present, drive initialize/new/prompt through it and
    # validate the SDK's own decoded frames against acp_bb_schema for cross-check.
    raise AssertionError("SDK present but SDK-driven smoke not yet implemented")


def _typescript_acp_sdk_present() -> bool:
    if shutil.which("node") is None:
        return False
    import subprocess

    for pkg in ("@zed-industries/agent-client-protocol", "@agentclientprotocol/sdk"):
        try:
            r = subprocess.run(
                ["node", "-e", f"require.resolve('{pkg}')"],
                capture_output=True,
                timeout=10,
            )
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


@pytest.mark.skipif(
    not _typescript_acp_sdk_present(),
    reason=(
        "No official ACP TypeScript SDK resolvable via node in this workspace "
        "(no deterministic npm/Node toolchain for a broad migration). "
        "Kept narrow and ready per the plan; enable once the SDK is vendored."
    ),
)
def test_official_typescript_sdk_smoke(gateway, make_editor):  # pragma: no cover - skipped
    raise AssertionError("TS SDK present but cross-SDK smoke not yet implemented")


# ───────────────────────── model + reasoning-effort selectors ───────────────


class TestSelectors:
    def test_new_session_advertises_modes_and_config_options(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        result = editor.session_new(cwd="/tmp")["result"]
        modes = result["modes"]
        assert modes["currentModeId"] == "default"
        assert {m["id"] for m in modes["availableModes"]} >= {"default", "low", "high", "max"}
        opts = result["configOptions"]
        assert opts[0]["id"] == "model"
        assert opts[0]["category"] == "model"
        assert opts[0]["type"] == "select"
        assert {o["value"] for o in opts[0]["options"]} == {"sonnet-4.6-1m", "opus-4.8"}
        assert opts[0]["currentValue"] == "sonnet-4.6-1m"  # default-first
        editor.assert_conformant()

    def test_set_mode_switches_effort_through_gateway(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        resp = editor.session_set_mode(sid, "high")
        assert resp["result"] == {}  # SetSessionModeResponse is empty
        # The adapter drove the gateway's REAL reasoning-effort endpoint.
        assert {"slot": sid, "reasoning_effort": "high"} in gateway.state.effort_switches
        assert gateway.state.slot_effort[sid] == "high"
        upd = editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "current_mode_update"
        )
        assert upd["params"]["update"]["currentModeId"] == "high"
        assert upd["params"]["sessionId"] == sid
        editor.assert_conformant()

    def test_set_mode_default_clears_effort(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        editor.session_set_mode(sid, "high")
        editor.session_set_mode(sid, "default")
        assert gateway.state.slot_effort[sid] == ""  # default id -> provider default
        editor.assert_conformant()

    def test_set_config_option_switches_model_and_recreates_provider(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        before = gateway.state.provider_recreations
        resp = editor.session_set_config_option(sid, "model", "opus-4.8")
        assert resp["result"]["configOptions"][0]["currentValue"] == "opus-4.8"
        assert {"slot": sid, "model": "opus-4.8"} in gateway.state.model_switches
        assert gateway.state.slot_model[sid] == "opus-4.8"
        # A model switch resets the slot session -> provider recreated next turn.
        assert gateway.state.provider_recreations == before + 1
        upd = editor.wait_update(
            lambda f: f["params"]["update"].get("sessionUpdate") == "config_option_update"
        )
        assert upd["params"]["update"]["configOptions"][0]["currentValue"] == "opus-4.8"
        editor.assert_conformant()

    def test_unadvertised_model_value_is_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        resp = editor.session_set_config_option(sid, "model", "gpt-9")
        assert _error(resp).get("code") == -32602
        # Rejected before touching the gateway model endpoint.
        assert not any(s["model"] == "gpt-9" for s in gateway.state.model_switches)
        editor.assert_conformant()

    def test_unknown_mode_is_invalid_params(self, gateway, make_editor):
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        resp = editor.session_set_mode(sid, "ludicrous")
        assert _error(resp).get("code") == -32602
        editor.assert_conformant()

    def test_model_switch_gateway_failure_is_internal_error(self, gateway, make_editor):
        gateway.state.fail_model_switch = True
        editor = make_editor(gateway)
        editor.initialize()
        sid = editor.session_new(cwd="/tmp")["result"]["sessionId"]
        resp = editor.session_set_config_option(sid, "model", "opus-4.8")
        # Gateway 500 on apply -> -32603, nothing announced (rollback).
        assert _error(resp).get("code") == -32603
        editor.assert_conformant()
