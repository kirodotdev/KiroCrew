"""The stat-only fingerprints of an agents directory apply the roster's own entry
rules, and leave skill-view aliases out exactly as the roster does.

``_dir_signature`` guards the ``list_agents``, project-names and parsed-specs
caches; ``agents_dir_revision`` pins the ``AgentsDirMemo`` answers of the KAS
projection and the tool-policy read. Both walk ``_iter_spec_entries``, and the
roster they protect (``iter_agent_spec_files``) leaves out every managed
``kirocrew-skill-view-*`` alias. A fingerprint that stat'ed those aliases would
cost one ``stat`` per alias per call -- on the event loop, in the spawn path --
and would move on every alias write, invalidating caches whose contents an alias
cannot change. So an alias is neither stat'ed nor named by either fingerprint,
while a real spec write still moves both. The one entry the fingerprints keep
and the roster drops is a Markdown spec shadowed by its JSON twin: a superset
of the roster, never less.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery import clear_list_agents_cache, list_agents
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX, iter_agent_spec_files

SPEC = "reviewer"
ALIAS = f"{NATIVE_SKILL_ALIAS_PREFIX}0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _isolated_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    clear_list_agents_cache()
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_RACY_WINDOW_NS", 0)
    yield
    clear_list_agents_cache()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _bump_mtime(path: Path, seconds: int = 5) -> None:
    """Move *path*'s mtime *seconds* into the past: whole seconds, so a write is
    never lost inside one filesystem timestamp tick, and backwards, so the
    revision's racy-window rule never sees a fresh entry."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - seconds * 1_000_000_000))


class _StatSpy:
    """A ``DirEntry`` stand-in that records the name of every entry ``stat``'ed."""

    def __init__(self, entry: os.DirEntry[str], seen: list[str]) -> None:
        self._entry = entry
        self._seen = seen

    def stat(self, *args: Any, **kwargs: Any) -> os.stat_result:
        self._seen.append(self._entry.name)
        return self._entry.stat(*args, **kwargs)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._entry, attr)


class _SpyingScandir:
    def __init__(self, inner: Any, seen: list[str]) -> None:
        self._inner = inner
        self._seen = seen

    def __enter__(self) -> "_SpyingScandir":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._inner.close()

    def __iter__(self) -> "_SpyingScandir":
        return self

    def __next__(self) -> _StatSpy:
        return _StatSpy(next(self._inner), self._seen)


def _spy_entry_stats(monkeypatch: pytest.MonkeyPatch, directory: Path) -> list[str]:
    """Record the name of every entry of *directory* that a ``scandir`` walk stats."""
    seen: list[str] = []
    real_scandir = os.scandir

    def spying_scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        inner = real_scandir(path, *args, **kwargs)
        if isinstance(path, int) or Path(os.fspath(path)) != directory:
            return inner
        return _SpyingScandir(inner, seen)

    monkeypatch.setattr(agent_discovery.os, "scandir", spying_scandir)
    return seen


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    _write_json(d / f"{SPEC}.json", {"name": SPEC, "model": "auto"})
    return d


# --- _dir_signature ------------------------------------------------------------


def test_an_alias_write_leaves_the_signature_unchanged_and_is_never_stated(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = agent_discovery._dir_signature(agents_dir)

    alias = agents_dir / f"{ALIAS}.json"
    _write_json(alias, {"name": ALIAS, "resources": []})
    _bump_mtime(alias)
    seen = _spy_entry_stats(monkeypatch, agents_dir)

    after = agent_discovery._dir_signature(agents_dir)

    assert after == before
    assert all(not name.startswith(NATIVE_SKILL_ALIAS_PREFIX) for name, _mtime in after)
    assert f"{SPEC}.json" in seen, "the spy must see the walk it is spying on"
    assert alias.name not in seen


def test_a_real_spec_write_still_moves_the_signature(agents_dir: Path) -> None:
    before = agent_discovery._dir_signature(agents_dir)

    _bump_mtime(agents_dir / f"{SPEC}.json")
    edited = agent_discovery._dir_signature(agents_dir)
    assert edited != before

    _write_json(agents_dir / "second.json", {"name": "second", "model": "auto"})
    assert agent_discovery._dir_signature(agents_dir) != edited


def test_the_signature_names_the_roster_plus_the_shadowed_twin_and_no_alias(
    agents_dir: Path,
) -> None:
    (agents_dir / "prose.md").write_text("---\nname: prose\n---\nbody\n", encoding="utf-8")
    (agents_dir / f"{SPEC}.md").write_text("---\nname: twin\n---\nbody\n", encoding="utf-8")
    _write_json(agents_dir / f"{ALIAS}.json", {"name": ALIAS})
    (agents_dir / f"{ALIAS}abc.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    roster = {path.name for path in iter_agent_spec_files(agents_dir)}
    fingerprinted = {name for name, _mtime in agent_discovery._dir_signature(agents_dir)}

    assert roster == {f"{SPEC}.json", "prose.md"}
    assert fingerprinted == roster | {f"{SPEC}.md"}


def test_alias_churn_does_not_invalidate_the_list_agents_cache(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert [a.name for a in list_agents(agents_dir=agents_dir)] == [SPEC]

    alias = agents_dir / f"{ALIAS}.json"
    _write_json(alias, {"name": ALIAS, "resources": []})
    _bump_mtime(alias)
    reads: list[Path] = []
    real_read = agent_discovery._read_agent_spec

    def recording_read(path: Path, **kwargs: Any) -> Any:
        reads.append(path)
        return real_read(path, **kwargs)

    monkeypatch.setattr(agent_discovery, "_read_agent_spec", recording_read)

    assert [a.name for a in list_agents(agents_dir=agents_dir)] == [SPEC]
    assert reads == [], "an alias write must be served from the roster cache"


# --- agents_dir_revision -----------------------------------------------------


def test_the_revision_neither_stats_nor_names_an_alias(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = agents_dir / f"{ALIAS}.json"
    _write_json(alias, {"name": ALIAS, "resources": []})
    before = agent_discovery.agents_dir_revision(agents_dir)
    assert before is not None

    _bump_mtime(alias)
    seen = _spy_entry_stats(monkeypatch, agents_dir)
    after = agent_discovery.agents_dir_revision(agents_dir)

    assert after == before
    assert [entry[0] for entry in after[1]] == [f"{SPEC}.json"]
    assert f"{SPEC}.json" in seen
    assert alias.name not in seen


def test_aliases_do_not_count_toward_the_revision_entry_cap(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_REVISION_MAX_ENTRIES", 2)
    monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_REVISION_OVERFLOW_WARNED", set())
    for n in range(5):
        _write_json(agents_dir / f"{NATIVE_SKILL_ALIAS_PREFIX}{n:024x}.json", {"name": str(n)})

    revision = agent_discovery.agents_dir_revision(agents_dir)

    assert revision is not None
    assert [entry[0] for entry in revision[1]] == [f"{SPEC}.json"]


def test_a_real_spec_write_still_moves_the_revision(agents_dir: Path) -> None:
    before = agent_discovery.agents_dir_revision(agents_dir)
    _bump_mtime(agents_dir / f"{SPEC}.json")
    after = agent_discovery.agents_dir_revision(agents_dir)
    assert before is not None and after is not None
    assert after != before
