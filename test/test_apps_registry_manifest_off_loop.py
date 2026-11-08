"""``_fetch_app_manifest`` reads ``app.json`` off the event loop on both paths.

The local-checkout fast path and the throwaway-clone path each probe the
manifest with ``is_file()`` before reading it. Both the probe and the read are
blocking filesystem calls, so they run in one ``asyncio.to_thread`` hop. These
tests pin that, and pin that a missing, a directory-shaped and a present
manifest behave exactly as they did when the probe ran on the loop.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from apps_registry_coverage_helpers import _fake_sandbox, _FakeProc

from kiro_crew.apps import registry

_GIT_URL = "https://github.com/o/demo.git"


@pytest.fixture
def probe_threads(monkeypatch):
    """Record, per ``app.json`` probe, whether it ran on the main (loop) thread."""
    seen: list[bool] = []
    real_is_file = Path.is_file

    def _recording_is_file(self, *args, **kwargs):
        if self.name == "app.json":
            seen.append(threading.current_thread() is threading.main_thread())
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _recording_is_file)
    return seen


def _local_checkout(monkeypatch, tmp_path, *, origin_matches: bool = True) -> tuple[Path, list]:
    """Point ``app_source_dir`` at a fresh checkout and stub the git probes.

    Returns the checkout dir and the list each origin check appends to, so a
    test can assert whether the origin subprocess would have been spawned.
    """
    src = tmp_path / "app-sources" / "demo"
    src.mkdir(parents=True)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)
    origin_calls: list[str] = []

    async def _origin(dest, git_url):
        origin_calls.append(git_url)
        return origin_matches

    async def _branch(dest, branch):
        return True

    monkeypatch.setattr(registry, "_clone_origin_matches", _origin)
    monkeypatch.setattr(registry, "_clone_branch_matches", _branch)
    return src, origin_calls


async def _fetch_local(git_url: str = _GIT_URL):
    return await registry._fetch_app_manifest("o/demo", "main", app_name="demo", git_url=git_url)


def _tmp_clone_dir(monkeypatch, tmp_path) -> Path:
    import tempfile

    target = tmp_path / "clone"
    target.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: str(target))
    return target


async def _fetch_clone():
    return await registry._fetch_app_manifest("o/demo", "main", git_url=_GIT_URL)


class TestLocalCheckoutManifestOffLoop:
    @pytest.mark.asyncio
    async def test_present_manifest_is_returned_and_probed_off_loop(
        self, monkeypatch, tmp_path, probe_threads
    ):
        src, origin_calls = _local_checkout(monkeypatch, tmp_path)
        (src / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

        assert await _fetch_local() == {"name": "demo"}
        assert origin_calls == [_GIT_URL]
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_missing_manifest_falls_through_without_the_origin_check(
        self, monkeypatch, tmp_path, probe_threads
    ):
        # A non-cloneable URL ends the fall-through in None rather than a clone.
        _src, origin_calls = _local_checkout(monkeypatch, tmp_path)

        assert await _fetch_local(git_url="bare-name") is None
        # No manifest, so the origin check (a git subprocess) is still skipped.
        assert origin_calls == []
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_directory_named_app_json_falls_through(
        self, monkeypatch, tmp_path, probe_threads
    ):
        src, origin_calls = _local_checkout(monkeypatch, tmp_path)
        (src / "app.json").mkdir()

        assert await _fetch_local(git_url="bare-name") is None
        assert origin_calls == []
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_origin_mismatch_never_returns_the_local_manifest(
        self, monkeypatch, tmp_path, probe_threads
    ):
        src, origin_calls = _local_checkout(monkeypatch, tmp_path, origin_matches=False)
        (src / "app.json").write_text(json.dumps({"name": "stale"}), encoding="utf-8")

        assert await _fetch_local(git_url="bare-name") is None
        assert origin_calls == ["bare-name"]
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_unreadable_manifest_falls_through(self, monkeypatch, tmp_path):
        src, _origin_calls = _local_checkout(monkeypatch, tmp_path)
        (src / "app.json").write_bytes(b"\xff\xfe not utf-8")

        assert await _fetch_local(git_url="bare-name") is None


class TestCloneManifestOffLoop:
    @pytest.mark.asyncio
    async def test_present_manifest_is_returned_and_probed_off_loop(
        self, monkeypatch, tmp_path, probe_threads
    ):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])

        assert await _fetch_clone() == {"name": "demo"}
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_missing_manifest_returns_none_and_is_probed_off_loop(
        self, monkeypatch, tmp_path, probe_threads
    ):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])

        assert await _fetch_clone() is None
        assert probe_threads == [False]

    @pytest.mark.asyncio
    async def test_directory_named_app_json_returns_none(
        self, monkeypatch, tmp_path, probe_threads
    ):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").mkdir()
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])

        assert await _fetch_clone() is None
        assert probe_threads == [False]
