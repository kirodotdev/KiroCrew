"""cli.json overlay tests: one file per work dir, shared by every session there.

A session's own pick is never read back from the shared overlay, a picked level
survives a peer's clear, and the async effort paths keep work-directory
filesystem calls off the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from effort_helpers import _UNPARSEABLE_OVERLAY_CONTENTS, _read_cli_overlay
from test_effort import _await_test

from conftest import requires_symlinks
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.effort import effort_settings_key
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers.acp import (
    _KIROCREW_EFFORT_OWNED_KEY,
    _clear_cli_overlay_effort,
    _write_cli_overlay,
)
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock


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

        with pytest.raises(RuntimeError, match="a link or in a folder outside the work dir"):
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
            await async_wait_until(held.is_set, describe=lambda: "held was never set")
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
            await async_wait_until(
                worker_started.is_set, describe=lambda: "worker_started was never set"
            )
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
                    await _await_test(start_task, "start_task")

        assert start_task is not None
        await async_wait_until(
            worker_finished.is_set, describe=lambda: "worker_finished was never set"
        )
        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")
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
            await async_wait_until(
                worker_started.is_set, describe=lambda: "worker_started was never set"
            )
            for _ in range(3):
                start_task.cancel()
                await asyncio.sleep(0)
                assert not start_task.done()
            assert not worker_finished.is_set()
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")
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
            await async_wait_until(
                worker_started.is_set, describe=lambda: "worker_started was never set"
            )
            clear_task.cancel()
            await asyncio.sleep(0)
            assert not clear_task.done()
            assert fence_down_after_the_worker == []
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await _await_test(clear_task, "clear_task")
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
