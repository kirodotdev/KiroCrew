"""Tests for agent discovery in ``agent_discovery.py``.

Focus on the robustness/security guards around scanning ``~/.kiro/agents/*.json``:
- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- A ``*.json`` symlink pointing at a sensitive credential file must NOT be read.

Tests use a tmp_path fake $HOME so the real filesystem is never touched.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from windows_link_screen_helpers import simulate_windows_link_screen

from conftest import requires_symlinks
from kiro_crew import agent_state
from kiro_crew.agent_discovery import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    AgentInfo,
    AmbiguousAgentSpecError,
    clear_list_agents_cache,
    clear_project_agent_cache,
    list_agents,
    project_agent_files,
    project_agent_name,
    project_agent_names,
    spec_by_declared_name,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX

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


class TestProjectScopeDiscovery:
    """Project-local ``<project>/.kiro`` agents, mirroring kiro-cli's workspace scope."""

    def test_project_agent_is_discovered_and_marked(self, fake_home, tmp_path):
        """An agent only in the project appears, tagged with the project scope."""
        d = _agents_dir(fake_home)
        (d / "userlevel.json").write_text(json.dumps({"name": "userlevel"}))
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "repobot.json").write_text(json.dumps({"name": "repobot"}))

        clear_list_agents_cache()
        agents = {a.name: a for a in list_agents(agents_dir=d, project_dir=str(proj))}
        assert set(agents) == {"userlevel", "repobot"}
        assert agents["repobot"].scope == SCOPE_PROJECT
        assert agents["userlevel"].scope == SCOPE_GLOBAL

    def test_omitting_project_dir_keeps_user_level_only(self, fake_home, tmp_path):
        """No project dir means no project scan — the pre-existing contract."""
        d = _agents_dir(fake_home)
        (d / "userlevel.json").write_text(json.dumps({"name": "userlevel"}))
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "repobot.json").write_text(json.dumps({"name": "repobot"}))

        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d)] == ["userlevel"]

    def test_project_agent_shadows_user_level_of_same_name(self, fake_home, tmp_path):
        """One entry survives, and it is the project one kiro-cli would actually run."""
        d = _agents_dir(fake_home)
        (d / "dup.json").write_text(json.dumps({"name": "dup", "description": "user level"}))
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "dup.json").write_text(
            json.dumps({"name": "dup", "description": "project level"})
        )

        clear_list_agents_cache()
        agents = [a for a in list_agents(agents_dir=d, project_dir=str(proj)) if a.name == "dup"]
        assert len(agents) == 1
        assert agents[0].scope == SCOPE_PROJECT
        assert agents[0].description == "project level"

    def test_declared_name_wins_over_filename(self, fake_home, tmp_path):
        """kiro-cli lists an agent by its declared name, so discovery must match."""
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "file-stem.json").write_text(json.dumps({"name": "declared"}))

        clear_list_agents_cache()
        names = [a.name for a in list_agents(agents_dir=d, project_dir=str(proj))]
        assert names == ["declared"]

    def test_native_skill_view_specs_are_not_listed_as_project_agents(self, fake_home, tmp_path):
        """``kirocrew-skill-view-*`` is the projection's own machine namespace.

        ``iter_agent_spec_files`` has always dropped these for the user-level
        scan, and ``_require_unshadowed_templates`` documents the omission as
        discovery's contract. The project scan reaches the twin rule through
        ``split_listed_spec_paths``, which carries only that rule, so the alias
        half is applied at the call site — without it a checkout could plant a
        view into the very roster the projection reads to decide what to
        project, and offer it as a pickable agent.
        """
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        agents = _project_agents_dir(proj)
        digest = "0" * 24
        alias_json = agents / f"{NATIVE_SKILL_ALIAS_PREFIX}{digest}.json"
        alias_json.write_text(json.dumps({"name": f"{NATIVE_SKILL_ALIAS_PREFIX}{digest}"}))
        alias_md = agents / f"{NATIVE_SKILL_ALIAS_PREFIX}md{digest}.md"
        alias_md.write_text(f"---\nname: {NATIVE_SKILL_ALIAS_PREFIX}md{digest}\n---\nPrompt\n")
        # A real project agent beside them: the scan must still work, so an
        # empty result cannot pass this test by accident.
        (agents / "repobot.json").write_text(json.dumps({"name": "repobot"}))

        clear_list_agents_cache()
        clear_project_agent_cache()
        leaked = [f for f in project_agent_files(str(proj)) if NATIVE_SKILL_ALIAS_PREFIX in f.name]
        assert leaked == [], (
            f"project scan listed the native skill-view alias(es) "
            f"{[f.name for f in leaked]} as project agent specs; "
            f"iter_agent_spec_files drops this namespace for the user-level scan"
        )
        assert [f.name for f in project_agent_files(str(proj))] == ["repobot.json"]
        assert project_agent_names(str(proj)) == frozenset({"repobot"})
        names = {a.name for a in list_agents(agents_dir=d, project_dir=str(proj))}
        assert not {
            n for n in names if n.startswith(NATIVE_SKILL_ALIAS_PREFIX)
        }, f"roster offered a native skill-view alias as a pickable agent: {sorted(names)}"

    def test_legacy_spec_is_not_offered_as_a_dispatchable_agent(self, fake_home, tmp_path):
        """``.agent-spec.json`` is not a location kiro-cli reads, so it must not be
        offered anywhere an agent gets dispatched — the picker would accept the name
        and the backend would then fail to activate the mode."""
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        kiro = proj / ".kiro"
        kiro.mkdir(parents=True)
        (kiro / "legacy.agent-spec.json").write_text(json.dumps({}))

        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d, project_dir=str(proj))] == []
        assert project_agent_files(str(proj)) == []
        assert project_agent_names(str(proj)) == frozenset()

    def test_legacy_spec_is_still_available_to_slack(self, tmp_path):
        """Slack's pre-existing convention keeps working via the opt-in flag."""
        proj = tmp_path / "repo"
        kiro = proj / ".kiro"
        kiro.mkdir(parents=True)
        spec = kiro / "legacy.agent-spec.json"
        spec.write_text(json.dumps({}))
        assert project_agent_files(str(proj), include_legacy=True) == [spec]

    def test_spec_suffix_is_stripped_from_the_fallback_name(self, tmp_path):
        """A spec with no declared name must not resolve as ``<name>.agent-spec``."""
        kiro = tmp_path / "repo" / ".kiro"
        kiro.mkdir(parents=True)
        spec = kiro / "legacy.agent-spec.json"
        spec.write_text(json.dumps({}))
        assert project_agent_name(spec) == "legacy"

    def test_sensitive_project_dir_yields_no_agents(self, tmp_path, monkeypatch):
        """A project path the security gate rejects must not be scanned at all."""
        monkeypatch.setattr(
            "kiro_crew.agent_discovery.is_sensitive_path",
            lambda p: str(p) == str(tmp_path / "secret"),
        )
        sel_events: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.agent_discovery._sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        proj = tmp_path / "secret"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        assert project_agent_files(str(proj)) == []
        # The refused scan must leave a denial trail, not just a debug line.
        assert any(e.get("outcome") == "denied" for e in sel_events), (
            f"sensitive project-dir rejection in project_agent_files must emit a "
            f"SEL denial: {sel_events}"
        )

    def test_list_agents_sensitive_project_dir_denied_before_any_stat(
        self, fake_home, tmp_path, monkeypatch
    ):
        """``list_agents`` refuses a sensitive project dir BEFORE stating under it.

        The sibling pin
        ``TestProjectAgentNameCache.test_sensitive_project_dir_denied_before_any_stat``
        patches ``_project_signature``, which this entry point does not call, so
        it leaves this scope's ordering uncovered.

        Regression: the refusal yielded no project specs and the cache signature
        was then built from ``_dir_signature`` on both project scopes anyway -- a
        ``scandir`` plus a ``stat`` per entry under the tree the refusal had just
        protected, while the recorded outcome said denied. The user-level scope
        still lists, and the one refusal still owes exactly one denial row.
        """
        import kiro_crew.agent_discovery as ad

        d = _agents_dir(fake_home)
        (d / "user-level.json").write_text(json.dumps({"name": "user-level"}))
        # Named "protected", not "secret": CodeQL's clear-text-logging query
        # treats a variable named `secret` as a credential and then follows this
        # plain temp path into the module's existing logs of a REFUSED path,
        # reporting them as new leaks. The refused path is what the trail shows.
        protected = tmp_path / "protected"
        (_project_agents_dir(protected) / "a.json").write_text(json.dumps({"name": "a"}))
        monkeypatch.setattr(
            "kiro_crew.agent_discovery.is_sensitive_path",
            lambda p: str(p) == str(protected),
        )
        real_signature = ad._dir_signature

        def _refuse_under_protected(target):
            # Scoped to the refused tree: the user-level scope legitimately
            # stats, so a blanket failure here would fire on every call and
            # prove nothing about the ordering.
            if str(protected) in str(target):
                pytest.fail(f"signature stat ran under a refused project dir: {target}")
            return real_signature(target)

        monkeypatch.setattr(ad, "_dir_signature", _refuse_under_protected)
        sel_events: list[dict] = []
        monkeypatch.setattr(
            ad,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        clear_list_agents_cache()

        names = [a.name for a in list_agents(agents_dir=d, project_dir=str(protected))]

        assert names == ["user-level"]
        assert [e["outcome"] for e in sel_events] == [
            "denied"
        ], f"one refusal owes exactly one denial row: {sel_events}"

    def test_list_agents_decides_project_sensitivity_exactly_once(
        self, fake_home, tmp_path, monkeypatch
    ):
        """One scope, ONE sensitivity verdict -- for the spec scan and the signature.

        ``list_agents`` takes its own verdict and then scans through the
        unguarded ``_scan_project_agent_files``, so one call decides this scope
        exactly once. A redundant second verdict inside ``project_agent_files``
        is not a stronger guard, because both reduce to
        ``is_sensitive_path(str(project_dir))``,
        which RE-RESOLVES the path on every call -- so the two answers can differ
        and one half of the function proceeds on a verdict the other half
        rejected. Counting the verdicts is what holds the shape: the behavioural
        pin below passes just as well while a redundant guard sits there
        agreeing, and only starts failing once the answers diverge.
        """
        import kiro_crew.agent_discovery as ad

        d = _agents_dir(fake_home)
        (d / "user-level.json").write_text(json.dumps({"name": "user-level"}))
        proj = tmp_path / "proj"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        verdicts: list[str] = []
        real_guard = ad._project_scope_denied

        def _counting_guard(project_dir, **kwargs):
            verdicts.append(str(project_dir))
            return real_guard(project_dir, **kwargs)

        monkeypatch.setattr(ad, "_project_scope_denied", _counting_guard)
        clear_list_agents_cache()

        list_agents(agents_dir=d, project_dir=str(proj))

        decided_once = [str(proj)]
        assert verdicts == decided_once, f"decided sensitivity more than once: {verdicts}"

    def test_list_agents_never_stats_a_scope_a_later_verdict_refuses(
        self, fake_home, tmp_path, monkeypatch
    ):
        """No stat under a scope ANY verdict in the call refused.

        ``is_sensitive_path`` re-resolves the path on every call, so two verdicts
        on one scope can disagree -- a symlink component repointed between them,
        or a fail-closed resolver stall landing on only the later one.
        Regression: when the refusal came second, the first verdict had already
        admitted the scope, so ``project_agent_files`` recorded a denial row and
        the cache signature statted the protected tree anyway -- the exact
        deny-and-read this module's guard exists to prevent.

        The invariant is the conjunction: a call must never both record a denial
        for a scope and stat under it.
        """
        import kiro_crew.agent_discovery as ad

        d = _agents_dir(fake_home)
        (d / "user-level.json").write_text(json.dumps({"name": "user-level"}))
        proj = tmp_path / "proj"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        queries: list[str] = []

        def _sensitive_from_the_second_query(p):
            # Scoped to this scope: the user-level dir legitimately resolves, so
            # answering for every path would refuse scopes this test is not about.
            if str(p) != str(proj):
                return False
            queries.append(str(p))
            return len(queries) >= 2

        monkeypatch.setattr(ad, "is_sensitive_path", _sensitive_from_the_second_query)
        statted: list[str] = []
        real_signature = ad._dir_signature

        def _recording_signature(target):
            if str(proj) in str(target):
                statted.append(str(target))
            return real_signature(target)

        monkeypatch.setattr(ad, "_dir_signature", _recording_signature)
        sel_events: list[dict] = []
        monkeypatch.setattr(
            ad,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        clear_list_agents_cache()

        list_agents(agents_dir=d, project_dir=str(proj))

        denied = [e for e in sel_events if e.get("outcome") == "denied"]
        assert not (denied and statted), (
            "a scope a verdict refused was statted anyway -- one refusal must drop "
            f"the scope whole: denials={denied} stats={statted}"
        )

    def test_missing_project_kiro_dir_is_not_an_error(self, tmp_path):
        """A checkout with no ``.kiro`` yields no agents rather than raising."""
        assert project_agent_files(str(tmp_path / "no-kiro")) == []
        assert project_agent_files(None) == []
        assert project_agent_files("") == []

    @pytest.mark.parametrize(
        ("scan_path", "include_legacy"),
        [(".kiro", True), (".kiro/agents", False)],
    )
    def test_non_directory_scan_scope_is_empty_without_a_denial(
        self, tmp_path, monkeypatch, scan_path, include_legacy
    ):
        """A regular file at either scan scope means nothing to enumerate.

        ``None`` is the sensitive-denied sentinel consumed by
        ``project_agent_files``; an empty iterable is the ordinary no-agents
        outcome. A wrong sentinel here emits a false SEL denial for a harmless
        malformed checkout.
        """
        import kiro_crew.agent_discovery as ad

        proj = tmp_path / "repo"
        occupied = proj / scan_path
        occupied.parent.mkdir(parents=True)
        occupied.write_text("not a directory", encoding="utf-8")
        sel_events: list[dict] = []
        monkeypatch.setattr(
            ad,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )

        assert project_agent_files(str(proj), include_legacy=include_legacy) == []
        assert sel_events == [], f"a non-directory scan scope emitted a denial: {sel_events}"

    def test_project_symlink_to_sensitive_file_is_not_read(self, fake_home, tmp_path):
        """The per-file resolved-target guard applies in the project scope too."""
        d = _agents_dir(fake_home)
        secret = tmp_path / "creds"
        secret.write_text("[default]\naws_access_key_id=AKIAEXAMPLE\n")
        proj = tmp_path / "repo"
        pdir = _project_agents_dir(proj)
        try:
            os.symlink(secret, pdir / "evil.json")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        def _sensitive(p):
            return str(p) == str(secret)

        clear_list_agents_cache()
        import kiro_crew.agent_discovery as ad

        original = ad.is_sensitive_canonical_path
        ad.is_sensitive_canonical_path = _sensitive
        try:
            names = [a.name for a in list_agents(agents_dir=d, project_dir=str(proj))]
        finally:
            ad.is_sensitive_canonical_path = original
        assert names == []

    @requires_symlinks
    def test_project_agents_dir_symlinked_into_sensitive_tree_is_not_scanned(
        self, fake_home, tmp_path
    ):
        """A ``.kiro/agents`` that RESOLVES into a sensitive tree is not enumerated.

        Distinct from the per-file guard: here the SCAN DIRECTORY itself is a
        symlink into a credential home, so a root-only sensitivity check passes
        but ``glob``/``scandir`` would still probe the protected directory. The
        dir-level guard (``_pinned_scan_dir``) must skip it entirely.
        """
        secret_tree = tmp_path / "creds_home"
        secret_tree.mkdir()
        (secret_tree / "leaked.json").write_text(json.dumps({"name": "leaked"}))
        proj = tmp_path / "repo"
        (proj / ".kiro").mkdir(parents=True)
        # <repo>/.kiro/agents -> the credential tree; the repo root is NOT sensitive.
        os.symlink(secret_tree, proj / ".kiro" / "agents")

        import kiro_crew.agent_discovery as ad

        original = ad.is_sensitive_path
        # Only the resolved credential tree is sensitive; the repo root is not.
        ad.is_sensitive_path = lambda p: os.path.realpath(str(p)) == os.path.realpath(
            str(secret_tree)
        )
        sel_events: list[dict] = []
        original_sel = ad._sel
        ad._sel = lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw))
        try:
            clear_list_agents_cache()
            # The glob site must not enumerate the symlinked dir.
            assert project_agent_files(str(proj)) == []
            # The leaked name must never surface through the cached names path.
            assert "leaked" not in project_agent_names(str(proj))
            # And the sensitive SUBDIR skip must leave a denial trail — the root
            # is not sensitive, so this row can only come from the scan-dir guard.
            assert any(
                e.get("outcome") == "denied" for e in sel_events
            ), f"sensitive scan-dir skip must emit a SEL denial: {sel_events}"
        finally:
            ad.is_sensitive_path = original
            ad._sel = original_sel
            clear_list_agents_cache()

    def test_cache_does_not_leak_between_projects(self, fake_home, tmp_path):
        """Two checkouts must not serve each other's agents from one cache entry."""
        d = _agents_dir(fake_home)
        one = tmp_path / "one"
        two = tmp_path / "two"
        (_project_agents_dir(one) / "only-one.json").write_text(json.dumps({"name": "only-one"}))
        (_project_agents_dir(two) / "only-two.json").write_text(json.dumps({"name": "only-two"}))

        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d, project_dir=str(one))] == ["only-one"]
        assert [a.name for a in list_agents(agents_dir=d, project_dir=str(two))] == ["only-two"]


class TestProjectAgentNameCache:
    """The per-turn resolver's name index: correct, cached, and per-project.

    The resolver consults this on EVERY turn of a project-agent-bound session, so a
    repeat call on an unchanged checkout must not re-read the specs.
    """

    def test_returns_declared_names(self, tmp_path):
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "file-stem.json").write_text(json.dumps({"name": "declared"}))
        clear_project_agent_cache()
        assert project_agent_names(str(proj)) == frozenset({"declared"})

    def test_an_oversized_project_key_is_answered_but_never_retained(self, tmp_path):
        """The count cap bounds HOW MANY keys live here, not how large one is.

        The key is the caller's raw ``project_dir`` spelling, so 256 keys of
        unbounded length is unbounded resident memory. A spelling past the named
        character bound is still answered -- it simply is not kept, which costs
        the next call a rescan, exactly what any miss here costs.

        Mutation guard: drop the length check in ``_store_project_agent_names``
        and the oversized key appears in the cache.
        """
        import kiro_crew.agent_discovery as ad

        oversized = "/" + "d" * (ad._PROJECT_NAMES_CACHE_MAX_KEY_CHARS + 1)
        entry = ((), frozenset({"declared"}))
        clear_project_agent_cache()

        ad._store_project_agent_names(oversized, entry)

        assert (
            oversized not in ad._PROJECT_NAMES_CACHE
        ), "an unbounded caller-supplied key must not be retained"

        within = "/" + "d" * 16
        ad._store_project_agent_names(within, entry)
        assert (
            within in ad._PROJECT_NAMES_CACHE
        ), "an ordinary path must still be cached, or every lookup rescans"

    def test_oversized_snapshot_is_answered_completely_but_not_retained(self, tmp_path):
        """Every retained snapshot field has its own cache-owned memory bound.

        A project above the names bound still returns its complete answer to the
        off-loop caller. It simply remains uncached, so the cache cannot retain
        an oversized names set. A separately oversized signature with a tiny
        name set proves the signature rows are independently bounded.

        Mutation guard: remove the two snapshot-size checks in
        ``_store_project_agent_names`` and both oversized entries are retained.
        """
        import kiro_crew.agent_discovery as ad

        names_project = tmp_path / "many-names"
        names_dir = _project_agents_dir(names_project)
        expected = frozenset(
            {f"agent-{index}" for index in range(ad._PROJECT_NAMES_CACHE_MAX_NAMES + 1)}
        )
        for name in expected:
            (names_dir / f"{name}.json").write_text(json.dumps({"name": name}))

        clear_project_agent_cache()

        assert project_agent_names(names_project) == expected
        assert (
            str(names_project) not in ad._PROJECT_NAMES_CACHE
        ), "a complete oversized names set must be returned without being retained"

        signature_key = str(tmp_path / "many-signature-rows")
        oversized_signature = (
            tuple(
                (f"agent-{index}.json", index)
                for index in range(ad._PROJECT_NAMES_CACHE_MAX_SIGNATURE_ROWS + 1)
            ),
        )
        ad._store_project_agent_names(signature_key, (oversized_signature, frozenset({"shared"})))
        assert (
            signature_key not in ad._PROJECT_NAMES_CACHE
        ), "an oversized signature must not be retained even when its names set is small"

    def test_a_truncated_listing_signature_is_never_reused(self, tmp_path):
        """A listing that aborts part-way must not be cached as the directory.

        ``_entries_signature`` describes a PREFIX when the walk raises mid-way.
        Cached bare, that prefix is indistinguishable from a complete scan of a
        smaller directory, so a second identical failure hits the entry and hands
        back names the directory has stopped declaring -- including an agent a
        folder's saved default still points at. The truncation row carries a
        per-call value for exactly that reason: equality must fail against a
        later PARTIAL signature too, not merely against a complete one.

        A walk that collects nothing is NOT a prefix: an empty signature is the
        documented answer for an absent or unlistable directory, so that case
        stays ``()`` and is pinned here beside the truncated one.

        Mutation guard: replace the row append with a bare ``pass`` and
        ``partial == partial`` becomes True, reusing the stale roster.
        """
        agents = _project_agents_dir(tmp_path / "repo")
        (agents / "a.json").write_text(json.dumps({"name": "a"}), encoding="utf-8")
        (agents / "b.json").write_text(json.dumps({"name": "b"}), encoding="utf-8")
        entries = sorted(os.scandir(agents), key=lambda e: e.name)

        import kiro_crew.agent_discovery as ad

        class _AbortsPartWay:
            """Stands in for a listing whose walk raises after the first entry."""

            def __iter__(self):
                yield from entries[:1]
                raise OSError(errno.EIO, "simulated mid-listing failure")

        complete = ad._entries_signature(entries)
        first_partial = ad._entries_signature(_AbortsPartWay())
        second_partial = ad._entries_signature(_AbortsPartWay())

        assert any(
            row[0] == ad._DIR_TRUNCATED_ROW for row in first_partial
        ), f"a truncated listing must say so in its signature: {first_partial}"
        assert complete != first_partial
        assert first_partial != second_partial, (
            "two identical failures must not share a signature, or the second "
            "reuses the first's stale roster"
        )

        class _FailsBeforeYielding:
            """A walk that collects nothing: no directory, not a prefix of one."""

            def __iter__(self):
                raise OSError(errno.EIO, "simulated immediate failure")
                yield  # pragma: no cover - makes this a generator

        assert ad._entries_signature(_FailsBeforeYielding()) == (), (
            "an empty signature is the documented answer for an unlistable "
            "directory and must not pick up a per-call truncation row"
        )

    def test_parses_enumerated_spec_through_held_directory_descriptor(self, tmp_path, monkeypatch):
        """A replacement directory cannot contribute an agent after enumeration.

        The descriptor-pinned walk is the owner-supplied (``raise_unverifiable=True``)
        caller's, so both halves ask for it. Asserted on EVERY platform through the
        held-descriptor double, which
        forces the pinned branch and fails if the scan re-opens the directory by
        name. Where the real ``O_NOFOLLOW``/``dir_fd``/``O_DIRECTORY`` syscalls
        exist the same contract is driven a second time against a real swap, so
        neither the double's fidelity nor the syscalls' behaviour is taken on
        trust -- and a platform without them still runs the first half rather
        than skipping the contract entirely.
        """
        import kiro_crew.agent_discovery as ad

        entry = MagicMock()
        entry.name = "enumerated.json"
        doubled = tmp_path / "repo"
        held = _project_agents_dir(doubled)
        (held / "enumerated.json").write_text(json.dumps({"name": "enumerated"}))
        state = _mock_pinned_directory(monkeypatch, real_path=held, entries=[entry])
        listed: list[list[Path]] = []
        real_split = ad.split_listed_spec_paths

        def _record(directory, paths, **kwargs):
            paths = list(paths)
            listed.append(paths)
            return real_split(directory, iter(paths), **kwargs)

        monkeypatch.setattr(ad, "split_listed_spec_paths", _record)
        clear_project_agent_cache()
        ad.project_agent_names(doubled, raise_unverifiable=True)

        assert state["scans"] and set(state["scans"]) == {
            state["fd"]
        }, f"a scan used a path instead of the held fd: {state['scans']}"
        assert [p.name for p in listed[-1]] == ["enumerated.json"], (
            "the roster's candidate paths came from somewhere other than the "
            f"entries the held descriptor yielded: {listed[-1]}"
        )

        if not (
            hasattr(os, "O_NOFOLLOW")
            and os.open in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
        ):
            return
        monkeypatch.undo()
        project = tmp_path / "real-repo"
        agents_dir = _project_agents_dir(project)
        (agents_dir / "enumerated.json").write_text(json.dumps({"name": "enumerated"}))
        real_split = ad.split_listed_spec_paths

        def _swap_after_enumeration(directory, paths, **kwargs):
            live, shadowed = real_split(directory, paths, **kwargs)
            held_directory = directory.with_name("agents-held")
            directory.rename(held_directory)
            directory.mkdir()
            (directory / "replacement.json").write_text(json.dumps({"name": "replacement"}))
            return live, shadowed

        monkeypatch.setattr(ad, "split_listed_spec_paths", _swap_after_enumeration)
        clear_project_agent_cache()

        assert project_agent_names(project, raise_unverifiable=True) == frozenset({"enumerated"})

    def test_sensitive_project_dir_denied_before_any_stat(self, tmp_path, monkeypatch):
        """A sensitive project dir is rejected BEFORE the signature stats, loudly.

        Regression: the cache path computed `_project_signature` (a stat pair
        under the caller-supplied dir) before `project_agent_files` rejected
        sensitivity — probing a protected tree, and silently: no SEL denial.
        """
        import kiro_crew.agent_discovery as ad

        # Named "protected", not "secret": CodeQL's clear-text-logging query
        # treats a variable named `secret` as a credential and then follows this
        # plain temp path into the module's existing logs of a REFUSED path,
        # reporting them as new leaks. The refused path is what the trail shows.
        protected = tmp_path / "protected"
        (_project_agents_dir(protected) / "a.json").write_text(json.dumps({"name": "a"}))
        monkeypatch.setattr(
            "kiro_crew.agent_discovery.is_sensitive_path",
            lambda p: str(p) == str(protected),
        )
        monkeypatch.setattr(
            ad,
            "_project_signature",
            lambda d: pytest.fail("signature stat ran on a sensitive project dir"),
        )
        sel_events: list[dict] = []
        monkeypatch.setattr(
            ad,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        clear_project_agent_cache()

        assert project_agent_names(str(protected)) == frozenset()
        assert (
            sel_events and sel_events[0]["outcome"] == "denied"
        ), f"sensitive-dir rejection must emit a SEL denial: {sel_events}"

    def test_decides_project_sensitivity_exactly_once(self, tmp_path, monkeypatch):
        """The sibling entry point decides once too, for the same reason.

        ``project_agent_names`` guards, stats its signature, and then scans
        through ``_scan_project_agent_files``, which does not re-decide the
        scope. Were a second verdict taken there, with the signature already
        taken, a refusal would record a denial for a tree this very call had
        just statted.
        """
        import kiro_crew.agent_discovery as ad

        proj = tmp_path / "proj"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        verdicts: list[str] = []
        real_guard = ad._project_scope_denied

        def _counting_guard(project_dir, **kwargs):
            verdicts.append(str(project_dir))
            return real_guard(project_dir, **kwargs)

        monkeypatch.setattr(ad, "_project_scope_denied", _counting_guard)
        clear_project_agent_cache()

        assert project_agent_names(str(proj)) == frozenset({"a"})
        decided_once = [str(proj)]
        assert verdicts == decided_once, f"decided sensitivity more than once: {verdicts}"

    def test_malformed_spec_is_not_dispatchable(self, tmp_path):
        """A file that does not parse must not contribute its filename fallback.

        Regression: a malformed/unreadable spec whose stem matched a stored agent
        name entered the allowlist, so session startup selected a mode kiro-cli
        could never load and failed at set_mode. Only a spec that parses can
        become a mode, so only parsed specs may contribute names.
        """
        proj = tmp_path / "repo"
        pdir = _project_agents_dir(proj)
        (pdir / "good.json").write_text(json.dumps({"name": "good"}))
        (pdir / "broken.json").write_text("{not json")
        (pdir / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        clear_project_agent_cache()
        assert project_agent_names(str(proj)) == frozenset({"good"})

    def test_parsed_spec_without_name_still_uses_filename_fallback(self, tmp_path):
        """The filename fallback survives for VALID specs that omit ``name`` —
        excluding malformed files must not tighten that pre-existing contract."""
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "nameless.json").write_text(json.dumps({"tools": []}))
        clear_project_agent_cache()
        assert project_agent_names(str(proj)) == frozenset({"nameless"})

    def test_repeat_call_does_not_reread_specs(self, tmp_path, monkeypatch):
        """A warm cache costs stats, not reads — the whole point of the index."""
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        clear_project_agent_cache()
        assert project_agent_names(str(proj)) == frozenset({"a"})

        import kiro_crew.agent_discovery as ad

        monkeypatch.setattr(
            ad, "_read_agent_spec", lambda p: pytest.fail("re-read a spec on a warm cache")
        )
        assert project_agent_names(str(proj)) == frozenset({"a"})

    def test_edit_invalidates_the_cache(self, tmp_path):
        """A new spec must be picked up without an explicit cache clear."""
        proj = tmp_path / "repo"
        pdir = _project_agents_dir(proj)
        (pdir / "a.json").write_text(json.dumps({"name": "a"}))
        clear_project_agent_cache()
        assert project_agent_names(str(proj)) == frozenset({"a"})

        b = pdir / "b.json"
        b.write_text(json.dumps({"name": "b"}))
        os.utime(pdir, (0, 0))  # defeat a coarse directory-mtime clock
        os.utime(b, None)
        assert project_agent_names(str(proj)) == frozenset({"a", "b"})

    def test_names_are_per_project(self, tmp_path):
        one, two = tmp_path / "one", tmp_path / "two"
        (_project_agents_dir(one) / "x.json").write_text(json.dumps({"name": "x"}))
        (_project_agents_dir(two) / "y.json").write_text(json.dumps({"name": "y"}))
        clear_project_agent_cache()
        assert project_agent_names(str(one)) == frozenset({"x"})
        assert project_agent_names(str(two)) == frozenset({"y"})

    def test_empty_and_missing_inputs_are_safe(self, tmp_path):
        assert project_agent_names(None) == frozenset()
        assert project_agent_names("") == frozenset()
        assert project_agent_names(str(tmp_path / "nope")) == frozenset()

    @pytest.mark.parametrize(
        "oversized", [False, True], ids=("ordinary-write-site", "oversized-name-write-site")
    )
    def test_cache_is_bounded_across_many_distinct_projects(self, tmp_path, oversized):
        """The cache key is a caller-supplied directory (the dashboard roster
        endpoint's debounced picker fires one scan per path prefix typed), so it
        must not grow one never-evicted entry per distinct path.

        Regression: before the bound, driving more than
        ``_PROJECT_NAMES_CACHE_MAX_KEYS`` distinct project dirs through
        ``project_agent_names`` left every one of them cached forever. The
        second, partial-roster write site (an oversized declared name) must be
        bounded too — it is a distinct write and a distinct hazard, since it is
        also keyed on the caller-supplied dir.
        """
        import kiro_crew.agent_discovery as ad

        declared_name = "x" * (ad._AGENT_NAME_MAX_CHARS + 1) if oversized else "a"
        clear_project_agent_cache()
        cap = ad._PROJECT_NAMES_CACHE_MAX_KEYS
        for i in range(cap + 50):
            proj = tmp_path / f"proj-{i}"
            (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": declared_name}))
            project_agent_names(str(proj))

        assert len(ad._PROJECT_NAMES_CACHE) <= cap, (
            f"cache grew to {len(ad._PROJECT_NAMES_CACHE)} entries, exceeding the "
            f"{cap}-entry cap"
        )


class TestListAgentsRobustness:
    def test_oversized_spec_is_rejected_not_slurped(self, fake_home, monkeypatch):
        """A spec over the safety cap is skipped — for BOTH scopes, since
        _read_agent_spec is the one reader.

        Regression: reads used a bare ``read_bytes()``, so a multi-gigabyte
        "agent config" was slurped whole into memory during a cache warm. The
        read now goes through hooks.safe_read_file_bytes, whose cap refuses it.
        """
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 64)
        d = _agents_dir(fake_home)
        (d / "small.json").write_text(json.dumps({"name": "small"}))
        big = json.dumps({"name": "big", "pad": "x" * 512})
        (d / "big.json").write_text(big)

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["small"]

    def test_survives_non_utf8_and_appledouble(self, fake_home):
        """A non-UTF-8 file (AppleDouble ``._*.json`` sidecar or arbitrary
        binary ``*.json``) must be skipped, not raise UnicodeDecodeError."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        # AppleDouble sidecar: starts with "._" and is non-UTF-8 binary.
        (d / "._good.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        # Arbitrary non-UTF-8 *.json that is not an AppleDouble name either.
        (d / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_non_dict_json(self, fake_home):
        """Valid JSON that is not an object (e.g. a top-level array) must be
        skipped, not raise AttributeError on data.get()."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "array.json").write_text(json.dumps([1, 2, 3]))
        (d / "scalar.json").write_text(json.dumps("just a string"))

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    @requires_symlinks
    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """A ``*.json`` symlink under ~/.kiro/agents/ that resolves to a
        sensitive credential path must NOT be read or returned."""
        d = _agents_dir(fake_home)
        (d / "real.json").write_text(json.dumps({"name": "real"}))

        # Plant a credential file under the sensitive ~/.aws dir and symlink
        # it in as a fake agent config. Even though it is valid JSON that
        # would parse, the sensitive-path guard must skip it.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"name": "evil"}))
        (d / "evil.json").symlink_to(creds)

        names = [a.name for a in list_agents(agents_dir=d)]
        assert "evil" not in names
        assert names == ["real"]

    def test_skips_non_dict_mcp_servers(self, tmp_path: Path) -> None:
        """list_agents must not crash when mcpServers is a list instead of a dict.

        A non-dict ``mcpServers`` raises AttributeError: 'list' object has no
        attribute 'keys'; the except clause must catch it so the loop keeps every
        sibling agent.
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text(
            json.dumps({"name": "bad", "model": "auto", "mcpServers": ["a", "b"]}),
            encoding="utf-8",
        )
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "good", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        names = {a.name for a in agents}
        assert "good" in names, "well-formed sibling agent must survive a bad mcpServers value"


class TestSpecModelCoercion:
    """``AgentInfo.model`` is declared ``str`` and must always BE one.

    ``~/.kiro/agents`` is shared with other tools whose specs spell ``model``
    differently. A non-string reached the dashboard via ``to_dict()`` ->
    ``/api/agents/installed`` and, rendered as a React child, threw error #31 —
    taking the whole Agent Templates tab (and every other agent's row) down.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            # ACP-style structured reference, observed in the wild. This exact
            # shape produced "object with keys {id}" in the React #31 message.
            {"id": "anthropic:claude-opus-4-8"},
            None,  # key present but null
            ["claude-opus-4-8"],
            42,
        ],
        ids=["dict-id", "null", "list", "int"],
    )
    def test_non_string_model_degrades_to_auto(self, tmp_path: Path, raw: object) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "model": raw}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.model == "auto"
        # to_dict() is what the API serialises — the guarantee has to hold there,
        # since that is the value the dashboard renders.
        assert isinstance(agent.to_dict()["model"], str)

    def test_string_model_is_preserved(self, tmp_path: Path) -> None:
        """The coercion must not flatten a legitimately pinned model."""
        d = tmp_path / "agents"
        d.mkdir()
        (d / "pinned.json").write_text(
            json.dumps({"name": "pinned", "model": "claude-opus-4-6"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.model == "claude-opus-4-6"

    def test_non_string_description_degrades_to_empty(self, tmp_path: Path) -> None:
        """``model`` is not the only rendered field, so it is not the only one guarded.

        The detail panel renders ``description`` as a JSX child too, and an object
        is truthy — so a foreign spec with a structured ``description`` blanks the
        whole tab exactly like a structured ``model`` does. Coercing per FIELD is
        what closes the class rather than the one observed instance.
        """
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "description": {"text": "hi"}, "model": "auto"}),
            encoding="utf-8",
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.description == ""
        assert isinstance(agent.to_dict()["description"], str)

    def test_string_description_is_preserved(self, tmp_path: Path) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        (d / "ok.json").write_text(
            json.dumps({"name": "ok", "description": "a real one", "model": "auto"}),
            encoding="utf-8",
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.description == "a real one"

    def test_bad_model_does_not_drop_sibling_agents(self, tmp_path: Path) -> None:
        """A foreign spec must cost only its own row, never the whole listing."""
        d = tmp_path / "agents"
        d.mkdir()
        (d / "foreign.json").write_text(
            json.dumps({"name": "foreign", "model": {"id": "anthropic:claude-opus-4-8"}}),
            encoding="utf-8",
        )
        (d / "good.json").write_text(
            json.dumps({"name": "good", "model": "auto"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        names = {a.name for a in list_agents(agents_dir=d)}
        assert names == {"foreign", "good"}

    def test_edition_supplied_row_is_coerced(self, tmp_path: Path, monkeypatch) -> None:
        """The edition seam is a SECOND ``AgentInfo`` construction site.

        Rows arrive from out-of-tree code, so ``AgentInfo.model: str`` has to be
        enforced there too — coercing only the on-disk path would leave the same
        crash reachable through an edition build.
        """
        d = tmp_path / "agents"
        d.mkdir()
        # Stub the seam at ``safe_context_call``: it is what ``_with_edition_agents``
        # funnels the platform lookup through, so this needs no platform context.
        monkeypatch.setattr(
            "kiro_crew.platform.context.safe_context_call",
            lambda *_a, **_kw: [
                {"name": "edition-foreign", "model": {"id": "anthropic:claude-opus-4-8"}}
            ],
        )
        clear_list_agents_cache()
        by_name = {a.name: a for a in list_agents(agents_dir=d)}
        assert by_name["edition-foreign"].model == "auto"

    def test_every_str_field_is_coerced_at_construction(self) -> None:
        """The invariant is on the CONSTRUCTOR, not on any one caller.

        `model` and `description` were the two fields observed failing, but
        `name`, `package`, `source` and `filename` are rendered bare too
        (`{a.name}`, `{a.package}`, `<SourceBadge source={a.source}>`,
        `a.filename.startsWith(...)`), so a per-field fix at one call site only
        looks complete. Constructing directly — as the out-of-tree edition seam
        does — must still yield the declared types.
        """
        info = AgentInfo(
            name={"id": "x"},  # type: ignore[arg-type]
            filename=None,  # type: ignore[arg-type]
            description=["a"],  # type: ignore[arg-type]
            model={"id": "anthropic:claude-opus-4-8"},  # type: ignore[arg-type]
            source=7,  # type: ignore[arg-type]
            package={"n": 1},  # type: ignore[arg-type]
        )
        assert info.name == ""
        assert info.filename == ""
        assert info.description == ""
        assert info.model == "auto"
        assert info.source == "builtin"
        assert info.package == ""
        # to_dict() is the wire shape the dashboard renders. Non-string fields
        # are excluded by NAME, not skipped silently: the lists render as chips
        # (one element each) and `kirocrew_owned` is the bool provenance flag —
        # everything else must be a plain string or React error #31 returns.
        assert all(
            isinstance(v, str)
            for k, v in info.to_dict().items()
            if k not in ("skills", "mcp_servers", "kirocrew_owned")
        )
        assert isinstance(info.to_dict()["kirocrew_owned"], bool)

    def test_list_fields_drop_only_the_unusable_elements(self) -> None:
        """`skills` / `mcp_servers` are rendered as chips, one element each.

        A bad entry costs itself, not the whole list — dropping the list would
        hide real skills the agent does have.
        """
        info = AgentInfo(
            name="a",
            filename="a.json",
            description="",
            model="auto",
            skills=["good", {"bad": 1}, "also-good"],  # type: ignore[list-item]
            mcp_servers=[None, "srv"],  # type: ignore[list-item]
        )
        assert info.skills == ["good", "also-good"]
        assert info.mcp_servers == ["srv"]

    def test_non_string_name_falls_back_to_filename_stem(self, tmp_path: Path) -> None:
        """A structured `name` must degrade the row, not silently DROP it.

        The package-detection branch does `stem.endswith(agent_name)`, which
        raised TypeError on a non-string name; the loop's broad `except` then
        swallowed it and the agent vanished from the listing entirely.
        """
        d = tmp_path / "agents"
        d.mkdir()
        (d / "weird.json").write_text(
            json.dumps({"name": {"id": "nope"}, "model": "auto"}), encoding="utf-8"
        )
        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.name == "weird"

    def test_edition_row_with_unusable_name_is_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Unlike cosmetic fields, an unusable NAME is not degraded.

        The name is the dedup key, the React list key, and the argument every
        mutation is addressed by, so a blank-named row would be unselectable and
        would collide with any other nameless row.
        """
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(
            "kiro_crew.platform.context.safe_context_call",
            lambda *_a, **_kw: [
                {"name": {"id": "nope"}, "model": "auto"},
                {"name": "usable", "model": "auto"},
            ],
        )
        clear_list_agents_cache()
        names = {a.name for a in list_agents(agents_dir=d)}
        assert names == {"usable"}


class TestListAgentsGlobalGuards:
    """Global agent loader edge cases."""

    @requires_symlinks
    def test_global_symlink_loop_skipped_not_crashed(self, tmp_path: Path) -> None:
        """A self-referential symlink is skipped, never an uncaught RuntimeError.

        Regression: ``resolve(strict=True)`` signals a symlink LOOP with
        RuntimeError (not OSError); only OSError was caught, so one
        ``ln -s loop.json loop.json`` in a user-writable agents dir crashed
        every surface that listed agents (e.g. Slack's ``!agent``).
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        loop = agents_dir / "loop.json"
        loop.symlink_to(loop)
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)
        assert not any(a.name == "loop" for a in agents)

    @requires_symlinks
    def test_global_broken_symlink_skipped(self, tmp_path: Path) -> None:
        """list_agents skips broken symlinks in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        broken = agents_dir / "broken.json"
        broken.symlink_to(tmp_path / "nonexistent.json")
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)
        assert not any(a.name == "broken" for a in agents)

    def test_global_bad_json_skipped(self, tmp_path: Path) -> None:
        """list_agents skips malformed JSON in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)


class TestListAgentsDedup:
    """Deduplication and AIM package-name extraction edge cases."""

    def test_aim_package_name_extracted(self, tmp_path: Path) -> None:
        """AIM filename pattern extracts package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # AIM filename pattern: {package}-{agent_name}.json
        (agents_dir / "MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"
        assert a.source == "package"

    def test_aim_kirocrew_package_source(self, tmp_path: Path) -> None:
        """A package-installed agent (e.g. KiroCrewAICapabilities) gets source='package'."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "KiroCrewAICapabilities-myskill.json").write_text(
            json.dumps({"name": "myskill", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myskill"), None)
        assert a is not None
        assert a.source == "package"

    def test_aim_package_preferred_over_builtin(self, tmp_path: Path) -> None:
        """AIM-packaged agent replaces same-name builtin in dedup."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # "dev.json" is builtin (stem == name). "zzz-MyPkg-dev.json" is AIM-packaged.
        # sorted() puts "dev.json" first, so builtin is seen first, then AIM replaces it.
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "zzz-MyPkg-dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        dev_agents = [a for a in agents if a.name == "dev"]
        assert len(dev_agents) == 1
        assert dev_agents[0].package == "zzz-MyPkg"

    def test_local_prefix_stripped_from_aim_package(self, tmp_path: Path) -> None:
        """AIM filename with 'local-' prefix has it stripped from package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"

    def test_local_twin_of_same_package_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 'local-' twin of the same package dedupes silently (no WARNING).

        Package managers publish a locally-built package as BOTH
        ``{package}-{name}.json`` and ``local-{package}-{name}.json``. Since the
        ``local-`` prefix is stripped from the package name, the twins collide on
        the same (name, package) — an expected layout, not an anomaly, so it
        must not log a self-contradictory "from packages 'X' and 'X'" WARNING
        per agent per scan.
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent_discovery"):
            agents = list_agents(agents_dir=agents_dir)
        dupes = [a for a in agents if a.name == "myagent"]
        assert len(dupes) == 1
        # First-seen wins, and which twin enumerates first is platform-
        # dependent (WindowsPath sorts case-insensitively, so "local-..."
        # can precede "MyPkg-..."). The fix deliberately leaves selection
        # untouched — assert only that exactly one twin survives.
        assert dupes[0].filename in ("MyPkg-myagent.json", "local-MyPkg-myagent.json")
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == _DISCOVERY_LOGGER
        ], "same-package local twin must not produce a WARNING"
        # The twin is still visible at debug for diagnosis.
        assert any("same-package twin" in r.getMessage() for r in caplog.records)

    def test_cross_package_duplicate_still_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A genuine name collision between two DIFFERENT packages still warns."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "AaaPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "BbbPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_discovery"):
            agents = list_agents(agents_dir=agents_dir)
        dupes = [a for a in agents if a.name == "myagent"]
        assert len(dupes) == 1
        assert dupes[0].package == "AaaPkg"  # first-seen wins
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "Duplicate agent name" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "AaaPkg" in warnings[0].getMessage()
        assert "BbbPkg" in warnings[0].getMessage()


class TestListAgentsCache:
    """list_agents caches parsed results per directory and reuses them while the
    stat-only directory signature is unchanged."""

    def test_cache_hit_skips_reparse(self, tmp_path: Path) -> None:
        """An unchanged signature returns the cached result without re-parsing."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()

        first = [a.name for a in list_agents(agents_dir=d)]
        assert first == ["v1"]

        # Rewrite the content but restore the original mtime so the signature is
        # unchanged: a re-parse would yield "v2"; a cache hit yields "v1".
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))

        second = [a.name for a in list_agents(agents_dir=d)]
        assert second == ["v1"], "unchanged signature must return the cached result"

    def test_cache_invalidates_on_add(self, tmp_path: Path) -> None:
        """Adding a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(json.dumps({"name": "a", "model": "auto"}), encoding="utf-8")
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

        (d / "b.json").write_text(json.dumps({"name": "b", "model": "auto"}), encoding="utf-8")
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

    def test_cache_invalidates_on_remove(self, tmp_path: Path) -> None:
        """Removing a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(json.dumps({"name": "a", "model": "auto"}), encoding="utf-8")
        (d / "b.json").write_text(json.dumps({"name": "b", "model": "auto"}), encoding="utf-8")
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

        (d / "b.json").unlink()
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

    def test_cache_invalidates_on_inplace_edit(self, tmp_path: Path) -> None:
        """An in-place content edit (newer mtime) invalidates the cache."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        # Bump mtime forward deterministically so the signature is guaranteed newer.
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert [a.name for a in list_agents(agents_dir=d)] == [
            "v2"
        ], "an in-place edit must invalidate the cache"

    def test_clear_cache_forces_rescan(self, tmp_path: Path) -> None:
        """clear_list_agents_cache() forces a fresh scan even when the signature
        is unchanged."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        # Change content but freeze the mtime so the signature would still hit ...
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
        # ... then force a clear: the next call must re-scan and see "v2".
        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v2"]


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


class TestSystematicScanFailureWarning:
    """A scan that rejects EVERY candidate spec warns once; anything less stays quiet.

    Regression: `_read_agent_spec` degrades per file to ``None`` at debug level, so
    a systematic refusal (e.g. the trusted-root gate rejecting an entire home
    layout) is indistinguishable at default log levels from an empty
    agents directory — discovery lists nothing and nothing says why.
    """

    def test_all_unreadable_user_specs_emit_one_warning(self, fake_home, caplog):
        d = _agents_dir(fake_home)
        for i in range(3):
            (d / f"broken-{i}.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1, "exactly one warning per scan, not per file"
        # Count asserted via the record's args: a digit-in-string match is
        # satisfiable by digits in the pytest tmp path.
        assert warnings[0].args[0] == 3
        assert str(d) in warnings[0].getMessage()

    def test_mixed_directory_emits_no_warning(self, fake_home, caplog):
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "broken.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]
        assert _discovery_warnings(caplog) == []

    def test_empty_and_absent_directories_emit_no_warning(self, fake_home, caplog):
        empty = _agents_dir(fake_home)
        absent = fake_home / "nowhere" / "agents"
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=empty) == []
            clear_list_agents_cache()
            assert list_agents(agents_dir=absent) == []
        assert _discovery_warnings(caplog) == [], "N=0 is not systematic failure"

    def test_all_unreadable_project_specs_emit_one_warning(self, fake_home, tmp_path, caplog):
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "bad-a.json").write_text("not json at all")
        (pd / "bad-b.json").write_text(json.dumps(["top-level", "array"]))
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d, project_dir=str(proj)) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].args[0] == 2
        assert str(pd) in warnings[0].getMessage()

    def test_project_agent_names_warns_on_systematic_failure(self, tmp_path, caplog):
        """The per-turn resolver's scan warns too — this is the exact path whose
        silence lets model resolution fall back to auto."""
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "bad.json").write_text("{broken")
        clear_project_agent_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(str(proj)) == frozenset()
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert str(pd) in warnings[0].getMessage()

    def test_project_agent_names_mixed_directory_emits_no_warning(self, tmp_path, caplog):
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "good.json").write_text(json.dumps({"name": "good"}))
        (pd / "bad.json").write_text("{broken")
        clear_project_agent_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(str(proj)) == frozenset({"good"})
        assert _discovery_warnings(caplog) == []

    def test_sidecar_only_directory_emits_no_warning(self, fake_home, caplog):
        """AppleDouble sidecars are rejected by design, not by failure — a
        directory holding only ``._*.json`` is empty of specs, not broken."""
        d = _agents_dir(fake_home)
        (d / "._ghost.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        assert _discovery_warnings(caplog) == []

    def test_row_construction_failure_counts_as_unparsed(self, fake_home, caplog, monkeypatch):
        """A spec that parses but whose row construction raises still ends in
        "discovery listed nothing" — the warning must cover that path too, so
        the parsed counter means "produced a row", not "JSON loaded"."""
        import kiro_crew.agent_discovery as mod

        d = _agents_dir(fake_home)
        (d / "parses.json").write_text(json.dumps({"name": "parses"}))

        def _boom(f, data):
            raise RuntimeError("row construction failed")

        monkeypatch.setattr(mod, "_global_agent_info", _boom)
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].args[0] == 1


class TestForkLineageEnrichment:
    """list_agents stamps forked_from/private_to onto global-scope rows from the
    agent_state sidecar (global scope only — forks are recorded against
    user-level templates). The sidecar lives under the isolated KIROCREW_HOME."""

    def test_forked_row_is_enriched(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "design-crew.json").write_text(json.dumps({"name": "design-crew"}))
        (d / "plain.json").write_text(json.dumps({"name": "plain"}))
        agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")

        clear_list_agents_cache()
        by_name = {a.name: a for a in list_agents(agents_dir=d)}

        assert by_name["design-crew"].forked_from == "kirocrew"
        assert by_name["design-crew"].private_to == "design-crew"
        # An un-forked sibling keeps the empty defaults.
        assert by_name["plain"].forked_from == ""
        assert by_name["plain"].private_to == ""

    def test_unforked_rows_have_empty_lineage_when_no_sidecar(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "solo.json").write_text(json.dumps({"name": "solo"}))

        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.forked_from == ""
        assert agent.private_to == ""


class TestSpecByDeclaredName:
    """The shared declared-name scan two session-start surfaces resolve through."""

    @staticmethod
    def _write(agents_dir: Path, filename: str, **fields: object) -> Path:
        path = agents_dir / filename
        path.write_text(json.dumps({"name": "kirocrew", **fields}), encoding="utf-8")
        return path

    def test_a_namespaced_spec_resolves_by_its_declared_name(self, tmp_path: Path) -> None:
        self._write(tmp_path, "SomePackage-kirocrew.json", description="namespaced")

        spec = spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert spec is not None and spec["description"] == "namespaced"

    def test_no_declared_match_is_none(self, tmp_path: Path) -> None:
        self._write(tmp_path, "SomePackage-other.json", name="other")
        (tmp_path / "other.json").write_text(json.dumps({"name": "other"}), encoding="utf-8")

        assert spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test") is None

    def test_two_specs_declaring_one_name_are_refused_naming_both(self, tmp_path: Path) -> None:
        self._write(tmp_path, "Alpha-kirocrew.json")
        self._write(tmp_path, "Beta-kirocrew.json")

        with pytest.raises(AmbiguousAgentSpecError) as exc:
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert "Alpha-kirocrew.json" in str(exc.value)
        assert "Beta-kirocrew.json" in str(exc.value)

    def test_only_the_first_matching_parse_is_held(self, tmp_path: Path, monkeypatch) -> None:
        """The refusal needs the duplicates' PATHS, not their parses.

        Each file is capped by the reader, so the parsed-spec memory the scan
        holds at its peak is set by how many parses it keeps at once. Holding
        one per match makes that the number of same-name files in a
        user-writable directory times the cap; holding one total makes it the
        cap. (Paths are kept one per candidate either way; they are small and
        the refusal message needs them.) The bound is a property of
        the scan WHILE it runs -- once it raises, any list it held dies with its
        frame either way -- so the probe sits inside the reader: on every read,
        each earlier parse except the first and the one the loop body last
        assigned must already be unreachable.
        """
        import gc
        import weakref

        from kiro_crew import agent_discovery

        class _Spec(dict):
            """A dict that can be weakly referenced."""

        for stem in ("Alpha", "Beta", "Gamma", "Delta"):
            self._write(tmp_path, f"{stem}-kirocrew.json")

        handed_out: list[weakref.ref] = []
        retained_mid_scan: list[str] = []

        def _reader(path: Path, *, operation: str, source: str) -> dict:
            # Reads 0..n-2 are done; read n-2's parse is still the loop's own
            # ``spec`` local until this call returns, so it is exempt. Read 0 is
            # the match the scan may return, so it is exempt. Everything else
            # must be gone.
            gc.collect()
            for ref in handed_out[1:-1]:
                spec = ref()
                if spec is not None:
                    retained_mid_scan.append(spec["origin"])
            spec = _Spec(name="kirocrew", origin=path.name)
            handed_out.append(weakref.ref(spec))
            return spec

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", _reader)

        with pytest.raises(AmbiguousAgentSpecError) as exc:
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert len(handed_out) == 4, "the scan must have read every candidate"
        assert retained_mid_scan == [], f"parses held past their read: {retained_mid_scan}"
        # The refusal still names every duplicate: paths are kept, parses are not.
        for stem in ("Alpha", "Beta", "Gamma", "Delta"):
            assert f"{stem}-kirocrew.json" in str(exc.value)


class TestScanCapabilityGate:
    """The scan is gated on ``pinned_fs.supports_pinned_walk()`` and refuses
    outright when it is False, rather than falling back to a by-name walk an
    ancestor swap could redirect -- the same standing rule
    ``pinned_fs.remove_tree_pinned`` states for itself.
    """

    def test_scan_entry_cap_reports_overflow_and_stops(self, tmp_path, monkeypatch, caplog) -> None:
        """The pinned walk retains at most the directory-entry cap and reports
        overflow. The by-name walk the session-established callers use is never
        truncated, so it has no partial roster to hand back -- pinned end to end
        by ``test_scan_overflow_never_becomes_a_partial_roster``."""
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import (
            _AGENT_DIRECTORY_MAX_ENTRIES,
            _pinned_scan_dir_fd,
        )

        class _CountingScandir:
            def __init__(self) -> None:
                self.pulls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def __iter__(self):
                return self

            def __next__(self):
                self.pulls += 1
                if self.pulls > _AGENT_DIRECTORY_MAX_ENTRIES + 8:
                    raise StopIteration
                entry = MagicMock()
                entry.name = f"agent-{self.pulls}.json"
                return entry

        scan = _CountingScandir()
        agents_dir = tmp_path / "repo" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        fd = 7303
        os_double = SimpleNamespace(**vars(os))
        os_double.scandir = lambda target: scan
        os_double.close = lambda _held_fd: None
        monkeypatch.setattr(discovery_mod, "os", os_double)
        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(discovery_mod, "is_sensitive_path", lambda _path: False)
        monkeypatch.setattr(discovery_mod, "fd_real_path", lambda _held_fd: str(agents_dir))
        monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", lambda *_a, **_kw: fd)

        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            with _pinned_scan_dir_fd(agents_dir) as (entries, dir_fd, overflow):
                assert entries is not None
                assert len(tuple(entries)) == _AGENT_DIRECTORY_MAX_ENTRIES
                assert overflow is True
                assert dir_fd == fd

        warnings = [record for record in caplog.records if record.name == _DISCOVERY_LOGGER]
        assert scan.pulls == _AGENT_DIRECTORY_MAX_ENTRIES + 1
        assert len(warnings) == 1
        assert warnings[0].args == (
            agents_dir,
            _AGENT_DIRECTORY_MAX_ENTRIES,
        )

    def test_scan_overflow_never_becomes_a_partial_roster(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import ScanUnverifiable

        monkeypatch.setattr(discovery_mod, "_AGENT_DIRECTORY_MAX_ENTRIES", 2)
        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        for name in ("a", "b", "c"):
            (agents_dir / f"{name}.json").write_text(json.dumps({"name": name}))
        clear_project_agent_cache()
        _fake_pinned_project_walk(monkeypatch, project)

        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            # A session-established caller (fork governance's shadow check among
            # them) reads the by-name walk, which the cap does not apply to: its
            # answer is the WHOLE directory, so absence from it is real absence.
            assert project_agent_names(project) == frozenset({"a", "b", "c"})
            with pytest.raises(ScanUnverifiable, match="2-entry scan cap"):
                project_agent_names(project, raise_unverifiable=True)

        assert (
            sum(
                record.name == _DISCOVERY_LOGGER and "entry cap" in record.message
                for record in caplog.records
            )
            == 1
        )

    def test_oversized_declared_name_narrows_session_roster_and_is_not_cached_complete(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import ScanUnverifiable

        name_limit = getattr(discovery_mod, "_AGENT_NAME_MAX_CHARS", 63)
        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        (agents_dir / "good.json").write_text(json.dumps({"name": "good"}))
        oversized = "x" * (name_limit + 1)
        (agents_dir / "oversized.json").write_text(json.dumps({"name": oversized}))
        clear_project_agent_cache()

        # Session path: the surviving sibling is served, not the whole
        # roster refused -- mirrors _bounded_scan_entries' split between a
        # strict request scan (refuses) and a pre-existing session scan
        # (consumes the bounded/valid prefix and reports the narrowing).
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(project) == frozenset({"good"})

        warnings = [
            record
            for record in caplog.records
            if record.name == _DISCOVERY_LOGGER and "agent-name cap" in record.message
        ]
        assert len(warnings) == 1
        assert oversized not in caplog.text
        cached_signature, cached_names = discovery_mod._PROJECT_NAMES_CACHE[str(project)]
        # The cached signature still carries the oversized-name marker so a
        # later verified (complete) scan cannot hit this as a complete
        # roster -- but the cached NAMES are the partial, not empty.
        assert cached_signature[-1] == (("\0oversized-name", 1),)
        assert cached_names == frozenset({"good"})

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(project) == frozenset({"good"})
        # The repeat call is now a cache HIT (see test_oversized_project_gets_cache_hits_too
        # below): nothing was rescanned, so no new "agent-name cap" warning fires.
        assert sum("agent-name cap" in record.message for record in caplog.records) == 0

        # Strict path is untouched: it still refuses the whole answer. Forced
        # pinned so the assertion reaches the name-cap refusal even on a
        # platform that cannot pin: raise_unverifiable=True would otherwise
        # raise ScanUnverifiable's PINNING message before the name cap is ever
        # consulted, never reaching this contract at all.
        _fake_pinned_project_walk(monkeypatch, project)
        with pytest.raises(ScanUnverifiable, match=rf"{name_limit}-character agent-name cap"):
            project_agent_names(project, raise_unverifiable=True)

    def test_oversized_project_gets_cache_hits_too(self, tmp_path, monkeypatch) -> None:
        """The regression this fix closes: a project whose cached (partial)
        signature carries the ``\\0oversized-name`` sentinel row must still get
        a cache HIT on an unchanged checkout, exactly like an ordinary project.

        Before the fix, ``cached[0] == signature`` compared the stored
        ``partial_signature`` (``*signature, _oversized_name_sig(n)``) against a
        FRESH ``signature`` that can never carry that extra row, so the
        comparison was false on every subsequent call — a permanent cache miss,
        rescanning the directory (and re-emitting the warning) on every turn.

        Mutation guard: this test fails on the pre-fix code because the second
        call rescans and trips the ``pytest.fail`` guard below.
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import ScanUnverifiable

        name_limit = getattr(discovery_mod, "_AGENT_NAME_MAX_CHARS", 63)
        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        (agents_dir / "good.json").write_text(json.dumps({"name": "good"}))
        (agents_dir / "oversized.json").write_text(json.dumps({"name": "x" * (name_limit + 1)}))
        clear_project_agent_cache()

        assert project_agent_names(project) == frozenset({"good"})
        cached_signature, _ = discovery_mod._PROJECT_NAMES_CACHE[str(project)]
        assert cached_signature[-1] == (("\0oversized-name", 1),)

        monkeypatch.setattr(
            discovery_mod,
            "_scan_project_agent_files",
            lambda *a, **kw: pytest.fail("re-scanned an unchanged oversized-name project"),
        )

        # Repeated non-strict calls hit the cache without rescanning.
        assert project_agent_names(project) == frozenset({"good"})
        assert project_agent_names(project) == frozenset({"good"})

        # The strict caller is UNAFFECTED by the partial-match branch: it must
        # still refuse rather than being served the cached partial roster. It
        # necessarily rescans to reach that refusal, so the guard above is
        # restored first.
        monkeypatch.undo()
        _fake_pinned_project_walk(monkeypatch, project)
        with pytest.raises(ScanUnverifiable, match=rf"{name_limit}-character agent-name cap"):
            project_agent_names(project, raise_unverifiable=True)

    def test_name_of_exactly_the_cap_is_accepted_not_narrowed(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """A name AT the cap is a normal roster entry, not the oversized case.

        The sibling test above uses ``name_limit + 1`` derived from the
        module's OWN constant, so it only proves the cap refuses one
        character past whatever that constant currently says -- it can never
        disagree with a wrong constant. This is the other half, pinned
        against the independent authority instead: a name of exactly
        ``kiro_crew.validation._AGENT_NAME_RE``'s maximum length (64, the
        grammar a dispatchable agent name is checked against) must be served
        like any other declared name, with no warning row, no
        ``ScanUnverifiable`` under ``raise_unverifiable=True``, and no
        oversized-name cache marker.
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.validation import _AGENT_NAME_RE

        authority_limit = 64
        assert _AGENT_NAME_RE.match("x" * authority_limit)
        assert not _AGENT_NAME_RE.match("x" * (authority_limit + 1))

        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        at_limit = "x" * authority_limit
        (agents_dir / "at-limit.json").write_text(json.dumps({"name": at_limit}))
        clear_project_agent_cache()

        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(project) == frozenset({at_limit})
        assert not any(
            record.name == _DISCOVERY_LOGGER and "agent-name cap" in record.message
            for record in caplog.records
        )
        cached_signature, cached_names = discovery_mod._PROJECT_NAMES_CACHE[str(project)]
        assert cached_signature[-1] != (("\0oversized-name", 1),)
        assert cached_names == frozenset({at_limit})

        # Strict path must serve the same name at the cap, not refuse; forced
        # pinned for the reason the sibling above gives.
        _fake_pinned_project_walk(monkeypatch, project)
        assert project_agent_names(project, raise_unverifiable=True) == frozenset({at_limit})

    _LONG_CHAIN_UNDER_J = {
        **{f"C:/j/{index}": f"C:/j/{index + 1}" for index in range(12)},
        "C:/links/a": "C:/j/0",
        "C:/j": "C:/real",
    }

    @pytest.mark.parametrize(
        ("links", "refused"),
        [
            ({"C:/links/a": "C:/local/agents"}, False),
            ({"C:/links/a": "//attacker/share"}, True),
            (
                {
                    "C:/links/a": "C:/links/b",
                    "C:/links/b": "C:/links/c",
                    "C:/links/c": "//attacker/share",
                },
                True,
            ),
            ({"C:/links/a": "C:/links/b", "C:/links/b": "C:/links/a"}, True),
            (
                {"C:/links/a": "C:/under-junction/agents", "C:/under-junction": "//attacker/share"},
                True,
            ),
            ({"C:/links/a": "C:/j/one", "C:/j/one": "C:/j/two", "C:/j": "C:/real"}, False),
            ({"C:/links/a": "C:/j/one", "C:/j/one": "C:/real", "C:/j": "C:/real"}, False),
            (_LONG_CHAIN_UNDER_J, False),
            (
                {"C:/links/a": "C:/j/one", "C:/j/one": "C:/j/two", "C:/j": "//attacker/share"},
                True,
            ),
            ({"C:/links/a": "C:/j/one", "C:/j/one": "C:/j/two", "C:/j": "C:/j/inner"}, True),
            (
                {
                    "C:/links/a": "C:/links/b",
                    "C:/links/b": PermissionError(errno.EACCES, "cannot inspect link"),
                },
                True,
            ),
            ({"C:/links/a": "\\target"}, True),
            ({"C:/links/a": "D:target"}, True),
        ],
        ids=(
            "single-local",
            "single-unc",
            "chained-unc",
            "cycle",
            "rewritten-target-under-unc-ancestor",
            "shared-local-junction",
            "hop-lands-on-junction-target",
            "long-chain-under-one-junction",
            "shared-unc-junction",
            "junction-into-itself",
            "uninspectable-hop",
            "root-relative-target",
            "drive-relative-target",
        ),
    )
    def test_link_chain_screen_judges_every_stored_target(
        self, monkeypatch, links, refused
    ) -> None:
        """The preflight is the Windows link screen ``validate_file_path`` runs.

        Every hop's stored target is read locally and judged before anything
        beneath it is touched; a vetted target is swapped in and the rewritten
        path walked again, so an ancestor junction under the rewritten target is
        reached too. A local chain passes; an untrusted share anywhere on it, a
        cycle, an uninspectable hop, or an ambiguous root- or drive-relative
        target refuses.
        """
        import ntpath

        import kiro_crew.agent_discovery as discovery_mod

        calls = simulate_windows_link_screen(monkeypatch, links, discovery_path_module=ntpath)

        assert discovery_mod._link_chain_refused("C:/links/a") is refused
        if "C:/under-junction" in links:
            assert (
                ntpath.normcase("C:\\under-junction\\agents") not in calls
            ), "readlink ran on the rewritten target before its ancestor was cleared"

    def test_link_chain_screen_refuses_hop_cap_exhaustion(self, monkeypatch) -> None:
        """A chain longer than the screen's own hop cap refuses, local or not."""
        import ntpath

        import kiro_crew.agent_discovery as discovery_mod
        import kiro_crew.hooks as hooks_mod

        cap = hooks_mod._WINDOWS_LINK_CHAIN_MAX
        links = {f"C:/links/{index}": f"C:/links/{index + 1}" for index in range(cap)}
        simulate_windows_link_screen(monkeypatch, links, discovery_path_module=ntpath)

        assert discovery_mod._link_chain_refused("C:/links/0") is True
        # One hop under the cap, ending on a local directory, is admitted.
        shorter = {f"C:/links/{index}": f"C:/links/{index + 1}" for index in range(cap - 1)}
        simulate_windows_link_screen(monkeypatch, shorter, discovery_path_module=ntpath)
        assert discovery_mod._link_chain_refused("C:/links/0") is False

    def test_link_chain_screen_never_refuses_off_windows(self, monkeypatch) -> None:
        import kiro_crew.agent_discovery as discovery_mod

        monkeypatch.setattr(discovery_mod, "_WINDOWS", False)
        monkeypatch.setattr(
            discovery_mod,
            "_screen_windows_links",
            lambda _path: pytest.fail("the screen ran off Windows"),
        )

        assert discovery_mod._link_chain_refused("/any/path") is False

    @pytest.mark.parametrize(
        ("stored_target", "admitted"),
        (
            ("\\\\?\\UNC\\roaming-server\\profiles\\alice\\.kiro\\crew\\agents", True),
            ("\\\\?\\unc\\roaming-server\\profiles\\alice\\.kiro\\crew\\agents", True),
            ("\\\\?\\UNC\\attacker\\share\\agents", False),
            ("\\\\roaming-server\\profiles\\alice\\.kiro\\crew\\agents", True),
            ("\\\\attacker\\share\\agents", False),
        ),
        ids=(
            "extended-length-trusted-share",
            "extended-length-trusted-share-lowercase-unc",
            "extended-length-untrusted-share",
            "plain-trusted-share",
            "plain-untrusted-share",
        ),
    )
    def test_extended_length_unc_link_target_agrees_with_validate_file_path(
        self, monkeypatch, stored_target, admitted
    ) -> None:
        r"""A link target is judged the way ``hooks.validate_file_path`` judges it.

        Windows ``os.readlink`` reports a share in extended-length form
        (``\\?\UNC\server\share\...``), and on a roaming profile the data home
        IS such a share. ``_normalize_windows_link_target`` -- the per-hop
        screen ``validate_file_path`` runs through ``_screen_windows_links`` --
        unwraps that prefix before the trusted-root check, so it admits a link
        into the data-home share. Both project-dir preflights and the directory
        signature reach that same screen, so each must reach the same verdict:
        refusing there would drop a roaming user's legitimate project agents
        with an SEL denial. An untrusted share stays refused in either spelling.
        """
        import ntpath

        import kiro_crew.agent_discovery as discovery_mod
        import kiro_crew.hooks as hooks_mod

        share_home = "\\\\roaming-server\\profiles\\alice\\.kiro\\crew"
        link = "C:\\work\\project\\.kiro\\agents"
        simulate_windows_link_screen(
            monkeypatch,
            {link: stored_target},
            data_home=share_home,
            discovery_path_module=ntpath,
        )

        validate_verdict = hooks_mod._normalize_windows_link_target(link, stored_target)
        assert (validate_verdict is not None) is admitted
        assert (hooks_mod.validate_file_path(link) is not None) is admitted
        assert discovery_mod._link_chain_refused(link) is (not admitted)

    @pytest.mark.parametrize("entry_point", ["_pinned_scan_dir", "files", "names"])
    def test_unpinnable_platform_refuses_when_asked(
        self, tmp_path, monkeypatch, entry_point
    ) -> None:
        """The default (``unsupported_ok=False``) is the NEW request-supplied
        ``?project_path=`` surface's contract: an unpinnable platform refuses
        rather than silently walking the caller-supplied path by name. The
        dashboard's explicit scan opts INTO that sharper signal through both
        public entry points (``project_agent_names`` on a cache miss), so an
        unverifiable scan never reads as "this project declares no agents" for
        the one caller with a UI state for "could not verify".
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import ScanUnverifiable, _pinned_scan_dir

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: False)
        proj = tmp_path / "repo"
        (_project_agents_dir(proj) / "a.json").write_text(json.dumps({"name": "a"}))
        clear_project_agent_cache()

        with pytest.raises(ScanUnverifiable):
            if entry_point == "_pinned_scan_dir":
                with _pinned_scan_dir(proj / ".kiro" / "agents"):
                    pass  # pragma: no cover - the context manager must raise on enter
            elif entry_point == "files":
                project_agent_files(str(proj), raise_unverifiable=True)
            else:
                project_agent_names(str(proj), raise_unverifiable=True)

    @pytest.mark.parametrize(
        ("ancestor_targets", "refused"),
        [
            ({"C:/work/junction": "//attacker/share"}, True),
            # The gap a benign shallow junction could hide: a local junction near
            # the root must not stop the walk before a DEEPER UNC-targeted
            # junction on the same chain is examined.
            (
                {
                    "C:/dotfiles-junction": "C:/actual/dotfiles",
                    "C:/actual/dotfiles/projects/evil-unc": "//evil/share",
                },
                True,
            ),
            # The mirror case: two links, BOTH targeting local directories, are
            # both cleared -- the screen is "examine every ancestor's own
            # target", not "refuse whenever there is more than one link".
            (
                {
                    "C:/outer-junction": "C:/outer-target",
                    "C:/outer-target/inner-junction": "C:/inner-target",
                },
                False,
            ),
        ],
        ids=("unc-ancestor", "local-above-unc-ancestor", "local-above-local-ancestor"),
    )
    def test_linked_ancestor_screen_judges_every_ancestor_before_any_probe(
        self, monkeypatch, ancestor_targets, refused
    ) -> None:
        """Every junction ancestor is judged by its OWN stored target, root-first,
        before ``realpath`` touches the chain; a disallowed UNC target anywhere on
        it refuses. The deeper junction is spelled through the shallow one's
        TARGET, so it is reached only because the screen swapped that target in.
        """
        import ntpath

        import kiro_crew.agent_discovery as discovery_mod

        first = list(ancestor_targets)[0]
        deeper = list(ancestor_targets)[-1]
        under_first = ntpath.relpath(deeper, ancestor_targets[first]) if deeper != first else ""
        target = ntpath.join(first, under_first, "repo", ".kiro", "agents")
        probes: list[str] = []
        simulate_windows_link_screen(
            monkeypatch,
            ancestor_targets,
            realpath=lambda path: probes.append(path) or path,
            discovery_path_module=ntpath,
        )

        assert discovery_mod._link_chain_refused(target) is refused
        assert probes == [], probes

    @pytest.mark.parametrize(
        ("entry_point", "under_local_junctions"),
        [("files", False), ("names", False), ("files", True)],
        ids=("files", "names", "files-under-two-local-junctions"),
    )
    def test_unpinnable_platform_still_discovers_real_agents_by_name(
        self, tmp_path, monkeypatch, entry_point, under_local_junctions
    ) -> None:
        """The regression this correction exists to close: on a platform that
        cannot pin a directory descriptor, EVERY pre-existing caller
        (per-turn resolution, ``spawn_run`` validation, Slack, the config
        loader) must keep discovering real ``<project>/.kiro/agents/*.json``
        entries exactly as `upstream/main` does on every platform -- not
        merely fail to raise, but actually return the agent. A prior version
        of this fix gated the whole scan and this returned ``[]`` for a
        project that genuinely has agents, which is the exact regression a
        Windows user would have hit in Slack, spawn validation and per-turn
        dispatch. Through ``project_agent_names`` a cache MISS must still
        compute a real signature and name set, not the sensitive-dir sentinel.

        Local junction ancestors cannot trigger an SMB/NTLM exchange, so the
        unpinned by-name walk still scans a project beneath two of them; that
        walk applies no linked-ancestor refusal of its own.
        """
        import kiro_crew.agent_discovery as discovery_mod

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: False)
        proj = tmp_path / "repo"
        if under_local_junctions:
            monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
            outer_ancestor = tmp_path / "outer-junction"
            inner_ancestor = outer_ancestor / "inner-junction"
            link_targets = {outer_ancestor: tmp_path / "outer-target"}
            link_targets[inner_ancestor] = tmp_path / "inner-target"
            for local_target in link_targets.values():
                local_target.mkdir()
            proj = inner_ancestor / "repo"
            os_double = SimpleNamespace(**vars(os))
            os_double.readlink = lambda path: (
                str(link_targets[path]) if path in link_targets else os.readlink(path)
            )
            monkeypatch.setattr(discovery_mod, "os", os_double)
        spec = _project_agents_dir(proj) / "a.json"
        spec.write_text(json.dumps({"name": "a"}))
        clear_project_agent_cache()

        if entry_point == "files":
            assert project_agent_files(str(proj)) == [spec]
        else:
            assert project_agent_names(str(proj), raise_unverifiable=False) == frozenset({"a"})

    def test_no_pinned_walk_support_still_scans_by_name_when_opted_in(
        self, tmp_path, monkeypatch
    ) -> None:
        """``unsupported_ok=True`` is the pre-existing-caller contract: on a
        platform that cannot pin, the scan degrades to the SAME by-name walk
        ``upstream/main`` has always run, rather than refusing -- proven here
        by actually finding an entry through it, not merely by not raising.
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: False)
        agents_dir = _project_agents_dir(tmp_path / "repo")
        (agents_dir / "a.json").write_text(json.dumps({"name": "a"}))

        with _pinned_scan_dir(agents_dir, unsupported_ok=True) as entries:
            assert entries is not None
            assert [e.name for e in entries] == ["a.json"]

    @requires_symlinks
    def test_ordinary_leaf_link_is_followed_not_refused_when_unpinnable(
        self, tmp_path, monkeypatch
    ) -> None:
        """A ``.kiro/agents`` that is an ordinary symlink into a LOCAL directory
        -- the dotfiles-managed checkout -- still yields its specs on an
        unpinnable platform, with NO audited denial. The leaf link is followed
        and judged by its resolved target, exactly as the pinned branch follows
        a leaf link; only a link whose target names an SMB share is refused. A
        blanket leaf ``is_link_or_junction`` screen refused this, dropping every
        project agent across Slack, per-turn resolution, ``spawn_run`` validation
        and the config loader, writing a false ``sensitive scan dir rejected``
        audit, and -- through the never-equal sensitive-dir signature --
        re-auditing on every turn.
        """
        import kiro_crew.agent_discovery as discovery_mod

        real_agents = tmp_path / "elsewhere"
        real_agents.mkdir()
        spec = real_agents / "a.json"
        spec.write_text(json.dumps({"name": "a"}))
        proj = tmp_path / "repo"
        (proj / ".kiro").mkdir(parents=True)
        os.symlink(real_agents, proj / ".kiro" / "agents")

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(discovery_mod, "_WINDOWS", True)
        sel_events: list[dict] = []
        monkeypatch.setattr(
            discovery_mod,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        clear_project_agent_cache()

        assert project_agent_files(str(proj)) == [proj / ".kiro" / "agents" / "a.json"]
        assert not any(
            e.get("outcome") == "denied" for e in sel_events
        ), f"an ordinary leaf link must not be audited as a sensitive denial: {sel_events}"

    @requires_symlinks
    def test_leaf_link_into_unc_is_still_refused_before_any_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """The narrowed screen still stops the SMB/NTLM probe it exists for: a
        leaf ``.kiro/agents`` that is a link whose stored target is UNC-shaped
        (``//host/share``) is refused by :func:`_link_chain_refused`, the
        screen the owner-supplied paths run before any resolving syscall,
        exactly as an ancestor junction is refused.
        """
        import kiro_crew.agent_discovery as discovery_mod

        proj = tmp_path / "repo"
        (proj / ".kiro").mkdir(parents=True)
        agents_dir = proj / ".kiro" / "agents"
        os.symlink("//attacker/share/agents", agents_dir)

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: False)
        monkeypatch.setattr(discovery_mod, "_WINDOWS", True)

        assert discovery_mod._link_chain_refused(str(agents_dir)) is True

    def test_scan_follows_a_benign_leaf_link_when_pinning_is_supported(
        self, tmp_path, monkeypatch
    ) -> None:
        """Confirms the ancestor pin does not also refuse the LEAF for being a
        link: ``.kiro``/``.kiro/agents`` symlinked into an ordinary directory
        (a dotfiles-managed checkout) must still be scanned, matching the
        pre-existing POSIX contract byte for byte.
        """
        from kiro_crew.agent_discovery import _pinned_scan_dir

        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "repo-dev.json").write_text("{}", encoding="utf-8")
        link = tmp_path / "agents"
        link.mkdir()
        entry = MagicMock()
        entry.name = "repo-dev.json"
        state = _mock_pinned_directory(monkeypatch, real_path=target, entries=[entry])

        with _pinned_scan_dir(link) as entries:
            assert entries is not None, "a benign leaf link must be scanned, not refused"
            assert [e.name for e in entries] == ["repo-dev.json"]
        ((_, opened_name, open_kwargs),) = state["opens"]
        assert opened_name == "agents"
        assert not (
            open_kwargs["flags"] & getattr(os, "O_NOFOLLOW", 0)
        ), "the leaf open must follow a benign link; only its ancestors are no-follow"
        assert state["scans"] == [state["fd"]]
        assert state["closes"] == [state["fd"]]

    def test_swapped_ancestor_is_unverifiable_without_false_denial(
        self, tmp_path, monkeypatch
    ) -> None:
        """The sharp caller raises; legacy callers degrade without a denial audit.

        Covers both entry points that reach the pin: ``project_agent_files``
        (guarded at its own ``except ScanUnverifiable:``) and
        ``project_agent_names`` (which reaches the pin through
        ``_project_signature`` before it ever consults ``project_agent_files``,
        so a guard on the files path alone does not cover it).
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import ScanUnverifiable
        from kiro_crew.pinned_fs import PinnedPathRefusal

        proj = tmp_path / "repo"
        _project_agents_dir(proj)
        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        sel_events: list[dict] = []
        monkeypatch.setattr(
            discovery_mod,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )

        def _boom(*_a, **_kw):
            raise PinnedPathRefusal("an ancestor was swapped for a link")

        monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", _boom)

        with pytest.raises(ScanUnverifiable, match="ancestor was swapped"):
            project_agent_files(str(proj), raise_unverifiable=True)
        assert project_agent_files(str(proj)) == []

        clear_project_agent_cache()
        with pytest.raises(ScanUnverifiable, match="ancestor was swapped"):
            project_agent_names(str(proj), raise_unverifiable=True)
        assert project_agent_names(str(proj)) == frozenset()
        assert sel_events == [], f"an unverifiable scan emitted a denial: {sel_events}"

        # The degraded lookup above must not have cached a signature that a
        # LATER raise_unverifiable=True call could read as a verified-empty
        # cache hit and skip the scan (and its sharp signal) entirely.
        with pytest.raises(ScanUnverifiable, match="ancestor was swapped"):
            project_agent_names(str(proj), raise_unverifiable=True)

    def test_sensitive_resolved_parent_is_denied_before_filesystem_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """A mocked symlinked ``.kiro`` is judged before its target is stat'd or opened."""
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir_fd

        sensitive = tmp_path / "sensitive"
        sensitive.mkdir()
        project = tmp_path / "repo"
        (project / ".kiro").mkdir(parents=True)
        agents_dir = project / ".kiro" / "agents"
        sensitive_real = os.path.realpath(sensitive)
        touched: list[str] = []
        sel_events: list[dict] = []
        fake_fd = 7302
        real_isdir = discovery_mod.os.path.isdir
        real_realpath = discovery_mod.os.path.realpath
        path_double = SimpleNamespace(**vars(os.path))
        os_double = SimpleNamespace(**vars(os))
        os_double.path = path_double
        os_double.close = lambda _fd: None
        monkeypatch.setattr(discovery_mod, "os", os_double)

        def simulated_realpath(path) -> str:
            if os.fspath(path) == os.fspath(project / ".kiro"):
                return sensitive_real
            return real_realpath(path)

        monkeypatch.setattr(
            discovery_mod,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw)),
        )
        monkeypatch.setattr(
            discovery_mod,
            "is_sensitive_path",
            lambda path: os.fspath(path) == sensitive_real,
        )
        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(discovery_mod.os.path, "realpath", simulated_realpath)
        monkeypatch.setattr(discovery_mod, "fd_real_path", lambda _fd: sensitive_real)

        def guarded_isdir(path) -> bool:
            if os.fspath(path) == sensitive_real:
                touched.append("isdir")
                raise AssertionError("sensitive parent was stat'd before denial")
            return real_isdir(path)

        def guarded_open(parent, *args, **kwargs):
            if os.fspath(parent) == sensitive_real:
                touched.append("open")
                raise AssertionError("sensitive parent was opened before denial")
            return fake_fd

        monkeypatch.setattr(discovery_mod.os.path, "isdir", guarded_isdir)
        monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", guarded_open)

        with _pinned_scan_dir_fd(agents_dir) as (entries, dir_fd, overflow):
            assert entries is None, "a sensitive resolved parent must use the denied sentinel"
            assert dir_fd is None
            assert overflow is False

        clear_project_agent_cache()
        assert project_agent_files(str(project), raise_unverifiable=True) == []
        assert project_agent_names(str(project), raise_unverifiable=True) == frozenset()
        assert any(event.get("outcome") == "denied" for event in sel_events)
        assert touched == []

    def test_a_sensitive_resolved_target_is_refused(self, tmp_path, monkeypatch) -> None:
        """The held descriptor's REAL path, not its mutable name, is sensitivity-checked."""
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir

        secret = tmp_path / "creds_home"
        secret.mkdir()
        link = tmp_path / "agents"
        link.mkdir()

        monkeypatch.setattr(
            discovery_mod,
            "is_sensitive_path",
            lambda p: os.path.realpath(str(p)) == os.path.realpath(str(secret)),
        )
        state = _mock_pinned_directory(monkeypatch, real_path=secret, entries=[])

        with _pinned_scan_dir(link) as entries:
            assert entries is None, "a leaf resolving into a sensitive tree must be refused"
        assert state["scans"] == [], "a sensitive held target was enumerated before refusal"
        assert state["closes"] == [state["fd"]]

    def test_a_missing_scan_dir_is_absent_not_unverifiable(self, tmp_path, monkeypatch) -> None:
        """A checkout with no ``.kiro`` yet is the ordinary case: absence, not
        a failed or refused scan -- must not raise and must not read as denied.
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        monkeypatch.setattr(
            discovery_mod,
            "open_in_pinned_parent",
            lambda *_a, **_kw: pytest.fail("an absent parent must short-circuit before pinning"),
        )

        with _pinned_scan_dir(tmp_path / "repo" / ".kiro" / "agents") as entries:
            assert entries == ()

    def test_a_strict_enumeration_failure_raises_scan_unverifiable(
        self, tmp_path, monkeypatch
    ) -> None:
        """An owner-supplied (``unsupported_ok=False``) scan whose ``os.scandir``
        raises ``OSError`` (e.g. a FUSE/NFS ``readdir`` EIO) must surface
        :class:`ScanUnverifiable`, never an empty tuple -- the confident-wrong-
        answer GPT 5.6 flagged: "not in this project" where the truth is
        "could not be read".
        """
        from kiro_crew.agent_discovery import ScanUnverifiable, _pinned_scan_dir_fd

        agents_dir = tmp_path / "repo" / ".kiro" / "agents"
        agents_dir.parent.mkdir(parents=True)
        state = _mock_pinned_directory(monkeypatch, real_path=agents_dir, entries=[])
        import kiro_crew.agent_discovery as discovery_mod

        def fake_scandir(target):
            state["scans"].append(target)
            raise OSError(errno.EIO, "input/output error")

        monkeypatch.setattr(discovery_mod.os, "scandir", fake_scandir)

        with pytest.raises(ScanUnverifiable, match="could not enumerate"):
            with _pinned_scan_dir_fd(agents_dir, unsupported_ok=False) as (
                entries,
                dir_fd,
                overflow,
            ):
                pass  # pragma: no cover - the raise happens before a yield

        # The descriptor is still closed: the raise runs inside the
        # try/finally that owns it, so a strict refusal must not leak the fd.
        assert state["closes"] == [state["fd"]]

    def test_a_legacy_enumeration_failure_still_degrades_to_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        """The fence: the SAME ``os.scandir`` ``OSError`` on the LEGACY
        (``unsupported_ok=True``) path must keep degrading to an empty tuple.
        Widening the strict-path fix to every caller would break Slack and
        per-turn resolution, which depend on degrade-to-empty on every platform.
        That path is the by-name walk even where pinning is available, so the
        held-descriptor open is never reached.
        """
        from kiro_crew.agent_discovery import _pinned_scan_dir_fd

        agents_dir = tmp_path / "repo" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        state = _mock_pinned_directory(monkeypatch, real_path=agents_dir, entries=[])
        import kiro_crew.agent_discovery as discovery_mod

        def fake_scandir(target):
            state["scans"].append(target)
            raise OSError(errno.EIO, "input/output error")

        monkeypatch.setattr(discovery_mod.os, "scandir", fake_scandir)

        with _pinned_scan_dir_fd(agents_dir, unsupported_ok=True) as (
            entries,
            dir_fd,
            overflow,
        ):
            assert entries == ()
            assert dir_fd is None
            assert overflow is False
        assert state["scans"] == [agents_dir]
        assert state["opens"] == []
        assert state["closes"] == []

    def test_a_plain_file_leaf_is_absence_not_a_refusal(self, tmp_path, monkeypatch) -> None:
        """Settles the brief's line-1111 judgment call from the code: on POSIX,
        ``open_in_pinned_parent``'s final ``os.open(name, O_DIRECTORY, ...)``
        raises ``NotADirectoryError`` directly for a LEAF that is a plain file
        -- this is not ``pin_parent``'s ancestor-swap ``PinnedPathRefusal``
        (that only wraps ELOOP/ENOTDIR on ancestor components, and is caught
        separately). There is nothing behind a regular file to protect or to
        have been swapped, so this stays absence on every ``unsupported_ok``
        value -- it must NOT raise ``ScanUnverifiable`` even when strict.
        """
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir_fd

        project = tmp_path / "repo"
        project.mkdir()
        # `.kiro` exists as a plain FILE, not a directory: the malformed-
        # checkout case the line-1111 comment names.
        (project / ".kiro").write_text("not a directory\n")
        agents_scope = project / ".kiro" / "agents"

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)

        for unsupported_ok in (False, True):
            with _pinned_scan_dir_fd(agents_scope, unsupported_ok=unsupported_ok) as (
                entries,
                dir_fd,
                overflow,
            ):
                assert (
                    entries == ()
                ), f"a plain-file leaf must read as absence, unsupported_ok={unsupported_ok}"
                assert dir_fd is None
                assert overflow is False


class TestPosixPinnedScanIsDescriptorRelative:
    """The POSIX branch must not re-resolve the directory by NAME after checking
    it: a writable project lets an attacker swap ``.kiro/agents`` for a symlink
    into a credential home between the check and the read, and a name-based
    ``glob`` would follow the swap while a descriptor-relative ``scandir`` reads
    the inode that was validated.
    """

    def test_a_mid_enumeration_error_degrades_instead_of_crashing(
        self, tmp_path, monkeypatch
    ) -> None:
        """A `@contextmanager` generator may yield exactly ONCE.

        With a lazy iterator, an `OSError` raised while the CALLER iterates is
        thrown back in at the yield, and yielding again from the handler raises
        `RuntimeError: generator didn't stop after throw()` -- which aborts the
        caller's whole command (Slack agent resolution) instead of degrading to
        "no project agents". Materializing before the yield is what makes the
        failure a value rather than a crash.
        """
        from kiro_crew.agent_discovery import _pinned_scan_dir

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "repo-dev.json").write_text("{}", encoding="utf-8")
        entry = MagicMock()
        entry.name = "repo-dev.json"
        state = _mock_pinned_directory(monkeypatch, real_path=agents, entries=[entry])

        with _pinned_scan_dir(agents) as entries:
            assert entries is not None
            # The contract that makes this safe: what is handed over is already
            # read, so nothing can fail partway through the caller's loop.
            assert isinstance(entries, list), (
                "entries must be materialized before the yield, or a mid-loop "
                "OSError re-enters the generator and crashes the caller"
            )
            assert [e.name for e in entries] == ["repo-dev.json"]
        assert state["scans"] == [state["fd"]]

    def test_entries_come_from_the_validated_inode_after_a_name_swap(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.agent_discovery import _pinned_scan_dir

        real = tmp_path / "agents"
        real.mkdir()
        (real / "repo-dev.json").write_text("{}", encoding="utf-8")

        decoy = tmp_path / "credentials"
        decoy.mkdir()
        (decoy / "stolen.json").write_text("{}", encoding="utf-8")
        entry = MagicMock()
        entry.name = "repo-dev.json"
        state = _mock_pinned_directory(monkeypatch, real_path=real, entries=[entry])

        with _pinned_scan_dir(real) as entries:
            assert entries is not None
            # The swap lands AFTER the open and before the listing is consumed,
            # which is exactly the window the finding describes.
            real.rename(tmp_path / "moved")
            decoy.rename(tmp_path / "agents")
            names = sorted(e.name for e in entries)

        assert names == ["repo-dev.json"], (
            "enumeration followed the swapped NAME instead of reading the "
            "descriptor it validated"
        )
        assert state["scans"] == [state["fd"]], "enumeration used a path, not the held fd"

    def test_a_missing_directory_is_nothing_here_not_a_denial(self, tmp_path, monkeypatch) -> None:
        """An absent `.kiro/agents` is the ordinary "no project agents" case; a
        denial here would emit a false security audit on every plain checkout."""
        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _pinned_scan_dir

        monkeypatch.setattr(discovery_mod, "supports_pinned_walk", lambda: True)
        pin_attempts: list[tuple[object, str, dict[str, object]]] = []

        def missing_leaf(parent, name, **kwargs):
            pin_attempts.append((parent, name, kwargs))
            raise FileNotFoundError(name)

        monkeypatch.setattr(discovery_mod, "open_in_pinned_parent", missing_leaf)

        with _pinned_scan_dir(tmp_path / "does-not-exist") as entries:
            assert entries is not None
            assert list(entries) == []
        assert len(pin_attempts) == 1
        assert pin_attempts[0][1] == "does-not-exist"


class TestProjectSignatureSnapshotBinding:
    def test_roster_from_a_different_directory_identity_is_not_cached(
        self, tmp_path, monkeypatch
    ) -> None:
        """A cache entry binds the signature and roster to one directory inode."""
        import contextlib

        import kiro_crew.agent_discovery as discovery_mod

        project = tmp_path / "repo"
        agents_dir = _project_agents_dir(project)
        (agents_dir / "poison.json").write_text(json.dumps({"name": "poison"}))
        clear_project_agent_cache()

        witnesses = [tmp_path / name for name in ("kiro-witness", "old-agents", "new-agents")]
        for witness in witnesses:
            witness.write_text(witness.name)
        fds = [os.open(witness, os.O_RDONLY) for witness in witnesses]
        calls = iter(
            (
                ((), fds[0], False),
                ((), fds[1], False),
                ((SimpleNamespace(name="poison.json"),), fds[2], False),
            )
        )

        @contextlib.contextmanager
        def swapped_scan(_directory, **_kwargs):
            yield next(calls)

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", swapped_scan)
        try:
            assert project_agent_names(project) == frozenset()
        finally:
            for fd in fds:
                os.close(fd)

        assert str(project) not in discovery_mod._PROJECT_NAMES_CACHE, (
            "names read from the replacement directory were cached under the "
            "original directory's signature"
        )

    def test_a_repoint_inside_the_request_window_audits_its_denial_and_still_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A scope readable when its signature was taken and sensitive when the
        scan runs is BOTH audited as denied AND refused.

        The identity compare raises on that mismatch, so ordering it ahead of the
        ``entries is None`` audit loses the denial row: the request is refused
        with 503 while SEL carries only the caller's ``allowed`` row, which reads
        as a scan that was permitted. Auditing first must not cost the refusal
        either -- dropping the compare would let a roster built WITHOUT the
        sensitive directory be cached against the pre-repoint signature.

        The DENIAL half is asserted on every platform, because that is the
        finding. The RAISE half needs a recorded directory identity to mismatch,
        which a platform that cannot pin a descriptor never produces -- there the
        scan refuses the directory without a snapshot error, and the test asserts
        that rather than skipping.
        """
        import contextlib

        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _ProjectSnapshotChanged, _scan_project_agent_files

        proj = tmp_path / "repo"
        agents_dir = _project_agents_dir(proj)
        (agents_dir / "a.json").write_text(json.dumps({"name": "a"}))
        # Taken while the directory is still readable, over the pinned walk the
        # owner-supplied scan uses: only that walk records the identity rows the
        # compare below fails to observe (the by-name walk records none). Where
        # the platform cannot pin, there is no identity to mismatch.
        readable = discovery_mod._project_signature(
            proj, unsupported_ok=not discovery_mod.supports_pinned_walk()
        )
        identity_recorded = discovery_mod._signature_dir_identity(readable[1]) is not None

        @contextlib.contextmanager
        def _now_sensitive(_d, **_kw):
            yield None, None, False

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", _now_sensitive)
        denials: list[dict] = []
        monkeypatch.setattr(discovery_mod, "_audit_denied", lambda **kw: denials.append(kw))

        if identity_recorded:
            with pytest.raises(_ProjectSnapshotChanged):
                _scan_project_agent_files(
                    proj, expected_signatures=readable, raise_unverifiable=True
                )
        else:
            specs = _scan_project_agent_files(
                proj, expected_signatures=readable, raise_unverifiable=True
            )
            assert specs == [], (
                "the sensitive directory was refused, so it must contribute no "
                f"spec even without an identity to mismatch: {specs}"
            )

        assert [d["error"] for d in denials] == [
            "sensitive scan dir rejected"
        ], f"the refused scan recorded no denial: {denials}"
        assert denials[0]["resources"] == str(agents_dir)


class TestProjectSignatureSensitiveDirSentinel:
    """A sensitive subdir's cache signature must differ from an empty dir's.

    GPT flagged that ``_project_signature`` returning ``()`` for BOTH "empty"
    and "sensitive, skipped" lets a cache warmed on a legitimately empty
    ``.kiro/agents`` survive an attacker later swapping that dir to a symlink
    into a credential home: the next call's signature is still ``()``, so
    ``project_agent_names`` treats it as an unchanged cache hit and never calls
    ``project_agent_files`` -- whose call is what emits the required SEL denial
    audit for the sensitive dir. The fix is a sentinel that cannot collide with
    any real ``_dir_signature`` output.
    """

    def test_a_refused_dir_is_never_served_from_cache(self, tmp_path, monkeypatch) -> None:
        """Every lookup of a refused directory must re-scan, because the scan is
        what emits the SEL denial.

        A stable sentinel matches the cached signature on the next lookup, so the
        cached result is served and the repeat probe goes unaudited -- the first
        attempt is recorded and an attacker's subsequent ones are silent.
        """
        import contextlib as _contextlib

        import kiro_crew.agent_discovery as discovery_mod
        from kiro_crew.agent_discovery import _project_signature

        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)

        @_contextlib.contextmanager
        def _refused(_d, **_kw):
            yield None, None, False

        monkeypatch.setattr(discovery_mod, "_pinned_scan_dir_fd", _refused)

        first = _project_signature(proj)
        second = _project_signature(proj)

        assert first != second, (
            "two lookups of a refused dir produced the SAME signature, so the "
            "second is a cache hit and its denial is never audited"
        )
        # Still distinguishable from a genuinely empty directory, which is the
        # other job this sentinel has to do.
        assert all(part != () for part in first), first


class TestTheRosterSignatureDoesNotTraverseChildEntries:
    """``_entries_signature`` fingerprints entry NAMES without following them."""

    def test_a_dangling_spec_symlink_is_fingerprinted_not_followed(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT flagged ``DirEntry.stat()``, which FOLLOWS by default.

        On Windows, statting a name that is a symlink to ``\\\\host\\share`` IS the
        outbound SMB/NTLM authentication, and the signature walk reaches every
        child of the scanned directory before ``_read_agent_spec``'s
        resolved-target guard runs. The directory hold protects the scan directory
        from being swapped; it says nothing about an entry planted inside it.

        A DANGLING link is the oracle and needs no Windows host: a following stat
        raises on it, which the old code swallowed into a ``0`` mtime, while a
        non-following stat reads the LINK's own mtime. A ``0`` here would also be
        a correctness bug in its own right — every dangling link would share one
        fingerprint, so repointing one could not invalidate the cache.
        """
        from kiro_crew.agent_discovery import _entries_signature

        entry = MagicMock()
        entry.name = "planted.json"
        entry.path = str(tmp_path / entry.name)
        entry.is_symlink.return_value = True
        entry.stat.side_effect = [
            SimpleNamespace(st_mtime_ns=17),
            FileNotFoundError("simulated dangling target"),
        ]
        monkeypatch.setattr(os, "readlink", lambda _path: str(tmp_path / "absent-target.json"))

        sig = _entries_signature([entry])

        names = {name for name, _ in sig}
        assert "planted.json" in names, "the planted entry was not fingerprinted at all"
        mtimes = {name: m for name, m in sig}
        assert mtimes["planted.json"] != 0, (
            "the mtime came back 0, which means the stat FOLLOWED the link and "
            "failed on its absent target -- on Windows that stat is the SMB "
            "authentication this must not perform"
        )
        assert entry.stat.call_args_list[0].kwargs == {"follow_symlinks": False}

    @requires_symlinks
    def test_editing_a_symlinked_specs_target_invalidates_the_signature(self, tmp_path) -> None:
        """A symlinked spec (``~/.kiro/agents/mine.json`` -> a dotfiles copy)
        whose TARGET is edited must change the directory signature.

        The link's OWN mtime catches a repoint but not an edit to the file it
        resolves to, so fingerprinting the link alone leaves ``_LIST_AGENTS_CACHE``
        and ``_PARSED_SPECS_CACHE`` serving the pre-edit model/tools/prompt for the
        process lifetime -- the very staleness ``agents_dir_revision`` returns
        ``None`` for on a symlinked spec. The fix folds the followed target mtime
        in additively, keeping the non-following link mtime (its SMB safety and its
        repoint detection) intact.
        """
        from kiro_crew.agent_discovery import _dir_signature

        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        # The real spec lives OUTSIDE the scanned dir; the scanned entry links to it.
        target = tmp_path / "dotfiles" / "mine.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"name": "mine", "model": "old"}))
        (agents / "mine.json").symlink_to(target)

        before = _dir_signature(agents)

        # Edit the TARGET only, and stamp ONLY its mtime to a fixed, past value --
        # the link's own mtime is untouched, so a signature that fingerprints the
        # link alone cannot tell this from no change at all.
        target.write_text(json.dumps({"name": "mine", "model": "new"}))
        os.utime(target, (1_000_000, 1_000_000))

        after = _dir_signature(agents)
        assert after != before, (
            "a symlinked spec's target edit did not change the signature -- the "
            "list-agents and parsed-spec caches would serve the pre-edit spec"
        )

    @requires_symlinks
    def test_pinned_signature_tracks_a_symlinked_specs_target(self, tmp_path) -> None:
        """The project-cache signature must fingerprint a linked spec's target
        through the descriptor-relative scan, not only through ``_dir_signature``.
        """
        from kiro_crew.agent_discovery import _project_signature

        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        target = tmp_path / "dotfiles" / "mine.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"name": "mine", "model": "old"}))
        (agents / "mine.json").symlink_to(target)

        before = _project_signature(tmp_path)
        target_rows = {name: mtime for name, mtime in before[1]}
        assert (
            "mine.json\0target" in target_rows
        ), "the descriptor-relative signature omitted the linked target row"
        assert target_rows["mine.json\0target"] == target.stat().st_mtime_ns

        target.write_text(json.dumps({"name": "mine", "model": "new"}))
        os.utime(target, (1_000_000, 1_000_000))

        after = _project_signature(tmp_path)
        assert (
            after != before
        ), "editing a linked spec's target did not invalidate the pinned signature"
