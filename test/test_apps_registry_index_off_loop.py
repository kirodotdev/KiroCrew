"""``_fetch_external_registry_index`` reads the clone's index off the event loop.

The three reads the fetch makes on the cloned registry -- the
``app-registry.json`` probe, its read, and the ``apps/*/app.json`` fallback
scan -- are blocking filesystem calls. They run in one ``asyncio.to_thread``
hop. These tests pin that, and pin that a present index, an empty index, a
missing index and a directory-shaped ``apps`` entry keep the outcomes the
inline version had.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.registry_pipeline.indexes import (
    _fetch_external_registry_index,
    _load_index_entries_sync,
)

_REPO_URL = "https://github.com/example/CoolApp.git"


@pytest.fixture
def probe_threads(monkeypatch):
    """Record, per registry-disk probe, whether it ran on the main (loop) thread."""
    seen: list[bool] = []
    real_is_file = Path.is_file
    real_is_dir = Path.is_dir

    def _recording_is_file(self, *args, **kwargs):
        if self.name in ("app-registry.json", "app.json"):
            seen.append(threading.current_thread() is threading.main_thread())
        return real_is_file(self, *args, **kwargs)

    def _recording_is_dir(self, *args, **kwargs):
        if self.name == "apps":
            seen.append(threading.current_thread() is threading.main_thread())
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _recording_is_file)
    monkeypatch.setattr(Path, "is_dir", _recording_is_dir)
    return seen


@pytest.fixture(autouse=True)
def stub_sandbox_and_sel(_floor_monkeypatch):
    """Stub the sandbox wrap and SEL so a probe test never spawns real git.

    Patches through ``_floor_monkeypatch``, not the test's shared
    monkeypatch: an ``undo()`` in a test must not lift the sandbox stub.
    """

    async def _passthrough_wrap(argv, **kwargs):
        return list(argv), None

    _floor_monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.indexes.wrap_argv_async", _passthrough_wrap
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.indexes.cgroup_scope_argv", lambda argv: argv
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.indexes.wrap_argv",
        lambda argv, **k: (list(argv), None),
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.apps.registry_pipeline.indexes._sel_fn",
        MagicMock(),
        raising=False,
    )


def _clone_proc():
    """A successful ``git clone`` subprocess stub."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 0
    return mock_proc


def _point_clone(monkeypatch, clone_dir: Path, build=None):
    """Point the fetch at ``clone_dir``: stub mkdtemp + the clone subprocess."""

    import kiro_crew.apps.registry_pipeline.indexes as _idx

    # The real mkdtemp hands the fetch an existing directory, so the stub does
    # too: build() -- like git clone -- writes into an existing root.
    clone_dir.mkdir(parents=True, exist_ok=True)

    async def _fake_create_subprocess_limited(*args, **kwargs):
        if build is not None:
            build(clone_dir)
        return _clone_proc()

    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: str(clone_dir))
    monkeypatch.setattr(_idx, "create_subprocess_limited", _fake_create_subprocess_limited)


class TestLoadIndexEntriesSync:
    def test_index_wins_over_fallback(self, tmp_path):
        (tmp_path / "app-registry.json").write_text(
            json.dumps([{"name": "from-index"}]), encoding="utf-8"
        )
        apps = tmp_path / "apps" / "from-tree"
        apps.mkdir(parents=True)
        (apps / "app.json").write_text("{}", encoding="utf-8")
        index_entries, fallback_entries = _load_index_entries_sync(tmp_path, "r", "main")
        assert index_entries == [{"name": "from-index"}]
        assert fallback_entries == []

    def test_empty_index_is_a_valid_index_not_a_fallback_trigger(self, tmp_path):
        (tmp_path / "app-registry.json").write_text("[]", encoding="utf-8")
        index_entries, fallback_entries = _load_index_entries_sync(tmp_path, "r", "main")
        assert index_entries == []
        assert fallback_entries == []

    def test_malformed_index_reaches_the_fallback_scan(self, tmp_path):
        (tmp_path / "app-registry.json").write_text("{broken", encoding="utf-8")
        apps = tmp_path / "apps" / "my-tool"
        apps.mkdir(parents=True)
        (apps / "app.json").write_text('{"name": "my-tool"}', encoding="utf-8")
        index_entries, fallback_entries = _load_index_entries_sync(
            tmp_path, "example/MyRepo", "main"
        )
        assert index_entries is None
        assert fallback_entries == [
            {
                "name": "my-tool",
                "repo": "example/MyRepo",
                "branch": "main",
                "subdirectory": "apps/my-tool",
            }
        ]

    def test_missing_app_json_rows_are_skipped(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()
        (apps / "app.json").mkdir()  # a DIRECTORY named app.json: not an app row
        index_entries, fallback_entries = _load_index_entries_sync(tmp_path, "r", "main")
        assert index_entries is None
        assert fallback_entries == []


class TestFetchReadsCloneOffTheLoop:
    @pytest.mark.asyncio
    async def test_index_read_probes_off_loop(self, monkeypatch, tmp_path, probe_threads):
        """The index path reads through the threaded helper, probes off the loop."""
        clone_dir = tmp_path / "clone"
        _point_clone(
            monkeypatch,
            clone_dir,
            build=lambda target: (target / "app-registry.json").write_text(
                json.dumps([{"name": "cool-app"}]), encoding="utf-8"
            ),
        )

        result = await _fetch_external_registry_index(_REPO_URL, "main")
        assert result == [{"name": "cool-app"}]
        # The index probe, its read's file probe and any fallback probe ran off
        # the main thread; the fetch's own clone stub stayed on the loop.
        assert probe_threads
        assert not any(probe_threads)

    @pytest.mark.asyncio
    async def test_fallback_scan_probes_off_loop(self, monkeypatch, tmp_path, probe_threads):
        clone_dir = tmp_path / "clone"

        def build(target: Path) -> None:
            apps = target / "apps" / "my-tool"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / "app.json").write_text('{"name": "my-tool"}', encoding="utf-8")

        _point_clone(monkeypatch, clone_dir, build=build)

        result = await _fetch_external_registry_index(_REPO_URL, "main")
        assert result is not None
        assert result[0]["name"] == "my-tool"
        assert result[0]["subdirectory"] == "apps/my-tool"
        assert probe_threads
        assert not any(probe_threads)

    @pytest.mark.asyncio
    async def test_empty_clone_is_none_without_loop_probes(self, monkeypatch, tmp_path):
        """An empty clone: no index, no apps -> None, as the inline version behaved."""
        clone_dir = tmp_path / "clone"
        _point_clone(monkeypatch, clone_dir)

        assert await _fetch_external_registry_index(_REPO_URL, "main") is None
