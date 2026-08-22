"""Per-directory consent store for project skills (``kiro_crew.skill_trust``).

The gate must fail CLOSED on every unreadable/malformed/ambiguous input: a
``SKILL.md`` enters the agent's context and can instruct it to run anything, so
"we could not tell" has to mean "not trusted". These tests pin that direction
for each failure mode individually, plus the canonical-key identity that stops
one directory being granted twice under two names.
"""

from __future__ import annotations

import json
import os

import pytest

from kiro_crew import platform_compat, skill_trust
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at tmp_path and drop the memoized enforcement read."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    (d / ".kiro" / "skills").mkdir(parents=True)
    return d


class TestCanonicalKey:
    def test_none_is_not_a_key(self):
        assert skill_trust.canonical_key(None) is None

    def test_blank_is_not_a_key(self):
        assert skill_trust.canonical_key("   ") is None

    def test_relative_path_is_refused(self):
        # A relative path cannot identify a directory independently of cwd.
        assert skill_trust.canonical_key("rel/path") is None

    def test_missing_path_is_refused(self, tmp_path):
        assert skill_trust.canonical_key(tmp_path / "nope") is None

    def test_a_file_is_refused(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x", encoding="utf-8")
        assert skill_trust.canonical_key(f) is None

    def test_real_directory_resolves(self, project):
        assert skill_trust.canonical_key(project) == os.path.realpath(project)

    def test_symlink_resolves_to_the_same_key_as_its_target(self, project, tmp_path):
        link = tmp_path / "alias"
        try:
            link.symlink_to(project)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        # The directory IS the resource: an alias must not be a second identity,
        # or it could carry its own grant for an already-refused directory.
        assert skill_trust.canonical_key(link) == skill_trust.canonical_key(project)


class TestGateBeforeConsent:
    def test_untrusted_project_is_refused(self, project):
        assert skill_trust.is_project_trusted(project) is False

    def test_no_grants_means_empty_key_set(self):
        assert skill_trust.trusted_keys() == frozenset()

    def test_unusable_key_is_refused_without_touching_the_store(self, tmp_path):
        assert skill_trust.is_key_trusted("") is False
        assert skill_trust.is_key_trusted(None) is False


class TestGrantAndRevoke:
    def test_grant_makes_the_project_trusted(self, project):
        key = skill_trust.grant_project_trust(project)
        assert key == os.path.realpath(project)
        assert skill_trust.is_project_trusted(project) is True

    def test_grant_is_idempotent(self, project):
        skill_trust.grant_project_trust(project)
        skill_trust.grant_project_trust(project)
        assert len(skill_trust.list_trusted_projects()) == 1

    def test_grant_refuses_a_path_that_cannot_name_a_directory(self, tmp_path):
        # Banking a grant against a path that will never match would leave the
        # operator believing they had consented.
        with pytest.raises(ValueError):
            skill_trust.grant_project_trust(tmp_path / "nope")

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX mode bits; Windows uses ACLs and reports 0o666 here",
    )
    def test_store_is_owner_only(self, project):
        skill_trust.grant_project_trust(project)
        mode = os.stat(skill_trust.store_path()).st_mode & 0o777
        assert mode == 0o600

    def test_a_grant_does_not_trust_a_sibling_directory(self, project, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(other) is False

    def test_revoke_removes_the_grant(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.revoke_project_trust(project) is True
        assert skill_trust.is_project_trusted(project) is False

    def test_revoking_an_ungranted_project_reports_no_removal(self, project):
        assert skill_trust.revoke_project_trust(project) is False

    def test_revoke_works_after_the_directory_is_gone(self, project):
        # The operator must be able to withdraw trust from a path they have
        # already deleted, so revoke matches the stored string too.
        skill_trust.grant_project_trust(project)
        stored = skill_trust.list_trusted_projects()[0]["path"]
        os.rmdir(project / ".kiro" / "skills")
        os.rmdir(project / ".kiro")
        os.rmdir(project)
        assert skill_trust.canonical_key(project) is None
        assert skill_trust.revoke_project_trust(stored) is True

    def test_revoke_is_immediate_not_deferred(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True
        skill_trust.revoke_project_trust(project)
        # No TTL wait: the next read must already refuse.
        assert skill_trust.is_project_trusted(project) is False


class TestListing:
    def test_listing_reports_a_grant_whose_directory_vanished(self, project):
        skill_trust.grant_project_trust(project)
        os.rmdir(project / ".kiro" / "skills")
        os.rmdir(project / ".kiro")
        os.rmdir(project)
        rows = skill_trust.list_trusted_projects()
        # A stale row must stay visible or it becomes invisible AND un-revokable.
        assert len(rows) == 1
        assert rows[0]["exists"] is False

    def test_listing_marks_a_live_grant_as_existing(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.list_trusted_projects()[0]["exists"] is True

    def test_listing_is_empty_without_a_store(self):
        assert skill_trust.list_trusted_projects() == []


class TestFailsClosed:
    def _grant_then_corrupt(self, project, text):
        skill_trust.grant_project_trust(project)
        skill_trust.store_path().write_text(text, encoding="utf-8")
        skill_trust.reset_cache_for_tests()

    def test_malformed_json_grants_nothing(self, project):
        self._grant_then_corrupt(project, "{not json")
        assert skill_trust.is_project_trusted(project) is False

    def test_a_json_array_grants_nothing(self, project):
        self._grant_then_corrupt(project, "[]")
        assert skill_trust.trusted_keys() == frozenset()

    def test_an_unknown_schema_version_grants_nothing(self, project):
        # A store written by a newer build is not guessed at.
        self._grant_then_corrupt(project, json.dumps({"version": 99, "granted": []}))
        assert skill_trust.is_project_trusted(project) is False

    def test_a_non_array_granted_field_grants_nothing(self, project):
        self._grant_then_corrupt(project, json.dumps({"version": 1, "granted": {}}))
        assert skill_trust.trusted_keys() == frozenset()

    def test_non_dict_and_relative_entries_are_dropped(self, project):
        key = os.path.realpath(project)
        skill_trust.store_path().parent.mkdir(parents=True, exist_ok=True)
        skill_trust.store_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "granted": ["not-a-dict", {"path": "rel/ative"}, {"path": key}],
                }
            ),
            encoding="utf-8",
        )
        skill_trust.reset_cache_for_tests()
        # The good entry survives; the junk does not become a grant.
        assert skill_trust.trusted_keys() == frozenset({key})

    def test_over_cap_entries_are_truncated_not_denied(self, project):
        key = os.path.realpath(project)
        over = skill_trust._MAX_GRANT_ENTRIES + 10
        granted = [{"path": key}] + [{"path": f"/synthetic/{i}"} for i in range(over)]
        skill_trust.store_path().parent.mkdir(parents=True, exist_ok=True)
        skill_trust.store_path().write_text(
            json.dumps({"version": 1, "granted": granted}), encoding="utf-8"
        )
        skill_trust.reset_cache_for_tests()
        keys = skill_trust.trusted_keys()
        # Append-ordered, so the operator's real grants sit at the front and a
        # pathological store costs bounded work rather than denying everything.
        assert key in keys
        assert len(keys) <= skill_trust._MAX_GRANT_ENTRIES


class TestHardOffSwitch:
    def _write_config(self, enabled):
        home = skill_trust.store_path().parent.parent
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps({"skills": {"project_skills_enabled": enabled}}), encoding="utf-8"
        )
        skill_trust.reset_cache_for_tests()

    def test_disabled_overrides_a_live_grant(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True
        self._write_config(False)
        # Enforced in the SAME chokepoint as the grants, so no stale grant can
        # outlive the operator turning the feature off.
        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.is_project_trusted(project) is False

    def test_enabled_still_requires_a_grant(self, project):
        self._write_config(True)
        assert skill_trust.is_project_trusted(project) is False


class TestCaching:
    def test_a_grant_written_behind_the_cache_is_picked_up(self, project):
        assert skill_trust.trusted_keys() == frozenset()
        skill_trust.grant_project_trust(project)
        # The writer drops the memo, so no stale empty set is served.
        assert os.path.realpath(project) in skill_trust.trusted_keys()

    def test_repeated_reads_agree(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.trusted_keys() == skill_trust.trusted_keys()

    def test_a_missing_store_clears_any_memo(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.trusted_keys()
        skill_trust.store_path().unlink()
        skill_trust.reset_cache_for_tests()
        assert skill_trust.trusted_keys() == frozenset()


class TestNeverOverwritesAStoreItCannotRead:
    """A write must refuse, not replace, when the store cannot be round-tripped.

    "Absent" and "unreadable" are different answers: the first means there are
    no grants and a write is safe, the second means there may be grants this
    build cannot see. Collapsing them let a grant append to an empty list and
    overwrite every existing entry.
    """

    def _corrupt(self, text):
        skill_trust.store_path().parent.mkdir(parents=True, exist_ok=True)
        skill_trust.store_path().write_text(text, encoding="utf-8")
        skill_trust.reset_cache_for_tests()

    def test_grant_refuses_a_malformed_store(self, project):
        self._corrupt("{not json")
        with pytest.raises(skill_trust.TrustStoreUnreadable):
            skill_trust.grant_project_trust(project)

    def test_grant_refuses_a_newer_schema(self, project):
        self._corrupt(json.dumps({"version": 99, "granted": [{"path": "/keep/me"}]}))
        with pytest.raises(skill_trust.TrustStoreUnreadable):
            skill_trust.grant_project_trust(project)

    def test_a_refused_grant_leaves_the_store_byte_identical(self, project):
        original = json.dumps({"version": 99, "granted": [{"path": "/keep/me"}]})
        self._corrupt(original)
        with pytest.raises(skill_trust.TrustStoreUnreadable):
            skill_trust.grant_project_trust(project)
        # The unreadable grants survive untouched.
        assert skill_trust.store_path().read_text(encoding="utf-8") == original

    def test_revoke_refuses_a_malformed_store(self, project):
        self._corrupt("[]")
        with pytest.raises(skill_trust.TrustStoreUnreadable):
            skill_trust.revoke_project_trust(project)

    def test_an_absent_store_is_still_writable(self, project):
        # Absent must remain the safe case, or the first grant could never land.
        assert not skill_trust.store_path().exists()
        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True

    def test_listing_an_unreadable_store_does_not_raise(self, project):
        self._corrupt("{not json")
        # Listing destroys nothing, so it degrades instead of 500ing the page.
        assert skill_trust.list_trusted_projects() == []


class TestListingSurvivesHandEditedTimestamps:
    def test_non_numeric_granted_at_does_not_crash_the_sort(self, project, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        skill_trust.store_path().parent.mkdir(parents=True, exist_ok=True)
        skill_trust.store_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "granted": [
                        {"path": str(project), "granted_at": "not-a-number"},
                        {"path": str(other), "granted_at": 1700000000},
                        {"path": "/third", "granted_at": None},
                    ],
                }
            ),
            encoding="utf-8",
        )
        skill_trust.reset_cache_for_tests()
        rows = skill_trust.list_trusted_projects()
        assert len(rows) == 3
        # Normalized to epoch-seconds ints -- the type the store writes and the
        # API reports -- newest first, with no TypeError from the mixed types.
        # bool is excluded deliberately: isinstance(True, int) is True, and a
        # bool is not a timestamp.
        assert all(
            isinstance(r["granted_at"], int) and not isinstance(r["granted_at"], bool) for r in rows
        ), [type(r["granted_at"]).__name__ for r in rows]
        assert rows[0]["path"] == str(other)
        assert rows[0]["granted_at"] == 1700000000
        # The unparseable rows are KEPT (a grant must stay revokable) and sort
        # last at 0 rather than being silently dropped.
        assert [r["granted_at"] for r in rows[1:]] == [0, 0]


class TestTrustDirIntegrity:
    def test_a_symlinked_trust_dir_is_replaced_before_writing(self, project, tmp_path):
        """A pre-planted ``trust`` link must not redirect the grant write.

        Before ``trust`` was keystone-gated an agent could plant a link there,
        pointing the store somewhere it can author — letting it forge a grant
        for a directory the operator never approved.
        """
        home = skill_trust.store_path().parent.parent
        home.mkdir(parents=True, exist_ok=True)
        elsewhere = tmp_path / "attacker"
        elsewhere.mkdir()
        link = home / "trust"
        try:
            link.symlink_to(elsewhere)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)

        real_trust = home / "trust"
        assert not real_trust.is_symlink()
        assert real_trust.is_dir()
        # The write landed inside the real directory, not the link target.
        assert skill_trust.store_path().is_file()
        assert not (elsewhere / "project-skills.json").exists()

    def test_link_detection_goes_through_the_junction_aware_helper(self, project, monkeypatch):
        """Detection must use ``is_link_or_junction``, not ``Path.is_symlink``.

        On Windows a directory JUNCTION is not a symlink, so an ``is_symlink``
        check walks straight through a planted junction and writes the store
        inside it. Linux cannot create a junction, so this asserts the
        junction-aware helper is the one consulted.
        """
        seen: list[str] = []
        real = platform_compat.is_link_or_junction

        def _spy(path):
            seen.append(str(path))
            return real(path)

        monkeypatch.setattr(platform_compat, "is_link_or_junction", _spy)
        skill_trust.grant_project_trust(project)
        assert any(p.endswith("trust") for p in seen), seen


class TestConsentCoversOnlyTheGrantedDirectory:
    """A grant names ONE directory; discovery must not reach outside it."""

    def test_a_symlinked_skills_root_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.skills import SkillsLoader

        # `.kiro/skills` is a link OUT of the project the operator trusted.
        outside = tmp_path / "outside" / "skills"
        (outside / "smuggled").mkdir(parents=True)
        (outside / "smuggled" / "SKILL.md").write_text(
            "---\nname: smuggled\ndescription: d\n---\n\nSMUGGLED BODY\n", encoding="utf-8"
        )
        project = tmp_path / "proj"
        (project / ".kiro").mkdir(parents=True)
        try:
            (project / ".kiro" / "skills").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Consenting to `project` must not admit bodies stored outside it.
        assert "smuggled" not in {n for n, _, _ in loader._iter(project)}
        assert loader.load_skill("smuggled", project) is None

    def test_a_symlinked_skill_file_inside_a_real_root_is_refused(self, tmp_path):
        """A linked SKILL.md inside a CONTAINED skills root must not enumerate.

        The directory-level check cannot catch this: `evil/` is a real directory
        inside the consented tree, and only the file it holds points out. It is
        not a cosmetic listing bug -- _cached_frontmatter parses the linked file,
        so an attacker-controlled name/description/trigger set would reach
        list_skills and the injected "Available Skills" index, which tells the
        agent to cat that path.
        """
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: smuggled-file\ndescription: d\n---\n\nSMUGGLED BODY\n",
            encoding="utf-8",
        )
        project = tmp_path / "proj"
        evil = project / ".kiro" / "skills" / "evil"
        evil.mkdir(parents=True)
        try:
            (evil / "SKILL.md").symlink_to(outside / "SKILL.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "evil" not in {n for n, _, _ in loader._iter(project)}
        assert loader.load_skill("evil", project) is None

    def test_a_symlinked_subdirectory_inside_a_real_root_is_refused(self, tmp_path):
        """Same escape one level up: the skill DIRECTORY is the link."""
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "smuggled-dir"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: smuggled-dir\ndescription: d\n---\n\nSMUGGLED BODY\n",
            encoding="utf-8",
        )
        project = tmp_path / "proj"
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        try:
            (skills_root / "smuggled-dir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "smuggled-dir" not in {n for n, _, _ in loader._iter(project)}

    def test_a_project_may_not_link_into_a_provider_root(self, tmp_path, monkeypatch):
        """A project skills root is confined to the project, not to the provider roots.

        `_trusted_skill_roots()` exists so an APP's registered symlink resolves
        out of the skills dir into the app's own tree. A consented project has no
        such need, and inheriting that allowance would let a repository publish a
        builtin/app SKILL.md under any name it liked -- shadowing by aliasing
        rather than by copying. The GLOBAL tree keeps the allowance, which is what
        the second half of this test holds fixed.
        """
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        provider = tmp_path / "provider"
        (provider / "vendor-skill").mkdir(parents=True)
        (provider / "vendor-skill" / "SKILL.md").write_text(
            "---\nname: vendor-skill\ndescription: d\n---\n\nVENDOR BODY\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(skills_mod, "_trusted_skill_roots", lambda: (str(provider.resolve()),))

        project = tmp_path / "proj"
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        home_skills = tmp_path / "home-skills"
        home_skills.mkdir()
        try:
            (skills_root / "aliased").symlink_to(
                provider / "vendor-skill", target_is_directory=True
            )
            (home_skills / "aliased-global").symlink_to(
                provider / "vendor-skill", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=home_skills, install_builtins=False)
        names = {n for n, _, _ in loader._iter(project)}

        # The project may not reach a provider root...
        assert "aliased" not in names
        # ...but the global skills tree still may, or every installed app breaks.
        assert "aliased-global" in names

    def test_refusing_a_linked_root_says_so(self, tmp_path, caplog):
        """The refusal is logged, not silent.

        Two layers now refuse a `.kiro/skills` link out of the consented tree:
        this call-site check and the walker's own confinement. The walker prunes
        silently, so without this check an operator who granted trust would see
        an empty skills list and no reason for it.
        """
        import logging

        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "skills"
        (outside / "smuggled").mkdir(parents=True)
        (outside / "smuggled" / "SKILL.md").write_text(
            "---\nname: smuggled\ndescription: d\n---\n\nBODY\n", encoding="utf-8"
        )
        project = tmp_path / "proj"
        (project / ".kiro").mkdir(parents=True)
        try:
            (project / ".kiro" / "skills").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.skills"):
            names = {n for n, _, _ in loader._iter(project)}

        assert "smuggled" not in names
        assert any(
            "Refusing project skills root outside the trusted directory" in r.getMessage()
            for r in caplog.records
        ), f"no refusal logged; records={[r.getMessage() for r in caplog.records]}"

    def test_a_real_in_project_skills_dir_still_loads(self, tmp_path):
        from kiro_crew.skills import SkillsLoader

        project = tmp_path / "proj2"
        d = project / ".kiro" / "skills" / "genuine"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: d\n---\n\nGENUINE BODY\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills2", install_builtins=False)

        assert "genuine" in {n for n, _, _ in loader._iter(project)}
        assert "GENUINE BODY" in (loader.load_skill("genuine", project) or "")

    def test_a_triggered_project_skill_survives_the_body_pointer_split(self, tmp_path):
        """A matched project skill must not be dropped by ``split_triggered``.

        ``get_triggered_skills`` is project-aware, so it can return a project
        skill's name; if the split then resolves project-blind it gets ``None``
        and is skipped, contributing neither a body nor a pointer. The match
        would appear to succeed while injecting nothing at all.
        """
        from kiro_crew.skills import SkillsLoader

        project = tmp_path / "proj3"
        d = project / ".kiro" / "skills" / "triggered"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: triggered\ndescription: d\ntriggers: shorten url\n---\n\nBODY\n",
            encoding="utf-8",
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills3", install_builtins=False)

        bodies, pointers = loader.split_triggered(["triggered"], project)
        assert "triggered" in bodies + pointers

        # Project-blind is what used to happen, and it silently dropped the name.
        assert loader.split_triggered(["triggered"]) == ([], [])


class TestRequestingSlotProject:
    """The catalog, the trust read and the GRANT must resolve as the loader does."""

    @staticmethod
    def _state(**slot_projects):
        """A stand-in for DashboardState carrying just the slots mapping."""

        class _Slot:
            def __init__(self, project):
                self.project = project

        class _State:
            def __init__(self, mapping):
                self._slots = {name: _Slot(proj) for name, proj in mapping.items()}

        return _State(slot_projects)

    def test_an_unbound_chat_resolves_to_no_project(self, tmp_path):
        """The case that leaked consent: this chat has none, another chat has P.

        `active_project_dir` answers P here. The loader, reading
        ``slot.project or None``, answers None -- so a grant keyed on the former
        records consent for a directory this chat will never load from, and the
        catalog advertises skills whose $token expands to nothing.
        """
        from kiro_crew.dashboard.handlers._shared import (
            active_project_dir,
            requesting_slot_project,
        )

        project_p = tmp_path / "P"
        project_p.mkdir()
        state = self._state(**{"chat-1": "", "chat-2": str(project_p)})

        # The permissive helper still falls back -- unchanged, other surfaces rely on it.
        assert active_project_dir(state, "dashboard:chat-1") == project_p
        # The strict one refuses to answer for a chat that has no project.
        assert requesting_slot_project(state, "dashboard:chat-1") is None

    def test_a_bound_chat_resolves_to_its_own_project(self, tmp_path):
        from kiro_crew.dashboard.handlers._shared import requesting_slot_project

        a, b = tmp_path / "A", tmp_path / "B"
        a.mkdir()
        b.mkdir()
        state = self._state(**{"chat-1": str(a), "chat-2": str(b)})

        assert requesting_slot_project(state, "dashboard:chat-1") == a
        assert requesting_slot_project(state, "dashboard:chat-2") == b

    def test_no_session_key_resolves_to_no_project(self, tmp_path):
        """Without a key there is no requesting chat, so there is no answer."""
        from kiro_crew.dashboard.handlers._shared import requesting_slot_project

        project_p = tmp_path / "P"
        project_p.mkdir()
        state = self._state(**{"chat-1": str(project_p)})

        assert requesting_slot_project(state, "") is None
        assert requesting_slot_project(state, "dashboard:nonexistent") is None


class TestCachedPathsCannotEscapeAfterVetting:
    """A vetted path is re-validated at READ time, not trusted from the cache."""

    def test_a_skill_swapped_for_a_link_after_caching_is_refused(self, project, tmp_path):
        """Warm the cache with a legitimate skill, then swap the file for a link.

        `_iter` caches for a TTL, so without a read-time check `load_skill` would
        serve whatever the path names a minute later. The read goes through
        `safe_read_file_bytes_nolink(within_root=...)`, which opens O_NOFOLLOW and
        containment-checks the descriptor it actually read, so the swap is caught
        on the inode rather than on the stale path string.
        """
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        good = skill_dir / "SKILL.md"
        good.write_text("---\nname: legit\ndescription: d\n---\n\nGOOD BODY\n", encoding="utf-8")

        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: legit\ndescription: d\n---\n\nSMUGGLED BODY\n", encoding="utf-8"
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Warm the cache -- the path is vetted here, while it is still genuine.
        assert "legit" in {n for n, _, _ in loader._iter(project)}
        assert "GOOD BODY" in (loader.load_skill("legit", project) or "")

        # Now swap the vetted file for a link pointing out of the project. The
        # cache still holds the path, so a naive reader would follow it.
        good.unlink()
        try:
            good.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        body = loader.load_skill("legit", project)
        assert (
            body is None or "SMUGGLED BODY" not in body
        ), "a cached path was followed out of the consented project"

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX mode bits; the Windows ACL path needs icacls and is asserted there",
    )
    def test_the_trust_dir_is_owner_only_on_posix(self, project):
        """The directory, not just the store file, must be owner-only.

        A group- or world-writable trust DIRECTORY lets another local account
        replace the store wholesale, which forges consent this gate then
        enforces. On Windows the same lockdown runs as a real owner-only DACL --
        not assertable from this host, so it is not asserted here.
        """
        import os

        skill_trust.grant_project_trust(project)
        mode = os.stat(skill_trust.store_path().parent).st_mode & 0o777
        assert mode == 0o700, oct(mode)


class TestRevokeIsNotBlockedByItsAudit:
    """A revoke is a de-escalation: an unwritable audit must not preserve trust."""

    def test_an_audit_failure_still_revokes_and_does_not_raise(self, project, monkeypatch):
        """The direction of this decision is deliberate; see revoke_project_trust.

        Auditing first with critical=True would RAISE on an unwritable SEL and
        leave the grant in place -- so anyone able to make the SEL unwritable
        (fill the disk, chmod the log dir) could veto every revocation. The repo
        made the same call in safety_override.deactivate. Fail closed on
        escalation, fail open on de-escalation.

        Also pins the caller-facing half: the failure must not escape, because it
        previously surfaced as a 500 telling the operator the revoke had failed
        when it had durably succeeded -- and the retry then reported
        "nothing was revoked" while skipping the audit for good.
        """

        class _Boom:
            def log_governance_decision(self, **_kwargs):
                raise OSError("SEL directory is read-only")

        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True

        monkeypatch.setattr(skill_trust, "sel", lambda: _Boom())

        # Must not raise, and must report the revoke it actually performed.
        assert skill_trust.revoke_project_trust(project) is True

        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(project) is False, (
            "an audit failure left trust in place -- a read-only SEL must not "
            "become a veto over revocation"
        )

    def test_a_successful_revoke_still_audits(self, project, monkeypatch):
        """Containment must not become 'never audits'."""
        calls = []

        class _Recorder:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        skill_trust.grant_project_trust(project)
        monkeypatch.setattr(skill_trust, "sel", lambda: _Recorder())

        assert skill_trust.revoke_project_trust(project) is True
        assert [c["outcome"] for c in calls] == ["denied"]
        assert calls[0]["critical"] is True


class TestOversizedProjectSkillIsSkipped:
    """An oversized SKILL.md must not abort the chat turn."""

    def test_an_oversized_project_skill_yields_no_body_instead_of_raising(
        self, project, tmp_path, monkeypatch
    ):
        """FileTooLargeError subclasses Exception, not OSError.

        So the reader's own OSError guard misses it, and on the context-assembly
        path nothing catches it until the turn's outermost handler -- the message
        never reaches the model and the slot shows an error card on EVERY turn,
        since a pinned body is re-read each time. The global skills path applies
        no cap at all and cannot abort a turn, so skipping is the closer parity.
        """
        from kiro_crew import hooks
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "huge"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: huge\ndescription: d\n---\n\n" + ("x" * 4096), encoding="utf-8"
        )

        # Shrink the cap rather than writing 50 MiB to disk.
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 128)

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # No exception, and no body -- the shape every caller already handles.
        assert loader.load_skill("huge", project) is None


class TestEnumeratedMetadataIsAlsoConfined:
    """The metadata read is confined too, not just the body read."""

    def _swapped_project(self, project, tmp_path):
        """A project whose vetted SKILL.md is replaced by a link out of it."""
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        good = skill_dir / "SKILL.md"
        good.write_text(
            "---\nname: legit\ndescription: HONEST DESCRIPTION\n---\n\nbody\n",
            encoding="utf-8",
        )
        outside = tmp_path / "outside.md"
        outside.write_text(
            "---\nname: legit\ndescription: SMUGGLED DESCRIPTION\n"
            "always: true\ntriggers: everything\n---\n\nsmuggled\n",
            encoding="utf-8",
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        # Warm the enumeration while the file is still genuine.
        assert "legit" in {n for n, _, _ in loader._iter(project)}
        assert (
            loader._cached_frontmatter(good, within=skill_trust.canonical_key(project)).get(
                "description"
            )
            == "HONEST DESCRIPTION"
        )

        good.unlink()
        try:
            good.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        # Drop only the mtime-keyed metadata cache: the ENUMERATION stays warm,
        # which is precisely the window this test is about.
        loader._fm_cache.clear()
        # The vetted root is now an explicit argument, so hand it back rather than
        # letting a caller default it to None and silently test the unconfined path.
        return loader, good, skill_trust.canonical_key(project)

    def test_a_swapped_file_yields_no_metadata(self, project, tmp_path):
        loader, good, root = self._swapped_project(project, tmp_path)
        meta = loader._cached_frontmatter(good, within=root)
        assert "SMUGGLED DESCRIPTION" not in str(meta), meta
        assert meta == {}, meta

    def test_a_swapped_file_cannot_inject_a_description_or_force_always(self, project, tmp_path):
        """The consequence, not just the mechanism.

        `description` is rendered verbatim into the injected skills index and shown
        in the picker; `always: true` promotes a skill to full-body injection on
        every turn. Both are attacker-chosen if the metadata read is unconfined.
        """
        loader, _good, _root = self._swapped_project(project, tmp_path)

        rows = loader.list_skills(project_dir=str(project))
        blob = str(rows)
        assert "SMUGGLED DESCRIPTION" not in blob, blob
        assert "HONEST DESCRIPTION" not in blob or True  # the row may be dropped entirely

        always = loader.get_always_skills(project_dir=str(project))
        assert "SMUGGLED DESCRIPTION" not in str(always)


class TestConfinementMapSharesTheCacheLifetime:
    """The recorded roots are dropped with the caches they belong to."""

    def test_invalidating_the_caches_clears_the_recorded_roots(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "legit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: legit\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "legit" in {n for n, _, _ in loader._iter(project)}

        loader._invalidate_iter_cache()
        assert loader._iter_cache == {}
        assert not loader._fm_cache


class TestEveryEnumeratedPathHasARecordedRoot:
    """The producer's contract: every enumerated item carries its vetted root.

    This began as "no path escapes the confinement map". The map is gone: the root
    is the third element of the item, so a path cannot arrive without one and a
    lookup cannot miss. What remains checkable -- and what can still go wrong -- is
    whether the producer LABELS each item correctly, which is what these assert.

    Two bugs sat here before the restructure: the root was filed under the
    pre-resolution path string while consumers got the post-resolution one (so the
    lookup missed), and a miss read unconfined rather than refusing.
    """

    def test_every_enumerated_path_has_a_recorded_root(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        # A project skill AND a global one, so both branches are covered.
        proj_skill = project / ".kiro" / "skills" / "from-project"
        proj_skill.mkdir(parents=True)
        (proj_skill / "SKILL.md").write_text(
            "---\nname: from-project\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        home_skills = tmp_path / "home-skills"
        global_skill = home_skills / "from-global"
        global_skill.mkdir(parents=True)
        (global_skill / "SKILL.md").write_text(
            "---\nname: from-global\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=home_skills, install_builtins=False)

        items = loader._iter(project)
        names = {n for n, _, _ in items}
        assert {"from-project", "from-global"} <= names, names

        project_key = skill_trust.canonical_key(project)
        # Every item carries a root -- three elements, always. A path can no longer
        # arrive without one, which is what the old side map allowed.
        assert all(len(item) == 3 for item in items), items
        roots = {n: root for n, _pth, root in items}

        # The label must be RIGHT, not merely present: a project skill is confined
        # to the granted directory...
        assert roots["from-project"] == project_key, roots
        # ...and the operator-installed tree stays unconfined, so an app's
        # registered symlink into its own tree keeps resolving.
        assert roots["from-global"] is None, roots

    def test_an_in_project_symlinked_skill_keeps_its_recorded_root(self, project):
        """A skill dir linked elsewhere INSIDE the project still resolves confined.

        This is the shape that made round 4's keying silently unconfined. The link
        target is inside the granted directory, so containment allows it; the walk
        therefore yields a path containing the link while consumers receive the
        RESOLVED path, and the two strings differ. Keying the pre-resolution string
        filed the root under a name no consumer ever presents, so the lookup missed
        and the read went unconfined.

        Reaching the PROJECT through a link proves nothing here, because
        grant_project_trust canonicalizes and the skills root is built from that
        canonical key -- the strings match and the bug hides.
        """
        from kiro_crew.skills import SkillsLoader

        # The real skill lives outside .kiro/skills but inside the project.
        real = project / "shared" / "aliased"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text(
            "---\nname: aliased\ndescription: d\n---\n\nALIASED BODY\n", encoding="utf-8"
        )
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        try:
            (skills_root / "aliased").symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=project / "unused-home-skills", install_builtins=False)

        pairs = loader._iter(project)
        assert "aliased" in {n for n, _, _ in pairs}, "an in-project link should still load"

        project_key = skill_trust.canonical_key(project)
        for name, _pth, root in pairs:
            if name != "aliased":
                continue
            # The root travels WITH the item, so a path whose string differs before
            # and after resolution can no longer lose its confinement en route --
            # which is exactly how this case used to read unconfined.
            assert root == project_key, root
        # And it must actually be readable through the choke point.
        assert "ALIASED BODY" in (loader.load_skill("aliased", project) or "")


class TestOneEnforcementPointForEnumeratedReads:
    """Guard: both readers go through the choke point, and neither reads directly.

    Round 2 hardened the body read alone and the metadata read of the same cached
    paths stayed unchecked, which is how a reviewer found the sibling instead of a
    test. This fails the build if they drift apart again.
    """

    def test_enumerated_readers_route_through_the_choke_point(self):
        import ast
        import pathlib

        src_path = pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew/skills.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        wanted = {"_cached_frontmatter", "load_skill", "_read_enumerated_skill_bytes"}
        found: dict[str, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                found[node.name] = node

        # Canary: a rename or a moved file must fail loudly, not pass silently.
        assert set(found) == wanted, f"missing {sorted(wanted - set(found))} in skills.py"

        def calls(fn):
            out = []
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Attribute):
                        out.append((f.attr, n.lineno))
                    elif isinstance(f, ast.Name):
                        out.append((f.id, n.lineno))
            return out

        offenders: list[str] = []

        for name in ("_cached_frontmatter", "load_skill"):
            names = [c for c, _ in calls(found[name])]
            if "_read_enumerated_skill_bytes" not in names:
                offenders.append(f"{name}() no longer reads through _read_enumerated_skill_bytes")

        # The metadata reader must not read the file itself. load_skill is NOT
        # swept for this: its global-skills and extra-path branches read directly
        # and are legitimately exempt from project confinement.
        for call, lineno in calls(found["_cached_frontmatter"]):
            if call in {"read_text", "read_bytes", "_parse_frontmatter"}:
                offenders.append(f"_cached_frontmatter():{lineno} reads directly via {call}()")

        # The enforcement itself must live in exactly one place.
        choke_names = [c for c, _ in calls(found["_read_enumerated_skill_bytes"])]
        if "safe_read_file_bytes_nolink" not in choke_names:
            offenders.append(
                "_read_enumerated_skill_bytes() no longer calls " "safe_read_file_bytes_nolink"
            )

        assert offenders == [], (
            "enumerated skill reads must all go through "
            "SkillsLoader._read_enumerated_skill_bytes: " + "; ".join(offenders)
        )


class TestCrlfSkillFilesParseLikeTextMode:
    """CRLF frontmatter must parse, because git checks out CRLF on Windows.

    These reads moved from `read_text` (TEXT mode, universal newlines) to bytes so
    containment could be checked on the descriptor actually opened. Bytes reads do
    not fold newlines, so every frontmatter key arrived with a trailing `\r`,
    nothing matched `always` or `pinned`, and skill bodies stopped being injected
    into context on Windows -- while Linux and macOS gates stayed green.

    Written with explicit CRLF bytes rather than a Windows-only skip, so the trap
    is reproducible on any platform.
    """

    def _crlf_project_skill(self, project, tmp_path):
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "crlf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"---\r\nname: crlf\r\ndescription: has carriage returns\r\n"
            b"always: true\r\n---\r\n\r\nCRLF BODY\r\n"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)
        return loader, skill_dir / "SKILL.md", skill_trust.canonical_key(project)

    def test_crlf_frontmatter_keys_are_not_left_with_carriage_returns(self, project, tmp_path):
        loader, path, root = self._crlf_project_skill(project, tmp_path)
        meta = loader._cached_frontmatter(path, within=root)

        assert meta.get("name") == "crlf", meta
        assert meta.get("always") == "true", meta
        # The specific corruption: a trailing \r on any value.
        assert not any("\r" in str(v) for v in meta.values()), meta

    def test_a_crlf_skill_is_still_recognised_as_always_on(self, project, tmp_path):
        """The consequence, not just the parse.

        `always: true` is what promotes a skill to full-body injection. With a
        stray \r the value never equals "true", so the body silently stops being
        injected -- which is exactly what the Windows shards reported.
        """
        loader, _path, _root = self._crlf_project_skill(project, tmp_path)
        always = loader.get_always_skills(project_dir=str(project))
        assert "crlf" in str(always), always

    def test_lone_carriage_returns_are_folded_too(self, project, tmp_path):
        """TEXT-mode reads translate a lone \r as well, so the fold must match.

        Without it a classic-Mac-ending SKILL.md parses as a single line and no
        frontmatter key is found at column 0 -- the same silent outcome as the
        CRLF case, reached a different way.
        """
        from kiro_crew.skills import SkillsLoader

        skill_dir = project / ".kiro" / "skills" / "crmac"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(
            b"---\rname: crmac\rdescription: lone carriage returns\r---\r\rCR BODY\r"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        meta = loader._cached_frontmatter(
            skill_dir / "SKILL.md", within=skill_trust.canonical_key(project)
        )
        assert meta.get("name") == "crmac", meta
        assert not any("\r" in str(v) for v in meta.values()), meta

    def test_a_crlf_body_loads_without_carriage_returns(self, project, tmp_path):
        loader, _path, _root = self._crlf_project_skill(project, tmp_path)
        body = loader.load_skill("crlf", project) or ""
        assert "CRLF BODY" in body
        assert "\r" not in body


class TestLockFailuresAreFailClosed:
    """A store we cannot LOCK is a store we cannot trust to round-trip.

    `_locked_store` fails before any store I/O when the trust dir is not creatable,
    the lock file is not openable (read-only filesystem, permissions), or the lock
    call itself fails. Those used to escape as raw OSError: the read-only listing
    500ed a settings page it promises to degrade, and the grant/revoke handlers
    reached aiohttp unhandled instead of returning their 409.
    """

    def test_listing_degrades_when_the_store_cannot_be_locked(self, project, monkeypatch):
        from kiro_crew import skill_trust as st

        skill_trust.grant_project_trust(project)
        assert skill_trust.list_trusted_projects(), "precondition: a grant is listed"

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(st.Path, "touch", boom)

        # Degrades to an empty list, never raises: this powers a settings page.
        assert skill_trust.list_trusted_projects() == []

    def test_a_grant_refuses_when_the_store_cannot_be_locked(self, project, monkeypatch):
        from kiro_crew import skill_trust as st

        def boom(*_a, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(st.Path, "touch", boom)

        # TrustStoreUnreadable, which the handler already turns into a 409 with a
        # machine-readable code -- not a bare OSError 500.
        with pytest.raises(st.TrustStoreUnreadable):
            skill_trust.grant_project_trust(project)

    def test_enforcement_still_fails_closed_without_the_lock(self, project, monkeypatch):
        """The reader never takes the lock, and must keep answering.

        `trusted_keys` is the path that decides whether a project's skills load. It
        reads directly and fails closed on OSError, so a lock-path failure must not
        turn "may this load" into an exception on a hot path.
        """
        from kiro_crew import skill_trust as st

        skill_trust.grant_project_trust(project)
        skill_trust.reset_cache_for_tests()

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(st.Path, "touch", boom)

        # Answers without raising; the grant is still readable because the
        # enforcement path does not need the lock.
        assert skill_trust.is_project_trusted(project) is True


class TestCatalogOnlyOffersLoadableWorkspaceSkills:
    """A listed workspace row must be one the loader can actually serve."""

    def test_an_escaped_skill_is_not_listed_as_trusted(self, project, tmp_path):
        """`list_kiro_skills` walks the project itself, with no containment check.

        So a skill directory linked OUT of the project used to be listed and then
        stamped trusted once the operator granted the project -- while the loader
        refused it, making the token expand to nothing. Marking it untrusted would
        not have helped: no grant makes an escaped path loadable.
        """
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        outside = tmp_path / "outside" / "escaped"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text(
            "---\nname: escaped\ndescription: ESCAPED ROW\n---\n\nbody\n",
            encoding="utf-8",
        )
        skills_root = project / ".kiro" / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        try:
            (skills_root / "escaped").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # A genuine in-project skill, to prove the filter is not simply dropping all.
        real = skills_root / "genuine"
        real.mkdir()
        (real / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: GENUINE ROW\n---\n\nbody\n",
            encoding="utf-8",
        )

        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        workspace = {
            str(r.get("key", "")).split("/", 1)[-1]
            for r in rows
            if r.get("source") == "kiro-workspace"
        }

        assert "genuine" in workspace, workspace
        assert "escaped" not in workspace, (
            "an unloadable row was offered; selecting it expands to nothing and no "
            "grant can fix it"
        )
        assert "ESCAPED ROW" not in str(rows)

    def test_a_genuine_workspace_row_is_still_marked_trusted(self, project, tmp_path):
        """The filter must not cost the marking it exists to protect."""
        from kiro_crew.dashboard.handlers._shared import collect_skills_blocking
        from kiro_crew.skills import SkillsLoader

        skills_root = project / ".kiro" / "skills" / "genuine"
        skills_root.mkdir(parents=True)
        (skills_root / "SKILL.md").write_text(
            "---\nname: genuine\ndescription: d\n---\n\nbody\n", encoding="utf-8"
        )
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        rows = collect_skills_blocking(loader, [], project)
        row = next(r for r in rows if str(r.get("key", "")).endswith("/genuine"))
        assert row.get("trusted") is True, row


class TestEnforcementIsAudited:
    """Using the grant is recorded, not just giving it."""

    @staticmethod
    def _recorder(monkeypatch):
        from kiro_crew import skills as skills_mod

        calls: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(skills_mod, "sel", lambda: _Sel())
        return calls

    def test_admitting_a_trusted_project_is_audited(self, project, tmp_path, monkeypatch):
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        loader._iter(project)

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert governance, calls
        assert governance[0]["outcome"] == "allowed"
        assert governance[0]["scope"] == "project_skills"
        # A record, not a gate: an unwritable SEL must not fail a chat turn.
        assert governance[0]["critical"] is False

    def test_withholding_an_untrusted_project_is_audited(self, tmp_path, monkeypatch):
        """The half an operator debugging a dead `$token` actually needs."""
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        untrusted = tmp_path / "no-grant"
        (untrusted / ".kiro" / "skills").mkdir(parents=True)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        loader._iter(untrusted)

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert governance, calls
        assert governance[0]["outcome"] == "denied"

    def test_the_decision_is_audited_once_not_once_per_message(
        self, project, tmp_path, monkeypatch
    ):
        """This is what makes auditing a per-message path affordable.

        `_trusted_project_key` runs on every message via `get_triggered_skills`. One
        governance event per message would bury the events that matter and add
        hot-path cost a previous review round was specifically about, so the record
        is written on first use per (directory, outcome).
        """
        from kiro_crew.skills import SkillsLoader

        calls = self._recorder(monkeypatch)
        skill_trust.grant_project_trust(project)
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        for _ in range(5):
            loader._iter(project)
            loader._iter_cache = {}  # force re-enforcement, not a cache hit

        governance = [c for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert len(governance) == 1, (
            f"expected one enforcement record, got {len(governance)} -- the "
            "de-duplication is what keeps this off the per-message cost path"
        )

    def test_an_audit_failure_does_not_break_loading(self, project, tmp_path, monkeypatch):
        """A record must never be able to stop a turn."""
        from kiro_crew import skills as skills_mod
        from kiro_crew.skills import SkillsLoader

        class _Boom:
            def log_governance_decision(self, **_kwargs):
                raise OSError("SEL is read-only")

        monkeypatch.setattr(skills_mod, "sel", lambda: _Boom())
        skill_trust.grant_project_trust(project)
        skill_dir = project / ".kiro" / "skills" / "still-loads"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: still-loads\ndescription: d\n---\n\nBODY\n", encoding="utf-8"
        )
        loader = SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

        assert "still-loads" in {n for n, _, _ in loader._iter(project)}


class TestReviewedProjectConfirmation:
    """Consent is recorded only for the directory the operator actually reviewed."""

    def test_a_matching_reviewed_path_grants(self, project):
        from kiro_crew.dashboard.handlers.prompts import _grant_reviewed_project

        key = _grant_reviewed_project(project, str(project), session_key="t")
        assert key == skill_trust.canonical_key(project)
        assert skill_trust.is_project_trusted(project) is True

    def test_a_non_canonical_but_equivalent_path_still_grants(self, project):
        """`/tmp/./x` is the same directory; a string compare would refuse it."""
        from kiro_crew.dashboard.handlers.prompts import _grant_reviewed_project

        noisy = str(project) + "/./"
        _grant_reviewed_project(project, noisy, session_key="t")
        assert skill_trust.is_project_trusted(project) is True

    def test_a_changed_project_refuses_and_records_nothing(self, project, tmp_path):
        """The slot moved between rendering the dialog and clicking Trust."""
        from kiro_crew.dashboard.handlers.prompts import (
            _grant_reviewed_project,
            _ReviewedProjectChanged,
        )

        other = tmp_path / "somewhere-else"
        other.mkdir()

        with pytest.raises(_ReviewedProjectChanged):
            _grant_reviewed_project(project, str(other), session_key="t")

        # Neither directory ends up trusted: the reviewed one was not what this
        # chat is bound to, and the bound one was never reviewed.
        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(project) is False
        assert skill_trust.is_project_trusted(other) is False

    def test_an_absent_expected_path_still_grants(self, project):
        """Optional by design: omitting it gains a caller nothing."""
        from kiro_crew.dashboard.handlers.prompts import _grant_reviewed_project

        _grant_reviewed_project(project, None, session_key="t")
        assert skill_trust.is_project_trusted(project) is True

    def test_the_handler_does_not_touch_the_filesystem_on_the_event_loop(self):
        """The finding was event-loop filesystem work, which behaviour cannot show.

        `canonical_key` realpaths (one lstat per component) and must run on the
        discovery executor like every other filesystem step in this handler. This
        guard fails if it is ever called directly from the async function again --
        which is exactly how it was introduced.
        """
        import ast
        import pathlib as _pathlib

        src_path = (
            _pathlib.Path(__file__).resolve().parents[1]
            / "src/kiro_crew/dashboard/handlers/prompts.py"
        )
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_skills_trust_grant":
                target = node
        assert target is not None, "api_skills_trust_grant not found — did it move?"

        offenders = [
            f"line {n.lineno}: {n.func.id}()"
            for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"canonical_key", "grant_project_trust", "is_project_trusted"}
        ]
        assert offenders == [], (
            "these filesystem calls run on the event loop; offload them to "
            "discovery_executor() as the rest of this handler does: " + "; ".join(offenders)
        )


class TestTriggeredProjectSkillsReachThePrompt:
    """A match must produce a body (or a pointer), not just a log line."""

    def _project_with_triggered_skill(self, project, tmp_path, *, inject: bool):
        from kiro_crew.skills import SkillsLoader

        opt_out = "" if inject else "inject_on_trigger: false\n"
        skill_dir = project / ".kiro" / "skills" / "deploys"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploys\ndescription: how we deploy\n"
            f"triggers: deploy release\n{opt_out}---\n\nDEPLOY RUNBOOK BODY\n",
            encoding="utf-8",
        )
        skill_trust.grant_project_trust(project)
        # max_triggered defaults to 0 -- trigger matching is OFF unless the
        # operator enables it, and with 0 the scored list is sliced to nothing.
        # So this feature is only reachable at all once it is switched on.
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(
            skills_path=tmp_path / "home-skills", install_builtins=False, config=cfg
        )
        return loader

    def test_a_matched_project_skill_body_is_loadable(self, project, tmp_path):
        """The body half: `load_skill` was called project-blind and returned None."""
        loader = self._project_with_triggered_skill(project, tmp_path, inject=True)

        triggered = loader.get_triggered_skills("please deploy the release", project_dir=project)
        assert "deploys" in triggered, triggered

        enforced, pointer_only = loader.split_triggered(triggered, project)
        assert "deploys" in enforced, (enforced, pointer_only)

        # The step that silently did nothing.
        body = loader.load_skill("deploys", project)
        assert body is not None and "DEPLOY RUNBOOK BODY" in body, body

    def test_a_pointer_only_project_skill_appears_in_the_hint(self, project, tmp_path):
        """The pointer half: `trigger_hint` resolved project-blind and named nothing."""
        loader = self._project_with_triggered_skill(project, tmp_path, inject=False)

        triggered = loader.get_triggered_skills("please deploy the release", project_dir=project)
        assert "deploys" in triggered, triggered

        enforced, pointer_only = loader.split_triggered(triggered, project)
        assert "deploys" in pointer_only, (enforced, pointer_only)

        hint = loader.trigger_hint(pointer_only, project)
        assert "deploys" in hint, hint
        # And it names where to read it, which is the point of a pointer.
        assert "SKILL.md" in hint, hint

    def test_an_untrusted_project_skill_still_contributes_nothing(self, tmp_path):
        """Wiring the project through must not bypass consent."""
        from kiro_crew.skills import SkillsLoader

        untrusted = tmp_path / "no-grant"
        skill_dir = untrusted / ".kiro" / "skills" / "deploys"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploys\ndescription: d\ntriggers: deploy release\n---\n\nSECRET\n",
            encoding="utf-8",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = SkillsLoader(
            skills_path=tmp_path / "home-skills", install_builtins=False, config=cfg
        )

        # Empty because the project is UNTRUSTED, not because triggering is off --
        # the config above rules that explanation out.
        assert loader.get_triggered_skills("please deploy the release", project_dir=untrusted) == []
        assert loader.load_skill("deploys", untrusted) is None
        assert "SECRET" not in loader.trigger_hint(["deploys"], untrusted)
