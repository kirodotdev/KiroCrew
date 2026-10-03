"""Tests for the shared reasoning-effort vocabulary (effort.py) and the
ACP provider cli.json overlay helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# O_NOFOLLOW is part of pinned_fs.supports_pinned_walk's required probe.
from conftest import make_dir_link, requires_o_nofollow, requires_symlinks
from kiro_crew import platform_compat
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
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_NAME, workspace_cli_settings_lock

_UNPARSEABLE_OVERLAY_CONTENTS = pytest.mark.parametrize(
    "contents",
    [
        b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}, "n": '
        + b"1" * 5000
        + b"}",
        b"[" * 200_000 + b"]" * 200_000,
    ],
    ids=["oversized-int-literal", "deeply-nested"],
)


def _read_cli_overlay(work_dir: Path) -> dict[str, str]:
    """Return effort entries from a workspace cli.json for test assertions."""
    cli_json = work_dir / ".kiro" / "settings" / "cli.json"
    if not cli_json.exists():
        return {}
    try:
        data = json.loads(cli_json.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    model_defaults = data.get("chat.modelDefaults")
    if not isinstance(model_defaults, dict):
        return {}
    result: dict[str, str] = {}
    for model, config in model_defaults.items():
        if not isinstance(config, dict):
            continue
        for key in ("output_config", "reasoning"):
            effort_config = config.get(key)
            if not isinstance(effort_config, dict):
                continue
            effort = effort_config.get("effort")
            if isinstance(effort, str) and effort:
                result[model] = effort
                break
    return result


def _tree(root: Path) -> list[str]:
    """Every path under *root*, relative and sorted, so a nested addition shows."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@contextmanager
def _record_work_dir_io(monkeypatch, work_dir: Path):
    """Record watched work-directory I/O with the calling thread identity."""
    absolute_path = os.path.abspath
    normalized_roots = {
        os.path.normcase(absolute_path(work_dir)),
        os.path.normcase(os.path.realpath(work_dir)),
    }
    calls: list[tuple[str, int]] = []

    def _under_root(arg: object) -> bool:
        try:
            text = os.fspath(arg)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        candidate = os.path.normcase(absolute_path(text))
        return any(
            candidate == root or candidate.startswith(root + os.path.sep)
            for root in normalized_roots
        )

    def _probe(name: str, original):
        def probe(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None or _under_root(path):
                calls.append((name, threading.get_ident()))
            return original(path, *args, **kwargs)

        return probe

    monkeypatch.setattr(os.path, "realpath", _probe("realpath", os.path.realpath))
    monkeypatch.setattr(os, "stat", _probe("stat", os.stat))
    monkeypatch.setattr(os, "lstat", _probe("lstat", os.lstat))
    monkeypatch.setattr(os, "open", _probe("open", os.open))
    monkeypatch.setattr(os, "listdir", _probe("listdir", os.listdir))
    monkeypatch.setattr(os, "mkdir", _probe("mkdir", os.mkdir))
    yield calls


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

    @pytest.mark.parametrize(
        "link_kind", [pytest.param("symlink", marks=requires_symlinks), "hardlink"]
    )
    def test_overlay_link_is_never_read_or_replaced(self, tmp_path, link_kind):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        target = tmp_path / "outside.json"
        secret = b'{"token": "SECRET"}'
        target.write_bytes(secret)
        cli_json = settings_dir / "cli.json"
        try:
            if link_kind == "symlink":
                cli_json.symlink_to(target)
            else:
                os.link(target, cli_json)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"{link_kind} is unavailable: {exc}")

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is False
        assert target.read_bytes() == secret
        assert not any(
            path != cli_json
            and path.is_file()
            and not path.is_symlink()
            and b"SECRET" in path.read_bytes()
            for path in settings_dir.iterdir()
        )

    @requires_o_nofollow
    def test_overlay_renamed_after_pinned_open_is_never_replaced(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        cli_json.write_bytes(b'{"chat.modelDefaults": {}}')
        replacement = settings_dir / "replacement.json"
        replacement_bytes = [b'{"replacement": "write"}', b'{"replacement": "clear"}']
        real_open = os.open

        def _replace_after_pinned_open(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "cli.json" and dir_fd is not None:
                replacement.write_bytes(replacement_bytes.pop(0))
                os.replace(replacement, cli_json)
            return fd

        # Both capability probes read ``os.open in os.supports_dir_fd``, which the wrapped
        # ``os.open`` fails, so the pinned branch is pinned on explicitly.
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(
            acp_provider.atomic_write_module, "pinned_parent_replace_supported", lambda: True
        )
        monkeypatch.setattr(acp_provider.os, "open", _replace_after_pinned_open)

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert replacement_bytes == []
        assert cli_json.read_bytes() == b'{"replacement": "clear"}'

    @requires_symlinks
    def test_dangling_cli_json_link_is_refused(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        try:
            cli_json.symlink_to(tmp_path / "missing.json")
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert cli_json.is_symlink()

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    @pytest.mark.parametrize("with_existing_file", [False, True])
    def test_redirected_settings_directory_is_never_written(
        self, tmp_path, redirect, with_existing_file
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        try:
            if redirect == ".kiro":
                external_settings = outside / ".kiro" / "settings"
                external_settings.mkdir(parents=True)
                platform_compat.symlink_or_junction(outside / ".kiro", work_dir / ".kiro")
            else:
                external_settings = outside / "settings"
                external_settings.mkdir(parents=True)
                (work_dir / ".kiro").mkdir()
                platform_compat.symlink_or_junction(
                    external_settings, work_dir / ".kiro" / "settings"
                )
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        cli_json = external_settings / "cli.json"
        before = b'{"token": "SECRET"}'
        if with_existing_file:
            cli_json.write_bytes(before)

        with pytest.raises(OSError, match="outside|directory"):
            _write_cli_overlay(work_dir, "claude-opus-4.7", "max")

        if with_existing_file:
            assert cli_json.read_bytes() == before
        else:
            assert not cli_json.exists()

    def test_settings_directory_linked_inside_work_dir_is_refused(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        redirected_settings = tmp_path / "redirected-settings"
        redirected_settings.mkdir()
        (tmp_path / ".kiro").mkdir()
        try:
            platform_compat.symlink_or_junction(redirected_settings, settings_dir)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        cli_json = redirected_settings / "cli.json"
        before = b'{"token": "SECRET"}'
        cli_json.write_bytes(before)

        with pytest.raises(OSError, match="settings directory"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="settings directory"):
            from kiro_crew.providers.acp import _write_tool_search_overlay

            _write_tool_search_overlay(tmp_path, True)
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        assert cli_json.read_bytes() == before
        assert not (redirected_settings / ".kirocrew-cli-settings.lock").exists()

    def test_settings_swap_after_lock_check_cannot_read_outside_overlay(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings
        from kiro_crew.providers.acp import _write_tool_search_overlay

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_cli_json = outside / "cli.json"
        secret = b'{"token": "SECRET"}'
        outside_cli_json.write_bytes(secret)
        settings_dir.rmdir()
        try:
            platform_compat.symlink_or_junction(outside, settings_dir)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                workspace_cli_settings,
                "_settings_dir_within_work_dir",
                lambda _work_dir: settings_dir,
            )
            with pytest.raises(OSError, match="settings directory"):
                _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
            with pytest.raises(OSError, match="settings directory"):
                _write_tool_search_overlay(work_dir, True)
            assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert outside_cli_json.read_bytes() == secret
        assert not (outside / CLI_SETTINGS_LOCK_NAME).exists()
        assert not any(
            path.is_file() and not path.is_symlink() and b"SECRET" in path.read_bytes()
            for path in work_dir.rglob("*")
        )

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_by_name_directory_swap_after_second_check_never_creates_outside_lock(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import pinned_fs, workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        else:
            (work_dir / ".kiro").mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0
        swap_blocked = False

        def _swap_after_second_check(path):
            nonlocal checks, swap_blocked
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 2:
                try:
                    settings_dir.mkdir(parents=True, exist_ok=True)
                    if redirect == ".kiro":
                        settings_dir.rmdir()
                        kiro_dir = work_dir / ".kiro"
                        try:
                            kiro_dir.rmdir()
                        except PermissionError:
                            swap_blocked = True
                        else:
                            platform_compat.symlink_or_junction(outside, kiro_dir)
                    else:
                        settings_dir.rmdir()
                        platform_compat.symlink_or_junction(outside, settings_dir)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(outside)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_second_check
        )

        failure = None
        yielded = None
        try:
            with workspace_cli_settings_lock(work_dir) as cli_json:
                yielded = cli_json
                if not swap_blocked:
                    pytest.fail("a swapped settings directory must not be locked")
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
            assert yielded == work_dir.resolve() / ".kiro" / "settings" / "cli.json"
            assert _tree(outside) == before
        else:
            assert failure is not None
            assert "settings directory" in str(failure)

    def test_by_name_first_check_swap_never_creates_outside_settings(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs, workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0

        def _swap_after_first_check(path):
            nonlocal checks
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 1:
                try:
                    platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_first_check
        )

        with pytest.raises(OSError, match="settings directory"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped .kiro directory must not be locked")

        assert not (outside / "settings").exists()

    def test_by_name_work_dir_alias_retarget_never_reaches_a_sibling_workspace(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import pinned_fs

        first = tmp_path / "first-workspace"
        second = tmp_path / "second-workspace"
        first.mkdir()
        (second / ".kiro" / "settings").mkdir(parents=True)
        sibling_cli_json = second / ".kiro" / "settings" / "cli.json"
        operator_document = b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}}'
        sibling_cli_json.write_bytes(operator_document)
        alias = tmp_path / "alias"
        try:
            make_dir_link(alias, first)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        real_pin_directory = platform_compat.pin_directory
        pins = 0

        def _retarget_alias_on_first_pin(path):
            nonlocal pins
            pinned = real_pin_directory(path)
            pins += 1
            if pins == 1:
                try:
                    platform_compat.unlink_link_or_junction(alias)
                    make_dir_link(alias, second)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return pinned

        before = _tree(second)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _retarget_alias_on_first_pin)

        yielded = None
        try:
            with workspace_cli_settings_lock(alias) as cli_json:
                yielded = cli_json
                cli_json.write_bytes(b"{}")
        except OSError as exc:
            assert "settings directory" in str(exc)

        assert pins >= 1
        if yielded is not None:
            assert yielded == first.resolve() / ".kiro" / "settings" / "cli.json"
        assert sibling_cli_json.read_bytes() == operator_document
        assert _tree(second) == before

    def test_work_dir_alias_retarget_after_the_second_resolution_stays_in_the_first_workspace(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings

        first = tmp_path / "first-workspace"
        second = tmp_path / "second-workspace"
        first.mkdir()
        second_cli_json = second / ".kiro" / "settings" / "cli.json"
        second_cli_json.parent.mkdir(parents=True)
        operator_document = b'{"chat.modelDefaults": {"m": {"output_config": {"effort": "max"}}}}'
        second_cli_json.write_bytes(operator_document)
        alias = tmp_path / "alias"
        try:
            make_dir_link(alias, first)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        resolutions = 0

        def _retarget_after_second_resolution(path):
            nonlocal resolutions
            settings_dir = real_settings_dir(path)
            resolutions += 1
            if resolutions == 2:
                try:
                    platform_compat.unlink_link_or_junction(alias)
                    make_dir_link(alias, second)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(second)
        monkeypatch.setattr(
            workspace_cli_settings,
            "_settings_dir_within_work_dir",
            _retarget_after_second_resolution,
        )

        with workspace_cli_settings_lock(alias) as cli_json:
            assert cli_json == first.resolve() / ".kiro" / "settings" / "cli.json"
            cli_json.write_bytes(b"{}")

        assert resolutions == 2
        assert second_cli_json.read_bytes() == operator_document
        assert _tree(second) == before
        assert (first / ".kiro" / "settings" / "cli.json").read_bytes() == b"{}"

    def test_by_name_fallback_creates_and_pins_fresh_settings(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

        with workspace_cli_settings_lock(tmp_path) as cli_json:
            assert cli_json == tmp_path / ".kiro" / "settings" / "cli.json"
            assert cli_json.parent.is_dir()
            assert not (tmp_path / ".kiro").is_symlink()
            assert not cli_json.parent.is_symlink()

    def test_by_name_lock_pins_directories_until_after_overlay_publish(self, tmp_path, monkeypatch):
        from kiro_crew import pinned_fs, platform_compat

        pins = []
        checked = []
        real_pin_directory = platform_compat.pin_directory
        real_open_lock_file = platform_compat.open_lock_file
        real_atomic_write = acp_provider.atomic_write

        def _record_pin_directory(path):
            descriptor = real_pin_directory(path)
            info = os.fstat(descriptor)
            pins.append((Path(path).name, descriptor, (info.st_dev, info.st_ino)))
            return descriptor

        def _assert_lock_pins_held(when):
            assert [name for name, _, _ in pins[:2]] == [".kiro", "settings"]
            for _, descriptor, identity in pins[:2]:
                info = os.fstat(descriptor)
                assert (info.st_dev, info.st_ino) == identity
            checked.append(when)

        @contextmanager
        def _opening_lock(path):
            if Path(path).name == CLI_SETTINGS_LOCK_NAME:
                _assert_lock_pins_held("lock")
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        def _publishing(path, content, **kwargs):
            if Path(path).name == "cli.json":
                _assert_lock_pins_held("publish")
            return real_atomic_write(path, content, **kwargs)

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _record_pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _opening_lock)
        monkeypatch.setattr(acp_provider, "atomic_write", _publishing)

        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")

        assert checked == ["lock", "publish"]
        for _, descriptor, identity in pins[:2]:
            try:
                info = os.fstat(descriptor)
            except OSError:
                continue
            # A released number can be reused by a later open; the pinned folder cannot.
            assert (info.st_dev, info.st_ino) != identity

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_by_name_pinned_directory_identity_rejects_a_swap_before_lock_open(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        real_open_lock_file = platform_compat.open_lock_file
        swap_blocked = False

        @contextmanager
        def _swap_before_lock_open(path):
            nonlocal swap_blocked
            if redirect == ".kiro":
                try:
                    settings_dir.rmdir()
                    kiro_dir = work_dir / ".kiro"
                    kiro_dir.rmdir()
                except PermissionError:
                    swap_blocked = True
                else:
                    platform_compat.symlink_or_junction(outside, kiro_dir)
            else:
                try:
                    settings_dir.rmdir()
                except PermissionError:
                    swap_blocked = True
                else:
                    platform_compat.symlink_or_junction(outside, settings_dir)
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        before = _tree(outside)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "open_lock_file", _swap_before_lock_open)

        failure = None
        try:
            with workspace_cli_settings_lock(work_dir) as cli_json:
                if not swap_blocked:
                    pytest.fail("a swapped settings directory must not be locked")
                assert cli_json == work_dir / ".kiro" / "settings" / "cli.json"
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
            assert _tree(outside) == before
        else:
            assert failure is not None
            assert "settings directory changed while it was opened" in str(failure)

    @pytest.mark.parametrize("when", ["at_its_pin", "after_settings_pin"])
    def test_by_name_reparse_point_on_held_kiro_is_refused(self, tmp_path, monkeypatch, when):
        import stat
        from types import SimpleNamespace

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        real_pin_directory = platform_compat.pin_directory
        real_lstat_by_name = pinned_fs.lstat_by_name
        reparsed = []
        lock_opens = []

        def _pin_directory(path):
            descriptor = real_pin_directory(path)
            pinned = Path(path).name
            if (when, pinned) in {("at_its_pin", ".kiro"), ("after_settings_pin", "settings")}:
                reparsed.append(path)
            return descriptor

        @contextmanager
        def _open_lock_file(path):
            lock_opens.append(path)
            yield None

        # The held .kiro reports what Windows reports for an empty folder that took a reparse
        # point in place: the same file id, the reparse attribute.
        def _lstat_by_name(target):
            info = real_lstat_by_name(target)
            if reparsed and info is not None and Path(target).name == ".kiro":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return info

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _open_lock_file)
        monkeypatch.setattr(pinned_fs, "lstat_by_name", _lstat_by_name)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a .kiro folder that took a reparse point must not be locked")

        assert lock_opens == []
        # Refused at its own check, nothing is created through a .kiro that is a reparse point.
        assert (work_dir / ".kiro" / "settings").exists() is (when == "after_settings_pin")

    @pytest.mark.parametrize("when", ["after_pins", "at_lock_open"])
    def test_by_name_reparse_point_on_held_settings_is_refused(self, tmp_path, monkeypatch, when):
        import stat
        from types import SimpleNamespace

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        real_pin_directory = platform_compat.pin_directory
        real_open_lock_file = platform_compat.open_lock_file
        real_lstat_by_name = pinned_fs.lstat_by_name
        reparsed = []
        lock_opens = []

        def _pin_directory(path):
            descriptor = real_pin_directory(path)
            if when == "after_pins" and Path(path).name == "settings":
                reparsed.append(path)
            return descriptor

        @contextmanager
        def _open_lock_file(path):
            lock_opens.append(path)
            if when == "at_lock_open":
                reparsed.append(path)
            with real_open_lock_file(path) as lock_file:
                yield lock_file

        # Linux cannot set a reparse point, so the held folder reports what Windows reports
        # for an empty folder that took one in place: the same file id, the reparse attribute.
        def _lstat_by_name(target):
            info = real_lstat_by_name(target)
            if reparsed and info is not None and Path(target).name == "settings":
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return info

        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "pin_directory", _pin_directory)
        monkeypatch.setattr(platform_compat, "open_lock_file", _open_lock_file)
        monkeypatch.setattr(pinned_fs, "lstat_by_name", _lstat_by_name)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a settings folder that took a reparse point must not be locked")

        assert len(lock_opens) == (0 if when == "after_pins" else 1)

    @pytest.mark.timeout(30)
    @requires_o_nofollow
    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    def test_directory_swap_after_lock_check_never_creates_outside_lock(
        self, tmp_path, monkeypatch, redirect
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        if redirect == ".kiro":
            (outside / "settings").mkdir()
        else:
            (work_dir / ".kiro").mkdir()
        real_settings_dir = workspace_cli_settings._settings_dir_within_work_dir
        checks = 0

        def _swap_after_check(path):
            nonlocal checks
            settings_dir = real_settings_dir(path)
            checks += 1
            if checks == 2:
                try:
                    if redirect == ".kiro":
                        settings = work_dir / ".kiro" / "settings"
                        if settings.exists():
                            settings.rmdir()
                        kiro_dir = work_dir / ".kiro"
                        if kiro_dir.exists():
                            kiro_dir.rmdir()
                        platform_compat.symlink_or_junction(outside, kiro_dir)
                    else:
                        settings = work_dir / ".kiro" / "settings"
                        if settings.exists():
                            settings.rmdir()
                        platform_compat.symlink_or_junction(outside, settings)
                except (OSError, NotImplementedError) as exc:
                    pytest.fail(f"symlink is unavailable: {exc}")
            return settings_dir

        before = _tree(outside)
        monkeypatch.setattr(
            workspace_cli_settings, "_settings_dir_within_work_dir", _swap_after_check
        )

        with pytest.raises(OSError, match="settings directory"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped settings directory must not be locked")

        assert _tree(outside) == before

    @requires_o_nofollow
    @pytest.mark.parametrize("swap", [".kiro", "settings", "removed"])
    def test_directory_swap_after_pinned_walk_never_creates_outside_lock(
        self, tmp_path, monkeypatch, swap
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        (work_dir / ".kiro" / "settings").mkdir(parents=True)
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        real_pinned_settings_dir_fd = workspace_cli_settings._pinned_settings_dir_fd

        def _swap_after_walk(resolved_work_dir):
            settings_fd = real_pinned_settings_dir_fd(resolved_work_dir)
            try:
                if swap == ".kiro":
                    (work_dir / ".kiro").rename(work_dir / ".kiro-real")
                    platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
                elif swap == "settings":
                    settings = work_dir / ".kiro" / "settings"
                    settings.rename(work_dir / ".kiro" / "settings-real")
                    platform_compat.symlink_or_junction(outside / "settings", settings)
                else:
                    (work_dir / ".kiro" / "settings").rmdir()
            except (OSError, NotImplementedError) as exc:
                os.close(settings_fd)
                pytest.fail(f"symlink is unavailable: {exc}")
            return settings_fd

        before = _tree(outside)
        monkeypatch.setattr(workspace_cli_settings, "_pinned_settings_dir_fd", _swap_after_walk)

        expected = "was removed" if swap == "removed" else "changed while it was opened"
        with pytest.raises(OSError, match=expected):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a swapped settings directory must not be locked")

        assert _tree(outside) == before

    @requires_o_nofollow
    def test_pinned_lock_create_lost_to_a_sibling_opens_the_siblings_lock(
        self, tmp_path, monkeypatch
    ):
        """A taker that loses the lock's create to a sibling holds the sibling's file.

        Darwin answers a nonexclusive ``O_CREAT`` open of a name two callers race to
        create with ``ENOENT``; the settings lock is taken through the exclusive-then-reopen
        helper, so a warm-pool fill and a chat start racing on one work dir both lock.
        """
        import errno

        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        real_open = os.open
        racing_creates = []

        def _darwin_racing_open(path, flags, mode=0o777, *, dir_fd=None):
            if (
                os.fspath(path) == CLI_SETTINGS_LOCK_NAME
                and flags & os.O_CREAT
                and not flags & os.O_EXCL
            ):
                racing_creates.append(flags)
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        # ``supports_pinned_walk`` reads ``os.open in os.supports_dir_fd``, which the patched
        # ``os.open`` fails, so the pinned branch is pinned on explicitly.
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(os, "open", _darwin_racing_open)

        with workspace_cli_settings_lock(work_dir) as cli_json:
            assert cli_json == settings / "cli.json"
            twin_fd = real_open(settings / CLI_SETTINGS_LOCK_NAME, os.O_RDWR)
            try:
                assert not platform_compat.try_acquire_lock(twin_fd, exclusive=True)
            finally:
                os.close(twin_fd)

        assert racing_creates == []

    @requires_o_nofollow
    def test_hardlinked_lock_twin_cannot_carry_a_swapped_settings_directory(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import workspace_cli_settings

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        try:
            os.link(
                settings / CLI_SETTINGS_LOCK_NAME, outside / "settings" / CLI_SETTINGS_LOCK_NAME
            )
        except OSError as exc:
            pytest.fail(f"hardlink is unavailable: {exc}")
        real_pinned_settings_dir_fd = workspace_cli_settings._pinned_settings_dir_fd

        def _swap_after_walk(resolved_work_dir):
            settings_fd = real_pinned_settings_dir_fd(resolved_work_dir)
            try:
                (work_dir / ".kiro").rename(work_dir / ".kiro-real")
                platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
            except (OSError, NotImplementedError) as exc:
                os.close(settings_fd)
                pytest.fail(f"symlink is unavailable: {exc}")
            return settings_fd

        before = _tree(outside)
        monkeypatch.setattr(workspace_cli_settings, "_pinned_settings_dir_fd", _swap_after_walk)

        with pytest.raises(OSError, match="settings directory changed while it was opened"):
            with workspace_cli_settings_lock(work_dir):
                pytest.fail("a lock reached through a swapped settings directory is not held")

        assert _tree(outside) == before

    @pytest.mark.parametrize("redirect", [".kiro", "settings"])
    @pytest.mark.parametrize("floor", ["pinned", "by_name"])
    def test_hardlinked_lock_twin_swapped_in_while_acquiring_is_refused(
        self, tmp_path, monkeypatch, floor, redirect
    ):
        from kiro_crew import pinned_fs, platform_compat

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / CLI_SETTINGS_LOCK_NAME).write_bytes(b"")
        outside = tmp_path / "outside"
        (outside / "settings").mkdir(parents=True)
        os.link(settings / CLI_SETTINGS_LOCK_NAME, outside / "settings" / CLI_SETTINGS_LOCK_NAME)
        moved = work_dir / ".kiro" if redirect == ".kiro" else settings
        target = outside if redirect == ".kiro" else outside / "settings"
        real_file_lock = platform_compat.file_lock
        swap_blocked = False

        @contextmanager
        def _swap_then_lock(fd, **kwargs):
            nonlocal swap_blocked
            try:
                moved.rename(moved.with_name(moved.name + "-real"))
            except PermissionError:
                swap_blocked = True
            else:
                platform_compat.symlink_or_junction(target, moved)
            with real_file_lock(fd, **kwargs):
                yield

        before = _tree(outside)
        if floor == "by_name":
            monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(platform_compat, "file_lock", _swap_then_lock)

        failure = None
        try:
            with workspace_cli_settings_lock(work_dir):
                pass
        except OSError as exc:
            failure = exc

        if swap_blocked:
            assert failure is None
        else:
            assert failure is not None
            assert "settings directory changed while it was acquired" in str(failure)
        assert _tree(outside) == before

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    @pytest.mark.parametrize("swap", ["link", "directory"])
    def test_settings_swap_after_read_never_redirects_publish(
        self, tmp_path, monkeypatch, writer, swap
    ):
        from kiro_crew.providers.acp import _write_tool_search_overlay

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement = tmp_path / "outside" if swap == "link" else settings_dir
        if swap == "link":
            replacement.mkdir()
        replacement_cli_json = replacement / "cli.json"
        sentinel = b'{"token": "SECRET"}'
        if swap == "link":
            replacement_cli_json.write_bytes(sentinel)
            before_names = {path.name for path in replacement.iterdir()}
        else:
            before_names = {"cli.json"}
        held_before = (settings_dir / "cli.json").read_bytes()
        real_read = acp_provider._read_cli_overlay_document

        def _swap_after_read(settings):
            document = real_read(settings)
            settings_dir.rename(held_settings_dir)
            try:
                if swap == "link":
                    platform_compat.symlink_or_junction(replacement, settings_dir)
                else:
                    settings_dir.mkdir()
            except (OSError, NotImplementedError) as exc:
                pytest.fail(f"settings replacement is unavailable: {exc}")
            if swap == "directory":
                replacement_cli_json.write_bytes(sentinel)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "_read_cli_overlay_document", _swap_after_read)
            if writer == "effort":
                with pytest.raises(OSError, match="workspace cli.json overlay|settings directory"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="workspace cli.json overlay|settings directory"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in replacement.iterdir()} == before_names
        assert (held_settings_dir / "cli.json").read_bytes() == held_before

    @requires_o_nofollow
    @pytest.mark.parametrize("writer", ["effort", "tool_search", "clear"])
    def test_settings_directory_replaced_after_the_publish_check_keeps_its_cli_json(
        self, tmp_path, monkeypatch, writer
    ):
        from kiro_crew.providers.acp import _write_tool_search_overlay
        from kiro_crew.workspace_cli_settings import LockedCliSettings

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement_cli_json = settings_dir / "cli.json"
        sentinel = b'{"operator.sentinel": true}'
        real_check = LockedCliSettings.holds_named_settings_dir
        checks = 0

        def _replace_after_first_check(settings):
            nonlocal checks
            holds = real_check(settings)
            checks += 1
            if checks == 1:
                settings_dir.rename(held_settings_dir)
                settings_dir.mkdir()
                replacement_cli_json.write_bytes(sentinel)
            return holds

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                LockedCliSettings, "holds_named_settings_dir", _replace_after_first_check
            )
            if writer == "effort":
                with pytest.raises(OSError, match="settings directory changed while"):
                    _write_cli_overlay(work_dir, "claude-opus-4.7", "low")
            elif writer == "tool_search":
                with pytest.raises(OSError, match="settings directory changed while"):
                    _write_tool_search_overlay(work_dir, True)
            else:
                assert _clear_cli_overlay_effort(work_dir, "claude-opus-4.7") is False

        assert checks == 2
        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in settings_dir.iterdir()} == {"cli.json"}

    @requires_o_nofollow
    def test_settings_directory_replacement_before_read_cannot_receive_publish(
        self, tmp_path, monkeypatch
    ):

        work_dir = tmp_path / "work"
        settings_dir = work_dir / ".kiro" / "settings"
        _write_cli_overlay(work_dir, "claude-opus-4.7", "max")
        held_settings_dir = work_dir / ".kiro" / "settings-held"
        replacement_cli_json = settings_dir / "cli.json"
        sentinel = (
            b'{"operator.sentinel": true, "chat.modelDefaults": {"claude-opus-4.7": '
            b'{"output_config": {"effort": "max"}}}}'
        )
        real_read = acp_provider._read_cli_overlay_document
        documents_read = []

        def _replace_before_read(settings):
            settings_dir.rename(held_settings_dir)
            settings_dir.mkdir()
            replacement_cli_json.write_bytes(sentinel)
            document = real_read(settings)
            documents_read.append(document)
            return document

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(
                acp_provider, "_read_cli_overlay_document", _replace_before_read
            )
            with pytest.raises(OSError, match="settings directory"):
                _write_cli_overlay(work_dir, "claude-opus-4.7", "low")

        assert documents_read and "operator.sentinel" not in documents_read[0]
        assert replacement_cli_json.read_bytes() == sentinel
        assert {path.name for path in settings_dir.iterdir()} == {"cli.json"}

    @pytest.mark.timeout(30)
    @pytest.mark.parametrize("kind", ["fifo", "directory"])
    def test_non_regular_cli_json_is_refused_without_blocking_or_leaking(
        self, tmp_path, monkeypatch, kind
    ):
        import stat

        from kiro_crew.providers.acp import _write_tool_search_overlay

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        simulated_fifo = kind == "fifo" and not hasattr(os, "mkfifo")
        if kind == "fifo" and not simulated_fifo:
            os.mkfifo(cli_json)
        elif kind == "fifo":
            cli_json.write_bytes(b"{}")
            cli_json_stat = os.stat(cli_json)
            real_fstat = os.fstat

            def _fstat_as_fifo(fd):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (cli_json_stat.st_dev, cli_json_stat.st_ino):
                    return os.stat_result(
                        (
                            stat.S_IFIFO | (info.st_mode & 0o7777),
                            *info[1 : os.stat_result.n_sequence_fields],
                        ),
                        {name: getattr(info, name) for name in dir(info) if name.startswith("st_")},
                    )
                return info

            monkeypatch.setattr(os, "fstat", _fstat_as_fifo)
        else:
            cli_json.mkdir()
        open_descriptors = Path("/proc/self/fd")

        def _refused_round():
            with pytest.raises(OSError, match="workspace cli.json overlay"):
                _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
            with pytest.raises(OSError, match="workspace cli.json overlay"):
                _write_tool_search_overlay(tmp_path, True)
            assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False

        _refused_round()
        open_before = len(os.listdir(open_descriptors)) if open_descriptors.is_dir() else None

        for _ in range(5):
            _refused_round()

        if open_before is not None:
            assert len(os.listdir(open_descriptors)) == open_before
        if kind == "fifo" and not simulated_fifo:
            assert cli_json.is_fifo()
        elif kind == "fifo":
            assert cli_json.is_file()
        else:
            assert cli_json.is_dir()

    @requires_symlinks
    def test_leaf_link_inside_settings_is_never_read_or_replaced(self, tmp_path):
        from kiro_crew.providers.acp import _write_tool_search_overlay

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        cli_json = settings_dir / "cli.json"
        other_json = settings_dir / "other.json"
        secret = b'{"token": "SECRET"}'
        other_json.write_bytes(secret)
        try:
            cli_json.symlink_to(other_json)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")

        with pytest.raises(OSError, match="is a link"):
            _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        with pytest.raises(OSError, match="is a link"):
            _write_tool_search_overlay(tmp_path, True)
        assert _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7") is False
        assert cli_json.is_symlink()
        assert other_json.read_bytes() == secret

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


class TestTheSharedOverlayIsNotReadBack:
    """The overlay is one file per WORK DIR, shared by every session there.

    A level in it may be another session's pick, so a provider never adopts it.
    It projects its own resolved level before each spawn instead: the level is
    written, or, when none resolves, the model's entry is removed so the model
    runs at its own default. These build a real kiro provider on a temp work dir
    and invoke the same projection start() runs before a spawn.
    """

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    def _write_operator_effort(work_dir, model, level):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8")) if cli_json.exists() else {}
        model_defaults = data.setdefault("chat.modelDefaults", {})
        model_cfg = model_defaults.setdefault(model, {})
        model_cfg[effort_settings_key(model)] = {"effort": level}
        cli_json.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _owned_effort(work_dir):
        path = work_dir / ".kiro" / "settings" / "cli.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        record = data.get(_KIROCREW_EFFORT_OWNED_KEY)
        return record if isinstance(record, dict) else {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [_MODEL, "auto"])
    async def test_operator_authored_effort_survives_default_construction_and_start(
        self, tmp_path, monkeypatch, model
    ):
        operator_model = self._MODEL if model == "auto" else model
        self._write_operator_effort(tmp_path, operator_model, "max")

        provider = self._provider(tmp_path, model=model)
        assert _read_cli_overlay(tmp_path) == {operator_model: "max"}

        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        await provider.start()

        assert _read_cli_overlay(tmp_path) == {operator_model: "max"}
        assert self._owned_effort(tmp_path) == {}

    @staticmethod
    def _write_unparseable_overlay(work_dir, contents):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        cli_json.write_bytes(contents)
        return cli_json

    @staticmethod
    def _write_undecodable_overlay(work_dir):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        contents = b'{"x": "\xff"}'
        cli_json.write_bytes(contents)
        return cli_json, contents

    @classmethod
    def _write_oversized_overlay(cls, work_dir):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        contents = json.dumps(
            {"chat.modelDefaults": {cls._MODEL: {"output_config": {"effort": "max"}}}}
        ).encode("utf-8")
        ceiling = acp_provider._CLI_JSON_MAX_BYTES
        contents += b" " * (ceiling + 1 - len(contents))
        cli_json.write_bytes(contents)
        return cli_json, contents

    @_UNPARSEABLE_OVERLAY_CONTENTS
    @pytest.mark.parametrize("model", [_MODEL, "auto"])
    def test_a_default_projection_ignores_a_file_no_parser_can_read(
        self, tmp_path, caplog, model, contents
    ):
        cli_json = self._write_unparseable_overlay(tmp_path, contents)
        provider = self._provider(tmp_path, model=model)

        warnings = self._pre_spawn_warnings(caplog, provider)

        assert warnings == []
        assert cli_json.read_bytes() == contents

    @_UNPARSEABLE_OVERLAY_CONTENTS
    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [_MODEL, "auto"])
    async def test_a_start_survives_a_file_no_parser_can_read(
        self, tmp_path, monkeypatch, model, contents
    ):
        cli_json = self._write_unparseable_overlay(tmp_path, contents)
        provider = self._provider(tmp_path, model=model)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert cli_json.read_bytes() == contents
        provider._start_kiro_runtime.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [_MODEL, "auto"])
    async def test_an_undecodable_overlay_does_not_break_a_default_session(
        self, tmp_path, monkeypatch, model
    ):
        # The pre-spawn removal runs from start() for every Default and auto
        # session in the work dir: a file kiro-cli cannot read either must not
        # take start down with it, and its bytes are the operator's, so they stay.
        cli_json, contents = self._write_undecodable_overlay(tmp_path)

        provider = self._provider(tmp_path, model=model)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        await provider.start()

        assert cli_json.read_bytes() == contents

    @pytest.mark.asyncio
    async def test_projecting_a_level_onto_an_undecodable_overlay_fails_closed(
        self, tmp_path, monkeypatch, caplog
    ):
        # The write cannot merge into the file and must not reset it, so the
        # projection reports failure rather than a level the file does not hold;
        # construction and start still succeed, and the live push at spawn is
        # what carries the level.
        cli_json, contents = self._write_undecodable_overlay(tmp_path)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            provider = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
            assert provider._apply_effort_overlay() is False
        assert cli_json.read_bytes() == contents
        assert any("overlay write failed" in r.getMessage() for r in caplog.records)

        provider._client.send_command = AsyncMock(return_value="")
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        await provider.start()

        assert cli_json.read_bytes() == contents
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "contents", [b"{ not json", b'{"x": "\xff"}'], ids=["malformed", "undecodable"]
    )
    async def test_change_effort_refuses_to_push_over_an_unmergeable_overlay(
        self, tmp_path, contents
    ):
        # change_effort pushes live only once the file holds the level. A file
        # the write cannot merge into is left as the operator wrote it, the
        # override is rolled back, and the caller is told.
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_bytes(contents)
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")

        with pytest.raises(RuntimeError, match="could not persist effort"):
            await provider.change_effort("max")

        assert cli_json.read_bytes() == contents
        assert self._MODEL not in provider._effort_per_model
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_reports_an_oversized_overlay_and_leaves_it_unchanged(
        self, tmp_path
    ):
        cli_json, contents = self._write_oversized_overlay(tmp_path)
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")

        with pytest.raises(RuntimeError, match="read ceiling"):
            await provider.change_effort("max")

        assert cli_json.read_bytes() == contents
        assert self._MODEL not in provider._effort_per_model
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_change_effort_names_a_linked_overlay_and_leaves_it_unchanged(self, tmp_path):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        shared_overlay = tmp_path / "shared-cli.json"
        contents = b'{"chat.modelDefaults": {}}'
        shared_overlay.write_bytes(contents)
        cli_json = settings / "cli.json"
        try:
            cli_json.symlink_to(shared_overlay)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")

        with pytest.raises(RuntimeError, match="a link or in a linked folder"):
            await provider.change_effort("max")

        assert cli_json.is_symlink()
        assert shared_overlay.read_bytes() == contents
        assert self._MODEL not in provider._effort_per_model
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_live_push_restores_the_prior_level_past_a_launch_length_hold(
        self, tmp_path, caplog
    ):
        # The rollback that restores the prior level waits the ACTION ceiling,
        # as the write it undoes did. Under the startup ceiling a holder that
        # outlasts a launch's wait leaves the file at the level the live push
        # never applied, and the next spawn adopts it.
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "low"})
        assert provider._apply_effort_overlay() is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}
        hold_secs = acp_provider.CLI_SETTINGS_LOCK_TIMEOUT_SECS + 0.5
        assert hold_secs < acp_provider.CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS / 4
        arm = threading.Event()
        held = threading.Event()
        release = threading.Event()

        def _hold_the_lock_past_a_launch_wait():
            if not arm.wait(timeout=5):
                return
            with workspace_cli_settings_lock(tmp_path):
                held.set()
                release.wait(timeout=acp_provider.CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS)

        async def _refuse_once_the_lock_is_held(*_args, **_kwargs):
            # The first write has landed by now; the holder takes the lock
            # before the rollback's write asks for it.
            arm.set()
            assert await asyncio.to_thread(held.wait, 5)
            releaser.start()
            raise RuntimeError("refused")

        provider._client.send_command = AsyncMock(side_effect=_refuse_once_the_lock_is_held)
        holder = threading.Thread(target=_hold_the_lock_past_a_launch_wait)
        releaser = threading.Timer(hold_secs, release.set)
        try:
            holder.start()
            with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
                with pytest.raises(RuntimeError, match="refused"):
                    await provider.change_effort("max")
        finally:
            arm.set()
            release.set()
            releaser.cancel()
            holder.join(timeout=5)
        assert not holder.is_alive()

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}
        assert provider._effort_per_model[self._MODEL] == "low"
        messages = [r.getMessage() for r in caplog.records]
        assert not any("rollback left the overlay set" in m for m in messages)
        assert any("live push failed" in m for m in messages)

    def test_an_oversized_overlay_write_is_refused_and_leaves_it_unchanged(self, tmp_path):
        cli_json, contents = self._write_oversized_overlay(tmp_path)

        with pytest.raises(ValueError, match="left unchanged"):
            _write_cli_overlay(tmp_path, self._MODEL, "low")

        assert cli_json.read_bytes() == contents

    @pytest.mark.asyncio
    async def test_clear_effort_reports_an_oversized_overlay_and_leaves_it_unchanged(
        self, tmp_path, caplog
    ):
        cli_json, contents = self._write_oversized_overlay(tmp_path)
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert await provider.clear_effort(owned_only=False) is None

        assert cli_json.read_bytes() == contents
        assert provider._effort_per_model[self._MODEL] == "max"
        assert any("read ceiling" in record.getMessage() for record in caplog.records)

    def test_a_clear_refuses_an_oversized_overlay(self, tmp_path, caplog):
        cli_json, contents = self._write_oversized_overlay(tmp_path)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert _clear_cli_overlay_effort(tmp_path, None, warn_unowned=True) is False

        assert cli_json.read_bytes() == contents
        assert any(
            str(acp_provider._CLI_JSON_MAX_BYTES) in record.getMessage()
            for record in caplog.records
        )

    def test_a_default_spawn_warns_when_the_overlay_is_too_large_to_read(self, tmp_path, caplog):
        self._write_oversized_overlay(tmp_path)
        provider = self._provider(tmp_path, model="auto")

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert provider._apply_effort_overlay() is False

        assert any(
            "may run a level another session projected" in record.getMessage()
            for record in caplog.records
        )

    def test_a_warning_sample_retains_a_bounded_number_of_bounded_pairs(self):
        pairs = acp_provider._BoundedCliEffortPairs()
        for index in range(500):
            pairs.add(f"model-{index}-" + "m" * 100_000, "e" * 100_000)

        assert pairs.sample_size == acp_provider._WARN_UNOWNED_MAX_ITEMS
        assert pairs.omitted == 500 - acp_provider._WARN_UNOWNED_MAX_ITEMS
        sample, suffix = pairs.render().rsplit(", ... ", 1)
        assert all(
            len(token) <= 2 * acp_provider._WARN_UNOWNED_MAX_CHARS + 1
            for token in sample.split(", ")
        )
        assert suffix == f"{pairs.omitted} more"

    def test_both_effort_log_populations_share_one_bounded_collector(self, tmp_path, monkeypatch):
        collector_type = acp_provider._BoundedCliEffortPairs
        constructions = []

        class _RecordingPairs(collector_type):
            def __init__(self):
                constructions.append(self)
                super().__init__()

        monkeypatch.setattr(acp_provider, "_BoundedCliEffortPairs", _RecordingPairs)
        acp_provider._warn_unowned_cli_effort(
            tmp_path / ".kiro" / "settings" / "cli.json",
            {self._MODEL: {"output_config": {"effort": "max"}}},
            self._MODEL,
        )
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        assert _clear_cli_overlay_effort(tmp_path, self._MODEL) is True

        assert len(constructions) == 2

    def test_a_level_kiro_crew_left_is_not_adopted(self, tmp_path):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        assert self._owned_effort(tmp_path) == {self._MODEL: "max"}

        provider = self._provider(tmp_path)
        assert provider._apply_effort_overlay() is True

        assert provider._resolve_effort() is None
        assert _read_cli_overlay(tmp_path) == {}
        assert self._owned_effort(tmp_path) == {}

    @pytest.mark.parametrize("model", ["claude-opus-4.7", "auto"], ids=["default-concrete", "auto"])
    def test_construction_leaves_owned_overlay_unchanged_until_pre_spawn_projection(
        self, tmp_path, model
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()

        provider = self._provider(tmp_path, model=model)

        assert cli_json.read_bytes() == before
        assert provider._apply_effort_overlay() is True
        assert _read_cli_overlay(tmp_path) == {}
        assert self._owned_effort(tmp_path) == {}

    def test_malformed_ownership_record_owns_nothing(self, tmp_path):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        data[_KIROCREW_EFFORT_OWNED_KEY] = "not a map"
        cli_json.write_text(json.dumps(data), encoding="utf-8")

        provider = self._provider(tmp_path)
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert self._owned_effort(tmp_path) == {}

    def test_operator_change_after_kiro_crew_write_survives_and_drops_ownership(self, tmp_path):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        self._write_operator_effort(tmp_path, self._MODEL, "low")

        provider = self._provider(tmp_path)
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}
        assert self._owned_effort(tmp_path) == {}

    def test_removed_owned_entry_cannot_authorize_a_later_operator_value(self, tmp_path):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        assert self._owned_effort(tmp_path) == {self._MODEL: "max"}

        self._write_operator_effort(tmp_path, self._MODEL, "low")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        data["chat.modelDefaults"].pop(self._MODEL)
        cli_json.write_text(json.dumps(data), encoding="utf-8")

        provider = self._provider(tmp_path)
        assert provider._apply_effort_overlay() is True
        assert self._owned_effort(tmp_path) == {}

        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(tmp_path)
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert self._owned_effort(tmp_path) == {}

    def test_levels_for_other_models_are_neither_adopted_nor_removed(self, tmp_path):
        # A live model switch reads this map, so an adopted entry would carry
        # another session's pick onto the model switched to.
        _write_cli_overlay(tmp_path, "gpt-5.6-sol", "max")

        provider = self._provider(tmp_path)

        assert provider._effort_per_model == {}
        assert _read_cli_overlay(tmp_path) == {"gpt-5.6-sol": "max"}

    def test_this_sessions_own_level_replaces_the_leftover(self, tmp_path):
        _write_cli_overlay(tmp_path, self._MODEL, "max")

        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "low"})
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}

    def test_every_spawn_projects_again(self, tmp_path):
        # The file can change between construction and a later (re)spawn.
        provider = self._provider(tmp_path)
        _write_cli_overlay(tmp_path, self._MODEL, "xhigh")

        assert provider._apply_effort_overlay() is True
        assert _read_cli_overlay(tmp_path) == {}

    def test_auto_session_clears_every_candidate_models_effort(self, tmp_path):
        _write_cli_overlay(tmp_path, self._MODEL, "xhigh")
        _write_cli_overlay(tmp_path, "gpt-5.6-sol", "max")

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {}

    def test_auto_session_preserves_non_object_model_defaults_while_dropping_stale_ownership(
        self, tmp_path
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": "operator-note",
                    _KIROCREW_EFFORT_OWNED_KEY: {self._MODEL: "max"},
                }
            ),
            encoding="utf-8",
        )

        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )
        assert provider._apply_effort_overlay() is True

        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data["chat.modelDefaults"] == "operator-note"
        assert _KIROCREW_EFFORT_OWNED_KEY not in data

    def test_a_default_session_gives_a_directory_no_overlay(self, tmp_path):
        # Nothing to clear means no lock, so no lock sidecar lands in a project.
        self._provider(tmp_path)

        assert not (tmp_path / ".kiro").exists()

    def test_an_auto_session_gives_a_directory_no_overlay(self, tmp_path):
        acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )

        assert not (tmp_path / ".kiro").exists()

    def test_a_clear_that_loses_the_lock_is_reported(self, tmp_path, monkeypatch, caplog):
        provider = self._provider(tmp_path)
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        seen = {}

        @contextmanager
        def _busy(_work_dir, *, timeout=None):
            seen["timeout"] = timeout
            raise OSError("lock busy")
            yield  # pragma: no cover - unreachable, keeps the generator shape

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "locked_workspace_cli_settings", _busy)
            with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
                assert provider._apply_effort_overlay() is False
        # A launch cannot wait, so the pre-spawn clear takes the startup ceiling.
        assert seen["timeout"] == acp_provider.CLI_SETTINGS_LOCK_TIMEOUT_SECS
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        messages = [r.getMessage() for r in caplog.records]
        assert any("could not be checked or cleared" in m for m in messages)
        assert not any("was busy" in m for m in messages)

    def test_an_auto_clear_that_loses_the_lock_is_reported_by_consequence(
        self, tmp_path, monkeypatch, caplog
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )

        @contextmanager
        def _busy(_work_dir, *, timeout=None):
            raise OSError("lock busy")
            yield  # pragma: no cover - unreachable, keeps the generator shape

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "locked_workspace_cli_settings", _busy)
            with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
                assert provider._apply_effort_overlay() is False
        messages = [r.getMessage() for r in caplog.records]
        assert any("could not be checked or cleared" in m for m in messages)
        assert not any("was busy" in m for m in messages)

    def _pre_spawn_warnings(self, caplog, provider) -> list[str]:
        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert provider._apply_effort_overlay() is True
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == acp_provider.__name__
        ]

    def test_default_spawn_warns_once_about_an_unowned_level_for_its_model(self, tmp_path, caplog):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()

        warnings = self._pre_spawn_warnings(caplog, self._provider(tmp_path))

        assert len(warnings) == 1
        assert self._MODEL in warnings[0]
        assert "max" in warnings[0]
        assert str(cli_json) in warnings[0]
        assert "did not write" in warnings[0]
        # The slot already has this model selected, so the picker remedy stands
        # on its own.
        assert warnings[0].endswith(
            "Remove it by picking a level and then Default in the effort picker, "
            "or by editing the file"
        )
        assert cli_json.read_bytes() == before

    def test_unowned_warning_escapes_and_bounds_file_derived_entries(self, tmp_path, caplog):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        first_model = "first-model"
        model_defaults = {
            first_model: {"output_config": {"effort": "x" * 5000}},
            "bidi\u202eentry": {"output_config": {"effort": "max"}},
            "forged\nWARNING: not a separate record": {"output_config": {"effort": "high"}},
        }
        for index in range(57):
            model_defaults[f"model-{index}"] = {"output_config": {"effort": "max"}}
        (settings / "cli.json").write_text(
            json.dumps({"chat.modelDefaults": model_defaults}), encoding="utf-8"
        )

        warnings = self._pre_spawn_warnings(
            caplog,
            acp_provider.AcpProvider(acp_backend=ACP_BACKEND_KIRO, work_dir=tmp_path, model="auto"),
        )

        assert len(warnings) == 1
        assert first_model in warnings[0]
        assert "\n" not in warnings[0]
        assert "\\nWARNING" in warnings[0]
        assert "\u202e" not in warnings[0]
        assert "\\u202e" in warnings[0]
        assert "... 50 more):" in warnings[0]
        assert len(warnings[0]) < 1800

    _CREDENTIAL = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )

    def test_unowned_warning_carries_no_fragment_of_a_credential_shaped_value_or_key(
        self, tmp_path, caplog
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / "cli.json").write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {
                        "credential-valued": {"output_config": {"effort": self._CREDENTIAL}},
                        self._CREDENTIAL: {"output_config": {"effort": "max"}},
                    }
                }
            ),
            encoding="utf-8",
        )

        warnings = self._pre_spawn_warnings(
            caplog,
            acp_provider.AcpProvider(acp_backend=ACP_BACKEND_KIRO, work_dir=tmp_path, model="auto"),
        )

        assert len(warnings) == 1
        for cut in (len(self._CREDENTIAL), 120, 80, 40, 20):
            assert self._CREDENTIAL[:cut] not in warnings[0]
        assert "eyJ" not in warnings[0]
        assert "<unrecognized>" in warnings[0]
        assert "credential-valued" in warnings[0]
        assert '"max"' in warnings[0]

    def test_unowned_warning_bounds_long_and_deeply_nested_effort_values(self, tmp_path, caplog):
        nested_effort: list[object] = []
        for _ in range(200_000):
            nested_effort = [nested_effort]

        long_model_key = "nested-" + "m" * 5_000
        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            acp_provider._warn_unowned_cli_effort(
                tmp_path / ".kiro" / "settings" / "cli.json",
                {
                    "long": {"output_config": {"effort": "x" * 5_000}},
                    long_model_key: {"output_config": {"effort": nested_effort}},
                },
                None,
            )

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == acp_provider.__name__
        ]
        assert len(warnings) == 1
        assert '"long"=<unrecognized>' in warnings[0]
        assert "<array of 1 items>" in warnings[0]
        assert "<id too long: 5007 chars>" in warnings[0]
        entries = warnings[0].split("Kiro Crew did not write (", 1)[1].split("):", 1)[0]
        assert len(entries) <= 2 * acp_provider._WARN_UNOWNED_MAX_CHARS + 2

    def test_kept_operator_value_is_escaped_and_bounded_in_debug_log(self, tmp_path, caplog):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {"chat.modelDefaults": {self._MODEL: {"output_config": {"effort": "x" * 5000}}}}
            ),
            encoding="utf-8",
        )

        with caplog.at_level(logging.DEBUG, logger=acp_provider.__name__):
            _write_cli_overlay(tmp_path, self._MODEL, "max")

        messages = [
            record.getMessage()
            for record in caplog.records
            if "kept operator-authored level" in record.getMessage()
        ]
        assert len(messages) == 1
        assert "effort=<unrecognized>" in messages[0]
        assert len(messages[0]) < 300

    def test_auto_spawn_warns_about_an_unowned_level_for_any_model(self, tmp_path, caplog):
        self._write_operator_effort(tmp_path, "gpt-5.6-sol", "high")
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )

        warnings = self._pre_spawn_warnings(caplog, provider)

        assert len(warnings) == 1
        assert "gpt-5.6-sol" in warnings[0]
        assert "high" in warnings[0]
        # Its picker remedy is reachable only once the listed model is selected.
        assert "selecting the listed model" in warnings[0]
        assert _read_cli_overlay(tmp_path) == {"gpt-5.6-sol": "high"}

    def test_default_spawn_stays_silent_when_it_removes_its_own_level(self, tmp_path, caplog):
        _write_cli_overlay(tmp_path, self._MODEL, "max")

        warnings = self._pre_spawn_warnings(caplog, self._provider(tmp_path))

        assert warnings == []
        assert _read_cli_overlay(tmp_path) == {}

    def test_owned_clear_logs_removed_levels_once_with_escaped_bounded_values(
        self, tmp_path, caplog
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        models = ["removed\nmodel"] + [f"model-{index}" for index in range(11)]
        stamp = 1_700_000_000
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {
                        model: {"output_config": {"effort": "lev\nel"}} for model in models
                    },
                    _KIROCREW_EFFORT_OWNED_KEY: {model: "lev\nel" for model in models},
                    "kirocrew.effortOwnedStamp": stamp,
                }
            ),
            encoding="utf-8",
        )
        os.utime(cli_json, (stamp, stamp))

        with caplog.at_level(logging.INFO, logger=acp_provider.__name__):
            assert _clear_cli_overlay_effort(tmp_path, None) is True

        removals = [
            record.getMessage()
            for record in caplog.records
            if "removed Kiro Crew-owned level" in record.getMessage()
        ]
        assert len(removals) == 1
        assert str(settings / "cli.json") in removals[0]
        assert '"removed\\nmodel"=<unrecognized>' in removals[0]
        assert "\n" not in removals[0]
        assert "... 2 more" in removals[0]
        assert "set it again" in removals[0]
        assert len(removals[0]) < 1800

    def test_auto_spawn_stays_silent_when_it_removes_every_owned_level(self, tmp_path, caplog):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        _write_cli_overlay(tmp_path, "gpt-5.6-sol", "high")
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model="auto",
        )

        warnings = self._pre_spawn_warnings(caplog, provider)

        assert warnings == []
        assert _read_cli_overlay(tmp_path) == {}

    def test_default_spawn_stays_silent_about_another_models_unowned_level(self, tmp_path, caplog):
        self._write_operator_effort(tmp_path, "claude-opus-4.6", "max")

        warnings = self._pre_spawn_warnings(caplog, self._provider(tmp_path))

        assert warnings == []
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.6": "max"}

    @pytest.mark.parametrize("model", ["claude-opus-4.7", "auto"])
    def test_spawn_stays_silent_about_a_level_under_the_other_family_key(
        self, tmp_path, caplog, model
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        (settings / "cli.json").write_text(
            json.dumps({"chat.modelDefaults": {self._MODEL: {"reasoning": {"effort": "max"}}}}),
            encoding="utf-8",
        )
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=tmp_path,
            model=model,
        )

        warnings = self._pre_spawn_warnings(caplog, provider)

        assert warnings == []

    @pytest.mark.asyncio
    async def test_a_spawn_waits_out_a_brief_holder_of_the_lock(self, tmp_path, monkeypatch):
        # Another chat's change_effort holds the lock off-loop across a read plus
        # an atomic write. On the loop thread the lock is single-shot, so a
        # spawn meeting that window on the loop would give up at once and read
        # the level that chat left. The pre-spawn projection waits it out off the
        # loop (the holder frees the lock well inside the startup ceiling).
        provider = self._provider(tmp_path)
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        held = threading.Event()
        release = threading.Event()

        def _hold_the_lock_briefly():
            with workspace_cli_settings_lock(tmp_path):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=_hold_the_lock_briefly)
        try:
            holder.start()
            assert held.wait(timeout=5)
            asyncio.get_running_loop().call_later(0.1, release.set)
            await provider.start()
        finally:
            release.set()
            holder.join(timeout=5)
        assert not holder.is_alive()

        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overlay_writer", ["_apply_effort_overlay", "_apply_tool_search_overlay"]
    )
    async def test_a_cancelled_spawn_waits_for_the_overlay_worker(
        self, tmp_path, monkeypatch, overlay_writer
    ):
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def _blocked_overlay():
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
            finally:
                worker_finished.set()
            raise RuntimeError("abandoned overlay failed")

        monkeypatch.setattr(provider, overlay_writer, _blocked_overlay)
        start_task = None
        try:
            start_task = asyncio.create_task(provider.start())
            assert await asyncio.to_thread(worker_started.wait, 5)
            start_task.cancel()
            await asyncio.sleep(0)
            assert not start_task.done()
            assert not worker_finished.is_set()
        finally:
            release_worker.set()
            if start_task is not None and not start_task.done():
                if start_task.cancelling() == 0:
                    start_task.cancel()
                with suppress(asyncio.CancelledError):
                    await start_task

        assert start_task is not None
        assert await asyncio.to_thread(worker_finished.wait, 5)
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert worker_finished.is_set()
        provider._start_kiro_runtime.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overlay_writer", ["_apply_effort_overlay", "_apply_tool_search_overlay"]
    )
    async def test_a_spawn_cancelled_again_while_it_waits_still_waits_for_the_worker(
        self, tmp_path, monkeypatch, overlay_writer
    ):
        # A slot close and then a gateway shutdown each cancel the start; the
        # second must not release a worker that could write after a successor.
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def _blocked_overlay():
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
            finally:
                worker_finished.set()

        monkeypatch.setattr(provider, overlay_writer, _blocked_overlay)
        start_task = asyncio.create_task(provider.start())
        try:
            assert await asyncio.to_thread(worker_started.wait, 5)
            for _ in range(3):
                start_task.cancel()
                await asyncio.sleep(0)
                assert not start_task.done()
            assert not worker_finished.is_set()
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert worker_finished.is_set()
        provider._start_kiro_runtime.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("switch", ["set_model", "reapply_live_effort"])
    async def test_operator_authored_effort_survives_a_live_switch_on_default(
        self, tmp_path, monkeypatch, switch
    ):
        # A live model switch on a slot whose effort is Default re-applies the
        # resolved default to the model switched to, which runs clear_effort with
        # no override and no workspace default. Nobody asked for that model's
        # entry to be removed, so an entry the operator wrote for it by hand is
        # not Kiro Crew's to delete; only the explicit clear endpoint may.
        target = "claude-sonnet-4.6"
        self._write_operator_effort(tmp_path, target, "max")
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")

        async def _switch_live(model):
            provider._client._model = model

        monkeypatch.setattr(provider._client, "set_model", _switch_live)

        if switch == "set_model":
            await provider.set_model(target)
        else:
            await _switch_live(target)
            assert await provider.reapply_live_effort("") is True

        assert provider._client._model == target
        assert _read_cli_overlay(tmp_path) == {target: "max"}
        assert self._owned_effort(tmp_path) == {}
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owned_only", [True, False])
    async def test_only_an_explicit_operator_clear_removes_an_unowned_entry(
        self, tmp_path, owned_only
    ):
        # The dashboard's explicit clear is the one caller that passes
        # owned_only=False: the operator has directly asked to remove this
        # model's entry, whoever wrote it. Every automatic clear keeps the guard.
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")

        assert await provider.clear_effort(owned_only=owned_only) is False

        expected = {self._MODEL: "max"} if owned_only else {}
        assert _read_cli_overlay(tmp_path) == expected
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_default_removes_operator_level_before_projecting_workspace_default(
        self, tmp_path, monkeypatch
    ):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(return_value="")
        real_atomic_write = acp_provider.atomic_write
        writes = []

        def _count_write(path, contents, **kwargs):
            writes.append((path, contents))
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", _count_write)
        assert await provider.clear_effort(owned_only=False) is True

        assert len(writes) == 1
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}
        assert self._owned_effort(tmp_path) == {self._MODEL: "low"}
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "low"})

    @pytest.mark.asyncio
    async def test_explicit_default_reports_a_non_object_parent_and_leaves_the_file_unchanged(
        self, tmp_path, caplog
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps({"chat.modelDefaults": "operator-note", "other.key": 1}),
            encoding="utf-8",
        )
        before = cli_json.read_bytes()
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(return_value="")

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert await provider.clear_effort(owned_only=False) is None

        assert cli_json.read_bytes() == before
        assert provider._effort_per_model[self._MODEL] == "high"
        provider._client.send_command.assert_not_awaited()
        assert any(
            "workspace overlay cannot be rewritten" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_explicit_default_that_cannot_publish_the_default_leaves_the_file_unchanged(
        self, tmp_path, monkeypatch, caplog
    ):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        cli_json = tmp_path / ".kiro" / "settings" / "cli.json"
        before = cli_json.read_bytes()
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(return_value="")
        real_atomic_write = acp_provider.atomic_write

        def _fail_default_publication(path, contents, **kwargs):
            document = json.loads(contents)
            model_defaults = document.get("chat.modelDefaults", {})
            model_config = model_defaults.get(self._MODEL, {})
            output_config = model_config.get("output_config", {})
            if output_config.get("effort") == "low":
                raise OSError("lock busy")
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", _fail_default_publication)
        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert await provider.clear_effort(owned_only=False) is None

        assert cli_json.read_bytes() == before
        assert provider._effort_per_model[self._MODEL] == "high"
        provider._client.send_command.assert_not_awaited()
        assert any(
            "workspace overlay cannot be rewritten" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_explicit_default_clears_the_other_family_shape_and_keeps_sibling_keys(
        self, tmp_path
    ):
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            json.dumps(
                {
                    "chat.modelDefaults": {
                        self._MODEL: {
                            "output_config": {"effort": "max", "active_sibling": True},
                            "reasoning": {"effort": "high", "other_sibling": "kept"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(return_value="")

        assert await provider.clear_effort(owned_only=False) is True

        model_cfg = json.loads(cli_json.read_text(encoding="utf-8"))["chat.modelDefaults"][
            self._MODEL
        ]
        assert model_cfg["output_config"] == {"active_sibling": True, "effort": "low"}
        assert model_cfg["reasoning"] == {"other_sibling": "kept"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("workspace_default", [None, "low"])
    async def test_the_explicit_clear_rewrites_inside_its_fence_and_pushes_after_it(
        self, tmp_path, monkeypatch, workspace_default
    ):
        # The fence refuses warm-runtime claims while it is up and dates the
        # rewrite as it comes down; the live push can wait on a busy session.
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: workspace_default} if workspace_default else None,
        )
        events: list[str] = []
        provider._client.send_command = AsyncMock(
            side_effect=lambda *_a, **_k: events.append("push")
        )

        @contextmanager
        def fence():
            events.append("fence up")
            try:
                yield
            finally:
                events.append("fence down")

        real_atomic_write = acp_provider.atomic_write

        def record_write(path, contents, **kwargs):
            events.append("write")
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", record_write)

        applied_live = await provider.clear_effort(owned_only=False, fence_rewrite=fence)

        assert applied_live is (workspace_default is not None)
        pushes = ["push"] if workspace_default else []
        assert events == ["fence up", "write", "fence down", *pushes]
        expected = {self._MODEL: workspace_default} if workspace_default else {}
        assert _read_cli_overlay(tmp_path) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("workspace_default", [None, "low"])
    async def test_a_cancelled_explicit_clear_lowers_its_fence_only_after_the_write(
        self, tmp_path, monkeypatch, workspace_default
    ):
        # A fence lowered while the worker can still write dates the rewrite too
        # early: a runtime spawned in between could read the entry it removes.
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: workspace_default} if workspace_default else None,
        )
        provider._client.send_command = AsyncMock(return_value="")
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        fence_down_after_the_worker: list[bool] = []
        real_atomic_write = acp_provider.atomic_write

        def blocked_write(path, contents, **kwargs):
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
                real_atomic_write(path, contents, **kwargs)
            finally:
                worker_finished.set()

        @contextmanager
        def fence():
            try:
                yield
            finally:
                fence_down_after_the_worker.append(worker_finished.is_set())

        monkeypatch.setattr(acp_provider, "atomic_write", blocked_write)
        clear_task = asyncio.create_task(
            provider.clear_effort(owned_only=False, fence_rewrite=fence)
        )
        try:
            assert await asyncio.to_thread(worker_started.wait, 5)
            clear_task.cancel()
            await asyncio.sleep(0)
            assert not clear_task.done()
            assert fence_down_after_the_worker == []
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await clear_task
        assert fence_down_after_the_worker == [True]
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_explicit_clear_whose_push_fails_after_the_rewrite_asks_for_a_reset(
        self, tmp_path, caplog
    ):
        # The file already holds the default, so a reset carries it to the
        # session. A raise would make the handler carry a removal the file no
        # longer needs, and refuse the pick when that Default cannot be saved.
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(side_effect=RuntimeError("runtime gone"))

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            assert await provider.clear_effort(owned_only=False) is False

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}
        assert self._MODEL not in provider._effort_per_model
        assert any("live push failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_automatic_clear_whose_push_fails_still_raises(self, tmp_path):
        # reapply_live_effort and the handler's undo read a raise as the live
        # re-apply failing, and False as nothing left to push.
        provider = self._provider(tmp_path, effort_defaults={self._MODEL: "low"})
        provider._client.send_command = AsyncMock(side_effect=RuntimeError("runtime gone"))

        with pytest.raises(RuntimeError, match="runtime gone"):
            await provider.clear_effort()

    @pytest.mark.asyncio
    async def test_an_automatic_clear_never_replaces_an_unowned_value(self, tmp_path, monkeypatch):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(tmp_path, effort_defaults={self._MODEL: "low"})
        provider._client.send_command = AsyncMock(return_value="")
        real_write = acp_provider._write_cli_overlay
        replace_unowned_values = []

        def _track_write(*args, **kwargs):
            replace_unowned_values.append(kwargs.get("replace_unowned", False))
            return real_write(*args, **kwargs)

        monkeypatch.setattr(acp_provider, "_write_cli_overlay", _track_write)
        assert provider._apply_effort_overlay() is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

        assert await provider.change_effort("high") is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

        assert await provider.clear_effort() is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert replace_unowned_values == [False, False, False]

    @pytest.mark.asyncio
    async def test_automatic_default_keeps_operator_level_and_pushes_workspace_default(
        self, tmp_path
    ):
        self._write_operator_effort(tmp_path, self._MODEL, "max")
        provider = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        provider._client.send_command = AsyncMock(return_value="")

        assert await provider.clear_effort() is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert self._owned_effort(tmp_path) == {}
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "low"})


class TestAPickedLevelSurvivesAPeersClear:
    """What a kiro session runs is its own pick, whatever the shared file says.

    The overlay is projected before the spawn and read by kiro-cli at the spawn.
    Between the two, another session in the same work dir can project its own
    Default and remove this session's entry: an unpinned session removes every
    Kiro Crew entry, a session on the same model removes this model's. The warm
    pool constructs such a session on every fill and every replenish, in the
    dashboard's own work dir, so the gap is met routinely. The session cannot
    stop a peer from projecting, so it pushes its level live once its session is
    up, the same ``/effort`` channel a live change uses.
    """

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("peer_model", ["auto", _MODEL])
    async def test_a_peers_clear_during_the_spawn_does_not_change_what_this_session_runs(
        self, tmp_path, monkeypatch, peer_model
    ):
        picked = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        picked._client.send_command = AsyncMock(return_value="")
        overlay_at_spawn = {}

        async def _spawn_while_a_default_peer_starts():
            # A warm-pool fill or a Default chat projects its own Default here,
            # before kiro-cli has read the picked session's overlay.
            peer = self._provider(tmp_path, model=peer_model)
            assert peer._apply_effort_overlay() is True
            overlay_at_spawn.update(_read_cli_overlay(tmp_path))

        monkeypatch.setattr(picked, "_start_kiro_runtime", _spawn_while_a_default_peer_starts)

        await picked.start()

        assert overlay_at_spawn == {}
        picked._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})

    @pytest.mark.asyncio
    async def test_a_session_on_default_pushes_nothing_after_the_spawn(self, tmp_path, monkeypatch):
        provider = self._provider(tmp_path)
        provider._client.send_command = AsyncMock(return_value="")
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_rejected_live_push_does_not_break_the_start(
        self, tmp_path, monkeypatch, caplog
    ):
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        provider._client.send_command = AsyncMock(side_effect=RuntimeError("refused"))
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert any("effort" in r.getMessage() and "max" in r.getMessage() for r in caplog.records)


class TestEffortPathsDoNoFilesystemIoOnTheLoop:
    """Async effort paths keep work-directory filesystem calls off the loop."""

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir: Path, *, model: str | None = None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    def _on_loop(calls: list[tuple[str, int]], loop_thread: int) -> list[str]:
        return [name for name, ident in calls if ident == loop_thread]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider_kwargs",
        [{}, {"effort_per_model": {_MODEL: "max"}}],
        ids=["default-spawn", "level-spawn"],
    )
    async def test_start_does_no_work_dir_io_on_the_loop_thread(
        self, tmp_path, monkeypatch, provider_kwargs
    ):
        work_dir = tmp_path / "ws"
        work_dir.mkdir()
        provider = self._provider(work_dir, **provider_kwargs)
        provider._client.send_command = AsyncMock(return_value="")
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock(return_value=None))
        loop_thread = threading.get_ident()

        with _record_work_dir_io(monkeypatch, work_dir) as calls:
            await provider.start()

        assert self._on_loop(calls, loop_thread) == []
        assert [name for name, ident in calls if ident != loop_thread]

    @pytest.mark.asyncio
    async def test_change_effort_does_no_work_dir_io_on_the_loop_thread(
        self, tmp_path, monkeypatch
    ):
        work_dir = tmp_path / "ws"
        work_dir.mkdir()
        provider = self._provider(work_dir)
        provider._client.send_command = AsyncMock(return_value="")
        loop_thread = threading.get_ident()

        with _record_work_dir_io(monkeypatch, work_dir) as calls:
            assert await provider.change_effort("max") is True

        assert self._on_loop(calls, loop_thread) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("workspace_default", [False, True], ids=["builtin", "workspace"])
    async def test_clear_effort_does_no_work_dir_io_on_the_loop_thread(
        self, tmp_path, monkeypatch, workspace_default
    ):
        work_dir = tmp_path / "ws"
        work_dir.mkdir()
        provider_kwargs = {"effort_per_model": {self._MODEL: "max"}}
        if workspace_default:
            provider_kwargs["effort_defaults"] = {self._MODEL: "low"}
        provider = self._provider(work_dir, **provider_kwargs)
        provider._client.send_command = AsyncMock(return_value="")
        assert await provider.change_effort("max") is True
        loop_thread = threading.get_ident()

        with _record_work_dir_io(monkeypatch, work_dir) as calls:
            result = await provider.clear_effort(owned_only=False)

        assert result is (True if workspace_default else False)
        assert self._on_loop(calls, loop_thread) == []


class TestDefaultSpawnOverlayFence:
    """Default projections share their read window while level writes exclude it."""

    _MODEL = "claude-opus-4.7"
    # A call the fence lets through still does real file I/O in a worker thread, which
    # a loaded Windows runner has taken past 0.2 s. A call the fence wrongly holds waits
    # out a 30 s or 32 s ceiling, so this deadline still tells the two apart.
    _UNBLOCKED_DEADLINE_SECS = 10.0

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    async def _until(condition, timeout: float) -> None:
        """Wait for fence state another task reaches only after its key resolves.

        A holder resolves its key in a worker thread before it reaches the fence,
        so one loop turn does not show that another task is waiting yet.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not condition():
            assert loop.time() < deadline, "the expected fence state was not reached"
            await asyncio.sleep(0.005)

    @pytest.mark.asyncio
    async def test_default_spawn_keeps_a_live_pick_out_of_its_runtime_start(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        default = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(default.start())
        await entered.wait()
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await asyncio.wait({pick_task}, timeout=1.0)

        assert _read_cli_overlay(tmp_path) == {}
        assert not pick_task.done()

        release.set()
        await start_task
        assert await pick_task is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

    @pytest.mark.asyncio
    async def test_second_default_spawn_enters_while_first_default_window_is_open(
        self, tmp_path, monkeypatch, caplog
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first_runtime_start():
            first_entered.set()
            await release_first.wait()

        async def second_runtime_start():
            second_entered.set()

        monkeypatch.setattr(first, "_start_kiro_runtime", first_runtime_start)
        monkeypatch.setattr(second, "_start_kiro_runtime", second_runtime_start)
        first_task = asyncio.create_task(first.start())
        await first_entered.wait()

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            second_task = asyncio.create_task(second.start())
            await asyncio.wait_for(second_entered.wait(), self._UNBLOCKED_DEADLINE_SECS)

        assert not any("could not be checked or cleared" in r.getMessage() for r in caplog.records)
        release_first.set()
        await first_task
        await second_task

    @pytest.mark.asyncio
    async def test_write_waits_for_windows_without_stopping_a_second_default_window(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def first_runtime_start():
            first_entered.set()
            await release_first.wait()

        async def second_runtime_start():
            second_entered.set()
            await release_second.wait()

        monkeypatch.setattr(first, "_start_kiro_runtime", first_runtime_start)
        monkeypatch.setattr(second, "_start_kiro_runtime", second_runtime_start)
        first_task = asyncio.create_task(first.start())
        await first_entered.wait()
        write_waiting = asyncio.Event()
        original_acquire = acp_provider._WorkspaceEffortOverlayFence.acquire

        async def signalling_acquire(fence, mode, timeout):
            if mode == "write":
                write_waiting.set()
            return await original_acquire(fence, mode, timeout)

        monkeypatch.setattr(
            acp_provider._WorkspaceEffortOverlayFence, "acquire", signalling_acquire
        )
        writer_task = asyncio.create_task(writer.change_effort("max"))
        await asyncio.wait_for(write_waiting.wait(), self._UNBLOCKED_DEADLINE_SECS)
        second_task = asyncio.create_task(second.start())
        await asyncio.wait_for(second_entered.wait(), self._UNBLOCKED_DEADLINE_SECS)
        assert not writer_task.done()

        release_first.set()
        await first_task
        assert not writer_task.done()
        release_second.set()
        await second_task
        assert await writer_task is True

    @pytest.mark.asyncio
    async def test_default_window_is_keyed_by_work_directory(self, tmp_path, monkeypatch):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        _write_cli_overlay(first_dir, self._MODEL, "max")
        first = self._provider(first_dir)
        other = self._provider(second_dir)
        other._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        monkeypatch.setattr(first, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(first.start())
        await entered.wait()
        assert (
            await asyncio.wait_for(other.change_effort("max"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )
        release.set()
        await start_task

    @pytest.mark.asyncio
    async def test_auto_spawn_keeps_a_different_models_pick_out_of_runtime_start(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        _write_cli_overlay(tmp_path, other_model, "max")
        default = self._provider(tmp_path, model="auto")
        sibling = self._provider(tmp_path, model=other_model)
        sibling._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(default.start())
        await entered.wait()
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await asyncio.wait({pick_task}, timeout=1.0)
        assert _read_cli_overlay(tmp_path) == {}
        assert not pick_task.done()
        release.set()
        await start_task
        assert await pick_task is True

    @pytest.mark.asyncio
    async def test_explicit_default_with_workspace_default_waits_for_default_window(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        default = self._provider(tmp_path)
        explicit = self._provider(tmp_path, effort_defaults={self._MODEL: "low"})
        explicit.arm_explicit_effort_default(nullcontext)
        entered = asyncio.Event()
        release = asyncio.Event()
        explicit_started = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        async def explicit_runtime_start():
            explicit_started.set()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(explicit, "_start_kiro_runtime", explicit_runtime_start)
        default_task = asyncio.create_task(default.start())
        await entered.wait()
        explicit_task = asyncio.create_task(explicit.start())
        await asyncio.wait({explicit_task}, timeout=1.0)
        assert not explicit_started.is_set()
        assert _read_cli_overlay(tmp_path) == {}
        release.set()
        await default_task
        await explicit_task
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}

    @pytest.mark.asyncio
    async def test_level_projection_waits_for_default_window_then_reasserts_live_level(
        self, tmp_path, monkeypatch
    ):
        default = self._provider(tmp_path)
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        level._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()
        level_started = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        async def level_runtime_start():
            level_started.set()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(level, "_start_kiro_runtime", level_runtime_start)
        default_task = asyncio.create_task(default.start())
        await entered.wait()
        level_task = asyncio.create_task(level.start())
        await asyncio.wait({level_task}, timeout=1.0)
        assert not level_started.is_set()
        assert _read_cli_overlay(tmp_path) == {}
        release.set()
        await default_task
        await level_task
        level._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})

    @pytest.mark.asyncio
    async def test_level_spawn_releases_write_fence_before_runtime_start(
        self, tmp_path, monkeypatch
    ):
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        sibling = self._provider(tmp_path)
        level._client.send_command = AsyncMock(return_value="")
        sibling._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await release.wait()

        monkeypatch.setattr(level, "_start_kiro_runtime", hold_runtime_start)
        level_task = asyncio.create_task(level.start())
        await entered.wait()
        assert (
            await asyncio.wait_for(sibling.change_effort("high"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )
        release.set()
        await level_task

    @pytest.mark.asyncio
    async def test_expired_window_fails_before_tool_search_or_runtime(self, tmp_path, monkeypatch):
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        waiter = self._provider(tmp_path)
        projection_started = threading.Event()
        release_projection = threading.Event()
        runtime_started = asyncio.Event()
        tool_search_calls = []

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        def apply_tool_search_overlay():
            tool_search_calls.append(True)
            return True

        async def waiting_runtime_start():
            runtime_started.set()

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(waiter, "_apply_tool_search_overlay", apply_tool_search_overlay)
        monkeypatch.setattr(waiter, "_start_kiro_runtime", waiting_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            0.03,
            raising=False,
        )
        holder_task = asyncio.create_task(holder.start())
        assert await asyncio.to_thread(projection_started.wait, 5)

        with pytest.raises(RuntimeError, match="Default effort overlay fence wait timed out"):
            await waiter.start()

        assert tool_search_calls == []
        assert not runtime_started.is_set()
        release_projection.set()
        await holder_task

    @pytest.mark.asyncio
    async def test_cancelled_window_releases_after_its_overlay_worker_settles(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_overlay():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return True

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_overlay)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        start_task = asyncio.create_task(provider.start())
        assert await asyncio.to_thread(worker_started.wait, 5)
        start_task.cancel()
        await asyncio.sleep(0)
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await asyncio.sleep(0)
        assert not pick_task.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert await pick_task is True

    @pytest.mark.asyncio
    async def test_cancelled_pending_window_does_not_block_next_write(self, tmp_path, monkeypatch):
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        waiter = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        projection_started = threading.Event()
        release_projection = threading.Event()

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            self._UNBLOCKED_DEADLINE_SECS,
            raising=False,
        )
        holder_task = asyncio.create_task(holder.start())
        assert await asyncio.to_thread(projection_started.wait, 5)
        fence = acp_provider._workspace_effort_overlay_fence(os.path.realpath(tmp_path))
        waiter_task = asyncio.create_task(waiter.start())
        await self._until(lambda: fence._pending_windows == 1, self._UNBLOCKED_DEADLINE_SECS)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        release_projection.set()
        await holder_task
        assert (
            await asyncio.wait_for(writer.change_effort("high"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )

    @pytest.mark.asyncio
    async def test_pick_fence_expiry_raises_without_writing(self, tmp_path, monkeypatch):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        holder = self._provider(tmp_path)
        picker = self._provider(tmp_path)
        picker._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await release_holder.wait()

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await holder_entered.wait()

        with pytest.raises(
            RuntimeError, match="could not persist effort.*workspace overlay is locked"
        ):
            await picker.change_effort("max")
        assert _read_cli_overlay(tmp_path) == {}
        assert picker._effort_per_model == {}
        release_holder.set()
        await holder_task

    @pytest.mark.asyncio
    async def test_workspace_default_clear_fence_expiry_returns_none_without_writing(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        clearer._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await release_holder.wait()

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await holder_entered.wait()

        assert await clearer.clear_effort(owned_only=False) is None
        assert _read_cli_overlay(tmp_path) == {}
        assert clearer._effort_per_model == {self._MODEL: "high"}
        clearer._client.send_command.assert_not_awaited()
        release_holder.set()
        await holder_task

    @pytest.mark.asyncio
    async def test_builtin_default_clear_fence_expiry_returns_none_without_writing(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(tmp_path, effort_per_model={self._MODEL: "high"})
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await release_holder.wait()

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await holder_entered.wait()

        assert await clearer.clear_effort(owned_only=False) is None
        assert _read_cli_overlay(tmp_path) == {}
        assert clearer._effort_per_model == {self._MODEL: "high"}
        release_holder.set()
        await holder_task

    @pytest.mark.asyncio
    async def test_live_workspace_default_clear_waits_for_a_default_window(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        clearer._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        clear_write_started = threading.Event()
        original_apply = clearer._apply_effort_overlay

        async def hold_runtime_start():
            holder_entered.set()
            await release_holder.wait()

        def observed_clear_write(**kwargs):
            clear_write_started.set()
            return original_apply(**kwargs)

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(clearer, "_apply_effort_overlay", observed_clear_write)
        holder_task = asyncio.create_task(holder.start())
        await holder_entered.wait()
        clear_task = asyncio.create_task(clearer.clear_effort(owned_only=False))
        assert not await asyncio.to_thread(clear_write_started.wait, 0.05)
        assert _read_cli_overlay(tmp_path) == {}

        release_holder.set()
        await holder_task
        assert await clear_task is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}

    @pytest.mark.asyncio
    async def test_sequential_writes_release_the_fence_and_default_start_enters(
        self, tmp_path, monkeypatch
    ):
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        default = self._provider(tmp_path)
        first._client.send_command = AsyncMock(return_value="")
        second._client.send_command = AsyncMock(return_value="")
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(default, "_start_kiro_runtime", AsyncMock())

        assert await first.change_effort("max") is True
        assert await second.change_effort("high") is True
        await asyncio.wait_for(default.start(), self._UNBLOCKED_DEADLINE_SECS)
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_symlinked_alias_shares_one_fence(self, tmp_path):
        real_work_dir = tmp_path / "real"
        alias_work_dir = tmp_path / "alias"
        real_work_dir.mkdir()
        try:
            platform_compat.symlink_or_junction(real_work_dir, alias_work_dir)
        except OSError as exc:
            pytest.fail(f"platform cannot create a directory symlink: {exc}")
        real_provider = self._provider(real_work_dir)
        alias_provider = self._provider(alias_work_dir)

        async with acp_provider._hold_workspace_effort_overlay_fence(
            real_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True
            async with acp_provider._hold_workspace_effort_overlay_fence(
                alias_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
            ) as alias_acquired:
                assert alias_acquired is False

    @pytest.mark.asyncio
    async def test_default_window_through_symlink_blocks_real_work_directory_write(self, tmp_path):
        real_work_dir = tmp_path / "real"
        alias_work_dir = tmp_path / "alias"
        real_work_dir.mkdir()
        try:
            platform_compat.symlink_or_junction(real_work_dir, alias_work_dir)
        except OSError as exc:
            pytest.fail(f"platform cannot create a directory symlink: {exc}")

        async with acp_provider._hold_workspace_effort_overlay_fence(alias_work_dir, "window", 0.1):
            async with acp_provider._hold_workspace_effort_overlay_fence(
                real_work_dir, "write", 0.01
            ) as acquired:
                assert acquired is False

    @pytest.mark.asyncio
    async def test_fence_key_is_resolved_before_the_registry_lookup(self, tmp_path, monkeypatch):
        original_to_thread = asyncio.to_thread

        async def yielding_to_thread(function, *args, **kwargs):
            if function is os.path.realpath:
                await asyncio.sleep(0)
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(acp_provider.asyncio, "to_thread", yielding_to_thread)
        original_acquire = acp_provider._WorkspaceEffortOverlayFence.acquire

        async def nonwaiting_write_acquire(fence, mode, timeout):
            if mode == "write" and fence._write_held:
                return False
            return await original_acquire(fence, mode, timeout)

        monkeypatch.setattr(
            acp_provider._WorkspaceEffortOverlayFence, "acquire", nonwaiting_write_acquire
        )
        release = asyncio.Event()
        outcomes_ready = asyncio.Event()
        outcomes: list[bool] = []

        async def hold_until_released():
            async with acp_provider._hold_workspace_effort_overlay_fence(
                tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
            ) as acquired:
                outcomes.append(acquired)
                if len(outcomes) == 2:
                    outcomes_ready.set()
                if acquired:
                    await release.wait()

        first = asyncio.create_task(hold_until_released())
        second = asyncio.create_task(hold_until_released())
        await asyncio.wait_for(outcomes_ready.wait(), self._UNBLOCKED_DEADLINE_SECS)
        release.set()
        await asyncio.gather(first, second)

        assert sorted(outcomes) == [False, True]

    @pytest.mark.asyncio
    async def test_fence_resolution_failure_fails_the_start_it_cannot_key(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        original_realpath = os.path.realpath

        def failing_realpath(_path):
            raise OSError("unavailable mount")

        monkeypatch.setattr(acp_provider.os.path, "realpath", failing_realpath)
        with pytest.raises(OSError, match="unavailable mount"):
            await provider.start()

        monkeypatch.setattr(acp_provider.os.path, "realpath", original_realpath)
        async with acp_provider._hold_workspace_effort_overlay_fence(
            tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_cancelled_fence_resolution_acquires_nothing(self, tmp_path, monkeypatch):
        provider = self._provider(tmp_path)
        original_realpath = os.path.realpath
        resolution_started = threading.Event()
        release_resolution = threading.Event()

        def blocked_realpath(path):
            resolution_started.set()
            assert release_resolution.wait(self._UNBLOCKED_DEADLINE_SECS)
            return original_realpath(path)

        monkeypatch.setattr(acp_provider.os.path, "realpath", blocked_realpath)
        start_task = asyncio.create_task(provider.start())
        assert await asyncio.to_thread(resolution_started.wait, self._UNBLOCKED_DEADLINE_SECS)
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        monkeypatch.setattr(acp_provider.os.path, "realpath", original_realpath)
        release_resolution.set()
        async with acp_provider._hold_workspace_effort_overlay_fence(
            tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_default_spawn_waits_for_level_projection_then_reads_a_cleared_overlay(
        self, tmp_path, monkeypatch
    ):
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        default = self._provider(tmp_path)
        worker_started = threading.Event()
        release_worker = threading.Event()
        default_entered = asyncio.Event()
        snapshot_at_runtime = {}

        def blocked_level_projection():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return _write_cli_overlay(tmp_path, self._MODEL, "max")

        async def snapshot_runtime_start():
            snapshot_at_runtime.update(_read_cli_overlay(tmp_path))
            default_entered.set()

        monkeypatch.setattr(level, "_apply_effort_overlay", blocked_level_projection)
        monkeypatch.setattr(level, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(default, "_start_kiro_runtime", snapshot_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            0.5,
            raising=False,
        )
        level_task = asyncio.create_task(level.start())
        assert await asyncio.to_thread(worker_started.wait, 5)
        default_task = asyncio.create_task(default.start())
        await asyncio.sleep(0.02)
        assert not default_entered.is_set()
        release_worker.set()
        await level_task
        await default_task
        assert snapshot_at_runtime == {}

    @pytest.mark.asyncio
    async def test_pending_window_blocks_a_write_after_the_held_write_releases(self, tmp_path):
        fence = acp_provider._workspace_effort_overlay_fence(os.path.realpath(tmp_path))
        assert await fence.acquire("write", 0.1) is True
        window_task = asyncio.create_task(fence.acquire("window", 0.1))
        await asyncio.sleep(0)
        assert fence._pending_windows == 1

        fence._write_held = False
        assert await fence.acquire("write", 0.01) is False

        fence._write_released.set()
        assert await window_task is True
        fence.release("window")

    @pytest.mark.asyncio
    async def test_write_arriving_while_a_window_is_pending_waits_for_its_runtime_start(
        self, tmp_path, monkeypatch
    ):
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        default = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        projection_started = threading.Event()
        release_projection = threading.Event()
        default_started = asyncio.Event()
        release_default = asyncio.Event()

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        async def hold_default_runtime_start():
            default_started.set()
            await release_default.wait()

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(default, "_start_kiro_runtime", hold_default_runtime_start)
        holder_task = asyncio.create_task(holder.start())
        assert await asyncio.to_thread(projection_started.wait, 5)
        fence = acp_provider._workspace_effort_overlay_fence(os.path.realpath(tmp_path))
        default_task = asyncio.create_task(default.start())
        await self._until(lambda: fence._pending_windows == 1, self._UNBLOCKED_DEADLINE_SECS)
        writer_task = asyncio.create_task(writer.change_effort("high"))
        release_projection.set()
        await holder_task
        await default_started.wait()
        assert not writer_task.done()
        release_default.set()
        await default_task
        assert await writer_task is True

    @pytest.mark.asyncio
    async def test_second_cancellation_while_waiting_for_overlay_worker_releases_window(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_overlay():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return True

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_overlay)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        start_task = asyncio.create_task(provider.start())
        assert await asyncio.to_thread(worker_started.wait, 5)
        start_task.cancel()
        await asyncio.sleep(0)
        start_task.cancel()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        assert (
            await asyncio.wait_for(sibling.change_effort("max"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )


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

    def test_unresolved_model_warns_once_naming_auto(self, caplog, tmp_path):
        # 'auto' collapses to "" through to_acp_id — nothing is pinned and the
        # overlay cannot be keyed. The gate names it 'auto' (the DEFAULT_MODEL
        # sentinel the backend resolves itself), matching the spawn-side
        # effort_dropped verdict so one drop event reads as one event.
        msgs = self._drop_warnings(
            caplog,
            tmp_path,
            session_key="dashboard:1",
            model_override="auto",
            reasoning_effort_override="max",
        )
        assert len(msgs) == 1
        assert "'auto'" in msgs[0]
        assert "'max'" in msgs[0]

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


class TestExplicitDefaultColdStart:
    """A pending explicit Default makes the pre-spawn projection remove the entry once.

    The pending projection does to ``cli.json`` what ``clear_effort(owned_only=False)``
    does on a live provider with the same backend, model and resolved level, and
    the provider reports it applied only when that projection succeeded.
    """

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=kwargs.pop("acp_backend", ACP_BACKEND_KIRO),
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    def _write(work_dir, data):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(json.dumps(data), encoding="utf-8")
        return cli_json

    def _operator_entry(self, work_dir, level, model=None):
        model = model or self._MODEL
        entry = {effort_settings_key(model): {"effort": level}}
        return self._write(work_dir, {"chat.modelDefaults": {model: entry}})

    @pytest.mark.asyncio
    async def test_start_removes_an_operator_entry_once_when_explicit_default_is_pending(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True

        # A later spawn of the same provider (a resume, a model swap) projects
        # with the ownership guard again.
        self._operator_entry(tmp_path, "high")
        await provider.start()
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}

    @pytest.mark.asyncio
    async def test_start_keeps_an_operator_entry_without_explicit_default(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is False

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_a_refused_file_leaves_the_default_pending_for_the_next_start(
        self, tmp_path, monkeypatch
    ):
        # A linked cli.json is never rewritten, so the projection fails and the
        # intent is not spent. The next start that can rewrite the file applies it.
        target = tmp_path / "outside.json"
        outside = {"chat.modelDefaults": {self._MODEL: {"output_config": {"effort": "max"}}}}
        target.write_text(json.dumps(outside), encoding="utf-8")
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        try:
            cli_json.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert provider.explicit_effort_default_applied is False
        assert json.loads(target.read_text(encoding="utf-8")) == outside

        cli_json.unlink()
        self._operator_entry(tmp_path, "max")
        await provider.start()

        assert provider.explicit_effort_default_applied is True
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_the_rewrite_runs_inside_its_fence_and_the_spawn_after_it(
        self, tmp_path, monkeypatch
    ):
        # The fence refuses warm-runtime claims while it is up and dates the
        # rewrite as it comes down: the write must be inside it, and the rest of
        # the start, which can take seconds, outside it.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        events: list[str] = []

        @contextmanager
        def fence():
            events.append("fence up")
            try:
                yield
            finally:
                events.append("fence down")

        real_atomic_write = acp_provider.atomic_write

        def record_write(path, contents, **kwargs):
            events.append("write")
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", record_write)
        monkeypatch.setattr(
            provider, "_start_kiro_runtime", AsyncMock(side_effect=lambda: events.append("spawn"))
        )
        provider.arm_explicit_effort_default(fence)

        await provider.start()

        assert events == ["fence up", "write", "fence down", "spawn"]
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_rewrite_still_knows_it_ran(
        self, tmp_path, monkeypatch
    ):
        # A cold start cancelled here arms the session's flag again unless the
        # provider reports the replacement ran, and a flag armed after the
        # removal removes the next level anyone projects for this model.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        fence_down_after_the_worker: list[bool] = []
        real_apply = provider._apply_effort_overlay

        def blocked_apply(**kwargs):
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
                return real_apply(**kwargs)
            finally:
                worker_finished.set()

        @contextmanager
        def fence():
            try:
                yield
            finally:
                fence_down_after_the_worker.append(worker_finished.is_set())

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_apply)
        provider.arm_explicit_effort_default(fence)
        start_task = asyncio.create_task(provider.start())
        try:
            assert await asyncio.to_thread(worker_started.wait, 5)
            start_task.cancel()
            await asyncio.sleep(0)
            assert not start_task.done()
            assert fence_down_after_the_worker == []
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert fence_down_after_the_worker == [True]
        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True
        provider._start_kiro_runtime.assert_not_awaited()

    def test_no_level_removes_both_family_entries_only_for_explicit_default(self, tmp_path):
        cli_json = self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {
                        "output_config": {"effort": "max"},
                        "reasoning": {"effort": "high"},
                    }
                },
                "other": 1,
            },
        )
        provider = self._provider(tmp_path)

        assert provider._apply_effort_overlay() is True
        guarded = json.loads(cli_json.read_text(encoding="utf-8"))
        assert guarded["chat.modelDefaults"][self._MODEL]["output_config"] == {"effort": "max"}
        assert guarded["chat.modelDefaults"][self._MODEL]["reasoning"] == {"effort": "high"}

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        entry = data.get("chat.modelDefaults", {}).get(self._MODEL, {})
        assert "effort" not in entry.get("output_config", {})
        assert "effort" not in entry.get("reasoning", {})
        assert data["other"] == 1

    def test_a_resolved_level_replaces_an_operator_entry(self, tmp_path):
        cli_json = self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "high"})

        assert provider._apply_effort_overlay(replace_unowned=True) is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {self._MODEL: "high"}

    def test_the_auto_model_keeps_the_ownership_guard(self, tmp_path):
        # Under auto no concrete model is known before kiro-cli reads the file,
        # so, as for a live auto session, only entries Kiro Crew recorded go.
        owned_model = "claude-sonnet-4.6"
        stamp = 1_700_000_000
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    owned_model: {"output_config": {"effort": "low"}},
                    self._MODEL: {"output_config": {"effort": "max"}},
                },
                _KIROCREW_EFFORT_OWNED_KEY: {owned_model: "low"},
                "kirocrew.effortOwnedStamp": stamp,
            },
        )
        os.utime(tmp_path / ".kiro" / "settings" / "cli.json", (stamp, stamp))
        provider = self._provider(tmp_path, model="auto")

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

    @pytest.mark.parametrize(
        ("model", "backend"),
        [("claude-haiku-4.5", ACP_BACKEND_KIRO), ("claude-opus-4.7", "claude")],
        ids=["model-without-effort", "backend-that-reads-no-overlay"],
    )
    def test_nothing_to_remove_counts_as_applied(self, tmp_path, model, backend):
        cli_json = self._operator_entry(tmp_path, "max", model=model)
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model=model, acp_backend=backend)

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert cli_json.read_bytes() == before


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
