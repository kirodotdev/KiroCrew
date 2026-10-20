"""Shared fixtures and test doubles for the ``test_agent_discovery*`` suite.

Split out of ``test_agent_discovery.py`` (file-line-cap split) so every sibling
file imports one copy instead of duplicating these -- duplication here would be
a behavioural risk if one copy later drifts from the others.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# caplog collects records from EVERY logger, not just the one at_level() names, so
# a negative "logged no warning" assertion must filter by logger: an unrelated
# neighbour's asyncio "Task was destroyed" record otherwise lands in the window
# and fails the assertion depending on how the suite is sharded.
_DISCOVERY_LOGGER = "kiro_crew.agent_discovery"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


def _project_agents_dir(root: Path) -> Path:
    d = root / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


def _discovery_warnings(caplog):
    """WARNING+ records from the discovery logger about a systematic scan failure.

    Filtered by logger name per the module-top note, and by message so the
    pre-existing shadowing/duplicate warnings never contaminate the count.
    """
    return [
        r
        for r in caplog.records
        if r.name == _DISCOVERY_LOGGER
        and r.levelno >= logging.WARNING
        and "parsed 0" in r.getMessage()
    ]


class _FakeScandir:
    """A descriptor listing for tests that must execute without OS ``dir_fd`` support."""

    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *_exc: object) -> None:
        return None


def _mock_pinned_directory(
    monkeypatch: pytest.MonkeyPatch, *, real_path: Path, entries: list[object]
) -> dict[str, object]:
    """Drive the pinned branch on every OS with one held-descriptor test double."""
    import kiro_crew.agent_discovery as discovery_mod

    fd = 7301
    state: dict[str, object] = {"fd": fd, "opens": [], "scans": [], "closes": []}

    def fake_open(parent, name, **kwargs):
        state["opens"].append((parent, name, kwargs))  # type: ignore[union-attr]
        return fd

    def fake_scandir(target):
        state["scans"].append(target)  # type: ignore[union-attr]
        assert target == fd, "the scan re-opened the directory by name"
        return _FakeScandir(entries)

    os_double = SimpleNamespace(**vars(os))
    os_double.scandir = fake_scandir
    os_double.close = lambda held_fd: state["closes"].append(held_fd)  # type: ignore[union-attr]
    monkeypatch.setattr(discovery_mod, "os", os_double)
    monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", fake_open)
    monkeypatch.setattr(discovery_mod, "fd_real_path", lambda held_fd: str(real_path))
    return state


def _fake_pinned_project_walk(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    """Force the pinned walk over *project*'s real ``.kiro``/``.kiro/agents`` on every OS.

    Forcing ``supports_pinned_walk`` alone is not enough: the pinned branch then
    calls ``pinned_fs.pin_parent`` and opens with ``os.O_DIRECTORY``, a flag real
    Windows does not have, so that force alone raises ``AttributeError`` there
    instead of reaching the contract under test. ``open_in_pinned_parent``, the
    descriptor listing and the descriptor-relative spec read are substituted, so
    the pinned branch never reaches that primitive on any platform. A by-name
    walk (``scandir`` on a path) still reads the real directory.
    """
    import kiro_crew.agent_discovery as discovery_mod

    fd_by_path: dict[str, int] = {}
    entries_by_fd: dict[int, list[os.DirEntry[str]]] = {}
    for fd, directory in enumerate((project / ".kiro", project / ".kiro" / "agents"), start=7304):
        with os.scandir(directory) as scan:
            entries_by_fd[fd] = list(scan)
        fd_by_path[os.path.normcase(os.path.normpath(os.fspath(directory)))] = fd
    real_by_fd = {fd: path for path, fd in fd_by_path.items()}

    def fake_open(parent, name, **_kwargs):
        return fd_by_path[os.path.normcase(os.path.normpath(os.path.join(parent, name)))]

    real_scandir = os.scandir
    os_double = SimpleNamespace(**vars(os))
    os_double.scandir = lambda target: (
        _FakeScandir(entries_by_fd[target]) if isinstance(target, int) else real_scandir(target)
    )
    os_double.close = lambda _fd: None
    monkeypatch.setattr(discovery_mod, "os", os_double)
    monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", fake_open)
    monkeypatch.setattr(discovery_mod, "fd_real_path", lambda fd: real_by_fd[fd])
    monkeypatch.setattr(
        discovery_mod,
        "_read_agent_spec_from_descriptor",
        lambda path, **_kwargs: json.loads(path.read_text()),
    )
