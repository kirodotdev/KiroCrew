"""Tests for :mod:`kiro_crew.skill_trust` -- the project-skills consent record.

A ``SKILL.md`` is prose that enters the agent's context and can instruct it to
run anything, so loading one out of whatever repository the operator happened to
clone is an execution-adjacent decision. This module is the gate on that
decision, which makes every property below a security property rather than a
convenience: each test therefore says WHY it matters, not just what it checks.

Isolation: ``KIROCREW_HOME`` is pinned to this test's ``tmp_path`` (the same
lever the rootdir conftest uses) and ``config.paths._resolved_home`` is reset
with it, because ``config_dir()`` memoises the resolved home in a module global
for the process lifetime and one xdist worker runs thousands of tests in one
process. The module also memoises the enforcement read against a stat
signature, so ``reset_cache_for_tests()`` runs around every test and after any
store write a test performs by hand.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import platform_compat, security, skill_trust
from kiro_crew.config import paths
from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture(autouse=True)
def _isolated_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the data home at this test's tmp dir and clear both caches.

    Without the ``_resolved_home`` reset, ``config_dir()`` would keep returning
    whatever home an earlier test on this worker resolved, so a grant written
    here could land in -- or be read from -- another test's store.
    """
    home = tmp_path / "data-home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    skill_trust.reset_cache_for_tests()
    yield
    skill_trust.reset_cache_for_tests()


def _real_dir(parent: Path, name: str) -> str:
    """Create ``parent/name`` and return its canonical path.

    Every comparison uses the realpath because ``tmp_path`` itself sits behind a
    symlink on macOS (``/var`` -> ``/private/var``), so asserting against the
    raw ``tmp_path`` string would fail there for a reason that has nothing to do
    with the code under test.
    """
    directory = parent / name
    directory.mkdir(parents=True, exist_ok=True)
    return os.path.realpath(directory)


def _write_store_raw(text: str) -> None:
    """Write *text* verbatim as the grant store, bypassing the writer.

    Needed for the fail-closed tests: the production writer cannot produce a
    malformed store or a future schema version, which are exactly the shapes a
    hand-edit, a partial write, or a downgrade after an upgrade produce.
    """
    path = skill_trust.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    skill_trust.reset_cache_for_tests()


class TestFreshInstall:
    def test_a_fresh_install_trusts_nothing(self, tmp_path: Path) -> None:
        """Trust must be opt-in per directory, never a default.

        If a fresh install trusted anything -- the current project, a
        remembered path, an empty store read as "allow all" -- an operator who
        never made a consent decision would already be loading skills out of
        whatever repository they opened.
        """
        project = _real_dir(tmp_path, "project")

        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.is_project_trusted(project) is False
        assert skill_trust.is_key_trusted(project) is False
        assert skill_trust.list_trusted_projects() == []
        assert not skill_trust.store_path().exists()


class TestGrant:
    def test_grant_trusts_exactly_that_directory(self, tmp_path: Path) -> None:
        """A grant is per-directory consent, so it must not spread.

        The operator approves one repository they have read. A grant that also
        covered a sibling, a parent, or "everything under tmp" would silently
        convert one decision into many.
        """
        granted = _real_dir(tmp_path, "granted")
        unrelated = _real_dir(tmp_path, "unrelated")

        key = skill_trust.grant_project_trust(granted)

        assert key == granted
        assert skill_trust.is_project_trusted(granted) is True
        assert skill_trust.is_key_trusted(granted) is True
        assert skill_trust.is_project_trusted(unrelated) is False
        assert skill_trust.is_key_trusted(unrelated) is False
        assert skill_trust.trusted_keys() == frozenset({granted})

    def test_grant_is_idempotent(self, tmp_path: Path) -> None:
        """Re-granting must not append a second row.

        Duplicate rows would make the trust list read as two separate consents
        for one directory, and a revoke that removed only one of them would
        leave the directory still trusted -- a revoke that visibly "worked" and
        did not.
        """
        project = _real_dir(tmp_path, "project")

        first = skill_trust.grant_project_trust(project)
        second = skill_trust.grant_project_trust(project)

        assert first == second == project
        rows = skill_trust.list_trusted_projects()
        assert [row["path"] for row in rows] == [project]

    def test_grant_refuses_a_path_that_cannot_name_a_directory(self, tmp_path: Path) -> None:
        """A grant banked against an unmatchable path is a false record.

        It would show in the trust list as consent while never matching any
        enforcement key, so the operator believes they enabled something they
        did not.
        """
        with pytest.raises(ValueError):
            skill_trust.grant_project_trust(tmp_path / "does-not-exist")
        assert skill_trust.list_trusted_projects() == []


class TestRevoke:
    def test_revoke_takes_effect_immediately(self, tmp_path: Path) -> None:
        """Withdrawing consent must not need a restart.

        The enforcement read is memoised, so a revoke that only updated the
        store would leave the running process still loading the project's
        skills -- the operator would revoke, see the row disappear, and remain
        exposed for the life of the gateway.
        """
        project = _real_dir(tmp_path, "project")
        skill_trust.grant_project_trust(project)
        # Warm the memo so the assertion below cannot pass by never having
        # cached anything in the first place.
        assert skill_trust.is_project_trusted(project) is True

        assert skill_trust.revoke_project_trust(project) is True

        assert skill_trust.is_project_trusted(project) is False
        assert skill_trust.is_key_trusted(project) is False
        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.list_trusted_projects() == []

    def test_revoking_something_never_granted_returns_false(self, tmp_path: Path) -> None:
        """The return value is the UI's only signal that anything changed.

        A revoke that always claimed success would let a UI report "trust
        removed" for a directory whose real grant lives under a different key,
        hiding a grant that is still in force.
        """
        granted = _real_dir(tmp_path, "granted")
        never = _real_dir(tmp_path, "never")
        skill_trust.grant_project_trust(granted)

        assert skill_trust.revoke_project_trust(never) is False
        assert skill_trust.is_project_trusted(granted) is True

    def test_revoke_is_not_masked_by_the_memoised_read(self, tmp_path: Path) -> None:
        """The writer must drop the memo itself, not lean on the stat signature.

        ``trusted_keys`` caches against ``(mtime_ns, size, inode)``, and that
        alone is not a safe invalidator: two writes inside one filesystem
        timestamp tick that happen to produce the same size are
        indistinguishable, and Windows mtime granularity is coarse enough for
        that to be routine. Pinning the signature to a constant here removes
        that accidental safety net, so this test fails if the write path stops
        clearing the memo -- exactly the residual staleness window a
        signature-only design leaves open.
        """
        project = _real_dir(tmp_path, "project")
        with patch.object(skill_trust, "_store_signature", return_value=(1, 2, 3)):
            skill_trust.grant_project_trust(project)
            assert skill_trust.is_project_trusted(project) is True

            assert skill_trust.revoke_project_trust(project) is True

            assert skill_trust.is_project_trusted(project) is False


class TestCanonicalKey:
    def test_returns_none_for_every_value_that_cannot_name_a_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``None`` is the "cannot identify a project" answer, and it is load-bearing.

        Each of these values could otherwise be coerced into something that
        matches a stored key: a relative path resolves against whatever the
        process CWD happens to be, and a file or a nonexistent path would let a
        caller bank trust on a name whose meaning can still change. A caller
        must treat ``None`` as untrusted, which the ``is_project_trusted``
        assertions below pin.

        The relative case names a directory that really EXISTS relative to the
        CWD (via ``monkeypatch.chdir``, which reverts itself -- a raw
        ``os.chdir`` would leave every later test on this xdist worker starting
        somewhere else). A relative name that does not resolve to anything would
        be refused by the existence check instead, so the test would still pass
        with the absolute-path guard deleted.
        """
        a_file = tmp_path / "a-file.txt"
        a_file.write_text("not a directory\n", encoding="utf-8")
        (tmp_path / "reldir").mkdir()
        monkeypatch.chdir(tmp_path)

        cases = {
            "none": None,
            "empty": "",
            "whitespace": "   \t ",
            "relative-but-existing": "reldir",
            "relative-dotted": os.path.join(".", "reldir"),
            "missing": str(tmp_path / "does-not-exist"),
            "file": str(a_file),
        }
        for label, value in cases.items():
            assert skill_trust.canonical_key(value) is None, label
            assert skill_trust.is_project_trusted(value) is False, label

        assert skill_trust.is_key_trusted(None) is False
        assert skill_trust.is_key_trusted("") is False

    def test_returns_the_realpath_of_an_existing_absolute_directory(self, tmp_path: Path) -> None:
        """The positive control for the test above.

        Without it, a ``canonical_key`` that returned ``None`` for absolutely
        everything would satisfy every negative assertion while disabling the
        feature outright.
        """
        project = _real_dir(tmp_path, "project")

        assert skill_trust.canonical_key(project) == project
        assert skill_trust.canonical_key(Path(project)) == project


class TestSymlinkAliasing:
    @requires_symlinks
    def test_a_symlink_to_a_granted_directory_is_trusted(self, tmp_path: Path) -> None:
        """Keys are canonical realpaths, so an alias can neither escape nor duplicate.

        Two consequences of one property. An operator who grants a repository
        and then opens it through a symlinked path must not be told they never
        consented; and an agent that plants an alias must not be able to obtain
        a SECOND, separately-revokable grant for a directory the operator
        already approved -- keying on the resolved directory is what makes the
        directory itself the resource.
        """
        real = _real_dir(tmp_path, "real-project")
        link = tmp_path / "alias"
        make_dir_link(link, Path(real))
        skill_trust.grant_project_trust(real)

        assert skill_trust.canonical_key(link) == real
        assert skill_trust.is_project_trusted(link) is True
        assert skill_trust.trusted_keys() == frozenset({real})

    @requires_symlinks
    def test_a_symlink_to_a_different_directory_is_not_trusted(self, tmp_path: Path) -> None:
        """Resolution must not become a way to inherit someone else's grant.

        A link whose target was never approved has to stay untrusted, otherwise
        planting a link next to a granted repository would launder consent onto
        an arbitrary directory.
        """
        granted = _real_dir(tmp_path, "granted")
        other = _real_dir(tmp_path, "other")
        link = tmp_path / "alias"
        make_dir_link(link, Path(other))
        skill_trust.grant_project_trust(granted)

        assert skill_trust.canonical_key(link) == other
        assert skill_trust.is_project_trusted(link) is False


class TestStoreLocationAndMode:
    def test_store_lives_in_the_keystone_protected_trust_subdir(self) -> None:
        """The agent's own file tools must not be able to forge a grant.

        What stops them is not this module -- it opens the path directly, as
        every keystone reader does -- but the whole-directory ``trust`` entry on
        the security deny list. If the store moved out of that directory, or the
        entry disappeared, the agent could write itself consent for any
        directory and the gate would be decorative.
        """
        path = skill_trust.store_path()

        assert path.parent == paths.config_dir() / skill_trust._TRUST_SUBDIR
        assert path.name == skill_trust._STORE_FILENAME
        assert skill_trust._TRUST_SUBDIR in security._CREW_SECRET_LEAVES

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="POSIX mode bits; Windows ACLs are not st_mode",
    )
    def test_store_file_is_owner_only(self, tmp_path: Path) -> None:
        """A world-readable grant list is a target list.

        It tells any other local account exactly which directories are worth
        planting a ``SKILL.md`` in, and the operator has already consented to
        those being loaded.
        """
        skill_trust.grant_project_trust(_real_dir(tmp_path, "project"))

        mode = stat.S_IMODE(skill_trust.store_path().stat().st_mode)

        assert mode == skill_trust._STORE_MODE
        assert mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH) == 0


class TestFailsClosed:
    def test_a_malformed_store_trusts_nothing(self, tmp_path: Path) -> None:
        """A store we cannot parse must grant nothing, not everything.

        A truncated write, a hand-edit, or a corrupted file is exactly the
        moment a permissive default would load skills nobody approved. Refusing
        costs the operator one click; loading cannot be undone.

        The malformed text deliberately still CONTAINS the project path, the way
        a write cut short mid-row does. A parser that fell back to scraping
        absolute-looking strings out of unparseable text would pass a garbage
        fixture with no paths in it, and fail here -- which is the point.
        """
        project = _real_dir(tmp_path, "project")
        skill_trust.grant_project_trust(project)
        _write_store_raw(
            '{"version": 1, "granted": [{"path": ' + json.dumps(project) + ', "granted_at"'
        )

        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.is_project_trusted(project) is False

    def test_a_store_that_is_not_an_object_trusts_nothing(self, tmp_path: Path) -> None:
        """Valid JSON of the wrong shape is still unreadable policy.

        A bare list or string parses cleanly, so a reader that only guarded
        against a ``JSONDecodeError`` would fall through to whatever attribute
        access does next.
        """
        project = _real_dir(tmp_path, "project")
        _write_store_raw(json.dumps([{"path": project}]))

        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.is_project_trusted(project) is False

    def test_a_newer_schema_version_trusts_nothing(self, tmp_path: Path) -> None:
        """A store written by a newer build must not be guessed at.

        After a downgrade, this build cannot know what a v2 row means -- whether
        a field it ignores narrows the grant, for instance. Reading the rows
        anyway would honour consent under semantics this build does not
        implement.
        """
        project = _real_dir(tmp_path, "project")
        _write_store_raw(
            json.dumps(
                {
                    "version": skill_trust._SCHEMA_VERSION + 1,
                    "granted": [{"path": project, "granted_at": 1}],
                }
            )
        )

        assert skill_trust.trusted_keys() == frozenset()
        assert skill_trust.is_project_trusted(project) is False

    def test_a_relative_stored_path_is_never_enforced(self, tmp_path: Path) -> None:
        """A hand-edited relative row must not match a canonical key.

        Enforcement keys are absolute realpaths, so a relative row can only ever
        match by accident -- and an accidental match is a grant the operator did
        not make.
        """
        _write_store_raw(
            json.dumps({"version": skill_trust._SCHEMA_VERSION, "granted": [{"path": "project"}]})
        )

        assert skill_trust.trusted_keys() == frozenset()


class TestOffSwitch:
    def test_the_off_switch_beats_a_recorded_grant(self, tmp_path: Path) -> None:
        """The operator's hard switch has to be unconditional to be a switch.

        An admin who disables project skills fleet-wide is not asking for "off
        except where someone already clicked yes" -- grants already on disk are
        precisely what they are trying to neutralise, and they cannot enumerate
        them.
        """
        project = _real_dir(tmp_path, "project")
        skill_trust.grant_project_trust(project)
        assert skill_trust.is_project_trusted(project) is True

        disabled = KiroCrewConfig.load()
        object.__setattr__(disabled.skills, "project_skills_enabled", False)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=disabled):
            skill_trust.reset_cache_for_tests()

            assert skill_trust.is_project_trusted(project) is False
            assert skill_trust.is_key_trusted(project) is False
            assert skill_trust.trusted_keys() == frozenset()

        # The grant itself is untouched -- the switch suppresses, it does not
        # silently delete the operator's record.
        skill_trust.reset_cache_for_tests()
        assert skill_trust.is_project_trusted(project) is True

    def test_an_unreadable_config_disables_the_feature(self, tmp_path: Path) -> None:
        """Unreadable policy is not permission.

        If a config that fails to load left the feature enabled, breaking the
        config would be a way to turn the gate's off switch off.
        """
        project = _real_dir(tmp_path, "project")
        skill_trust.grant_project_trust(project)

        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            side_effect=RuntimeError("config unreadable"),
        ):
            skill_trust.reset_cache_for_tests()

            assert skill_trust.trusted_keys() == frozenset()
            assert skill_trust.is_project_trusted(project) is False


class TestListing:
    def test_a_grant_whose_directory_was_deleted_is_still_listed(self, tmp_path: Path) -> None:
        """An invisible stale grant cannot be revoked.

        The row must keep showing with ``exists=False`` so the operator can
        clear it. Filtering it out would hide a live record that starts
        enforcing again the moment the path reappears -- a re-clone of the same
        repository at the same location silently inherits consent nobody
        re-granted.
        """
        project = _real_dir(tmp_path, "doomed")
        skill_trust.grant_project_trust(project)
        Path(project).rmdir()

        rows = skill_trust.list_trusted_projects()

        assert [row["path"] for row in rows] == [project]
        assert rows[0]["exists"] is False
        # And it is revokable without the directory existing.
        assert skill_trust.revoke_project_trust(project) is True
        assert skill_trust.list_trusted_projects() == []

    def test_listing_reports_exists_true_for_a_live_grant(self, tmp_path: Path) -> None:
        """Positive control for the flag above.

        A ``list_trusted_projects`` that hard-coded ``exists=False`` would pass
        the stale-grant test while telling the operator every one of their real
        projects is gone.
        """
        project = _real_dir(tmp_path, "project")
        skill_trust.grant_project_trust(project)

        rows = skill_trust.list_trusted_projects()

        assert [row["path"] for row in rows] == [project]
        assert rows[0]["exists"] is True
        assert isinstance(rows[0]["granted_at"], int)
