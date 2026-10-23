"""The workspace ``cli.json`` is read at most ``_CLI_JSON_MAX_BYTES`` and never on the loop.

A repository can track ``.kiro/settings/cli.json`` at any size. The provider
constructor runs on the gateway event loop, and every overlay writer reads and
rewrites the whole file under the shared settings lock, so an unbounded file blocked
every session and grew the process by a multiple of its size. A file past the ceiling
is now left unread and unchanged: no key of Kiro Crew's lands in it, and the writers
say so instead of replacing the person's settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers import acp_cli_overlay
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_cli_overlay import (
    _CLI_JSON_MAX_BYTES,
    _clear_cli_overlay_effort,
    _OverlayTooLarge,
    _read_cli_overlay,
    _write_cli_overlay,
    _write_tool_search_overlay,
)

MODEL = "claude-opus-4.7"
HUGE = 256 * 1024 * 1024


def _overlay(work_dir: Path) -> Path:
    return work_dir / ".kiro" / "settings" / "cli.json"


def _sparse(work_dir: Path, size: int) -> Path:
    """A ``size``-byte cli.json that costs no disk: reading it all would still cost the RAM."""
    path = _overlay(work_dir)
    path.parent.mkdir(parents=True)
    with path.open("wb") as handle:
        handle.truncate(size)
    return path


def _padded(work_dir: Path, size: int, document: dict) -> Path:
    """A valid JSON document padded with trailing whitespace to exactly ``size`` bytes."""
    path = _overlay(work_dir)
    path.parent.mkdir(parents=True)
    body = json.dumps(document).encode()
    path.write_bytes(body + b" " * (size - len(body)))
    return path


def _fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _provider(work_dir: Path, backend: str = ACP_BACKEND_KIRO, **kwargs: object) -> AcpProvider:
    return AcpProvider(acp_backend=backend, work_dir=work_dir, model=MODEL, **kwargs)


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def test_the_ceiling_is_one_mebibyte() -> None:
    assert _CLI_JSON_MAX_BYTES == 1024 * 1024


class TestReaderIsBounded:
    def test_a_file_past_the_ceiling_reads_as_empty_without_being_read(self, tmp_path):
        _sparse(tmp_path, HUGE)
        tracemalloc.start()
        try:
            assert _read_cli_overlay(tmp_path) == {}
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # The ceiling plus one byte at most, never a multiple of the file.
        assert peak < 4 * _CLI_JSON_MAX_BYTES

    def test_a_file_exactly_at_the_ceiling_is_still_read(self, tmp_path):
        doc = {"chat.modelDefaults": {MODEL: {"output_config": {"effort": "max"}}}}
        _padded(tmp_path, _CLI_JSON_MAX_BYTES, doc)
        assert _read_cli_overlay(tmp_path) == {MODEL: "max"}

    def test_one_byte_past_the_ceiling_is_refused(self, tmp_path):
        doc = {"chat.modelDefaults": {MODEL: {"output_config": {"effort": "max"}}}}
        _padded(tmp_path, _CLI_JSON_MAX_BYTES + 1, doc)
        assert _read_cli_overlay(tmp_path) == {}

    def test_a_byte_order_mark_still_decodes(self, tmp_path):
        path = _overlay(tmp_path)
        path.parent.mkdir(parents=True)
        doc = {"chat.modelDefaults": {MODEL: {"reasoning": {"effort": "low"}}}}
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode())
        assert _read_cli_overlay(tmp_path) == {MODEL: "low"}


class TestWritersLeaveAnOversizedFileUnchanged:
    def test_the_effort_writer_refuses_and_leaves_the_file(self, tmp_path):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        with pytest.raises(_OverlayTooLarge):
            _write_cli_overlay(tmp_path, MODEL, "max")
        assert _fingerprint(path) == before

    def test_the_tool_search_writer_refuses_and_leaves_the_file(self, tmp_path):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        with pytest.raises(_OverlayTooLarge):
            _write_tool_search_overlay(tmp_path, True)
        assert _fingerprint(path) == before

    def test_a_refusal_does_not_grow_the_process_by_the_file_size(self, tmp_path):
        _sparse(tmp_path, HUGE)
        tracemalloc.start()
        try:
            with pytest.raises(_OverlayTooLarge):
                _write_cli_overlay(tmp_path, MODEL, "max")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 4 * _CLI_JSON_MAX_BYTES

    def test_the_clear_answers_false_and_leaves_the_file(self, tmp_path):
        # The file may still name a level for the model: the clear did not happen.
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        assert _clear_cli_overlay_effort(tmp_path, MODEL) is False
        assert _fingerprint(path) == before

    def test_a_file_at_the_ceiling_is_still_rewritten(self, tmp_path):
        _padded(tmp_path, _CLI_JSON_MAX_BYTES, {"keep": "me"})
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(_overlay(tmp_path).read_text(encoding="utf-8"))
        assert data["keep"] == "me"
        assert data["toolSearch.enabled"] is True

    def test_a_rewrite_that_would_pass_the_ceiling_is_refused(self, tmp_path):
        # Indented, one small integer per line is several times its compact size, so a
        # file under the ceiling can publish one over it -- which the next read would
        # then refuse. The writer declines instead of leaving that file behind.
        path = _overlay(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"x": [1] * 200_000}, separators=(",", ":")), encoding="utf-8")
        assert path.stat().st_size < _CLI_JSON_MAX_BYTES
        before = path.read_bytes()
        for write in (
            lambda: _write_cli_overlay(tmp_path, MODEL, "max"),
            lambda: _write_tool_search_overlay(tmp_path, True),
        ):
            with pytest.raises(_OverlayTooLarge):
                write()
            assert path.read_bytes() == before

    def test_a_malformed_small_file_is_still_replaced(self, tmp_path):
        path = _overlay(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        _write_cli_overlay(tmp_path, MODEL, "max")
        assert _read_cli_overlay(tmp_path) == {MODEL: "max"}


class TestProviderLeavesAnOversizedFileAlone:
    def test_the_effort_apply_answers_false_and_never_raises(self, tmp_path):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        provider = _provider(tmp_path, effort_per_model={MODEL: "max"})
        assert provider._apply_effort_overlay() is False
        assert _fingerprint(path) == before

    def test_the_tool_search_apply_never_raises(self, tmp_path):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        provider = _provider(tmp_path, tool_search=True)
        provider._apply_tool_search_overlay()
        assert _fingerprint(path) == before

    @pytest.mark.asyncio
    async def test_recovery_seeds_nothing_from_an_oversized_file(self, tmp_path):
        _sparse(tmp_path, HUGE)
        provider = _provider(tmp_path)
        await provider._recover_effort_overlay()
        assert provider._effort_per_model == {}


class TestConstructionOpensNoFile:
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_huge_file_costs_the_constructor_nothing(self, tmp_path, backend):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        real_open = Path.open
        real_os_open = os.open
        touched: list[str] = []

        def _path_open(self, *args, **kwargs):
            if self.name == "cli.json":
                touched.append(str(self))
            return real_open(self, *args, **kwargs)

        def _os_open(target, *args, **kwargs):
            if Path(os.fsdecode(target)).name == "cli.json":
                touched.append(str(target))
            return real_os_open(target, *args, **kwargs)

        tracemalloc.start()
        try:
            with patch.object(Path, "open", _path_open), patch("os.open", _os_open):
                provider = _provider(tmp_path, backend, effort_per_model={MODEL: "max"})
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert touched == []
        assert peak < HUGE // 16
        assert _fingerprint(path) == before
        assert provider._effort_per_model == {MODEL: "max"}


class TestStartWritesOffTheLoop:
    @pytest.mark.asyncio
    async def test_the_recovery_read_and_both_writes_run_in_worker_threads(self, tmp_path):
        provider = _provider(tmp_path, effort_per_model={MODEL: "max"}, tool_search=True)
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def _track(name, real):
            def _wrapped(*args, **kwargs):
                seen[name] = threading.get_ident()
                return real(*args, **kwargs)

            return _wrapped

        with (
            patch.object(
                acp_cli_overlay,
                "_read_cli_overlay",
                _track("read", acp_cli_overlay._read_cli_overlay),
            ),
            patch.object(
                acp_cli_overlay,
                "_write_cli_overlay",
                _track("effort", acp_cli_overlay._write_cli_overlay),
            ),
            patch.object(
                acp_cli_overlay,
                "_write_tool_search_overlay",
                _track("search", acp_cli_overlay._write_tool_search_overlay),
            ),
            patch.object(AcpProvider, "_start_kiro_runtime", _noop),
            patch.object(AcpProvider, "_note_zero_tool_servers", _noop),
            patch.object(AcpProvider, "_bind_unbound_effort", _noop),
            patch.object(AcpProvider, "_apply_initial_effort", _noop),
        ):
            await provider.start()

        assert set(seen) == {"read", "effort", "search"}
        assert loop_thread not in seen.values()
        assert asyncio.get_running_loop() is not None

    @pytest.mark.asyncio
    async def test_start_survives_an_oversized_file_and_leaves_it(self, tmp_path):
        path = _sparse(tmp_path, HUGE)
        before = _fingerprint(path)
        provider = _provider(tmp_path, effort_per_model={MODEL: "max"}, tool_search=True)

        with (
            patch.object(AcpProvider, "_start_kiro_runtime", _noop),
            patch.object(AcpProvider, "_note_zero_tool_servers", _noop),
            patch.object(AcpProvider, "_bind_unbound_effort", _noop),
            patch.object(AcpProvider, "_apply_initial_effort", _noop),
        ):
            await provider.start()

        assert _fingerprint(path) == before
