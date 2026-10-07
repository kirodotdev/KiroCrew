"""The dashboard names what a live session reports running, not what config says.

Each fact comes from the session's own report: the prompt response's
``model_usage`` for the model that ran, the ``configOptions`` a
``session/set_config_option`` answers with for the effort in force, and the
provider for its backend and crew. The SPA reads whether Crew's own servers
reached it off the session MCP report, with mirrors of two sets pinned here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp._dispatch import parse_prompt_turn_model
from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
)
from kiro_crew.dashboard.chat_turn.model_fallback import _sync_served_model
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.mcp_cleanup import CONTROL_PLANE_SERVERS
from kiro_crew.providers.acp import AcpProvider

_WEBSITE_SRC = Path(__file__).resolve().parents[1] / "website" / "src"

# A claude-agent-acp 0.73.0 prompt response, trimmed: the main loop ran a model
# other than the one the session selected, and an internal call ran a small one.
_PROMPT_RESULT = {
    "_meta": {
        "quota": {
            "model_usage": [
                {"model": "claude-haiku-4-5", "token_count": {"totalTokens": 900}},
                {
                    "model": "global.anthropic.claude-opus-5[1m]",
                    "token_count": {"totalTokens": 176129, "outputTokens": 141},
                },
            ],
        },
    },
    "stopReason": "end_turn",
}


def _effort_option(current: str) -> dict:
    return {
        "category": "thought_level",
        "currentValue": current,
        "id": "effort",
        "options": [{"value": v} for v in ("default", "low", "high", "max")],
        "type": "select",
    }


def test_turn_model_is_the_row_that_spent_the_most_tokens() -> None:
    assert parse_prompt_turn_model(_PROMPT_RESULT) == "global.anthropic.claude-opus-5[1m]"


@pytest.mark.parametrize(
    "result",
    [
        {"stopReason": "end_turn"},
        {"_meta": {"quota": {"model_usage": "x"}}},
        {"_meta": {"quota": {"model_usage": [{"model": "", "token_count": {"totalTokens": 9}}]}}},
        {"_meta": {"quota": {"model_usage": [{"model": "a\nb"}, 7, None]}}},
        None,
    ],
)
def test_turn_model_is_empty_when_the_response_names_no_model(result) -> None:
    assert parse_prompt_turn_model(result) == ""


def test_turn_model_is_redacted_whole_before_its_cap() -> None:
    # Harness-authored text bound for the chip, the Slack footer and the usage record: a
    # cap taken first passed a key through raw and cut this one to a fragment at 200.
    secret = "AKIAIOSFODNN7EXAMPLE"
    model = "m" * 190 + " " + secret
    rows = [{"model": model, "token_count": {"totalTokens": 5}}]
    turn_model = parse_prompt_turn_model({"_meta": {"quota": {"model_usage": rows}}})
    assert "AKIA" not in turn_model
    assert len(turn_model) <= 200


def test_client_turn_model_follows_prompt_responses_and_resets_with_the_session(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._track_prompt_usage(_PROMPT_RESULT)
    assert client.turn_model == "global.anthropic.claude-opus-5[1m]"
    # A turn that names no model keeps the last report.
    client._track_prompt_usage({"stopReason": "end_turn"})
    assert client.turn_model == "global.anthropic.claude-opus-5[1m]"
    client._store_session_config({"configOptions": []})
    assert client.turn_model == ""


@pytest.mark.asyncio
async def test_a_turn_without_a_response_or_a_switch_drops_the_last_turns_model(tmp_path) -> None:
    # The footer and the usage record would otherwise bill this turn to the last one's model.
    client = AcpClient(work_dir=tmp_path)
    client._track_prompt_usage(_PROMPT_RESULT)
    client._read_message = AsyncMock(return_value=None)
    client._is_process_alive = MagicMock(return_value=True)
    client._stale_eligible = True
    with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
        assert [action async for action, _ in client._prompt_loop(req_id=1, timeout=5.0)] == []
    assert client.turn_model == ""

    client._track_prompt_usage(_PROMPT_RESULT)
    client._session_id = "sid"
    client._send_request = AsyncMock(return_value=3)
    await client.set_model("claude-opus-4.8")
    assert client.turn_model == ""


@pytest.mark.asyncio
async def test_claude_effort_is_the_level_the_adapter_answers_with(tmp_path) -> None:
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client = provider._client
    client._session_id = "sid"
    client._acp_config_options = [_effort_option("default")]
    assert provider.applied_effort == ""
    client._send_request = AsyncMock(return_value=7)
    client._wait_for_response = AsyncMock(return_value={"configOptions": [_effort_option("max")]})

    await client.set_config_option("effort", "max")

    assert provider.applied_effort == "max"


@pytest.mark.asyncio
async def test_an_oversized_answer_leaves_the_cached_options(tmp_path) -> None:
    # The answer replaces the cached options and its effort rides every slots snapshot,
    # so one past the size bound is not kept: this 70 KB level would ride each one.
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client = provider._client
    client._session_id = "sid"
    client._acp_config_options = [_effort_option("high")]
    client._send_request = AsyncMock(return_value=7)
    answer = {"configOptions": [_effort_option("x" * 70_000)]}
    client._wait_for_response = AsyncMock(return_value=answer)

    await client.set_config_option("effort", "max")

    assert client.acp_config_options == [_effort_option("high")]
    assert provider.applied_effort == "high"


def test_a_reported_effort_that_is_no_effort_name_reads_as_none(tmp_path) -> None:
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    provider._client._acp_config_options = [_effort_option("Max\n" + "x" * 300)]
    assert provider.applied_effort is None


def test_kiro_effort_is_the_overlay_level_for_the_current_model(tmp_path) -> None:
    pinned = AcpProvider(
        work_dir=tmp_path / "pinned",
        acp_backend=ACP_BACKEND_KIRO,
        model="claude-opus-4.8",
        effort_per_model={"claude-opus-4.8": "high"},
    )
    unpinned = AcpProvider(work_dir=tmp_path / "auto", acp_backend=ACP_BACKEND_KIRO)
    assert pinned.applied_effort == "high"
    assert unpinned.applied_effort == ""


def test_codex_effort_rides_its_model_id_when_no_effort_option_is_advertised(tmp_path) -> None:
    # codex-acp 1.12.0 advertised mode, collaboration_mode and model, and named
    # the level only in its current pair id.
    provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    provider._client._acp_config_options = [{"id": "model", "currentValue": "openai.gpt-6.1-sol"}]
    provider._client._resolved_model_id = "openai.gpt-6.1-sol[max]"
    assert provider.applied_effort == "max"
    provider._client._resolved_model_id = "openai.gpt-6.1-sol"
    assert provider.applied_effort is None


def test_effort_is_unknown_without_an_effort_channel(tmp_path) -> None:
    assert AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE).applied_effort is None


def test_session_agent_names_the_crew_else_the_agent_spec(tmp_path) -> None:
    crew = AcpProvider(work_dir=tmp_path, agent="kirocrew-worker", crew_agent="cr-writer")
    template = AcpProvider(work_dir=tmp_path, agent="kirocrew-worker", crew_agent="")
    assert crew.session_agent == "cr-writer"
    assert template.session_agent == "kirocrew-worker"


@pytest.mark.asyncio
async def test_the_footer_names_the_reported_model_and_the_usage_row_the_selected_one(
    tmp_path, monkeypatch
) -> None:
    # One model, two spellings. A row keyed by the harness's spelling would split
    # that model's usage history in two; the footer is there to say what ran.
    from test_dashboard_chat import TestRunChatTransientRetry as _Suite

    from kiro_crew.acp.types import TurnUsage
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.handlers import usage as usage_mod
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    shard_dir = tmp_path / "usage" / "tokens"
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

    async def _stream(msg):
        client.turn_model = "global.anthropic.claude-opus-5[1m]"
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=2.5, duration_ms=1200))

    state = _Suite._make_state(tmp_path, monkeypatch)
    client = _Suite._client(_stream)
    client._resolved_model_id = "claude-opus-5"
    client.turn_model = ""
    _Suite._wire_sessions(state, client)
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, "hello")
        await _Suite._drain_bg(state)

    rows = [
        json.loads(line)
        for shard in sorted(shard_dir.glob("*.jsonl"))
        for line in shard.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["model"] for row in rows] == ["claude-opus-5"]
    footers = [
        m["meta"]["turn_stats"]
        for m in slot.messages
        if m.get("role") == "assistant" and "turn_stats" in (m.get("meta") or {})
    ]
    assert [footer["model"] for footer in footers] == ["global.anthropic.claude-opus-5[1m]"]


def test_the_spa_reads_tools_off_the_same_servers_and_harnesses() -> None:
    # The composer warns "No tools" off the session MCP report with mirrors of these
    # sets: a server renamed or a self-mounting harness added on one side alone
    # would put that warning on every session, or hide it.
    source = (_WEBSITE_SRC / "lib" / "mcpSessionReport.ts").read_text(encoding="utf-8")
    servers = re.search(r"CONTROL_PLANE_SERVERS = \[([^\]]*)\]", source)
    harnesses = re.search(r"SPEC_SERVERS_OFF_WIRE = \[([^\]]*)\]", source)
    assert servers and harnesses, "a mirror literal moved: update this parser with it"
    assert tuple(re.findall(r"'([^']+)'", servers.group(1))) == CONTROL_PLANE_SERVERS
    backend_ids = dict(
        re.findall(
            r"export const (ACP_BACKEND_\w+) = '([^']*)'",
            (_WEBSITE_SRC / "api" / "acpBackend.ts").read_text(encoding="utf-8"),
        )
    )
    mirrored = {backend_ids[name] for name in re.findall(r"\w+", harnesses.group(1))}
    assert mirrored == ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE


def test_sync_records_every_fact_and_teardown_forgets_them() -> None:
    slot = _ChatSlot("facts")
    provider = SimpleNamespace(
        served_model="global.anthropic.claude-fable-5[1m]",
        turn_model="global.anthropic.claude-opus-5[1m]",
        capabilities=SimpleNamespace(backend=ACP_BACKEND_CLAUDE),
        session_agent="cr-writer",
        applied_effort="",
    )
    _sync_served_model(slot, provider)
    payload = slot.to_dict()
    assert {k: payload[k] for k in ("served_model", "turn_model", "served_backend")} == {
        "served_model": "global.anthropic.claude-fable-5[1m]",
        "turn_model": "global.anthropic.claude-opus-5[1m]",
        "served_backend": ACP_BACKEND_CLAUDE,
    }
    assert (payload["served_agent"], payload["served_effort"]) == ("cr-writer", "")

    slot.forget_session_model_state()
    payload = slot.to_dict()
    assert payload["turn_model"] == ""
    assert (payload["served_backend"], payload["served_agent"], payload["served_effort"]) == (
        None,
        None,
        None,
    )
