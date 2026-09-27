"""Tests for the shared reasoning-effort vocabulary (effort.py) and the
ACP provider cli.json overlay helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from effort_helpers import _UNPARSEABLE_OVERLAY_CONTENTS, _read_cli_overlay

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_pool as rp
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.effort import (
    EFFORT_LEVELS,
    EFFORT_VALUES,
    effort_settings_key,
    is_valid_effort,
    model_supports_effort,
    resolve_effort_for_model,
)
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers.acp import (
    _KIROCREW_EFFORT_OWNED_KEY,
    _clear_cli_overlay_effort,
    _write_cli_overlay,
)
from kiro_crew.testing.wait import default_timeout


async def _await_test(awaitable, what: str):
    try:
        async with asyncio.timeout(default_timeout()) as deadline:
            return await awaitable
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise AssertionError(f"timed out waiting for {what}") from exc


@pytest.mark.asyncio
async def test_await_test_propagates_awaited_timeout():
    async def raise_timeout():
        raise TimeoutError("awaited operation timed out")

    with pytest.raises(TimeoutError, match="awaited operation timed out"):
        await _await_test(raise_timeout(), "inner timeout")


@pytest.mark.asyncio
async def test_await_test_reports_own_deadline_and_cancels_awaitable(monkeypatch):
    cancelled = asyncio.Event()

    async def never_finishes():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(sys.modules[__name__], "default_timeout", lambda: 0)

    with pytest.raises(AssertionError, match="^timed out waiting for blocked test$"):
        await _await_test(never_finishes(), "blocked test")
    assert cancelled.is_set()


class TestEffortVocabulary:
    def test_levels_include_xhigh_ordered(self):
        assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    def test_values_add_empty_sentinel(self):
        assert EFFORT_VALUES == frozenset({"", "low", "medium", "high", "xhigh", "max"})

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_is_valid_effort_true(self, level: str):
        assert is_valid_effort(level)

    @pytest.mark.parametrize("bad", ["", "LOW", "ultra", " low", 5, None, ["max"]])
    def test_is_valid_effort_false(self, bad: object):
        assert not is_valid_effort(bad)


class TestModelSupportsEffort:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4.7",
            "claude-sonnet-4.6",
            "global.anthropic.claude-opus-4-8[1m]",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "claude-fable-5",
            "global.anthropic.claude-fable-5[1m]",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
        ],
    )
    def test_opus_sonnet_fable_gpt_supported(self, model: str):
        assert model_supports_effort(model)

    @pytest.mark.parametrize(
        "model",
        [
            None,
            "",
            "auto",
            "amazon.nova-pro-v1:0",
            "deepseek-3.2",
            "minimax-m2.5",
            "glm-5",
            "qwen3-coder-next",
        ],
    )
    def test_unsupported(self, model: str | None):
        assert not model_supports_effort(model)

    def test_raw_haiku_id_never_supports_effort_even_with_registry_fold(self):
        # The registry has no Haiku Bedrock profile, so claude-haiku-4.5 (a kiro
        # id) is registered as a claude_code ALIAS of Sonnet 4.6 1M (the cheapest
        # VALID Bedrock fold — passing it through verbatim would crash a CC
        # session with -32603). But "Haiku never supports effort" is a HARD rule
        # that must win over the registry: model_supports_effort is provider-
        # agnostic, and a kiro/acp Haiku agent reaches it with the RAW
        # "claude-haiku-4.5" spelling (the kiro path does NOT translate). So the
        # raw id must report False, NOT inherit Sonnet's supports_effort flag.
        from kiro_crew import model_registry as mr

        # The fold itself is unchanged — claude_code translation -> Sonnet id.
        assert mr.to_provider_id("claude-haiku-4.5", "claude_code") == (
            "global.anthropic.claude-sonnet-4-6[1m]"
        )
        # The raw kiro Haiku id is correctly effort-INCAPABLE (haiku guard wins).
        assert model_supports_effort("claude-haiku-4.5") is False
        # On the claude_code path the value reaching here is the FOLDED Sonnet
        # provider id (translated at the factory boundary), which IS capable.
        assert model_supports_effort("global.anthropic.claude-sonnet-4-6[1m]") is True
        # A model the registry does NOT list still uses the substring heuristic.
        assert model_supports_effort("some-haiku-thing") is False


class TestEffortSettingsKey:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4.7",
            "claude-sonnet-4.6",
            "claude-fable-5",
            "global.anthropic.claude-opus-4-8[1m]",
            None,
            "auto",
        ],
    )
    def test_claude_and_default_use_output_config(self, model: str | None):
        assert effort_settings_key(model) == "output_config"

    @pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "GPT-5.6-Terra"])
    def test_gpt_uses_reasoning(self, model: str):
        assert effort_settings_key(model) == "reasoning"


class TestResolveEffortForModel:
    def test_slot_override_wins(self):
        assert (
            resolve_effort_for_model(
                "claude-opus-4.7",
                slot_overrides={"claude-opus-4.7": "low"},
                defaults={"claude-opus-4.7": "max"},
            )
            == "low"
        )

    def test_falls_back_to_defaults(self):
        assert (
            resolve_effort_for_model("claude-opus-4.7", defaults={"claude-opus-4.7": "high"})
            == "high"
        )

    def test_defaults_accept_json_string(self):
        # Frontend setVariable only stores strings, so defaults may arrive
        # JSON-encoded.
        assert (
            resolve_effort_for_model("claude-opus-4.7", defaults='{"claude-opus-4.7": "xhigh"}')
            == "xhigh"
        )

    def test_none_when_model_incapable(self):
        # 'auto' is genuinely effort-incapable (registry maps it to ""). (Haiku
        # folds to Sonnet and IS effort-capable — see
        # TestModelSupportsEffort.test_haiku_4_5_folds_to_sonnet_and_supports_effort.)
        assert resolve_effort_for_model("auto", slot_overrides={"auto": "max"}) is None

    def test_none_when_no_level(self):
        assert resolve_effort_for_model("claude-opus-4.7") is None

    def test_malformed_defaults_ignored(self):
        assert resolve_effort_for_model("claude-opus-4.7", defaults="not json") is None
        assert resolve_effort_for_model("claude-opus-4.7", defaults=12345) is None


class TestCliOverlay:
    def test_write_then_read_roundtrip(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "xhigh")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "xhigh"}
        # Verify on-disk shape matches kiro-cli's expected format.
        cli = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "xhigh"

    def test_write_merges_preserves_other_keys(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text(
            json.dumps(
                {
                    "chat.enableNotifications": True,
                    "chat.modelDefaults": {
                        "claude-opus-4.6": {"output_config": {"effort": "high"}}
                    },
                }
            )
        )
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        data = json.loads((settings_dir / "cli.json").read_text(encoding="utf-8"))
        # Existing unrelated setting preserved.
        assert data["chat.enableNotifications"] is True
        # Both models present.
        assert data["chat.modelDefaults"]["claude-opus-4.6"]["output_config"]["effort"] == "high"
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "max"

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert _read_cli_overlay(tmp_path) == {}

    def test_read_malformed_returns_empty(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text("{ not json")
        assert _read_cli_overlay(tmp_path) == {}

    def test_read_undecodable_returns_empty(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_bytes(b'{"x": "\xff"}')
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.parametrize(
        "contents",
        [
            b"{ not json",
            b'{"x": "\xff"}',
            b"[1, 2]",
            b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}, "n": '
            + b"1" * 5000
            + b"}",
            b"[" * 200_000 + b"]" * 200_000,
        ],
        ids=["malformed", "undecodable", "non-object", "oversized-int-literal", "deeply-nested"],
    )
    def test_write_onto_an_unmergeable_file_raises_and_leaves_it_unchanged(
        self, tmp_path, contents
    ):
        # A silent return here would let the caller report a persisted level the
        # next respawn drops, and a reset to {} would destroy operator data. The
        # file is neither.
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_bytes(contents)

        with pytest.raises(ValueError, match="left unchanged"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")

        assert cli_json.read_bytes() == contents
        assert _read_cli_overlay(tmp_path) == {}

    def test_write_onto_an_unreadable_file_raises_the_read_error(self, tmp_path, monkeypatch):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()
        real_read = acp_provider.safe_read_file_bytes_nolink

        def _flaky_read(raw, *args, **kwargs):
            if Path(raw).name == "cli.json":
                raise OSError("sharing violation")
            return real_read(raw, *args, **kwargs)

        def _failed_pinned_read(_settings_fd, _cli_json):
            raise OSError("sharing violation")

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "safe_read_file_bytes_nolink", _flaky_read)
            temporary_patches.setattr(acp_provider, "_read_pinned_cli_json", _failed_pinned_read)
            with pytest.raises(OSError, match="sharing violation"):
                _write_cli_overlay(tmp_path, "claude-opus-4.7", "low")

        assert cli_json.read_bytes() == before

    def test_projection_publishes_value_and_record_in_one_write(self, tmp_path, monkeypatch):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        writes = []
        real_atomic_write = acp_provider.atomic_write

        def _record_write(target, content, **kwargs):
            writes.append(target)
            real_atomic_write(target, content, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", _record_write)
        _write_cli_overlay(tmp_path, model, "low")

        assert [target.name for target in writes] == ["cli.json"]
        data = json.loads(writes[0].read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] == "low"
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {model: "low"}

    def test_failed_projection_write_leaves_value_and_record_agreeing(self, tmp_path, monkeypatch):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()
        writes = []

        def _fail_write(target, content, **kwargs):
            writes.append(target)
            raise OSError("disk full")

        monkeypatch.setattr(acp_provider, "atomic_write", _fail_write)
        with pytest.raises(OSError, match="disk full"):
            _write_cli_overlay(tmp_path, model, "low")

        assert [target.name for target in writes] == ["cli.json"]
        assert cli_json.read_bytes() == before
        data = json.loads(before)
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] == "max"
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {model: "max"}

    def test_own_write_stamp_keeps_owned_clear_valid(self, tmp_path):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))

        assert data["kirocrew.effortOwnedStamp"] == int(cli_json.stat().st_mtime)
        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {}

    @staticmethod
    def _stamp_clock(monkeypatch):
        """Drive the stamp clock by hand, so a repeat write would take a different stamp."""
        from types import SimpleNamespace

        from kiro_crew import workspace_cli_settings

        clock = [1_790_000_000.0]
        monkeypatch.setattr(workspace_cli_settings, "time", SimpleNamespace(time=lambda: clock[0]))

        def advance(seconds):
            clock[0] += seconds

        return advance

    def test_same_level_write_leaves_cli_json_bytes_and_mtime_unchanged(
        self, tmp_path, monkeypatch
    ):
        model = "claude-opus-4.7"
        advance = self._stamp_clock(monkeypatch)
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()
        before_stat = cli_json.stat()
        advance(10)

        _write_cli_overlay(tmp_path, model, "max")

        after_stat = cli_json.stat()
        assert cli_json.read_bytes() == before
        assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
            before_stat.st_ino,
            before_stat.st_mtime_ns,
        )
        data = json.loads(before)
        assert data["kirocrew.effortOwnedStamp"] == int(after_stat.st_mtime)
        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {}

    def test_level_change_writes_and_restamps(self, tmp_path, monkeypatch):
        model = "claude-opus-4.7"
        advance = self._stamp_clock(monkeypatch)
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = json.loads(cli_json.read_text(encoding="utf-8"))
        advance(10)

        _write_cli_overlay(tmp_path, model, "low")

        after = json.loads(cli_json.read_text(encoding="utf-8"))
        assert after["chat.modelDefaults"][model]["output_config"]["effort"] == "low"
        assert after[_KIROCREW_EFFORT_OWNED_KEY] == {model: "low"}
        assert after["kirocrew.effortOwnedStamp"] == int(cli_json.stat().st_mtime)
        assert after["kirocrew.effortOwnedStamp"] == before["kirocrew.effortOwnedStamp"] + 10
        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {}

    def test_same_level_write_after_outside_resave_drops_the_void_record(
        self, tmp_path, monkeypatch
    ):
        model = "claude-opus-4.7"
        advance = self._stamp_clock(monkeypatch)
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        resaved = cli_json.read_bytes()
        cli_json.write_bytes(resaved)
        assert json.loads(resaved)["kirocrew.effortOwnedStamp"] != int(cli_json.stat().st_mtime)
        advance(10)

        _write_cli_overlay(tmp_path, model, "max")

        after = json.loads(cli_json.read_text(encoding="utf-8"))
        assert cli_json.read_bytes() != resaved
        assert after["chat.modelDefaults"][model]["output_config"]["effort"] == "max"
        assert _KIROCREW_EFFORT_OWNED_KEY not in after
        assert "kirocrew.effortOwnedStamp" not in after
        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {model: "max"}

    def test_same_value_operator_rewrite_voids_owned_clear(self, tmp_path, caplog):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        data["chat.modelDefaults"][model]["output_config"]["effort"] = "max"
        cli_json.write_text(json.dumps(data), encoding="utf-8")
        assert data["kirocrew.effortOwnedStamp"] != int(cli_json.stat().st_mtime)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert (
                _clear_cli_overlay_effort(tmp_path, model, owned_only=True, warn_unowned=True)
                is True
            )
        assert _read_cli_overlay(tmp_path) == {model: "max"}
        assert any(
            "holds an effort level Kiro Crew did not write" in record.getMessage()
            for record in caplog.records
        )
        after = json.loads(cli_json.read_text(encoding="utf-8"))
        assert _KIROCREW_EFFORT_OWNED_KEY not in after

    def test_unrelated_tool_search_write_drops_void_effort_ownership(self, tmp_path):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        data["chat.modelDefaults"][model]["output_config"]["effort"] = "max"
        cli_json.write_text(json.dumps(data), encoding="utf-8")

        acp_provider._write_tool_search_overlay(tmp_path, True)

        after_write = json.loads(cli_json.read_text(encoding="utf-8"))
        assert _KIROCREW_EFFORT_OWNED_KEY not in after_write
        assert _clear_cli_overlay_effort(tmp_path, None, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {model: "max"}

    def test_tool_search_write_preserves_valid_effort_ownership(self, tmp_path):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")

        acp_provider._write_tool_search_overlay(tmp_path, True)
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["kirocrew.effortOwnedStamp"] == int(cli_json.stat().st_mtime)

        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {}

    def test_operator_save_after_publish_voids_owned_clear(self, tmp_path, monkeypatch):
        model = "claude-opus-4.7"
        real_atomic_write = acp_provider.atomic_write
        operator_saves = []

        def _publish_then_operator_save(target, content, **kwargs):
            real_atomic_write(target, content, **kwargs)
            Path(target).write_text(content, encoding="utf-8")
            operator_saves.append(Path(target))

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "atomic_write", _publish_then_operator_save)
            _write_cli_overlay(tmp_path, model, "max")

        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        written = json.loads(cli_json.read_text(encoding="utf-8"))
        assert operator_saves == [cli_json]
        assert written["kirocrew.effortOwnedStamp"] != int(cli_json.stat().st_mtime)

        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {model: "max"}
        after = json.loads(cli_json.read_text(encoding="utf-8"))
        assert _KIROCREW_EFFORT_OWNED_KEY not in after

    @pytest.mark.parametrize(
        "stored_mtime_quantum_ns",
        [
            pytest.param(1, id="nanosecond"),
            pytest.param(2_000_000_000, id="fat-two-second"),
        ],
    )
    def test_operator_save_one_clock_tick_behind_the_publish_voids_owned_clear(
        self, tmp_path, monkeypatch, stored_mtime_quantum_ns
    ):
        from types import SimpleNamespace

        from kiro_crew import workspace_cli_settings

        model = "claude-opus-4.7"
        publish_ns = 1_790_000_000_002_000_000
        monkeypatch.setattr(
            workspace_cli_settings, "time", SimpleNamespace(time=lambda: publish_ns / 1e9)
        )
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        published = json.loads(cli_json.read_text(encoding="utf-8"))
        if stored_mtime_quantum_ns == 2_000_000_000:
            assert published["kirocrew.effortOwnedStamp"] % 2 == 0
        cli_json.write_text(cli_json.read_text(encoding="utf-8"), encoding="utf-8")
        # The kernel dates a save from a clock that can trail time.time() by one tick
        # (about 16 ms on Windows), so a save just after a second boundary can be
        # stored behind the publish clock.
        lagging_save_ns = publish_ns - 16_000_000
        stored_lagging_save_ns = (
            lagging_save_ns // stored_mtime_quantum_ns * stored_mtime_quantum_ns
        )
        os.utime(cli_json, ns=(stored_lagging_save_ns, stored_lagging_save_ns))

        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        assert _read_cli_overlay(tmp_path) == {model: "max"}

    def test_recursive_document_clear_fails_closed_without_touching_file(self, tmp_path):
        model = "claude-opus-4.7"
        depth = sys.getrecursionlimit() * 3 // 5
        raw = (
            b'{"chat.modelDefaults":{"claude-opus-4.7":{"output_config":{"effort":"max"}}},'
            b'"deep":' + b"[" * depth + b"0" + b"]" * depth + b"}"
        )
        parsed = json.loads(raw)
        assert isinstance(parsed, dict), "the JSON reader must accept the recursive fixture"
        with pytest.raises(RecursionError):
            copy.deepcopy(parsed)

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_bytes(raw)
        before = cli_json.read_bytes()
        before_mtime = cli_json.stat().st_mtime_ns

        assert _clear_cli_overlay_effort(tmp_path, model) is False
        assert _clear_cli_overlay_effort(tmp_path, None) is False
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is False
        assert cli_json.read_bytes() == before
        assert cli_json.stat().st_mtime_ns == before_mtime

    def test_failed_clear_write_leaves_value_and_record_agreeing(self, tmp_path, monkeypatch):
        model = "claude-opus-4.7"
        _write_cli_overlay(tmp_path, model, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()
        writes = []

        def _fail_write(target, content, **kwargs):
            writes.append(target)
            raise OSError("disk full")

        monkeypatch.setattr(acp_provider, "atomic_write", _fail_write)
        assert _clear_cli_overlay_effort(tmp_path, model) is False

        assert [target.name for target in writes] == ["cli.json"]
        assert cli_json.read_bytes() == before
        data = json.loads(before)
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] == "max"
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {model: "max"}

    def test_same_operator_value_is_not_claimed_by_projection(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_text(
            json.dumps(
                {"chat.modelDefaults": {"claude-opus-4.7": {"output_config": {"effort": "max"}}}}
            ),
            encoding="utf-8",
        )

        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "max"}
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    def test_projection_preserves_unowned_null_effort_byte_for_byte(self, tmp_path):
        model = "claude-opus-4.7"
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_text(
            json.dumps({"chat.modelDefaults": {model: {"output_config": {"effort": None}}}}),
            encoding="utf-8",
        )
        before = cli_json.read_bytes()

        _write_cli_overlay(tmp_path, model, "max")

        assert cli_json.read_bytes() == before
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] is None
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    def test_owned_clear_preserves_unowned_null_effort_byte_for_byte(self, tmp_path):
        model = "claude-opus-4.7"
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_text(
            json.dumps({"chat.modelDefaults": {model: {"output_config": {"effort": None}}}}),
            encoding="utf-8",
        )
        before = cli_json.read_bytes()

        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True

        assert cli_json.read_bytes() == before
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] is None
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    @pytest.mark.parametrize("recorded", [None, ""], ids=["null", "empty-string"])
    def test_malformed_record_entry_does_not_own_null_effort(self, tmp_path, recorded):
        model = "claude-opus-4.7"
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {model: {"output_config": {"effort": None}}},
                    _KIROCREW_EFFORT_OWNED_KEY: {model: recorded},
                }
            ),
            encoding="utf-8",
        )

        _write_cli_overlay(tmp_path, model, "max")
        after_projection = json.loads(cli_json.read_text(encoding="utf-8"))
        assert after_projection["chat.modelDefaults"][model]["output_config"]["effort"] is None
        assert _KIROCREW_EFFORT_OWNED_KEY not in after_projection

        assert _clear_cli_overlay_effort(tmp_path, model, owned_only=True) is True
        after_clear = json.loads(cli_json.read_text(encoding="utf-8"))
        assert after_clear["chat.modelDefaults"][model]["output_config"]["effort"] is None
        assert _KIROCREW_EFFORT_OWNED_KEY not in after_clear

    def test_same_operator_value_under_other_key_does_not_disown_projection(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_text(
            json.dumps(
                {"chat.modelDefaults": {"claude-opus-4.7": {"reasoning": {"effort": "max"}}}}
            ),
            encoding="utf-8",
        )

        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")

        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {"claude-opus-4.7": "max"}
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "max"}
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    def test_clear_removes_only_target_model(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        _write_cli_overlay(tmp_path, "claude-opus-4.6", "high")
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.6": "high"}

    def test_clear_missing_file_noop(self, tmp_path):
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")  # must not raise
        assert _read_cli_overlay(tmp_path) == {}

    def test_explicit_operator_clear_removes_an_unowned_entry(self, tmp_path):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / "cli.json").write_text(
            json.dumps(
                {"chat.modelDefaults": {"claude-opus-4.7": {"output_config": {"effort": "max"}}}}
            ),
            encoding="utf-8",
        )

        assert (
            _clear_cli_overlay_effort(
                tmp_path,
                "claude-opus-4.7",
                owned_only=False,
            )
            is True
        )
        assert _read_cli_overlay(tmp_path) == {}

    def test_clear_reports_success_only_when_the_file_stops_naming_the_model(self, tmp_path):
        # The postcondition is about the FILE, so an absent file and an absent
        # entry are both successes -- there is nothing left for a spawn to read.
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is True
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is True
        assert _read_cli_overlay(tmp_path) == {}

    @_UNPARSEABLE_OVERLAY_CONTENTS
    @pytest.mark.parametrize("model", ["claude-opus-4.7", None])
    def test_clear_leaves_a_file_no_parser_can_read_unchanged(self, tmp_path, model, contents):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_bytes(contents)

        assert _clear_cli_overlay_effort(tmp_path, model) is True
        assert cli_json.read_bytes() == contents

    def test_clear_leaves_a_malformed_cli_json_unchanged(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        malformed = b"{ not json"
        cli_json.write_bytes(malformed)

        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is True
        assert cli_json.read_bytes() == malformed

    @pytest.mark.parametrize("model", ["claude-opus-4.7", None])
    def test_clear_leaves_an_undecodable_cli_json_unchanged(self, tmp_path, model):
        # Non-UTF-8 bytes fail in read_text, before json sees them. That is a
        # ValueError like malformed JSON, not the OSError of a failed read, and
        # the file names no effort for anyone, so the answer is the same no-op.
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        undecodable = b'{"x": "\xff"}'
        cli_json.write_bytes(undecodable)

        assert _clear_cli_overlay_effort(tmp_path, model) is True
        assert cli_json.read_bytes() == undecodable

    def test_clear_separates_a_malformed_file_from_a_failed_read(self, tmp_path, monkeypatch):
        # Two very different facts share one code path. A malformed file names no
        # effort for anyone and `_read_cli_overlay` reads it as {} too, so the
        # postcondition already holds. A read that fails while the file EXISTS
        # and the lock is held is transient IO, and the level may still be on
        # disk -- reporting a clear there is the silent stale reload this return
        # value exists to prevent.
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text("{ not json", encoding="utf-8")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is True

        (settings_dir / "cli.json").write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {"claude-opus-4.7": {"output_config": {"effort": "max"}}},
                    _KIROCREW_EFFORT_OWNED_KEY: {"claude-opus-4.7": "max"},
                }
            ),
            encoding="utf-8",
        )
        real_read = acp_provider.safe_read_file_bytes_nolink

        def _flaky_read(raw, *args, **kwargs):
            if Path(raw).name == "cli.json":
                raise OSError("sharing violation")
            return real_read(raw, *args, **kwargs)

        def _failed_pinned_read(_settings_fd, _cli_json):
            raise OSError("sharing violation")

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "safe_read_file_bytes_nolink", _flaky_read)
            temporary_patches.setattr(acp_provider, "_read_pinned_cli_json", _failed_pinned_read)
            assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "max"}

    def test_clear_reports_failure_only_when_the_lock_is_genuinely_stuck(
        self, tmp_path, monkeypatch
    ):
        # The clear takes the ACTION ceiling, far above projection's own
        # sub-second critical section, so losing the lock means a stuck holder
        # rather than routine contention. That is what keeps this answer
        # two-valued instead of needing a third state for a failure that would
        # otherwise happen by design.
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        seen = {}

        @contextmanager
        def _busy(_work_dir, *, timeout=None):
            seen["timeout"] = timeout
            raise OSError("lock busy")
            yield  # pragma: no cover - unreachable, keeps the generator shape

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "locked_workspace_cli_settings", _busy)
            assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        assert seen["timeout"] == acp_provider.CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "max"}

    def test_gpt_write_uses_reasoning_key_and_roundtrips(self, tmp_path):
        # kiro-cli persists GPT effort under `reasoning`, not `output_config`;
        # the wrong key is silently ignored, so the on-disk shape must match.
        _write_cli_overlay(tmp_path, "gpt-5.6-luna", "max")
        assert _read_cli_overlay(tmp_path) == {"gpt-5.6-luna": "max"}
        cli = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli.read_text(encoding="utf-8"))
        model_cfg = data["chat.modelDefaults"]["gpt-5.6-luna"]
        assert model_cfg["reasoning"]["effort"] == "max"
        assert "output_config" not in model_cfg

    @pytest.mark.parametrize(
        "model,current_key,stale_key",
        [
            ("gpt-5.6-luna", "reasoning", "output_config"),
            ("claude-opus-4.7", "output_config", "reasoning"),
        ],
    )
    def test_write_preserves_other_family_effort(
        self, tmp_path, model: str, current_key: str, stale_key: str
    ):
        """The other family key's ``effort`` is left alone, not swept.

        A sweep of that key would only matter to a reader that adopts whatever
        ``effort`` the file holds, and no provider reads the overlay back into
        its own effort map. kiro-cli ignores the wrong key, so the value there
        is inert, and it may be the operator's: a sweep would be a destructive
        write with nothing to protect.
        """
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli = settings_dir / "cli.json"
        cli.write_text(
            json.dumps(
                {"chat.modelDefaults": {model: {stale_key: {"effort": "low", "preserved": True}}}}
            )
        )

        _write_cli_overlay(tmp_path, model, "max")

        model_cfg = json.loads(cli.read_text(encoding="utf-8"))["chat.modelDefaults"][model]
        assert model_cfg[current_key]["effort"] == "max"
        assert model_cfg[stale_key] == {"effort": "low", "preserved": True}

    def test_mixed_families_coexist(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "high")
        _write_cli_overlay(tmp_path, "gpt-5.6-sol", "medium")
        assert _read_cli_overlay(tmp_path) == {
            "claude-opus-4.7": "high",
            "gpt-5.6-sol": "medium",
        }

    def test_clear_removes_gpt_reasoning_key(self, tmp_path):
        _write_cli_overlay(tmp_path, "gpt-5.6-luna", "max")
        _clear_cli_overlay_effort(tmp_path, "gpt-5.6-luna")
        assert _read_cli_overlay(tmp_path) == {}
        # The whole model entry is dropped once its only sub-key is empty.
        cli = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli.read_text(encoding="utf-8"))
        assert "gpt-5.6-luna" not in data.get("chat.modelDefaults", {})

    def test_projection_keeps_different_operator_level_and_automatic_clear(self, tmp_path):
        model = "claude-opus-4.7"
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {model: {"output_config": {"effort": "low"}}},
                    "operator": {"kept": True},
                }
            ),
            encoding="utf-8",
        )
        before = cli_json.read_bytes()

        _write_cli_overlay(tmp_path, model, "max")
        assert cli_json.read_bytes() == before

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model=model,
        )
        assert provider._apply_effort_overlay() is True
        assert cli_json.read_bytes() == before
        assert _read_cli_overlay(tmp_path) == {model: "low"}

    def test_projection_drops_only_stale_ownership_when_operator_value_differs(self, tmp_path):
        model = "claude-opus-4.7"
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {model: {"output_config": {"effort": "low"}}},
                    _KIROCREW_EFFORT_OWNED_KEY: {model: "max"},
                    "operator": {"kept": True},
                }
            ),
            encoding="utf-8",
        )

        _write_cli_overlay(tmp_path, model, "high")

        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] == "low"
        assert data["operator"] == {"kept": True}
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    @pytest.mark.parametrize(
        "document",
        [
            {"chat.modelDefaults": None},
            {"chat.modelDefaults": "operator-note"},
            {"chat.modelDefaults": {"claude-opus-4.7": "operator-note"}},
            {"chat.modelDefaults": {"claude-opus-4.7": {"output_config": "operator-note"}}},
            {"chat.modelDefaults": {"claude-opus-4.7": {"output_config": {"effort": 7}}}},
        ],
        ids=[
            "model-defaults-null",
            "model-defaults",
            "model-entry",
            "active-key",
            "active-effort",
        ],
    )
    def test_projection_keeps_present_non_object_operator_shapes(self, tmp_path, document):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(json.dumps(document), encoding="utf-8")
        before = cli_json.read_bytes()

        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")

        assert cli_json.read_bytes() == before

    @pytest.mark.parametrize(
        ("document", "path"),
        [
            ({"chat.modelDefaults": None, "other.key": 1}, "chat.modelDefaults"),
            ({"chat.modelDefaults": "operator-note", "other.key": 1}, "chat.modelDefaults"),
            (
                {
                    "chat.modelDefaults": {"claude-opus-4.7": "operator-note"},
                    "other.key": 1,
                },
                "chat.modelDefaults.claude-opus-4.7",
            ),
            (
                {
                    "chat.modelDefaults": {"claude-opus-4.7": {"output_config": "operator-note"}},
                    "other.key": 1,
                },
                "chat.modelDefaults.claude-opus-4.7.output_config",
            ),
        ],
        ids=["model-defaults-null", "model-defaults-string", "model-entry", "active-key"],
    )
    def test_explicit_default_refuses_a_non_object_parent_and_leaves_the_file(
        self, tmp_path, document, path
    ):
        model = "claude-opus-4.7"
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(json.dumps(document), encoding="utf-8")
        before = cli_json.read_bytes()

        with pytest.raises(acp_provider._OverlayParentNotAnObject, match=path):
            _write_cli_overlay(tmp_path, model, "max", replace_unowned=True)

        assert cli_json.read_bytes() == before

    def test_explicit_default_replaces_a_non_object_effort_value_under_an_object_key(
        self, tmp_path
    ):
        model = "claude-opus-4.7"
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {model: {"output_config": {"effort": 7}}},
                    "other.key": 1,
                }
            ),
            encoding="utf-8",
        )

        _write_cli_overlay(tmp_path, model, "max", replace_unowned=True)

        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"][model]["output_config"]["effort"] == "max"
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {model: "max"}
        assert data["other.key"] == 1


class TestFactoryEffortThreading:
    """The provider factory must thread the slot's reasoning_effort_override
    into effort_per_model for BOTH ACP backends — otherwise a cold start
    (or the handler's reset-then-respawn) never applies the persisted effort."""

    def _capture_provider_kwargs(
        self, provider_name: str, *, config_effort: str = "", **factory_call
    ):
        # Both factory branches lazily `from kiro_crew.providers.acp import
        # AcpProvider` (circular-import workaround). That import runs inside
        # create_provider_factory(), so patch the source module symbol BEFORE
        # building the factory, then capture the construction kwargs.
        cfg = KiroCrewConfig()
        cfg.agent.provider = provider_name
        cfg.agent.reasoning_effort = config_effort
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    @pytest.mark.parametrize(
        "provider_name,expected_key",
        [
            # kiro (acp) threads the raw model.
            ("acp", "claude-opus-4.7"),
        ],
    )
    def test_valid_effort_on_opus_threads_per_model(self, provider_name, expected_key):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="xhigh",
        )
        assert kwargs.get("effort_per_model") == {expected_key: "xhigh"}

    def test_valid_effort_on_gpt_threads_per_model_kiro(self):
        # GPT models: the raw model id is threaded and effort is honored on
        # the kiro backend.
        kwargs = self._capture_provider_kwargs(
            "acp",
            session_key="dashboard:1",
            model_override="gpt-5.6-luna",
            reasoning_effort_override="max",
        )
        assert kwargs.get("effort_per_model") == {"gpt-5.6-luna": "max"}

    @pytest.mark.parametrize("provider_name", ["acp"])
    def test_effort_on_incapable_model_not_threaded(self, provider_name):
        # 'auto' supports no effort on the kiro backend (kiro errors on auto).
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="auto",
            reasoning_effort_override="high",
        )
        assert kwargs.get("effort_per_model") == {}

    @pytest.mark.parametrize("provider_name", ["acp"])
    def test_invalid_effort_not_threaded(self, provider_name):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="ultra",
        )
        assert kwargs.get("effort_per_model") == {}


class TestFactoryDropWarning:
    """The factory's effort gate is the single authority that drops a requested
    effort, so IT names the drop: one warning at the gate covers every
    surface that funnels through it (spawn, dashboard slot, cron) and cannot
    drift from the decision it reports on. Silence stays the contract when the
    effort is delivered, invalid, or absent."""

    _LOGGER = "kiro_crew.config.loader"

    def _drop_warnings(self, caplog, tmp_path, **factory_call) -> list[str]:
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            with caplog.at_level(logging.WARNING, logger=self._LOGGER):
                # cwd is tmp_path-scoped so the factory never falls through to
                # _session_work_dir() -> workspace_root(), which would CREATE
                # the operator's real workspace dir as a test side effect.
                factory(cwd=str(tmp_path), **factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == self._LOGGER
            and r.levelno == logging.WARNING
            and "will not be applied" in r.getMessage()
        ]

    def test_non_capable_model_warns_once_naming_model_and_level(self, caplog, tmp_path):
        msgs = self._drop_warnings(
            caplog,
            tmp_path,
            session_key="dashboard:1",
            model_override="deepseek-3.2",
            reasoning_effort_override="high",
        )
        assert len(msgs) == 1
        assert "'deepseek-3.2'" in msgs[0]
        assert "'high'" in msgs[0]
        # Attribution: the session the drop happened for is in the line.
        assert "dashboard:1" in msgs[0]

    def test_unresolved_model_carries_the_level_without_a_factory_warning(self, caplog, tmp_path):
        # 'auto' collapses to "" through to_acp_id: nothing is pinned, so no
        # model id exists yet to key the level under. The factory carries it
        # unkeyed and the provider judges it against the model the backend
        # reports serving, so the factory itself has no drop to report.
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            with caplog.at_level(logging.WARNING, logger=self._LOGGER):
                factory(
                    cwd=str(tmp_path),
                    session_key="dashboard:1",
                    model_override="auto",
                    reasoning_effort_override="max",
                )
        kwargs = mock_provider.call_args.kwargs
        assert kwargs["effort_per_model"] == {}
        assert kwargs["unbound_effort"] == "max"
        assert not [
            r
            for r in caplog.records
            if r.name == self._LOGGER and "will not be applied" in r.getMessage()
        ]

    def test_explicit_override_warns_every_time(self, caplog, tmp_path):
        # A caller's own request being dropped is the event this gate exists
        # to surface — an explicit override never dedupes, so a config-default
        # drop cannot burn the key and silence a later per-slot request
        # (Design review on this PR).
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            with caplog.at_level(logging.WARNING, logger=self._LOGGER):
                factory(
                    session_key="dashboard:1",
                    model_override="deepseek-3.2",
                    reasoning_effort_override="high",
                    cwd=str(tmp_path),
                )
                factory(
                    session_key="dashboard:2",
                    model_override="deepseek-3.2",
                    reasoning_effort_override="high",
                    cwd=str(tmp_path),
                )
        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == self._LOGGER
            and r.levelno == logging.WARNING
            and "will not be applied" in r.getMessage()
        ]
        assert len(msgs) == 2
        assert "dashboard:1" in msgs[0]
        assert "dashboard:2" in msgs[1]

    def test_config_default_drop_warns_once_per_factory(self, caplog, tmp_path):
        # A static config fact (agent.reasoning_effort with a non-capable
        # model, no per-call override) must not repeat on every provider
        # construction — the factory dedupes it per (model, level).
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.reasoning_effort = "high"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            with caplog.at_level(logging.WARNING, logger=self._LOGGER):
                factory(
                    session_key="dashboard:1",
                    model_override="deepseek-3.2",
                    cwd=str(tmp_path),
                )
                factory(
                    session_key="dashboard:2",
                    model_override="deepseek-3.2",
                    cwd=str(tmp_path),
                )
        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == self._LOGGER
            and r.levelno == logging.WARNING
            and "will not be applied" in r.getMessage()
        ]
        assert len(msgs) == 1

    def test_config_default_dedupe_does_not_silence_explicit_override(self, caplog, tmp_path):
        # The exact interleave the Design review flagged: a config-default
        # drop fires first and burns its dedupe key; a later EXPLICIT request
        # for the same (model, level) must still warn.
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.reasoning_effort = "high"
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            with caplog.at_level(logging.WARNING, logger=self._LOGGER):
                factory(
                    session_key="dashboard:1",
                    model_override="deepseek-3.2",
                    cwd=str(tmp_path),
                )
                factory(
                    session_key="cron:job-1",
                    model_override="deepseek-3.2",
                    reasoning_effort_override="high",
                    cwd=str(tmp_path),
                )
        msgs = [
            r.getMessage()
            for r in caplog.records
            if r.name == self._LOGGER
            and r.levelno == logging.WARNING
            and "will not be applied" in r.getMessage()
        ]
        assert len(msgs) == 2
        assert "cron:job-1" in msgs[1]

    def test_capable_model_stays_silent(self, caplog, tmp_path):
        msgs = self._drop_warnings(
            caplog,
            tmp_path,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="xhigh",
        )
        assert msgs == []

    def test_invalid_effort_stays_silent(self, caplog, tmp_path):
        # An invalid level is not a "valid requested effort dropped" — it was
        # never eligible for the overlay, so the gate says nothing.
        msgs = self._drop_warnings(
            caplog,
            tmp_path,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="ultra",
        )
        assert msgs == []

    def test_no_effort_stays_silent(self, caplog, tmp_path):
        msgs = self._drop_warnings(
            caplog,
            tmp_path,
            session_key="dashboard:1",
            model_override="deepseek-3.2",
        )
        assert msgs == []


class TestPoolEffortPostClaim:
    """A requested reasoning effort on a warm-pool claim is applied post-claim
    via provider.change_effort, recovering pool-hit startup latency without
    bypassing the pool."""

    @pytest.mark.asyncio
    async def test_pool_claim_with_effort_override_applies_effort_post_claim(self):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"

        pooled = MagicMock(spec=AcpProvider)
        pooled.client = MagicMock()
        pooled.client._model = "claude-sonnet-4.6"
        pooled.client.rekey = MagicMock()
        pooled.change_effort = AsyncMock(return_value=True)
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.cwd = ""

        factory = MagicMock(return_value=pooled)
        mgr = SessionManager(cfg, factory)
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        provider, is_new, resumed = await mgr.get_or_create(
            "slot-1",
            agent=None,
            reasoning_effort_override="high",
        )

        assert provider is pooled
        mgr._drain_and_claim.assert_awaited_once()
        pooled.change_effort.assert_awaited_once_with("high")
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_claim_with_unsupported_model_logs_warning(self, caplog):
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"

        pooled = MagicMock(spec=AcpProvider)
        pooled.client = MagicMock()
        pooled.client._model = "deepseek-3.2"  # not effort-capable
        pooled.client.rekey = MagicMock()
        pooled.change_effort = AsyncMock(return_value=False)
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.cwd = ""

        factory = MagicMock(return_value=pooled)
        mgr = SessionManager(cfg, factory)
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        with caplog.at_level(logging.WARNING):
            provider, is_new, resumed = await mgr.get_or_create(
                "slot-2",
                agent=None,
                reasoning_effort_override="high",
            )

        assert provider is pooled
        pooled.change_effort.assert_awaited_once_with("high")
        assert any(
            "reasoning effort 'high' will not be applied (session slot-2)" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_pool_claim_with_change_effort_exception_logs_warning_and_spares_session(
        self, caplog
    ):
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 2
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"

        pooled = MagicMock(spec=AcpProvider)
        pooled.client = MagicMock()
        pooled.client._model = "claude-sonnet-4.6"
        pooled.client.rekey = MagicMock()
        pooled.change_effort = AsyncMock(side_effect=RuntimeError("KAS effort unsupported"))
        pooled.is_process_alive = MagicMock(return_value=True)
        pooled.cwd = ""

        factory = MagicMock(return_value=pooled)
        mgr = SessionManager(cfg, factory)
        mgr._drain_and_claim = AsyncMock(return_value=pooled)

        with caplog.at_level(logging.WARNING):
            provider, is_new, resumed = await mgr.get_or_create(
                "slot-3",
                agent=None,
                reasoning_effort_override="high",
            )

        assert provider is pooled
        pooled.change_effort.assert_awaited_once_with("high")
        assert any(
            "Pool post-claim: failed to apply reasoning effort 'high' (session slot-3)" in r.message
            for r in caplog.records
        )


class TestFactoryDefaultEffortFallback:
    """``agent.reasoning_effort`` is the global default for sessions that carry
    no per-slot override. A slot override always wins; the default only fills
    the gap, so a brand-new session starts at the user's configured effort
    instead of the provider/model default."""

    def _capture(self, *, config_effort: str, **factory_call):
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.reasoning_effort = config_effort
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    def test_config_default_applies_when_slot_has_no_override(self):
        kwargs = self._capture(
            config_effort="high",
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "high"}

    def test_slot_override_beats_config_default(self):
        kwargs = self._capture(
            config_effort="low",
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="max",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "max"}

    def test_empty_config_default_threads_nothing(self):
        kwargs = self._capture(
            config_effort="",
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {}

    def test_config_default_not_applied_to_incapable_model(self):
        """A default must never be forced onto a model that rejects effort."""
        kwargs = self._capture(
            config_effort="high",
            session_key="dashboard:1",
            model_override="auto",
        )
        assert kwargs.get("effort_per_model") == {}

    def test_invalid_config_default_ignored(self):
        kwargs = self._capture(
            config_effort="ultra",
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {}


class TestCodeReviewSageOverlayKeepsAcpOwnership(unittest.TestCase):
    """Code Review Sage's overlay writer against the `AcpProvider` ownership record."""

    def test_write_effort_overlay_keeps_acp_owned_entry_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            first_model = "claude-opus-4.7"
            second_model = "claude-sonnet-4.6"
            acp_provider._write_cli_overlay(work_dir, first_model, "max")

            rp._write_effort_overlay(tmp, second_model, "high")

            cli_json = work_dir / ".kiro" / "settings" / "cli.json"
            written = json.loads(cli_json.read_text(encoding="utf-8"))
            self.assertEqual(written["kirocrew.effortOwnedStamp"], int(cli_json.stat().st_mtime))
            self.assertTrue(
                acp_provider._clear_cli_overlay_effort(work_dir, first_model, owned_only=True)
            )
            data = json.loads(
                (work_dir / ".kiro" / "settings" / "cli.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(first_model, data.get("chat.modelDefaults", {}))
            self.assertEqual(
                data["chat.modelDefaults"][second_model]["output_config"]["effort"],
                "high",
            )

    def test_operator_save_after_publish_voids_acp_owned_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            first_model = "claude-opus-4.7"
            second_model = "claude-sonnet-4.6"
            acp_provider._write_cli_overlay(work_dir, first_model, "max")
            cli_json = work_dir / ".kiro" / "settings" / "cli.json"
            real_atomic_write_text = rp.store.atomic_write_text
            operator_saves = []

            def _publish_then_operator_save(target, text, **kwargs):
                real_atomic_write_text(target, text, **kwargs)
                Path(target).write_text(text, encoding="utf-8")
                operator_saves.append(Path(target))

            with unittest.mock.patch.object(
                rp.store, "atomic_write_text", _publish_then_operator_save
            ):
                rp._write_effort_overlay(tmp, second_model, "high")

            written = json.loads(cli_json.read_text(encoding="utf-8"))
            self.assertEqual([path.resolve() for path in operator_saves], [cli_json.resolve()])
            self.assertNotEqual(written["kirocrew.effortOwnedStamp"], int(cli_json.stat().st_mtime))
            self.assertTrue(
                acp_provider._clear_cli_overlay_effort(work_dir, first_model, owned_only=True)
            )
            after = json.loads(cli_json.read_text(encoding="utf-8"))
            self.assertEqual(
                after["chat.modelDefaults"][first_model]["output_config"]["effort"],
                "max",
            )
            self.assertEqual(
                after["chat.modelDefaults"][second_model]["output_config"]["effort"],
                "high",
            )
            self.assertNotIn("kirocrew.effortOwned", after)
