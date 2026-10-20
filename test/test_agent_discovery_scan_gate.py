"""Tests for the pinned-walk scan capability gate (``TestScanCapabilityGate``).

Split out of ``test_agent_discovery.py`` (file-line-cap gate); see that module's
docstring for the full sibling-file map. Shares fixtures/doubles from
``agent_discovery_test_helpers``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agent_discovery_test_helpers import (
    _DISCOVERY_LOGGER,
    _fake_pinned_project_walk,
    _mock_pinned_directory,
    _project_agents_dir,
)
from windows_link_screen_helpers import simulate_windows_link_screen

from conftest import requires_symlinks
from kiro_crew.agent_discovery import (
    clear_project_agent_cache,
    project_agent_files,
    project_agent_names,
)


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

        with monkeypatch.context() as scoped:
            scoped.setattr(
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
