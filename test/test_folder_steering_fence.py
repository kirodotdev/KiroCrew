"""The memory-store fence fails CLOSED while the ``workspaces`` table is unknown.

The fence folder steering applies at admission and at collection is built from
the configuration's ``workspaces`` table, which can place a Global V1 memory
workspace at an absolute directory anywhere. When a load could not read that
table -- an unparseable ``config.json``, a non-object ``workspaces`` value, or
the load raising outright -- the default directories are NOT the fence: a
declared steering root that merely contains the operator's external workspace
would carry that person's memory Markdown into a member's prompt with nothing
going red. These pins hold every consumer to refusing, not narrowing.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew import folder_steering
from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG, DEGRADED_WORKSPACES
from kiro_crew.dashboard.chat_folders import _validate_steering_dirs
from kiro_crew.folder_steering import (
    collect_folder_steering,
    crosses_memory_silo,
    memory_silo_fence,
    render_folder_steering,
)

_needs_pinned_walk = pytest.mark.skipif(
    not folder_steering.pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-relative directory opens are unavailable on this host",
)


def _cfg(*sections: str) -> KiroCrewConfig:
    return replace(KiroCrewConfig(), _degraded_sections=frozenset(sections))


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


@_needs_pinned_walk
@pytest.mark.parametrize(
    "shape",
    ["whole-config-unreadable", "workspaces-table-unreadable", "load-raises"],
)
def test_validate_fails_closed_while_the_fence_is_incomplete(tmp_path, monkeypatch, shape):
    """Admission refuses a harmless root on every degraded shape and names why."""
    if shape == "whole-config-unreadable":
        cfg = _cfg(DEGRADED_WHOLE_CONFIG)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    elif shape == "workspaces-table-unreadable":
        cfg = _cfg(DEGRADED_WORKSPACES)
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    else:

        def _boom(cls):
            raise RuntimeError("config exploded")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    harmless = tmp_path / "standards"
    harmless.mkdir()
    resolved, err = _validate_steering_dirs([str(harmless)])
    assert resolved == []
    assert err and "fence is incomplete" in err, err
    # Clearing never reaches the fence, so a degraded config can still REMOVE
    # steering -- it just cannot add any.
    assert _validate_steering_dirs([]) == ([], None)


@_needs_pinned_walk
def test_validate_admits_the_same_root_when_the_fence_is_complete(tmp_path, monkeypatch):
    """The control for the fail-closed pin: an intact config admits the root."""
    cfg = _cfg()
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    harmless = tmp_path / "standards"
    harmless.mkdir()
    resolved, err = _validate_steering_dirs([str(harmless)])
    assert err is None and resolved == [str(harmless.resolve())]


def test_crosses_memory_silo_clears_nothing_on_a_degraded_fence(tmp_path, monkeypatch):
    """Without explicit silos the predicate builds the fence itself; an unknown
    fence answers ``True`` for every root."""
    cfg = _cfg(DEGRADED_WORKSPACES)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    fence = memory_silo_fence()
    assert not fence.complete and "workspaces" in fence.degraded
    assert crosses_memory_silo(tmp_path / "anywhere")
    # The same root against the incomplete ROOTS alone is not flagged -- which
    # is exactly why a caller must refuse on ``degraded`` before using them.
    assert not crosses_memory_silo(tmp_path / "anywhere", fence.roots)


def test_fence_is_complete_and_names_configured_workspaces_on_an_intact_load(tmp_path, monkeypatch):
    from kiro_crew.config.sections import WorkspaceConfig

    research = tmp_path / "elsewhere" / "research-ws"
    research.mkdir(parents=True)
    cfg = _cfg()
    cfg.workspaces["research"] = WorkspaceConfig(dir=str(research))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    fence = memory_silo_fence()
    assert fence.complete
    assert research.resolve() in fence.roots


@_needs_pinned_walk
def test_collect_reads_nothing_and_says_so_while_the_fence_is_incomplete(tmp_path, monkeypatch):
    """The collector fails closed IN-BAND: no document is read, the section
    carries a ``fence`` omission notice, and the control read succeeds once the
    fence is complete."""
    root = tmp_path / "standards"
    root.mkdir()
    (root / "rule.md").write_text("ALWAYS-RULE-SENTINEL", encoding="utf-8")
    intact = _cfg()
    degraded = _cfg(DEGRADED_WORKSPACES)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: degraded))
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert list(docs) == []
    assert [o.kind for o in docs.omissions] == ["fence"]
    assert docs.omissions[0].count == 1
    section = render_folder_steering(docs)
    assert "ALWAYS-RULE-SENTINEL" not in section
    assert "memory-store fence is incomplete" in section
    assert "no folder steering is loaded" in section
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: intact))
    docs = collect_folder_steering([str(root)], project=None, home=_fake_home(tmp_path))
    assert [body for _, body in docs] == ["ALWAYS-RULE-SENTINEL"]
    assert docs.omissions == []


def test_fence_resolves_every_workspace_from_one_config_snapshot(tmp_path, monkeypatch):
    """ONE load, ONE snapshot: no per-name reload can diverge from ``degraded``.

    ``workspace_dir_for(name)`` re-loads the config for each name and silently
    falls back to the base directory when that load disagrees with the first.
    If the fence used it, a concurrent config write between the two reads would
    leave an operator's external workspace out of ``roots`` while ``degraded``
    -- judged on the first snapshot -- still read complete. The fence must
    therefore resolve every workspace from the snapshot it already holds and
    load exactly once.
    """
    from kiro_crew.config import loader as L
    from kiro_crew.config.sections import WorkspaceConfig

    research = tmp_path / "elsewhere" / "research-ws"
    research.mkdir(parents=True)
    snapshot = _cfg()
    snapshot.workspaces["research"] = WorkspaceConfig(dir=str(research))
    loads: list[int] = []

    def _load(cls):
        loads.append(1)
        # A SECOND load would see a document with no workspaces at all (the
        # shape a concurrent rewrite or a transient read failure produces).
        return snapshot if len(loads) == 1 else _cfg()

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))

    def _forbidden(*args, **kwargs):
        raise AssertionError("the fence must not resolve a workspace through a reload")

    monkeypatch.setattr(L, "workspace_dir_for", _forbidden)
    monkeypatch.setattr(folder_steering, "workspace_dir_for", _forbidden, raising=False)
    fence = memory_silo_fence()
    assert loads == [1], "exactly one config load"
    assert fence.complete
    assert research.resolve() in fence.roots
    assert crosses_memory_silo(research / "notes", fence.roots)


def test_loader_marks_a_non_object_workspaces_table_as_degraded(tmp_path, monkeypatch):
    """The loader REPORTS a malformed ``workspaces`` value instead of silently
    replacing it, so the fence can tell "no workspaces configured" apart from
    "the workspaces could not be read"."""
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    cfgp.write_text(json.dumps({"workspaces": []}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    assert DEGRADED_WORKSPACES in cfg.degraded_sections
    # The malformed table contributes nothing; only the synthesized default remains.
    assert set(cfg.workspaces) <= {"default"}
    cfgp.write_text(json.dumps({"workspaces": {}}), encoding="utf-8")
    assert DEGRADED_WORKSPACES not in KiroCrewConfig.load().degraded_sections
