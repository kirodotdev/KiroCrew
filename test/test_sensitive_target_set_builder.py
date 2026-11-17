"""The sensitive target set is the same set, however cheaply it is built.

``security.paths_targets`` anchors every declared sensitive leaf under ``$HOME`` and
under each home override, lexically and through ``realpath``. The path gates compare
a candidate against that set, so a builder that drops or adds one member changes
what is fenced, and nothing else would say so.

These tests hold the builder to a reference: the build written out one path at a
time, asking ``os.path.realpath`` about each path on its own and joining every
spelling where it is used. The reference is slow and plain on purpose. The two are
built over fixture homes -- shallow and deep, leaves present and missing, a linked
leaf, a relative link, a dangling link, a link loop, a linked folder on the way to a
leaf, a linked ``$HOME``, every home override -- and must come out as the same set
with the same ``resolution_differed`` answer. A link repointed between two builds,
or a folder swapped for a link, must move both the same way: the builder reads each
folder once per build and keeps nothing for the next.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.security import paths_lists, paths_targets
from kiro_crew.security.paths_targets import _ResolvedRoots

_LISTS = {
    "read": list(paths_lists._SENSITIVE_HOME_DIRS),
    "write": list(paths_lists._SENSITIVE_HOME_DIRS) + list(paths_lists._WRITE_PROTECTED_HOME_PATHS),
    "keystone": list(paths_lists._KEYSTONE_ARTIFACT_PARENTS),
}

_OVERRIDE_ENVS = (
    "KIROCREW_HOME",
    "KIRO_HOME",
    "KIROCREW_OS_HOME",
    *paths_targets._ADAPTER_ROOT_ENVS,
)


def _real(path: str) -> str | None:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _reference_targets(home_dirs: list[str], roots: _ResolvedRoots) -> tuple[set[str], bool]:
    """The target set and whether any resolved path came back spelled differently.

    One ``realpath`` per path, one join per use, no shared work: what the builder
    has to equal, not how it has to get there.
    """
    segments = paths_lists._leaf_segments
    home = roots.home
    resolved: dict[str, str | None] = {}

    def resolve(path: str) -> str | None:
        if path not in resolved:
            resolved[path] = _real(path)
        return resolved[path]

    def anchor(root: str, entry: str) -> str:
        return os.path.join(root, *segments(entry)).casefold()

    def both_separators(root: str, entry: str) -> set[str]:
        parts = segments(entry)
        return {
            os.path.join(root, *parts).casefold(),
            "/".join([root.rstrip("/\\"), *parts]).casefold(),
            "\\".join([root.rstrip("/\\"), *parts]).casefold(),
        }

    targets = {anchor(home, entry) for entry in home_dirs}
    if roots.os_home:
        for entry in home_dirs:
            targets |= both_separators(roots.os_home, entry)
        os_home_real = resolve(roots.os_home) or roots.os_home
        if os_home_real.casefold() != roots.os_home.casefold():
            for entry in home_dirs:
                targets |= both_separators(os_home_real, entry)
    home_real = resolve(home) or home
    if home_real.casefold() != home.casefold():
        targets |= {anchor(home_real, entry) for entry in home_dirs}
    if roots.logical_home.casefold() != home.casefold():
        targets |= {anchor(roots.logical_home, entry) for entry in home_dirs}
    if roots.crew_home:
        for entry in home_dirs:
            for prefix in paths_lists._CREW_HOME_PREFIXES:
                if entry == prefix or entry.startswith(prefix + "/"):
                    leaf = entry[len(prefix) :].lstrip("/")
                    full = (
                        os.path.join(roots.crew_home, *segments(leaf)) if leaf else roots.crew_home
                    )
                    targets.add(full.casefold())
                    full_real = resolve(full)
                    if full_real is not None:
                        targets.add(full_real.casefold())
                    break
    for entry in home_dirs:
        full_real = resolve(os.path.join(home, *segments(entry)))
        if full_real is not None:
            targets.add(full_real.casefold())
    if roots.kiro_home:
        for leaf in paths_lists._KIRO_CLI_WRITE_TIER_LEAVES:
            if leaf not in home_dirs:
                continue
            tail = segments(leaf)[1:]
            full = os.path.join(roots.kiro_home, *tail) if tail else roots.kiro_home
            targets.add(full.casefold())
            full_real = resolve(full)
            if full_real is not None:
                targets.add(full_real.casefold())
    adapter_roots = dict(roots.adapter_roots)
    for leaf, root_envs, under_root in paths_targets._OVERRIDE_ANCHORED_LEAVES:
        if leaf not in home_dirs:
            continue
        for env in root_envs:
            root = adapter_roots.get(env)
            if not root:
                continue
            full = os.path.join(root, *segments(under_root))
            targets.add(full.casefold())
            full_real = resolve(full)
            if full_real is not None:
                targets.add(full_real.casefold())
    differed = any(
        value is not None
        and os.path.normcase(os.path.normpath(value)) != os.path.normcase(os.path.normpath(key))
        for key, value in resolved.items()
    )
    return targets, differed


def _roots(monkeypatch, logical_home: Path, **overrides: Path) -> _ResolvedRoots:
    """The roots for *logical_home* with exactly *overrides* set among the home overrides."""
    for name in _OVERRIDE_ENVS:
        if name != "KIROCREW_HOME":
            monkeypatch.delenv(name, raising=False)
    # The suite pins KIROCREW_HOME per test; it is repointed here, never removed.
    overrides.setdefault("KIROCREW_HOME", logical_home / ".kiro" / "crew")
    for name, value in overrides.items():
        monkeypatch.setenv(name, str(value))
    return paths_targets._resolve_root_anchors(str(logical_home))


def _assert_same_set(roots: _ResolvedRoots) -> dict[str, set[str]]:
    built_sets: dict[str, set[str]] = {}
    for name, home_dirs in _LISTS.items():
        built = paths_targets._home_dir_targets_uncached(home_dirs, roots)
        expected, differed = _reference_targets(home_dirs, roots)
        assert set(built) == expected, name
        assert built.resolution_differed is differed, name
        built_sets[name] = set(built)
    return built_sets


def _make_leaves(home: Path, every: int = 1) -> None:
    """Create every *every*-th read leaf under *home* as a folder."""
    for entry in _LISTS["read"][::every]:
        (home / entry).mkdir(parents=True, exist_ok=True)


def _deep(tmp_path: Path) -> Path:
    home = tmp_path.joinpath(*"abcdefghij")
    home.mkdir(parents=True)
    return home


@pytest.mark.parametrize("deep", [False, True], ids=["shallow", "deep"])
@pytest.mark.parametrize("every", [0, 1, 3], ids=["no-leaves", "all-leaves", "some-leaves"])
def test_a_plain_home_builds_the_reference_set(monkeypatch, tmp_path, deep, every):
    home = _deep(tmp_path) if deep else tmp_path / "home"
    home.mkdir(exist_ok=True)
    if every:
        _make_leaves(home, every)
    sets = _assert_same_set(_roots(monkeypatch, home))
    assert len(sets["read"]) >= len(_LISTS["read"])


@requires_symlinks
@pytest.mark.parametrize("deep", [False, True], ids=["shallow", "deep"])
def test_a_home_with_links_builds_the_reference_set(monkeypatch, tmp_path, deep):
    home = _deep(tmp_path) if deep else tmp_path / "home"
    home.mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "aws").mkdir(parents=True)
    (elsewhere / "ssh").mkdir()
    (elsewhere / "config" / "gcloud").mkdir(parents=True)
    os.symlink(elsewhere / "aws", home / ".aws")  # a linked leaf
    os.symlink(os.path.relpath(elsewhere / "ssh", home), home / ".ssh")  # a relative link
    os.symlink(tmp_path / "nowhere", home / ".gnupg")  # a dangling link
    os.symlink(home / ".azure", home / ".gpg")  # a loop of two links
    os.symlink(home / ".gpg", home / ".azure")
    os.symlink(elsewhere / "config", home / ".config")  # a linked folder above leaves
    (home / ".kiro" / "crew").mkdir(parents=True)
    roots = _roots(monkeypatch, home)
    sets = _assert_same_set(roots)
    assert str(elsewhere / "aws").casefold() in sets["read"]
    assert str(elsewhere / "config" / "gcloud").casefold() in sets["read"]
    assert paths_targets._home_dir_targets_uncached(_LISTS["read"], roots).resolution_differed


@requires_symlinks
def test_a_link_that_leads_back_through_itself_builds_the_reference_set(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.symlink(home, home / ".alias")
    os.symlink(home / ".alias" / ".config" / "..", home / ".config")
    os.symlink(home / ".config", home / ".aws")
    (home / ".kiro" / "crew").mkdir(parents=True)
    _assert_same_set(_roots(monkeypatch, home))


@requires_symlinks
def test_a_linked_home_builds_the_reference_set(monkeypatch, tmp_path):
    real_home = tmp_path / "local" / "home" / "someone"
    real_home.mkdir(parents=True)
    _make_leaves(real_home, 4)
    logical_home = tmp_path / "home-link"
    os.symlink(real_home, logical_home)
    roots = _roots(monkeypatch, logical_home)
    assert roots.home != roots.logical_home
    sets = _assert_same_set(roots)
    assert str(logical_home / ".ssh").casefold() in sets["read"]
    assert str(real_home / ".ssh").casefold() in sets["read"]


@requires_symlinks
def test_every_home_override_builds_the_reference_set(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    crew = tmp_path / "custom-crew"
    (crew / "real-sessions").mkdir(parents=True)
    os.symlink(crew / "real-sessions", crew / "sessions")  # a linked leaf under the override
    kiro = tmp_path / "custom-kiro"
    (kiro / "agents").mkdir(parents=True)
    pod_home = tmp_path / "pod-home"
    pod_home.mkdir()
    adapters = {}
    for index, name in enumerate(paths_targets._ADAPTER_ROOT_ENVS):
        adapters[name] = tmp_path / f"adapter-{index}"
        adapters[name].mkdir()
    roots = _roots(
        monkeypatch,
        home,
        KIROCREW_HOME=crew,
        KIRO_HOME=kiro,
        KIROCREW_OS_HOME=pod_home,
        **adapters,
    )
    assert roots.crew_home and roots.kiro_home and roots.os_home
    assert all(root for _env, root in roots.adapter_roots)
    sets = _assert_same_set(roots)
    assert str(kiro / "agents").casefold() in sets["write"]
    assert str(kiro / "agents").casefold() not in sets["read"]
    assert str(pod_home / ".ssh").casefold() in sets["read"]


@requires_symlinks
def test_a_repointed_link_moves_both_builds_the_same_way(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    os.symlink(first, home / ".aws")
    roots = _roots(monkeypatch, home)
    before = _assert_same_set(roots)["read"]
    assert str(first).casefold() in before and str(second).casefold() not in before
    (home / ".aws").unlink()
    os.symlink(second, home / ".aws")
    after = _assert_same_set(roots)["read"]
    assert str(second).casefold() in after and str(first).casefold() not in after


@requires_symlinks
def test_a_folder_swapped_for_a_link_between_two_builds_is_seen_by_the_second(
    monkeypatch, tmp_path
):
    """Nothing read in one build is kept for the next: the second reads the link."""
    home = tmp_path / "home"
    (home / ".config" / "gcloud").mkdir(parents=True)
    moved = tmp_path / "moved-config"
    (moved / "gcloud").mkdir(parents=True)
    roots = _roots(monkeypatch, home)
    before = _assert_same_set(roots)["read"]
    assert str(moved / "gcloud").casefold() not in before
    (home / ".config" / "gcloud").rmdir()
    (home / ".config").rmdir()
    os.symlink(moved, home / ".config")
    after = _assert_same_set(roots)["read"]
    assert str(moved / "gcloud").casefold() in after
    assert str(home / ".config" / "gcloud").casefold() in after


@requires_symlinks
def test_roots_spelled_with_dot_segments_and_extra_slashes_build_the_reference_set(
    monkeypatch, tmp_path
):
    home = tmp_path / "home"
    home.mkdir()
    crew = tmp_path / "custom-crew"
    crew.mkdir()
    kiro = tmp_path / "custom-kiro"
    kiro.mkdir()
    os.symlink(kiro, tmp_path / "kiro-link")
    spelled = {
        "KIROCREW_HOME": f"{tmp_path}/x/../custom-crew//",
        "KIRO_HOME": f"{tmp_path}/./kiro-link/",
        "KIROCREW_OS_HOME": f"{tmp_path}//missing/../pod-home/.",
    }
    for name in _OVERRIDE_ENVS:
        if name != "KIROCREW_HOME":
            monkeypatch.delenv(name, raising=False)
    for name, value in spelled.items():
        monkeypatch.setenv(name, value)
    roots = paths_targets._resolve_root_anchors(str(home))
    assert roots.crew_home == os.path.realpath(spelled["KIROCREW_HOME"])
    assert roots.kiro_home == os.path.realpath(spelled["KIRO_HOME"]) == str(kiro)
    assert roots.os_home == os.path.realpath(spelled["KIROCREW_OS_HOME"])
    _assert_same_set(roots)
