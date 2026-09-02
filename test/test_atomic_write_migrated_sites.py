"""Regression tests for the two writers migrated onto the shared atomic helper.

``ChannelManager._save_channel`` and ``model_registry.persist_kiro_windows`` both
hand-rolled a temp-write-and-rename whose temp filename was DERIVED FROM THE
DESTINATION (``<id>.json.tmp`` / ``model_windows.json.tmp``). Two writers
targeting the same file therefore shared one temp name, so a payload could be
published half-written or a rename could fail outright, and neither site got the
bounded retry ``atomic_write`` applies to the Windows rename window.

The load-bearing assertions here are the "occupied temp name" ones. They occupy
the deterministic temp path with a DIRECTORY, which is that collision expressed
deterministically: the hand-rolled ``open(tmp, "w")`` raises ``IsADirectoryError``
and — because both sites swallow their own write errors — the destination
silently never appears. ``atomic_write`` picks a unique ``mkstemp`` name and is
unaffected. Reverting either site to the hand-rolled form fails those tests, so
they cannot pass by accident.

The remaining assertions pin the semantics the migration had to PRESERVE, since a
shared helper makes it easy to change them by accident: the umask default file
mode (neither narrowed to ``0o600`` nor widened) and no leftover temp file.
Durability is unchanged too — both sites are best-effort by contract and neither
passed ``fsync``, which is asserted by requiring ``fsync`` to stay absent from
the recorded call kwargs.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import kiro_crew.channel as channel_mod
from kiro_crew import model_registry as mr
from kiro_crew.channel import ChannelManager


def _umask_default_mode() -> int:
    """The mode ``open(path, "w")`` would have produced under this umask."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestChannelSaveUsesSharedHelper:
    @pytest.fixture(autouse=True)
    def _channels_dir(self, tmp_path):
        self._root = tmp_path / "channels"
        self._root.mkdir()
        self._dir = str(self._root)

    def test_save_routes_through_atomic_write(self, monkeypatch):
        """The persist path must call the shared helper, not its own rename."""
        calls: list[tuple[str, dict]] = []
        original = channel_mod.atomic_write

        def recording(path, content, **kwargs):
            calls.append((str(path), kwargs))
            original(path, content, **kwargs)

        monkeypatch.setattr(channel_mod, "atomic_write", recording)

        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("topic")
        assert ch is not None
        assert ch.add_agent(role="Tester", agent_name="a", task="t") is not None

        assert calls, "channel persist did not go through atomic_write"
        assert all(p.endswith(f"{ch.id}.json") for p, _ in calls)
        # Durability/permissions come from the helper's defaults: the hand-rolled
        # site passed neither fsync nor an explicit mode, so neither may appear.
        for _, kwargs in calls:
            assert "fsync" not in kwargs
            assert "mode" not in kwargs

    def test_save_survives_an_occupied_deterministic_temp_name(self):
        """A directory squatting on ``<id>.json.tmp`` must not lose the write.

        This is the collision the shared helper exists to remove. The
        hand-rolled writer opened that exact path, so this fails on revert.
        """
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("topic")
        assert ch is not None
        target = self._root / f"{ch.id}.json"
        squatter = self._root / f"{ch.id}.json.tmp"
        squatter.mkdir()

        assert ch.add_agent(role="Tester", agent_name="a", task="t") is not None

        assert target.is_file(), "the persist was lost to the occupied temp name"
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["id"] == ch.id
        assert any(m["role"] == "Tester" for m in data["members"].values())
        assert squatter.is_dir(), "the helper must not have touched the squatter"

    def test_save_keeps_umask_default_mode_and_leaves_no_temp(self):
        """The migration must not widen or narrow the published file mode."""
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("topic")
        assert ch is not None
        assert ch.add_agent(role="Tester", agent_name="a", task="t") is not None
        target = self._root / f"{ch.id}.json"

        assert target.is_file()
        if os.name != "nt":  # POSIX permission bits only
            assert _mode_of(target) == _umask_default_mode()
        assert list(self._root.glob("*.tmp")) == []


class TestPersistKiroWindowsUsesSharedHelper:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        saved = dict(mr._KIRO_WINDOWS)
        mr._KIRO_WINDOWS.clear()
        mr._KIRO_WINDOWS["probe-model-zzz"] = 272_000
        yield
        mr._KIRO_WINDOWS.clear()
        mr._KIRO_WINDOWS.update(saved)

    def test_persist_routes_through_atomic_write(self, monkeypatch):
        calls: list[tuple[str, dict]] = []
        original = mr.atomic_write

        def recording(path, content, **kwargs):
            calls.append((str(path), kwargs))
            original(path, content, **kwargs)

        monkeypatch.setattr(mr, "atomic_write", recording)

        mr.persist_kiro_windows()

        assert calls, "persist_kiro_windows did not go through atomic_write"
        assert calls[0][0].endswith("model_windows.json")
        assert "fsync" not in calls[0][1]
        assert "mode" not in calls[0][1]

    def test_persist_survives_an_occupied_deterministic_temp_name(self):
        """A directory on ``model_windows.json.tmp`` must not lose the cache.

        The hand-rolled writer wrote ``path.with_suffix(".json.tmp")`` and
        swallowed the resulting ``IsADirectoryError`` as an ``OSError``, so the
        sidecar never appeared. This fails on revert.
        """
        path = mr._kiro_windows_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        squatter = path.with_suffix(".json.tmp")
        squatter.mkdir()

        mr.persist_kiro_windows()

        assert path.is_file(), "the persist was lost to the occupied temp name"
        assert json.loads(path.read_text(encoding="utf-8"))["probe-model-zzz"] == 272_000
        assert squatter.is_dir(), "the helper must not have touched the squatter"

    def test_persist_creates_the_parent_and_keeps_umask_default_mode(self):
        """The helper owns the ``mkdir`` the site used to perform by hand."""
        path = mr._kiro_windows_cache_path()

        mr.persist_kiro_windows()

        assert path.is_file()
        if os.name != "nt":
            assert _mode_of(path) == _umask_default_mode()
        assert list(path.parent.glob("*.json.tmp")) == []
