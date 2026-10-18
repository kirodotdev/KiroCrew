"""Per-directory consent store for project skills (``kiro_crew.skill_trust``).

The gate must fail CLOSED on every unreadable/malformed/ambiguous input: a
``SKILL.md`` enters the agent's context and can instruct it to run anything, so
"we could not tell" has to mean "not trusted". These tests pin that direction
for each failure mode individually, plus the canonical-key identity that stops
one directory being granted twice under two names.

``test_skill_trust_store_project_skill_loading.py`` covers the skills loader's side of a
grant (confined discovery and reads, CRLF, undecodable and oversized files, the
enforcement audit, triggered and indexed skills), plus the trust directory's owner-only
mode. ``test_skill_trust_store_operator_consent.py`` covers the operator's side: the
dashboard trust endpoints, the reviewed-key confirmation, the workspace catalog, and how
grant, revoke and listing behave when the store cannot be locked or written, or when a
revoke's audit fails. All three share the ``project`` and autouse ``_isolated_home``
fixtures in ``skill_trust_store_helpers.py``.
"""

from __future__ import annotations

import json
import os

import pytest
from skill_trust_store_helpers import (  # noqa: F401 -- fixtures register by import or by name
    _isolated_home,
    project_fixture,
)

from kiro_crew import platform_compat, skill_trust
from kiro_crew.config.loader import KiroCrewConfig

pytestmark = pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="project skills require no-follow directory-descriptor traversal",
)


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

    def test_a_full_store_refuses_a_new_grant_without_evicting_one(self, project):
        entries = [
            {"path": f"/synthetic/project-{i}", "granted_at": i}
            for i in range(skill_trust._MAX_GRANT_ENTRIES)
        ]
        path = skill_trust.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = json.dumps({"version": 1, "granted": entries}) + "\n"
        path.write_text(before, encoding="utf-8")

        with pytest.raises(RuntimeError, match="full"):
            skill_trust.grant_project_trust(project)

        assert path.read_text(encoding="utf-8") == before

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

    def test_revoke_matches_a_stored_network_path_without_resolving_it(self, monkeypatch):
        """A stored exact key is removable without triggering an SMB probe."""
        network_path = "//server/share/project"
        path = skill_trust.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "granted": [{"path": network_path, "granted_at": 1}],
                }
            ),
            encoding="utf-8",
        )

        def filesystem_probe(_path):
            raise AssertionError("stored exact path reached filesystem resolution")

        monkeypatch.setattr(skill_trust, "canonical_key", filesystem_probe)

        assert skill_trust.revoke_project_trust(network_path) is True

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

    @pytest.mark.parametrize("malformed", ["false", 1, 0, [], {}])
    def test_malformed_values_fail_closed(self, project, malformed):
        skill_trust.grant_project_trust(project)
        self._write_config(malformed)

        assert KiroCrewConfig.load().skills.project_skills_enabled is False
        assert skill_trust.is_project_trusted(project) is False

    def test_a_malformed_skills_section_fails_closed(self, project):
        skill_trust.grant_project_trust(project)
        home = skill_trust.store_path().parent.parent
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"skills": "not-an-object"}), encoding="utf-8")
        skill_trust.reset_cache_for_tests()

        assert KiroCrewConfig.load().skills.project_skills_enabled is False
        assert skill_trust.is_project_trusted(project) is False

    @pytest.mark.parametrize("filename", ["config.json", "config.local.json"])
    def test_a_truncated_config_source_fails_closed(self, project, filename):
        skill_trust.grant_project_trust(project)
        home = skill_trust.store_path().parent.parent
        home.mkdir(parents=True, exist_ok=True)
        if filename == "config.local.json":
            (home / "config.json").write_text("{}", encoding="utf-8")
        (home / filename).write_text('{"skills": {"project_skills_enabled":', encoding="utf-8")
        skill_trust.reset_cache_for_tests()

        assert KiroCrewConfig.load().skills.project_skills_enabled is False
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

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX mode changes; Windows project traversal is disabled",
    )
    def test_a_permission_change_invalidates_cached_grants(self, project):
        skill_trust.grant_project_trust(project)
        assert skill_trust.trusted_keys()
        path = skill_trust.store_path()

        path.chmod(0o000)
        try:
            assert skill_trust.trusted_keys() == frozenset()
        finally:
            path.chmod(0o600)


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

    @pytest.mark.parametrize("operation", ["grant", "revoke"])
    def test_mutators_refuse_a_non_object_grant_row(self, project, tmp_path, operation):
        other = tmp_path / "other-project"
        other.mkdir()
        original = json.dumps(
            {
                "version": 1,
                "granted": ["unknown-future-row", {"path": os.path.realpath(project)}],
            }
        )
        self._corrupt(original)

        with pytest.raises(skill_trust.TrustStoreUnreadable):
            if operation == "grant":
                skill_trust.grant_project_trust(other)
            else:
                skill_trust.revoke_project_trust(project)

        assert skill_trust.store_path().read_text(encoding="utf-8") == original

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
