"""The skill-read, hook, audit and advertised-model wiring an ``AcpClient`` feeds.

A skill read is noted under a bounded set and credited once its tool call
completes, and a missing or failing observer raises nothing; a hook failure on an
audit-source client never breaks dispatch, and a failing audit of an interrupted
tool is logged; the advertised-model cache seeds the model settings only when
warm, and a capture feeds it. Shared helpers live in
``acp_client_more_coverage_helpers.py``.
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from acp_client_more_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _client,
    _pinned_kiro_cli_version,
)

import kiro_crew.acp.client as acp_client
import kiro_crew.acp.skill_reads as skill_reads
from kiro_crew import model_registry as mr
from kiro_crew.acp.skill_reads import _MAX_NOTED_SKILL_READS, _call_key
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE

# ── Skill-read telemetry ──


class _Observer:
    def __init__(self, keys=None, resolve_exc=None, credit_exc=None):
        self._keys = keys or []
        self._resolve_exc = resolve_exc
        self._credit_exc = credit_exc
        self.credited: list[list[str]] = []

    def resolve_tool_read_keys(self, tool_name, raw_params, command):
        if self._resolve_exc:
            raise self._resolve_exc
        return list(self._keys)

    def credit_skill_reads(self, keys):
        if self._credit_exc:
            raise self._credit_exc
        self.credited.append(list(keys))


def _skill_call(tool_call_id="tc-1"):
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tool_call_id,
        tool_name="fs_read",
        raw_tool_params={"path": "/skills/demo/SKILL.md"},
    )


class TestSkillReadTelemetry:
    @pytest.mark.asyncio
    async def test_noted_set_is_capped_then_credited_on_completion(self, tmp_path, monkeypatch):
        observer = _Observer(keys=["demo"])
        monkeypatch.setattr(skill_reads, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)
        client._skill_reads.noted = {f"old-{i}" for i in range(_MAX_NOTED_SKILL_READS)}
        client._skill_reads.pending = {"old-0": ["stale"]}

        await client._skill_reads.note(_skill_call())

        # cleared wholesale, then re-seeded under the call's fixed-size key
        assert client._skill_reads.noted == {_call_key("tc-1")}
        assert client._skill_reads.pending == {_call_key("tc-1"): ["demo"]}

        client._skill_reads.credit(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_final=True)
        )

        assert observer.credited == [["demo"]]
        assert client._skill_reads.pending == {}

    @pytest.mark.asyncio
    async def test_resolution_failure_leaves_nothing_pending(self, tmp_path, monkeypatch):
        observer = _Observer(resolve_exc=RuntimeError("skills tree unreadable"))
        monkeypatch.setattr(skill_reads, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)

        await client._skill_reads.note(_skill_call("tc-2"))

        assert client._skill_reads.pending == {}

    def test_credit_is_skipped_without_an_observer(self, tmp_path, monkeypatch):
        client = _client(tmp_path)
        client._skill_reads.pending = {_call_key("tc-3"): ["demo"]}
        monkeypatch.setattr(skill_reads, "get_global_skill_read_observer", lambda: None)

        client._skill_reads.credit(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-3", tool_final=True)
        )

        assert client._skill_reads.pending == {}  # popped, but nothing delivered

    def test_credit_failure_is_swallowed(self, tmp_path, monkeypatch):
        observer = _Observer(credit_exc=RuntimeError("ledger locked"))
        monkeypatch.setattr(skill_reads, "get_global_skill_read_observer", lambda: observer)
        client = _client(tmp_path)
        client._skill_reads.pending = {_call_key("tc-4"): ["demo"]}

        client._skill_reads.credit(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-4", tool_final=True)
        )

        assert observer.credited == []


# ── Hook engine parity for audit-source clients ──


class TestHookFailuresAreNonFatal:
    @pytest.mark.asyncio
    async def test_pre_tool_hook_error_does_not_break_dispatch(self, tmp_path, monkeypatch):
        client = _client(tmp_path, audit_source="code-review-sage")
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: object())
        fired: list[str] = []

        async def _boom(store, tool_name, tool_input, **kwargs):
            fired.append(tool_name)
            raise RuntimeError("hook script exploded")

        monkeypatch.setattr(acp_client, "fire_tool_hooks", _boom)

        # Must return normally — a hook failure never blocks the tool.
        await client._maybe_fire_pre_tool_hooks(
            AcpEvent(kind=EVENT_TOOL_CALL, title="ls", tool_input="{}")
        )

        assert fired == ["ls"]  # the hook really was attempted before it failed

    @pytest.mark.asyncio
    async def test_post_tool_hook_strips_the_running_prefix(self, tmp_path, monkeypatch):
        fired: list[dict] = []

        class _Store:
            async def fire(self, event, **kwargs):
                fired.append({"event": event, **kwargs})

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _Store())
        client = _client(tmp_path, audit_source="knowledge-pool")
        client._observed_tool_calls["tc-9"] = ("Running: ls -la", "execute")

        await client._maybe_fire_post_tool_hooks(
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-9",
                tool_output="total 0",
                tool_final=True,
            )
        )

        assert fired[0]["tool_name"] == "ls -la"
        assert fired[0]["tool_response"] == {"output": "total 0"}

    @pytest.mark.asyncio
    async def test_post_tool_hook_error_is_swallowed(self, tmp_path, monkeypatch):
        attempts: list[str] = []

        class _Store:
            async def fire(self, event, **kwargs):
                attempts.append(event)
                raise RuntimeError("hook store down")

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _Store())
        client = _client(tmp_path, audit_source="knowledge-pool")

        await client._maybe_fire_post_tool_hooks(
            AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-10", tool_output="x")
        )

        assert attempts == [HOOK_EVENT_POST_TOOL_USE]


class TestToolInterruptedAudit:
    def test_sel_failure_does_not_propagate(self, tmp_path, monkeypatch, caplog):
        client = _client(tmp_path)
        client._session_id = "sid"

        def _boom():
            raise RuntimeError("SEL backend unavailable")

        monkeypatch.setattr("kiro_crew.sel.sel", _boom)

        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            client._emit_tool_interrupted_sel("unit-test")

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "SEL audit failed for tool_interrupted" in messages


# ── Provider-advertised model cache wiring (client side) ──


class TestAdvertisedModelCacheWiring:
    """The client half of sourcing model selection from the provider's own
    advertised list: the seed reads the cache, and a capture feeds it.

    The module-global ``mr._ADVERTISED_MODELS`` is isolated per test.
    """

    def _read_seed(self, tmp_path: Path) -> dict:
        return json.loads((tmp_path / ".claude" / "settings.local.json").read_text())

    def test_seed_availableModels_from_advertised_cache(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        served = [
            "global.anthropic.claude-opus-5[1m]",
            "global.anthropic.claude-opus-4-8[1m]",
        ]
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": served})
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert self._read_seed(tmp_path)["availableModels"] == served

    def test_cold_cache_seeds_no_model_keys_at_all(self, tmp_path, monkeypatch):
        # No static-registry fallback: a guessed allowlist poisons the adapter's
        # union+dedup merge for any model the registry has not caught up on, so an
        # unseeded file (adapter falls back to its own provider list) beats a stale
        # one. The post-capture re-seed writes the model key, and the list only when
        # another settings source defines one.
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE, model="claude-opus-5")
        client._write_claude_local_settings()
        seed = self._read_seed(tmp_path)
        assert "availableModels" not in seed
        assert "model" not in seed

    def test_claude_capture_feeds_and_flags_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._capture_available_models(
            {"models": {"availableModels": [{"modelId": "global.anthropic.claude-opus-5[1m]"}]}}
        )
        assert mr.advertised_models("claude_code") == ["global.anthropic.claude-opus-5[1m]"]
        assert client._advertised_models_changed is True

    def test_non_claude_capture_does_not_feed_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        client = _client(tmp_path)  # default backend is kiro-cli
        client._capture_available_models(
            {"models": {"availableModels": [{"modelId": "claude-opus-4.8"}]}}
        )
        assert mr.advertised_models("claude_code") == []
        assert client._advertised_models_changed is False

    @pytest.mark.asyncio
    async def test_set_model_folds_bare_id_onto_advertised_spelling(self, tmp_path, monkeypatch):
        # The warm-pool 4.8 fix: a claim that switches model must send the
        # versioned [1m] id the backend serves at 1M, not the bare spelling.
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-4-8[1m]"]}
        )
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=1)
        client.set_config_option = AsyncMock(return_value=None)
        await client.set_model("claude-opus-4-8")
        assert client._model == "global.anthropic.claude-opus-4-8[1m]"
        assert client._resolved_model_id == "global.anthropic.claude-opus-4-8[1m]"

    @pytest.mark.asyncio
    async def test_set_model_reseeds_settings_on_claude(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # The re-seed half: set_model refreshes settings.local.json so a pooled
        # runtime's stale spawn-time seed is overwritten with the claimed model.
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-4-8[1m]"]}
        )
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=1)
        client.set_config_option = AsyncMock(return_value=None)
        await client.set_model("claude-opus-4-8")
        seed = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert seed["model"] == "global.anthropic.claude-opus-4-8[1m]"
        assert "global.anthropic.claude-opus-4-8[1m]" in seed["availableModels"]

    @pytest.mark.asyncio
    async def test_set_model_on_non_member_backend_neither_folds_nor_reseeds(
        self, tmp_path, monkeypatch
    ):
        # codex is a MODEL_VIA_CONFIG_OPTION backend but NOT a member of
        # ADVERTISED_MODEL_SELECTION / SEED_LOCAL_SETTINGS, so a warm claim must
        # switch the model verbatim: no fold onto a cached [1m] spelling, no
        # settings.local.json. Guards the capability gating against a regression to
        # "any config-option backend".
        monkeypatch.setattr(
            mr, "_ADVERTISED_MODELS", {"claude_code": ["global.anthropic.claude-opus-4-8[1m]"]}
        )
        client = _client(tmp_path, acp_backend=ACP_BACKEND_CODEX)
        client._session_id = "sid"
        client.set_config_option = AsyncMock(return_value=None)
        await client.set_model("gpt-5-codex")
        assert client._model == "gpt-5-codex"  # sent verbatim, no fold
        assert not (tmp_path / ".claude" / "settings.local.json").exists()
