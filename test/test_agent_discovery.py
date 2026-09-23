"""Tests for agent discovery in ``agent_discovery.py``.

Focus on the robustness/security guards around scanning ``~/.kiro/agents/*.json``:
- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- A ``*.json`` symlink pointing at a sensitive credential file must NOT be read.

Tests use a tmp_path fake $HOME so the real filesystem is never touched.

Split (file-line-cap gate) into sibling files that share the fixtures and test
doubles in ``agent_discovery_test_helpers``:
- ``test_agent_discovery_spec_coercion.py``
- ``test_agent_discovery_warnings.py``
- ``test_agent_discovery_scan_gate.py``
- ``test_agent_discovery_pinned_signature.py``
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agent_discovery_test_helpers import (
    _agents_dir,
    _mock_pinned_directory,
    _project_agents_dir,
    fake_home,
)

from conftest import requires_symlinks
from kiro_crew.agent_discovery import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    clear_list_agents_cache,
    clear_project_agent_cache,
    list_agents,
    project_agent_files,
    project_agent_name,
    project_agent_names,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX

__all__ = ["fake_home"]


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
        listed: list[list[Path]] = []
        real_split = ad.split_listed_spec_paths

        def _record(directory, paths, **kwargs):
            paths = list(paths)
            listed.append(paths)
            return real_split(directory, iter(paths), **kwargs)

        with monkeypatch.context() as scoped:
            state = _mock_pinned_directory(scoped, real_path=held, entries=[entry])
            scoped.setattr(ad, "split_listed_spec_paths", _record)
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
