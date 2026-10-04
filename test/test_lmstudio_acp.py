"""Offline contract tests for KiroCrew's direct LM Studio ACP adapter.

The adapter translates ACP onto one local LM Studio server's OpenAI-compatible
wire.  Every test here drives a scripted double for its single HTTP seam and
never opens a socket, so what is pinned is the contract the rest of KiroCrew
reads back: the frame kind that closes a tool call, the stop reason a local
budget refusal wears, the context-meter frame, and the loopback-only endpoint
and live model catalog the adapter is allowed to reach.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp.direct_toolkit import STOP_REASON_LOCAL_LIMIT
from kiro_crew.acp.lmstudio_server import (
    _TOOLS_SPEC,
    LmStudioAcpServer,
    LmStudioProtocolError,
    _base_url,
)
from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_REFUSAL,
    UPDATE_USAGE,
)

_BASE_URL = "http://127.0.0.1:1234/v1"


class _ScriptedHost:
    """The adapter's only HTTP seam, scripted.

    It answers exactly the two endpoints the adapter is allowed to use, records
    every path asked for, and keeps the completion payloads so a test can prove
    what the adapter actually sent.
    """

    def __init__(self, catalog: list[dict], completions: list[dict] | None = None) -> None:
        self.catalog = catalog
        self.completions = list(completions or [])
        self.paths: list[str] = []
        self.payloads: list[dict] = []

    def __call__(self, path: str, payload: dict | None = None, **_kwargs) -> dict:
        self.paths.append(path)
        if path == "/api/v1/models":
            return {"models": list(self.catalog)}
        assert path == "/chat/completions", f"unexpected LM Studio endpoint: {path}"
        assert self.completions, "the adapter asked for more completions than were scripted"
        self.payloads.append(payload or {})
        return self.completions.pop(0)


def _request(request_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _catalog_row(
    model_id: str,
    *,
    loaded_context: int | None = None,
    max_context: int | None = None,
    name: str | None = None,
) -> dict:
    """One ``/api/v1/models`` row, in the shape LM Studio serves."""
    row: dict = {"type": "llm", "key": model_id}
    if loaded_context is not None:
        row["loaded_instances"] = [{"config": {"context_length": loaded_context}}]
    if max_context is not None:
        row["max_context_length"] = max_context
    if name is not None:
        row["display_name"] = name
    return row


def _completion(model_id: str, text: str | None = "local response", **extra) -> dict:
    return {
        "model": model_id,
        "choices": [{"message": {"content": text}}],
        **extra,
    }


def _tool_call_completion(model_id: str, name: str, arguments: dict) -> dict:
    return {
        "model": model_id,
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ],
    }


def _server(monkeypatch, host: _ScriptedHost) -> LmStudioAcpServer:
    server = LmStudioAcpServer(base_url=_BASE_URL)
    server._permission_decider = lambda *_args: True
    monkeypatch.setattr(server, "_http_json", host)
    return server


def _new_session(server: LmStudioAcpServer, cwd) -> str:
    created = server.handle(_request(1, "session/new", {"cwd": str(cwd), "mcpServers": []}))
    return created[0]["result"]["sessionId"]


def _prompt(server: LmStudioAcpServer, session_id: str, text: str) -> list[dict]:
    return server.handle(
        _request(
            2,
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )
    )


def _updates(frames: list[dict], *, kind: str | None = None) -> list[dict]:
    updates = [
        frame["params"]["update"] for frame in frames if frame.get("method") == "session/update"
    ]
    if kind is not None:
        updates = [update for update in updates if update.get("sessionUpdate") == kind]
    return updates


class TestEndpointResolution:
    def test_defaults_to_the_loopback_lmstudio_endpoint(self) -> None:
        assert _base_url(None) == "http://127.0.0.1:1234/v1"

    @pytest.mark.parametrize(
        "value",
        ["http://127.0.0.1:1234", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1"],
    )
    def test_accepts_a_loopback_v1_url(self, value: str) -> None:
        assert _base_url(value).endswith("/v1")

    @pytest.mark.parametrize(
        "value",
        [
            "https://example.com/v1",  # remote
            "http://10.0.0.2:1234/v1",  # remote
            "http://localhost:1234/v1",  # name-resolution dependent, never accepted
            "http://user:secret@127.0.0.1:1234/v1",  # credentialed
            "http://127.0.0.1:1234/other",  # not the /v1 root
            "http://127.0.0.1:1234/v1?key=1",  # query data
        ],
    )
    def test_refuses_a_non_loopback_or_credentialed_url(self, value: str) -> None:
        with pytest.raises(LmStudioProtocolError):
            _base_url(value)


class TestLiveModelCatalog:
    def test_reads_the_catalog_from_the_live_models_endpoint(self, monkeypatch) -> None:
        host = _ScriptedHost(
            [
                _catalog_row("qwen/chat-8b", loaded_context=32_768, name="Chat 8B"),
                _catalog_row("qwen/idle-8b", max_context=131_072),
            ]
        )
        server = _server(monkeypatch, host)

        models = server._models()

        assert host.paths == ["/api/v1/models"]
        assert [model["modelId"] for model in models] == ["qwen/chat-8b", "qwen/idle-8b"]
        # The window is the model's OWN: a loaded instance's active
        # ``context_length`` when one exists, otherwise the advertised maximum.
        assert [model["contextWindow"] for model in models] == [32_768, 131_072]
        assert [model["name"] for model in models] == ["Chat 8B", "qwen/idle-8b"]
        # Which models are actually loaded is remembered, and it is what makes
        # the next prompt's window refresh cheap rather than a model load.
        assert server._loaded_context_window_models == {"qwen/chat-8b"}

    def test_offers_only_standalone_chat_models(self, monkeypatch) -> None:
        """A speculative-decoding draft head is not a chat model."""
        host = _ScriptedHost(
            [
                _catalog_row("qwen/chat-8b", loaded_context=32_768),
                _catalog_row("qwen/chat-8b-mtp", loaded_context=32_768),
                _catalog_row("base/draft", loaded_context=32_768),
            ]
        )
        server = _server(monkeypatch, host)

        assert [model["modelId"] for model in server._models()] == ["qwen/chat-8b"]

    def test_each_read_replaces_the_previous_catalog_snapshot(self, monkeypatch) -> None:
        host = _ScriptedHost([_catalog_row("qwen/chat-8b", loaded_context=32_768)])
        server = _server(monkeypatch, host)
        assert [model["modelId"] for model in server._models()] == ["qwen/chat-8b"]

        # The operator unloads the model and loads another: the old row must not
        # linger, and neither must its remembered loaded window.
        host.catalog = [_catalog_row("qwen/other-4b", max_context=8_192)]

        models = server._models()
        assert [model["modelId"] for model in models] == ["qwen/other-4b"]
        assert server._loaded_context_window_models == set()

    def test_a_turn_uses_only_the_catalog_and_completion_endpoints(
        self, monkeypatch, tmp_path
    ) -> None:
        host = _ScriptedHost(
            [_catalog_row("qwen/chat-8b", loaded_context=32_768)],
            [_completion("qwen/chat-8b")],
        )
        server = _server(monkeypatch, host)
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "Reply briefly.")

        assert frames[-1]["result"]["stopReason"] == "end_turn"
        # A session/new catalog read, the prompt's window refresh, then one
        # completion -- and no other endpoint than these two.
        assert host.paths == ["/api/v1/models", "/api/v1/models", "/chat/completions"]
        # The adapter advertises its built-in trio on the first call, with no
        # MCP servers mounted.
        assert host.payloads[0]["tools"] == _TOOLS_SPEC
        assert host.payloads[0]["model"] == "qwen/chat-8b"


class TestToolCallTerminalFrame:
    def test_a_terminal_tool_frame_is_a_tool_call_update(self, monkeypatch, tmp_path) -> None:
        """A tool call must END on the wire under a kind the client reads.

        The ACP client maps ``sessionUpdate == "tool_call"`` to a NEW call
        (EVENT_TOOL_CALL) and only ``tool_call_update`` to a result
        (EVENT_TOOL_RESULT). A terminal frame sent under the initial kind
        therefore leaves the call open on every consumer: the dashboard pill
        stays "Running", no tool result is broadcast, and the tool-stall
        watchdog stays armed. A fast tool that never streams a progress frame
        shows the symptom in its purest form, so the frame KIND is pinned here
        rather than the rendered output.
        """
        from kiro_crew.acp._dispatch import parse_session_update

        target = tmp_path / "written.txt"
        host = _ScriptedHost(
            [_catalog_row("qwen/chat-8b", loaded_context=32_768)],
            [
                _tool_call_completion(
                    "qwen/chat-8b", "write_file", {"path": str(target), "content": "done"}
                ),
                _completion("qwen/chat-8b", "finished"),
            ],
        )
        server = _server(monkeypatch, host)
        monkeypatch.setattr(
            "kiro_crew.acp.lmstudio_server.iter_tool",
            lambda *_args, **_kwargs: iter([("final", "wrote written.txt")]),
        )
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "Write the file.")

        updates = [update for update in _updates(frames) if update.get("toolCallId") == "call-1"]
        terminals = [update for update in updates if update.get("status") == "completed"]
        assert terminals, "the adapter must close the call"
        assert all(update["sessionUpdate"] == "tool_call_update" for update in terminals)

        # Parsed through the SAME parser the client uses, the call's lifecycle
        # is one new call and exactly one terminal result.
        events = [event for update in updates for event in parse_session_update(dict(update))]
        calls = [event for event in events if event.kind == EVENT_TOOL_CALL]
        results = [event for event in events if event.kind == EVENT_TOOL_RESULT]
        finals = [
            event for event in results if event.tool_final and event.tool_status == "completed"
        ]
        assert len(calls) == 1, "exactly the initial frame is a new call"
        assert len(finals) == 1, "the terminal frame must close the call"
        assert "wrote written.txt" in (finals[0].tool_output or "")


class TestLocalBudgetGuard:
    def test_an_oversized_request_stops_as_a_local_limit(self, monkeypatch, tmp_path) -> None:
        """The guard signals the local-limit stop reason, never "refusal".

        A request with no safe context boundary is refused locally BEFORE any
        completion is attempted. Reading that as the ACP ``refusal`` reason
        would render "response declined by the model" for a bound the operator
        set on their own machine, so the adapter owns a distinct reason and this
        pins it.
        """
        from kiro_crew.acp import lmstudio_server

        monkeypatch.setattr(lmstudio_server, "_LOCAL_MODEL_PROMPT_BUDGET_TOKENS", 32_768)
        host = _ScriptedHost([_catalog_row("qwen/chat-8b", loaded_context=262_144)])
        server = _server(monkeypatch, host)
        monkeypatch.setattr(
            server,
            "_poll_completion",
            lambda *_args, **_kwargs: pytest.fail("an oversized request must not reach inference"),
        )
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "user text " * 30_000)

        assert frames[-1]["result"]["stopReason"] == STOP_REASON_LOCAL_LIMIT
        assert frames[-1]["result"]["stopReason"] != STOP_REASON_REFUSAL
        assert "/chat/completions" not in host.paths
        # The refusal is stated to the person, not swallowed.
        assert (
            "no safe context boundary"
            in _updates(frames, kind="agent_message_chunk")[0]["content"]["text"]
        )

    def test_the_models_own_window_is_the_automatic_bound(self, monkeypatch, tmp_path) -> None:
        """With no operator budget, the bound is the live model's own window."""
        host = _ScriptedHost([_catalog_row("qwen/chat-8b", loaded_context=32_768)])
        server = _server(monkeypatch, host)
        monkeypatch.setattr(
            server,
            "_poll_completion",
            lambda *_args, **_kwargs: pytest.fail("an oversized request must not reach inference"),
        )
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "x" * 200_000)

        assert frames[-1]["result"]["stopReason"] == STOP_REASON_LOCAL_LIMIT
        assert "/chat/completions" not in host.paths


class TestUsageFrame:
    def test_the_completion_usage_is_forwarded_as_a_context_usage_frame(
        self, monkeypatch, tmp_path
    ) -> None:
        """The dashboard's context meter is fed ONLY by this adapter frame.

        The window-size frame carries only the denominator; a
        ``usage_update {used, size}`` frame is what sets the numerator. The
        adapter's one non-streaming completion returns a ``usage`` object, and
        dropping it left the meter reading 0% for a whole session even though
        the window beside it was right.
        """
        host = _ScriptedHost(
            [_catalog_row("qwen/chat-8b", loaded_context=32_768)],
            [
                _completion(
                    "qwen/chat-8b",
                    usage={"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290},
                )
            ],
        )
        server = _server(monkeypatch, host)
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "hello")

        usage_frames = _updates(frames, kind=UPDATE_USAGE)
        assert usage_frames, "the adapter must emit a usage_update frame"
        # ``used`` is this request's prompt plus its completion: the occupancy
        # the next request will carry. ``size`` is the model's own window.
        assert usage_frames[-1]["used"] == 1234 + 56
        assert usage_frames[-1]["size"] == 32_768

    def test_a_completion_without_usage_emits_no_context_usage_frame(
        self, monkeypatch, tmp_path
    ) -> None:
        host = _ScriptedHost(
            [_catalog_row("qwen/chat-8b", loaded_context=32_768)],
            [_completion("qwen/chat-8b")],
        )
        server = _server(monkeypatch, host)
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "hello")

        assert _updates(frames, kind=UPDATE_USAGE) == []

    def test_a_malformed_usage_count_does_not_abort_the_turn(self, monkeypatch, tmp_path) -> None:
        """The counts ride back AFTER the output, so they are best-effort: a
        junk count reads as 0 instead of turning a delivered answer into a
        failed turn."""
        host = _ScriptedHost(
            [_catalog_row("qwen/chat-8b", loaded_context=32_768)],
            [
                _completion(
                    "qwen/chat-8b",
                    usage={"prompt_tokens": {"bad": 1}, "completion_tokens": "junk"},
                )
            ],
        )
        server = _server(monkeypatch, host)
        session_id = _new_session(server, tmp_path)

        frames = _prompt(server, session_id, "hello")

        assert frames[-1]["result"]["stopReason"] == "end_turn"
        usage_frames = _updates(frames, kind=UPDATE_USAGE)
        assert usage_frames and usage_frames[-1]["used"] == 0
