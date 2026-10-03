"""The standing auto-approve switch lives on an UNOPENABLE keystone, not in config.json.

A read-only seal on ``config.json`` closes a write to the sealed NAME. It cannot close
the inode behind that name: the crew data-home root is writable in every sandbox, the
agent runs as the operator's uid and so OWNS the document, ``link(2)`` needs no write
permission on the file it names a second time, and a bind mount seals a MOUNT rather
than an inode. ``sandbox`` states that fact itself where it refuses a governance
ceiling whose ``st_nlink`` is not 1.

The switch therefore lives on a leaf a sandboxed process cannot OPEN. Two properties
carry that, and each gets its own assertion below because either alone is
insufficient:

* masked rather than sealed read-only -- a sealed-but-readable leaf is still a
  ``link(2)`` source;
* a DIRECTORY rather than a file -- Linux refuses ``link(2)`` on a directory outright,
  so the alias shape has no source even in principle.

Every claim has a discriminating control beside it, so a vacuous or misspelled set
cannot make this file pass.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys

import pytest

from kiro_crew import platform_compat, sandbox, standing_approval
from kiro_crew.config import loader
from kiro_crew.security import paths as security_paths

#: The genuine predicate, captured before the module fixture pins it away, so the
#: platform class can put it back and exercise the real branches.
_REAL_SPAWN_DELEGATES_MASKING = sandbox.spawn_delegates_masking


@pytest.fixture(autouse=True)
def maskable_host(monkeypatch):
    """Pin the host as one whose sandbox CAN mask the keystone.

    A grant is honoured only where the mask holding the leaf out of an agent's reach is
    in force, so every case here that asserts a document grants needs that precondition
    stated rather than inherited from whatever the test machine happens to support. The
    cases about the mask itself state their own departure from this.

    Delegation is pinned for the same reason and not as a convenience: the mask is a Linux
    bind mount or a macOS Seatbelt profile, so on a Windows runner every one of those
    assertions would be reading the platform answer instead of the behaviour it names.
    The DELEGATION PREDICATE is what gets pinned rather than ``sys.platform``, so that the
    real host still decides every other platform branch -- notably ``migration_notice``'s
    own rendering default, whose test would go vacuous against a forced platform.
    ``TestTheMaskIsPlatformBound`` is where the platform answer is the subject, and it
    restores the real predicate.

    The in-sandbox marker is cleared for the third precondition:
    ``credential_mask_applies`` refuses a process already inside a Crew sandbox, so a
    suite run from inside one would read every grant case as unmasked.
    """
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "namespace")
    monkeypatch.setattr(sandbox, "spawn_delegates_masking", lambda: False)
    monkeypatch.delenv(sandbox._IN_SANDBOX_MARKER, raising=False)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Point every reader of the data home at a scratch tree."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(loader, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    return home


def _write_grant(home, document: object) -> None:
    leaf = home / loader.STANDING_APPROVAL_DIRNAME
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / loader.STANDING_APPROVAL_FILENAME).write_text(
        document if isinstance(document, str) else json.dumps(document),
        encoding="utf-8",
    )


def _activate_grant(document: object = None, *, mode: str = "auto") -> None:
    """Write a granting document, as the operator does after upgrading.

    The gateway draws a FIRST-TRUSTED-INIT boundary under the mask on the first startup
    that runs this masked version (:func:`standing_approval.establish_trusted_init`),
    quarantining any grant present at that instant. A grant is honoured only when an
    AUTHENTIC gateway boundary marker exists -- i.e. the grant was written AFTER a
    trusted init, which only the operator can do on the masked version. This models that
    order: draw the boundary on the still-empty keystone dir (as a trusted gateway init
    would, quarantining nothing), then write the grant AFTER it, so ``is_declared`` sees
    a fresh post-init grant. The trust root is the marker's PROVENANCE, not its mtime, so
    no backdating is needed. *mode* selects the masked tier the boundary is drawn under.
    ``crew_home`` fixtures point the readers at a scratch tree, so this operates on it.
    """
    if document is None:
        document = {standing_approval.GRANT_FIELD: True}
    standing_approval.establish_trusted_init(mode)
    _write_grant(loader.config_dir(), document)


def _revalidate(mode: str) -> bool:
    """Drive the standing-override revalidation the way the gateway applier does.

    The whole-call ``revalidate_standing_override`` wrapper was subtracted (it had zero
    production consumers; the gateway calls the two halves directly, suspending before its
    ``to_thread`` yield). This composes ``suspend_standing_override_trust`` +
    ``_finish_standing_override_revalidation`` with no yield between them, which is the
    exact pair the gateway runs, so a test exercises the real seams rather than a
    test-only facade.
    """
    from kiro_crew import safety_override

    suspension = safety_override.suspend_standing_override_trust()
    return safety_override._finish_standing_override_revalidation(mode, suspension)


class TestKeystonePlacement:
    """Where the leaf sits in the two fences, and that the two agree on its name."""

    def test_the_leaf_is_on_the_agent_file_tool_keystone_floor(self):
        assert loader.STANDING_APPROVAL_DIRNAME in security_paths._CREW_SECRET_LEAVES
        # Discriminating control: an ordinary crew-home leaf is NOT on the floor, so a
        # pass above cannot come from a set that swallows every name.
        assert "config.json" not in security_paths._CREW_SECRET_LEAVES

    def test_the_leaf_is_masked_not_merely_sealed_read_only(self):
        """The whole point of the move: unopenable in-sandbox, not write-denied.

        A read-only seal leaves the document READABLE, and a readable inode the caller
        owns is a ``link(2)`` source. Being in the readonly set instead of the hidden
        one would reproduce exactly the residual this leaf exists to close.
        """
        assert loader.STANDING_APPROVAL_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
        assert loader.STANDING_APPROVAL_DIRNAME not in sandbox._CREW_READONLY_LEAVES
        # Control: the sealed-but-readable disposition really is a different set with
        # real members, so the second assertion is not vacuously true.
        assert "computer_use.json" in sandbox._CREW_READONLY_LEAVES
        assert "computer_use.json" not in sandbox._CREW_HIDDEN_LEAVES

    def test_the_leaf_is_precreated_so_the_isdir_guarded_mask_is_not_vacuous(self):
        """An absent name gets NO bind, and absent is the default on every install.

        ``mount(2)`` cannot mask a path that does not exist and the mask loop guards on
        ``isdir``, so without pre-creation a sandboxed process could CREATE the
        directory in the writable data-home root and write the grant the gateway reads
        back as the operator's own standing authority.
        """
        assert loader.STANDING_APPROVAL_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_the_mask_and_the_reader_name_the_same_directory(self):
        """``sandbox`` spells the leaf as a literal to stay off the config-loader import
        chain, exactly as it does for the live-target pointer. This is the pin that
        keeps the two spellings from drifting into two different directories -- which
        would mask one name while the gateway read another.
        """
        assert sandbox._STANDING_APPROVAL_LEAF == loader.STANDING_APPROVAL_DIRNAME

    def test_the_grant_document_lives_inside_the_classified_directory(self, crew_home):
        """A directory entry covers every child; a file entry would leave the container
        writable, which is the same hole one level up.
        """
        path = loader.standing_approval_path()
        assert path.parent.name == loader.STANDING_APPROVAL_DIRNAME
        assert path.name == loader.STANDING_APPROVAL_FILENAME
        assert path.parent.parent == crew_home

    def test_a_hidden_leaf_needs_no_child_readable_classification(self):
        """``test_sandbox_governance_mask`` pins the child-readable/withheld pair
        complete and disjoint over ``_CREW_SANDBOX_VISIBLE_LEAVES |
        _CREW_READONLY_LEAVES``. A hidden leaf is in neither source, so it is correctly
        absent from both halves rather than unclassified.
        """
        union = set(sandbox._CREW_SANDBOX_VISIBLE_LEAVES) | set(sandbox._CREW_READONLY_LEAVES)
        assert loader.STANDING_APPROVAL_DIRNAME not in union
        assert loader.STANDING_APPROVAL_DIRNAME not in sandbox._CREW_CHILD_READABLE_LEAVES
        assert loader.STANDING_APPROVAL_DIRNAME not in sandbox._CREW_CHILD_WITHHELD_LEAVES


class TestPrecreateIsBehavioural:
    """Membership in the precreate list is not the property; creation is."""

    def test_materialise_creates_the_directory_owner_only(self, crew_home):
        target = crew_home / loader.STANDING_APPROVAL_DIRNAME
        assert not target.exists()  # fresh install: nothing declared yet

        created = sandbox._materialize_maskable_dirs()

        assert target.is_dir()
        assert str(target) in created
        if os.name == "posix":
            # Owner-only whatever the umask: no group or other access.
            assert (target.stat().st_mode & 0o077) == 0

    def test_an_empty_directory_is_the_readers_absent_equivalent(self, crew_home):
        """What a sandboxed reader would see through the mask must mean NO GRANT.

        This is the criterion the precreate lists require of every leaf they
        materialise, and for a GRANT the empty form is the safe direction.
        """
        sandbox._materialize_maskable_dirs()
        assert (crew_home / loader.STANDING_APPROVAL_DIRNAME).is_dir()
        assert standing_approval.is_declared("auto") is False


class TestDirectoryHasNoAliasSource:
    """The property that makes a directory close the hole rather than move it."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX link(2) semantics")
    def test_the_kernel_refuses_a_second_name_for_a_directory(self, crew_home):
        """``link(2)`` on a directory is EPERM, so the shape the issue reports for a
        sealed FILE has no source here. Run against the real keystone directory rather
        than an arbitrary one, so the assertion is about this leaf.
        """
        sandbox._materialize_maskable_dirs()
        target = crew_home / loader.STANDING_APPROVAL_DIRNAME
        alias = crew_home / "alias-attempt"

        with pytest.raises(OSError) as err:
            os.link(str(target), str(alias), follow_symlinks=False)

        assert not alias.exists()
        # Control: the same call on a regular FILE in the same directory SUCCEEDS --
        # which is the residual for a sealed file leaf, and why this leaf is a
        # directory.
        regular = crew_home / "ordinary.json"
        regular.write_text("{}", encoding="utf-8")
        regular.chmod(0o444)
        file_alias = crew_home / "ordinary-alias.json"
        os.link(str(regular), str(file_alias))
        assert file_alias.stat().st_ino == regular.stat().st_ino
        assert err.value.errno != 0


class TestReadsFailClosed:
    """An absent or unreadable leaf resolves to refusal -- the issue's done-when."""

    def test_absent_leaf_is_no_grant(self, crew_home):
        assert standing_approval.is_declared("auto") is False

    def test_absent_document_inside_an_existing_directory_is_no_grant(self, crew_home):
        (crew_home / loader.STANDING_APPROVAL_DIRNAME).mkdir()
        assert standing_approval.is_declared("auto") is False

    def test_unparseable_document_is_no_grant(self, crew_home):
        _write_grant(crew_home, "{not json")
        assert standing_approval.is_declared("auto") is False

    def test_a_json_non_object_is_no_grant(self, crew_home):
        _write_grant(crew_home, [True])
        assert standing_approval.is_declared("auto") is False

    @pytest.mark.parametrize("value", ["true", "false", "0", "no", 1, 0, [], {}, None])
    def test_only_a_real_boolean_true_grants(self, crew_home, value):
        """A truthy STRING must not grant. ``"false"`` and ``"0"`` are truthy in
        Python, so a bare ``bool(...)`` here would read an explicit disable as the
        standing grant -- the same trap ``_read_skip_permissions`` documents.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: value})
        assert standing_approval.is_declared("auto") is False

    def test_an_explicit_boolean_true_grants(self, crew_home):
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()  # gateway records trusted provenance for the operator's grant
        assert standing_approval.is_declared("auto") is True

    def test_an_explicit_boolean_false_does_not_grant(self, crew_home):
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: False})
        assert standing_approval.is_declared("auto") is False

    def test_an_unreadable_document_is_no_grant(self, crew_home):
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs POSIX permission bits and a non-root uid")
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()  # record provenance while the document is still readable
        path = loader.standing_approval_path()
        path.chmod(0o000)
        try:
            assert standing_approval.is_declared("auto") is False
        finally:
            path.chmod(0o600)
        # Control: the very same document reads as a grant once it is readable again,
        # so the refusal above came from the permission and not from the content.
        assert standing_approval.is_declared("auto") is True

    def test_a_close_failure_is_no_grant(self, crew_home, monkeypatch):
        """A failing ``close`` resolves to no grant instead of escaping the reader.

        The reader runs on the gateway's startup thread outside any ``try``, so an
        ``OSError`` leaving it aborts boot. Withholding the grant is the fail-closed
        answer, and it is the whole point of catching a call whose only job is to
        release a descriptor the read is already finished with.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()  # record provenance so the control below actually grants
        # Control: the document grants while ``close`` behaves, so the refusal below
        # comes from the close failure and not from the content.
        assert standing_approval.is_declared("auto") is True

        real_close = os.close

        def failing_close(fd: int) -> None:
            real_close(fd)
            raise OSError(errno.EIO, "simulated close failure")

        # Patched for exactly one call: ``standing_approval.os`` is the stdlib module,
        # so the substitution is visible to every caller while it stands.
        with monkeypatch.context() as patched:
            patched.setattr(standing_approval.os, "close", failing_close)
            assert standing_approval.is_declared("auto") is False


class TestTheReaderRefusesAnAliasedGrant:
    """The reader must not resolve the alias shapes this keystone exists to deny.

    The mask and the directory stop an in-sandbox process from PLACING a link, but a
    reader that follows one would hand back a grant from wherever it points. An
    operator aliasing the document onto a sandbox-visible path with a dotfile manager
    is the ordinary way that happens, so the refusal belongs in the reader too --
    the same disposition ``sandbox`` takes for a ceiling it cannot cover under a
    second name.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_a_symlinked_grant_is_refused(self, crew_home):
        elsewhere = crew_home.parent / "aliased-grant.json"
        elsewhere.write_text(json.dumps({standing_approval.GRANT_FIELD: True}), encoding="utf-8")
        leaf = crew_home / loader.STANDING_APPROVAL_DIRNAME
        leaf.mkdir(parents=True)
        (leaf / loader.STANDING_APPROVAL_FILENAME).symlink_to(elsewhere)

        assert standing_approval.is_declared("auto") is False
        # Control: the identical document at a REAL path does grant, so the refusal
        # above came from the link and not from the content.
        (leaf / loader.STANDING_APPROVAL_FILENAME).unlink()
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()
        assert standing_approval.is_declared("auto") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_a_symlinked_parent_directory_is_refused(self, crew_home):
        """A symlinked ``standing-approval`` DIRECTORY must be refused too.

        O_NOFOLLOW on the leaf alone guards ``grant.json`` but not its parent: an
        operator who aliases the whole directory onto a sandbox-visible target (a
        dotfile manager is the ordinary way that happens) hands the agent a writable
        parent for the document, while the mask predicate -- which matches by
        ``normpath`` and never resolves links -- still answers masked. The reader
        pins the parent no-follow and opens the leaf relative to it, so the aliased
        directory is refused at the open rather than followed.
        """
        target = crew_home.parent / "aliased-approval-dir"
        target.mkdir(parents=True)
        (target / loader.STANDING_APPROVAL_FILENAME).write_text(
            json.dumps({standing_approval.GRANT_FIELD: True}), encoding="utf-8"
        )
        link = crew_home / loader.STANDING_APPROVAL_DIRNAME
        link.symlink_to(target, target_is_directory=True)

        assert standing_approval.is_declared("auto") is False
        # Control: the identical document under a REAL directory at the keystone
        # name does grant, so the refusal came from the aliased parent, not content.
        link.unlink()
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()
        assert standing_approval.is_declared("auto") is True

    def test_a_second_hard_link_is_refused(self, crew_home):
        """A second name for this inode is the shape the whole keystone denies.

        The mask covers a path rather than an inode, so a document reachable under
        another name is writable through that name whatever the mask says.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()  # record provenance for the sole-link document
        path = loader.standing_approval_path()
        assert standing_approval.is_declared("auto") is True  # sole link: granted

        alias = crew_home / "second-name.json"
        os.link(str(path), str(alias))
        assert path.stat().st_nlink == 2
        assert standing_approval.is_declared("auto") is False

        # Breaking the extra link restores the grant, so the refusal tracked the
        # link count rather than anything else about the file.
        alias.unlink()
        assert path.stat().st_nlink == 1
        assert standing_approval.is_declared("auto") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO semantics")
    def test_a_non_regular_grant_is_refused(self, crew_home):
        leaf = crew_home / loader.STANDING_APPROVAL_DIRNAME
        leaf.mkdir(parents=True)
        os.mkfifo(str(leaf / loader.STANDING_APPROVAL_FILENAME))
        assert standing_approval.is_declared("auto") is False

    def test_an_oversized_grant_is_refused(self, crew_home):
        """A huge file at this name must not be buffered into the gateway on boot."""
        padding = "x" * (standing_approval._MAX_GRANT_BYTES + 100)
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True, "pad": padding})
        assert loader.standing_approval_path().stat().st_size > standing_approval._MAX_GRANT_BYTES
        assert standing_approval.is_declared("auto") is False


class TestTheFirstTrustedInitMarkerQuarantinesAPlant:
    """A keystone present before this gateway drew its first trusted-init boundary is a
    pre-upgrade plant, not an operator grant: it is refused and quarantined. A grant
    written AFTER the gateway drew the boundary under the mask is a fresh operator
    declaration and is honoured.

    The trust root is the boundary marker ``establish_trusted_init`` draws under the
    mask, authenticated by PROVENANCE (a genuine regular single-link file reached
    through an ``O_NOFOLLOW``/``O_DIRECTORY`` parent pin) rather than by its mtime --
    because the marker filename is a published constant a pre-upgrade agent can plant
    and backdate too. A planted marker is therefore not authentic and does not supply a
    boundary.
    """

    def test_a_grant_present_before_any_trusted_init_is_refused_and_quarantined(self, crew_home):
        """The exact pre-upgrade plant: ``grant.json`` on disk before the gateway ever
        drew a trusted-init boundary. No authentic marker exists, so the grant predates
        trusted init and is refused; the file is renamed aside so it is never read again.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        path = loader.standing_approval_path()
        quarantined = path.with_name(path.name + ".quarantined")
        marker = loader.standing_approval_init_marker_path()
        assert not marker.exists()  # precondition: this gateway has not run masked yet

        assert standing_approval.is_declared("auto") is False
        # The plant is moved aside, not left in place to be read again.
        assert not path.exists()
        assert quarantined.exists()
        assert json.loads(quarantined.read_text(encoding="utf-8")) == {
            standing_approval.GRANT_FIELD: True
        }

    def test_a_plant_behind_a_symlinked_marker_is_quarantined(self, crew_home):
        """A pre-upgrade agent that points ``.trusted-init`` at a file it controls
        (a symlink) does not supply an authentic boundary: the symlink fails the
        no-follow discipline, so ``establish_trusted_init`` redraws a genuine marker
        and quarantines the co-present plant.
        """
        keydir = crew_home / loader.STANDING_APPROVAL_DIRNAME
        keydir.mkdir(parents=True)
        marker = loader.standing_approval_init_marker_path()
        path = loader.standing_approval_path()
        target = crew_home / "attacker-controlled-boundary"
        target.write_text("1\n", encoding="utf-8")
        marker.symlink_to(target)
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})

        standing_approval.establish_trusted_init("auto")
        assert not path.exists()
        assert path.with_name(path.name + ".quarantined").exists()
        # The redrawn marker is a genuine regular single-link file, not the symlink.
        assert standing_approval._marker_is_authentic() is True
        assert standing_approval.is_declared("auto") is False

    def test_a_symlinked_marker_is_not_authentic(self, crew_home):
        """A ``.trusted-init`` that is a symlink (what an attacker plants to point the
        boundary at a file it controls) is not an authentic marker.
        """
        keydir = crew_home / loader.STANDING_APPROVAL_DIRNAME
        keydir.mkdir(parents=True)
        marker = loader.standing_approval_init_marker_path()
        target = crew_home / "attacker-controlled-boundary"
        target.write_text("1\n", encoding="utf-8")
        marker.symlink_to(target)
        assert standing_approval._marker_is_authentic() is False

    def test_a_fresh_grant_written_after_trusted_init_is_honoured(self, crew_home):
        """The ordinary case: the gateway draws a trusted-init boundary under the mask
        on the still-empty keystone dir, THEN the operator writes the grant. An authentic
        marker exists and nothing was quarantined, so the grant is honoured.
        """
        standing_approval.establish_trusted_init("auto")
        marker = loader.standing_approval_init_marker_path()
        assert marker.exists()
        assert standing_approval._marker_is_authentic() is True
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})

        assert standing_approval.is_declared("auto") is True
        # It was honoured, not quarantined: the document is still in place.
        assert loader.standing_approval_path().exists()
        assert (
            not loader.standing_approval_path()
            .with_name(loader.STANDING_APPROVAL_FILENAME + ".quarantined")
            .exists()
        )

    def test_establish_trusted_init_does_not_draw_a_boundary_on_an_unmasked_host(
        self, crew_home, monkeypatch
    ):
        """The boundary is drawn only where the live mode masks the leaf. On an unmasked
        host no marker is drawn (an unmasked host grants nothing anyway), so a later call
        does not certify a grant written while the leaf was reachable.
        """
        monkeypatch.setattr(standing_approval, "_keystone_is_masked", lambda _m: False)
        standing_approval.establish_trusted_init("off")
        assert not loader.standing_approval_init_marker_path().exists()

    def test_establish_trusted_init_is_idempotent(self, crew_home):
        """An authentic marker already present is left untouched: a second init does not
        re-quarantine a now-legitimate grant and does not redraw the boundary.
        """
        standing_approval.establish_trusted_init("auto")
        marker = loader.standing_approval_init_marker_path()
        first_mtime = os.lstat(str(marker)).st_mtime
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        # A second init must NOT quarantine the post-init grant or move the boundary.
        standing_approval.establish_trusted_init("auto")
        assert os.lstat(str(marker)).st_mtime == first_mtime
        assert loader.standing_approval_path().exists()
        assert standing_approval.is_declared("auto") is True

    def test_the_marker_lives_in_the_masked_keystone_directory(self):
        """The marker's trust rests on the same mask as the grant: it sits inside the
        ``standing-approval`` directory, which is bind-masked out of every agent
        sandbox, so an in-sandbox agent on this version cannot create or forge it.
        """
        marker = loader.standing_approval_init_marker_path()
        grant = loader.standing_approval_path()
        assert marker.parent == grant.parent
        assert marker.parent.name == loader.STANDING_APPROVAL_DIRNAME
        assert loader.STANDING_APPROVAL_DIRNAME in sandbox._CREW_HIDDEN_LEAVES

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_the_keystone_directory_is_tightened_to_0700(self, crew_home):
        """The keystone dir must not be group/other-readable even if it pre-exists under
        umask: ``establish_trusted_init`` chmods it to 0700.

        POSIX-only: on Windows ``os.chmod`` honors only the read-only bit, so the
        directory keeps its 0o777 mode and this exact-mode assertion cannot hold.
        The production chmod is best-effort there (it logs and continues), and the
        mask -- not the mode bits -- is the boundary against the agent.
        """
        keydir = crew_home / loader.STANDING_APPROVAL_DIRNAME
        keydir.mkdir(parents=True)
        # 0o755 is the deliberate insecure precondition (dir left group-readable under
        # umask) that the assertion below proves establish_trusted_init tightens to 0o700,
        # so the insecure-file-permissions rule is a false positive on this setup line.
        os.chmod(str(keydir), 0o755)  # nosemgrep
        standing_approval.establish_trusted_init("auto")
        assert stat.S_IMODE(os.lstat(str(keydir)).st_mode) == 0o700

    def test_quarantine_failure_still_refuses_the_grant(self, crew_home, monkeypatch):
        """If the rename cannot happen, the plant is refused regardless -- quarantine
        failure never upgrades a plant into a grant.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})

        def failing_replace(src, dst):
            raise OSError(errno.EACCES, "simulated quarantine failure")

        monkeypatch.setattr(standing_approval.os, "replace", failing_replace)
        assert standing_approval.is_declared("auto") is False

    def test_quarantine_failure_does_not_draw_the_marker(self, crew_home, monkeypatch):
        """A failed quarantine must not draw the trusted-init marker.

        GPT 6.1 F2: if ``_quarantine_plant`` swallows its ``os.replace`` failure (an
        unmasked pre-upgrade agent can block the rename with a single ``mkdir`` that
        plants a ``grant.json.quarantined`` DIRECTORY) and ``establish_trusted_init``
        then draws the marker anyway, the plant survives behind a now-authentic marker
        and the NEXT boot honours it as a grant. So on a quarantine failure the marker
        must stay UNdrawn, keeping the read path fail-closed across restarts.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        marker = loader.standing_approval_init_marker_path()

        def failing_replace(src, dst):
            raise OSError(errno.EACCES, "simulated quarantine failure")

        monkeypatch.setattr(standing_approval.os, "replace", failing_replace)
        standing_approval.establish_trusted_init("auto")

        # The marker was NOT drawn, so a second boot still finds no authentic marker
        # and keeps refusing the surviving plant rather than certifying it.
        assert not marker.exists()
        assert standing_approval.is_declared("auto") is False

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO semantics")
    def test_a_fifo_marker_does_not_hang_the_boundary_draw(self, crew_home):
        """A planted FIFO at the MARKER name must not hang the gateway boot.

        GPT 6.1 F4: ``_marker_is_authentic`` opened the marker ``O_RDONLY`` without
        ``O_NONBLOCK``, and ``marker.exists()`` is true for a FIFO -- so a pre-upgrade
        agent that plants ``.trusted-init`` as a writer-less FIFO would block the open
        forever before the post-open ``S_ISREG`` check could reject it, hanging
        ``start_dashboard`` on the boot path. With ``O_NONBLOCK`` the open returns, the
        FIFO is rejected as non-regular, and the boundary draw proceeds.
        """
        marker = loader.standing_approval_init_marker_path()
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.mkfifo(str(marker))

        # Must return promptly (no writer on the FIFO) and treat the FIFO as not
        # authentic, then redraw a genuine regular marker over it.
        assert standing_approval._marker_is_authentic() is False
        standing_approval.establish_trusted_init("auto")
        assert stat.S_ISREG(os.lstat(str(marker)).st_mode)


class TestTheMigrationCommandRuns:
    """The notice's whole value is that the operator can act on it.

    The remedy is the same on every platform now, so it is written once and resolved
    here rather than branched: the defect these tests exist for is a string verified only
    where it was written, and a single rendering has one place to verify.

    The notice is deliberately NOT a runnable redirection command on any platform. A
    ``printf ... > path`` line an operator copy-pastes follows a symlink a pre-upgrade
    agent can plant at ``path`` (the crew data-home root is agent-writable), overwriting
    an attacker-chosen target with operator privilege. So the remedy names the path
    and the exact one line and asks the operator to create the file by hand.
    """

    def test_the_command_uses_the_resolved_path_not_an_env_var(self, crew_home):
        notice = standing_approval.migration_notice("auto")
        # $KIROCREW_HOME is UNSET on a default installation, where the data home
        # comes from config_dir() -- an env-var form expands to /standing-approval
        # and fails at the filesystem root.
        assert "$KIROCREW_HOME" not in notice
        assert str(loader.standing_approval_path()) in notice

    def test_the_rendering_uses_no_posix_shell_builtin(self, crew_home):
        """No single spelling writes this JSON in both cmd and PowerShell, so the
        text states the path and the line rather than pretending to run.
        """
        notice = standing_approval.migration_notice("auto")
        assert "mkdir -p" not in notice
        assert "printf" not in notice
        assert "'" not in notice.split("this one line:")[1]
        assert f'{{"{standing_approval.GRANT_FIELD}": true}}' in notice

    def test_the_rendering_is_not_a_runnable_redirection(self, crew_home):
        """The text must NOT hand the operator a ``printf ... > path`` line: a
        copy-pasted redirection follows a symlink a pre-upgrade agent can plant at the
        keystone path, overwriting an attacker-chosen target with operator privilege.
        It names the path and the line for manual creation.
        """
        notice = standing_approval.migration_notice("auto")
        assert "printf" not in notice
        assert ">" not in notice
        assert "create the file" in notice
        assert f'{{"{standing_approval.GRANT_FIELD}": true}}' in notice

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX path creation")
    def test_creating_the_named_file_by_hand_produces_a_document_that_grants(self, crew_home):
        """End to end: the notice names the path and the exact line; the operator writes
        that file by hand AFTER upgrading, and -- once masked -- the hand-written
        document grants.

        The gateway records a first-trusted-init marker under the mask on the startup
        that runs this masked version, and a grant written AFTER that marker is a fresh
        post-init operator declaration (not a pre-upgrade plant), so it is honoured.
        This models that order: the trusted init happens, then the operator writes.

        Deliberately does NOT extract and execute a shell command from the notice: the
        notice emits none, precisely so a copy-pasted redirection cannot follow a
        planted symlink. This asserts the operator's manual action grants.
        """
        notice = standing_approval.migration_notice("auto")
        assert standing_approval.is_declared("auto") is False
        # The gateway drew a trusted-init boundary under the mask on this masked version,
        # on the still-empty keystone dir (nothing to quarantine). A grant the operator
        # writes AFTER it is a fresh post-init declaration, so it is honoured.
        standing_approval.establish_trusted_init("auto")
        # The one line the notice tells the operator to put in the file.
        document = f'{{"{standing_approval.GRANT_FIELD}": true}}'
        assert document in notice
        path = loader.standing_approval_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document + "\n", encoding="utf-8")
        assert path.is_file()

        # Once masked, the hand-written post-init document grants on the next read.
        assert standing_approval.is_declared("auto") is True

    def test_the_default_rendering_follows_the_host(self, crew_home):
        """Omitting the mask parameter resolves it from the requested mode."""
        assert standing_approval.migration_notice("auto") == standing_approval.migration_notice(
            "auto", masked=True
        )


class TestConfigKeyNoLongerGrants:
    """The migration is explicit: the retired key grants nothing and says so."""

    def test_the_retired_key_is_not_read_by_the_keystone_reader(self, crew_home):
        """The reader must not consult ``config.json`` at all. Writing the retired
        declaration into a config document next to an absent keystone must leave the
        answer at no-grant.
        """
        (crew_home / "config.json").write_text(
            json.dumps({"agent": {"dangerously_skip_permissions": True}}), encoding="utf-8"
        )
        assert standing_approval.is_declared("auto") is False

    def test_the_migration_notice_names_the_path_and_the_document(self):
        notice = standing_approval.migration_notice("auto")
        assert loader.STANDING_APPROVAL_DIRNAME in notice
        assert loader.STANDING_APPROVAL_FILENAME in notice
        assert "dangerously_skip_permissions" in notice
        # Actionable for a reader with no context: it must say the old key grants
        # nothing, not merely that something moved.
        assert "no longer grants" in notice

    def test_the_notice_is_ascii_so_every_log_sink_renders_it(self):
        standing_approval.migration_notice("auto").encode("ascii")


class TestStartupHonoursOnlyTheKeystone:
    """The gateway startup path reads the keystone, and announces a stranded key."""

    def _cfg(self, declared: bool):
        class _Agent:
            dangerously_skip_permissions = declared
            sandbox = "auto"

        class _Cfg:
            agent = _Agent()

        return _Cfg()

    @pytest.fixture()
    def startup(self, monkeypatch, crew_home):
        from kiro_crew.dashboard import server

        monkeypatch.setattr(server, "apply_config_duration", lambda: None)
        calls: list[str] = []

        class _Result:
            active = True
            ttl = 0

        monkeypatch.setattr(
            server, "grant_declared_yolo", lambda: (calls.append("granted"), _Result())[1]
        )
        # The startup path resolves the sandbox mode from ``config.live.current``
        # immediately before activation, so a mid-boot flip cannot activate under a
        # stale boot mode (pinned in the dashboard/Slack suites). These tests inject the
        # mode through the ``cfg`` they pass, so echo that cfg back as the live config --
        # otherwise ``live.current`` with no armed watcher would disk-reload a default
        # mode and ignore the injected one these cases depend on.
        from kiro_crew.config import live as _live

        monkeypatch.setattr(_live, "current", lambda fallback, **_k: fallback)
        return server, calls

    def test_a_stranded_config_key_grants_nothing(self, startup, caplog):
        server, calls = startup
        with caplog.at_level("WARNING"):
            server._apply_startup_yolo(object(), self._cfg(declared=True))
        assert calls == []
        assert "no longer grants" in caplog.text

    def test_the_keystone_grants_and_logs_no_migration_warning(self, startup, caplog, crew_home):
        server, calls = startup
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()  # record trusted provenance for the operator's grant
        with caplog.at_level("WARNING"):
            server._apply_startup_yolo(object(), self._cfg(declared=False))
        assert calls == ["granted"]
        assert "no longer grants" not in caplog.text

    def test_neither_set_grants_nothing_and_says_nothing(self, startup, caplog):
        server, calls = startup
        with caplog.at_level("WARNING"):
            server._apply_startup_yolo(object(), self._cfg(declared=False))
        assert calls == []
        assert "no longer grants" not in caplog.text


class TestTheGrantNeedsTheMaskItRestsOn:
    """The keystone's integrity IS the bind mask, so a startup that will not mask the
    leaf must not honour the grant.

    An unconfined agent subprocess sees the real data home. No mount mask covers the
    leaf there, so that subprocess can create the directory and write the document
    itself, and the next startup reads what it wrote as the operator's standing
    authority. The document is an authorization record only while something outside
    the agent's reach holds it.

    Two resolved states are unconfined, which is why the refusal has to be keyed on
    whether the leaf is masked for agent subprocesses and NOT on the value of
    ``agent.sandbox``:

    * the mode resolves to ``off`` for the spawn, and
    * the mode is ``auto``, no backend is available, and the operator has taken the
      ``agent.sandbox_allow_unsandboxed_exec`` opt-in.

    ``test_a_floor_that_clamps_off_back_up_still_grants`` is the counterweight, and it
    is what refuses a gate spelled as ``cfg.agent.sandbox == "off"``: a governance floor
    raises a requested ``off`` to a confined tier, so that host IS masked and must keep
    granting.
    """

    def _cfg(self, *, sandbox_mode: str, declared: bool = False):
        class _Agent:
            dangerously_skip_permissions = declared
            sandbox = sandbox_mode
            sandbox_allow_unsandboxed_exec = False

        class _Cfg:
            agent = _Agent()

        return _Cfg()

    @pytest.fixture()
    def startup(self, monkeypatch, crew_home):
        from kiro_crew.dashboard import server

        monkeypatch.setattr(server, "apply_config_duration", lambda: None)
        calls: list[str] = []

        class _Result:
            active = True
            ttl = 0

        monkeypatch.setattr(
            server, "grant_declared_yolo", lambda: (calls.append("granted"), _Result())[1]
        )
        # The startup path resolves the sandbox mode from ``config.live.current``
        # immediately before activation, so a mid-boot flip cannot activate under a
        # stale boot mode (pinned in the dashboard/Slack suites). These tests inject the
        # mode through the ``cfg`` they pass, so echo that cfg back as the live config --
        # otherwise ``live.current`` with no armed watcher would disk-reload a default
        # mode and ignore the injected one these cases depend on.
        from kiro_crew.config import live as _live

        monkeypatch.setattr(_live, "current", lambda fallback, **_k: fallback)
        return server, calls

    def test_an_unconfined_startup_refuses_the_grant(self, startup, crew_home, caplog):
        server, calls = startup
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        with caplog.at_level("WARNING"):
            server._apply_startup_yolo(object(), self._cfg(sandbox_mode="off"))
        assert calls == []
        assert "is UNAVAILABLE on this host" in caplog.text

    def test_a_confined_startup_grants(self, startup, crew_home):
        server, calls = startup
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant()
        server._apply_startup_yolo(object(), self._cfg(sandbox_mode="auto"))
        assert calls == ["granted"]

    def test_a_floor_that_clamps_off_back_up_still_grants(self, startup, crew_home, monkeypatch):
        server, calls = startup
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "cc")
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant(mode="off")  # the floor clamps "off" up to a masked tier
        server._apply_startup_yolo(object(), self._cfg(sandbox_mode="off"))
        assert calls == ["granted"]

    def test_no_backend_refuses_even_with_the_unsandboxed_opt_in(
        self, startup, crew_home, monkeypatch
    ):
        server, calls = startup
        monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "none")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        server._apply_startup_yolo(object(), self._cfg(sandbox_mode="auto"))
        assert calls == []

    def test_an_unmasked_host_with_no_grant_stays_silent(self, startup, crew_home, caplog):
        server, calls = startup
        with caplog.at_level("WARNING"):
            server._apply_startup_yolo(object(), self._cfg(sandbox_mode="off"))
        assert calls == []
        assert "is UNAVAILABLE on this host" not in caplog.text


class TestTheMaskIsPlatformBound:
    """Where Crew's own mask runs, and therefore where the standing grant exists.

    The mask is a Linux bind mount or a macOS Seatbelt profile, and ``wrap_argv`` hands
    the spawn to kiro-cli's internal sandbox on Windows and on macOS where that sandbox
    is enabled. A delegated spawn is confined, but by something that applies none of
    Crew's masks, so the leaf is reachable from inside it and the declaration is not an
    authorization there.

    ``test_an_undelegated_macos_host_masks`` is the control that keeps this about
    delegation rather than about macOS: the same platform masks when kiro-cli's own
    sandbox is off.
    """

    @pytest.fixture(autouse=True)
    def _the_real_predicate(self, monkeypatch, crew_home):
        """Restore the genuine delegation predicate; the module fixture pins it away."""
        monkeypatch.setattr(sandbox, "spawn_delegates_masking", _REAL_SPAWN_DELEGATES_MASKING)
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
        monkeypatch.setattr(sandbox.sys, "platform", "linux")

    def test_a_maskable_linux_host_masks(self):
        assert standing_approval._keystone_is_masked("auto") is True

    def test_windows_can_never_mask(self, monkeypatch):
        monkeypatch.setattr(sandbox.sys, "platform", "win32")
        assert standing_approval._keystone_is_masked("auto") is False

    def test_delegated_macos_cannot_mask(self, monkeypatch):
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
        assert standing_approval._keystone_is_masked("auto") is False

    def test_an_undelegated_macos_host_masks(self, monkeypatch):
        monkeypatch.setattr(sandbox.sys, "platform", "darwin")
        assert standing_approval._keystone_is_masked("auto") is True

    def test_a_delegated_host_refuses_a_written_grant_at_startup(self, monkeypatch, crew_home):
        """The platform answer reaches the startup path, not just the predicate."""
        from kiro_crew.dashboard import server

        monkeypatch.setattr(server, "apply_config_duration", lambda: None)
        monkeypatch.setattr(sandbox.sys, "platform", "win32")
        calls: list[str] = []
        monkeypatch.setattr(server, "grant_declared_yolo", lambda: calls.append("granted"))
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})

        class _Agent:
            dangerously_skip_permissions = False
            sandbox = "auto"

        class _Cfg:
            agent = _Agent()

        server._apply_startup_yolo(object(), _Cfg())
        assert calls == []


class TestAnOuterCrewSandboxIsNotTheMask:
    """A process already inside a Crew sandbox does not inherit a keystone mask.

    ``wrap_argv`` passes an in-sandbox spawn through rather than nesting, so no adapter
    mask is installed for the child. The outer sandbox still confines it, but it was
    built for its own tier's hidden dirs -- which deliberately leave ``~/.aws``,
    ``~/.ssh`` and ``~/.kube`` readable for kiro-cli's sake -- so it names no rule over
    this leaf. ``credential_mask_applies`` is where that is decided, and the grant
    inherits the refusal by composing it.
    """

    def test_an_in_sandbox_startup_cannot_mask(self, monkeypatch, crew_home):
        monkeypatch.setenv(sandbox._IN_SANDBOX_MARKER, "1")
        # credential_mask_applies refuses an in-sandbox spawn only when
        # `_macos_sandbox_state() is not False` -- on a macOS CI runner that is not
        # confined by Seatbelt it returns False, which would skip the refusal and let
        # detect_backend answer masked. Pin it to None (unknown) so the in-sandbox
        # refusal is exercised deterministically on every platform, which is the
        # condition this test is about.
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
        assert standing_approval._keystone_is_masked("auto") is False

    def test_clearing_the_marker_restores_the_mask(self, monkeypatch, crew_home):
        """Control: the same host masks once it is not inside a sandbox itself."""
        monkeypatch.delenv(sandbox._IN_SANDBOX_MARKER, raising=False)
        assert standing_approval._keystone_is_masked("auto") is True

    def test_an_in_sandbox_startup_refuses_a_written_grant(self, monkeypatch, crew_home):
        """The refusal reaches the startup path, not just the predicate."""
        from kiro_crew.dashboard import server

        monkeypatch.setattr(server, "apply_config_duration", lambda: None)
        monkeypatch.setenv(sandbox._IN_SANDBOX_MARKER, "1")
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
        calls: list[str] = []
        monkeypatch.setattr(server, "grant_declared_yolo", lambda: calls.append("granted"))
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})

        class _Agent:
            dangerously_skip_permissions = False
            sandbox = "auto"

        class _Cfg:
            agent = _Agent()

        server._apply_startup_yolo(object(), _Cfg())
        assert calls == []


class TestTheLeafMustSitInsideTheMaskedSet:
    """The one question the two sandbox predicates do not answer for this grant.

    A host can build Crew's mask and still name no rule over THIS leaf, so a masked
    host is a necessary condition and not a sufficient one. Containment rather than
    equality, because the masked target is the directory and the grant is a file in it.
    """

    def test_a_leaf_outside_every_masked_target_is_refused(self, monkeypatch, crew_home):
        monkeypatch.setattr(
            sandbox, "_crew_hidden_sandbox_targets", lambda: (str(crew_home / "elsewhere"),)
        )
        assert standing_approval._keystone_is_masked("auto") is False

    def test_a_leaf_inside_a_masked_target_is_covered(self, monkeypatch, crew_home):
        """Control: the same host covers the leaf once a target contains it."""
        monkeypatch.setattr(
            sandbox,
            "_crew_hidden_sandbox_targets",
            lambda: (str(loader.standing_approval_path().parent),),
        )
        assert standing_approval._keystone_is_masked("auto") is True


class TestTheNoticeMatchesTheMask:
    """A remedy is printed only where writing the document would actually grant."""

    def test_a_maskable_host_gets_the_remedy(self, crew_home):
        text = standing_approval.migration_notice("auto", masked=True)
        assert "create the file" in text
        assert "printf" not in text
        assert "UNAVAILABLE" not in text

    def test_the_masked_remedy_activates_on_a_single_restart(self, crew_home):
        """Once masked, writing the document and restarting once grants. The notice must
        walk that plainly, and must NOT carry the retired marker/provenance ritual
        language (the first-boot marker was subtracted as theatre).
        """
        text = standing_approval.migration_notice("auto", masked=True)
        low = text.lower()
        assert "restart" in low
        assert "create the file" in low
        # The retired rituals must be gone: no two-restart dance, no first-boot marker,
        # no HMAC anchor/trusted-init ceremony.
        assert "first restart refuses" not in low
        assert "re-write" not in low
        assert "anchor" not in low

    def test_an_unmasked_host_is_told_the_grant_is_unavailable(self, crew_home):
        text = standing_approval.migration_notice("auto", masked=False)
        assert "UNAVAILABLE" in text
        assert "mkdir -p" not in text
        assert "create the file" not in text
        assert "yolo_duration" in text

    def test_the_notice_resolves_the_mask_from_the_mode_it_is_given(self, crew_home, monkeypatch):
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
        monkeypatch.setattr(sandbox, "detect_backend", lambda config_mode="auto": "namespace")
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        assert "create the file" in standing_approval.migration_notice("auto")
        assert "UNAVAILABLE" in standing_approval.migration_notice("off")


class TestRevalidateStandingOverrideOnLiveSandboxChange:
    """F1: a LIVE ``agent.sandbox`` flip must revalidate (and revoke) the standing grant.

    ``agent.sandbox`` is not restart-gated -- ``config.sections`` marks it without
    ``restart`` and its own doc says a change "applies to sessions started after it" -- so
    a flip from a masked mode to an unmasked one (e.g. ``off``) at runtime removes the mask
    precondition the declared standing grant rests on. The declared grant is evaluated once
    at boot and held permanently in memory, so without revalidation a new UNMASKED session
    would inherit permanent auto-approval AND be able to reach the keystone itself.

    The revalidation (``suspend_standing_override_trust`` + the finish step, composed by
    ``_revalidate`` the way the gateway applier runs them) is the trusted-applier hook,
    wired onto the always-constructed GatewayOrchestrator's live-config watcher. These
    tests exercise it directly against a real grant established through the same harness
    the rest of this module uses.
    """

    @pytest.fixture(autouse=True)
    def _fresh_override(self):
        from kiro_crew import safety_override

        safety_override.reset_singleton()
        yield
        safety_override.reset_singleton()

    def _arm_declared_grant(self, crew_home, mode: str = "auto"):
        """Establish a provenanced masked grant AND install the declared override in memory,
        exactly as ``grant_declared_yolo`` does at boot."""
        from kiro_crew import safety_override

        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant(mode=mode)
        # Precondition: the on-disk declaration is honoured under the masked mode.
        assert standing_approval.is_declared(mode) is True
        result = safety_override.grant_declared_yolo()
        assert result.active is True
        so = safety_override.safety_override()
        assert so.is_declared is True  # the LIVE grant is the operator's declared grant
        return so

    def test_revokes_the_declared_override_when_the_flip_is_to_an_unmasked_mode(self, crew_home):

        so = self._arm_declared_grant(crew_home, mode="auto")
        # 'off' does not mask the keystone, so the declaration is not an authorization.
        assert standing_approval.is_declared("off") is False

        revoked = _revalidate("off")

        assert revoked is True
        # The override is dropped from in-memory state BEFORE any 'off' session runs.
        assert so.is_declared is False
        assert so.is_active() is False

    def test_retains_the_declared_override_when_the_new_mode_still_masks(self, crew_home):

        so = self._arm_declared_grant(crew_home, mode="auto")
        # 'strict' still masks the keystone (a masking mode), so the grant is legitimate.
        assert standing_approval.is_declared("strict") is True

        revoked = _revalidate("strict")

        assert revoked is False
        assert so.is_declared is True
        assert so.is_active() is True

    def test_revocation_precedes_any_session_governed_by_the_new_mode(self, crew_home):
        """The revocation is synchronous in the applier, so by the time it returns the
        in-memory override is already gone -- a session started under the new mode cannot
        observe the stale grant."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        assert so.is_active() is True

        _revalidate("off")

        # No window: the state is dropped the moment the trusted applier returns.
        assert so.is_active() is False
        assert so.is_declared is False

    def test_does_not_touch_an_adhoc_grant(self, crew_home):
        """Only the DECLARED grant is mask-derived. An operator's ad-hoc timed grant is
        their own decision and must survive a sandbox flip untouched."""
        from kiro_crew import safety_override

        so = safety_override.safety_override()
        so.activate(source="dashboard", ttl=3600)
        assert so.is_active() is True
        assert so.is_declared is False

        revoked = _revalidate("off")

        assert revoked is False
        assert so.is_active() is True  # the ad-hoc grant is untouched

    def test_noop_when_no_grant_is_active(self, crew_home):
        from kiro_crew import safety_override

        so = safety_override.safety_override()
        assert so.is_active() is False

        assert _revalidate("off") is False
        assert so.is_active() is False

    # ── DERIVED trust is torn down synchronously on the flip (GPT F1) ──
    #
    # ``deactivate`` drops only in-memory grant state. A declared grant is
    # session-wide, so it also wrote ``approval_policy="auto"`` onto its slots and
    # into the shared channel-trust mapping, and ``subagent_manager.admission.
    # parent_trusted`` reads THAT policy directly (gate.py:1270-1272,1288-1290). The
    # revalidation must invoke the SAME synchronous ``on_policy_revoked`` teardown the
    # operator-revoke path uses, BEFORE the deactivate, so a spawn admitted right after
    # the flip cannot read a stale "auto". These mirror the ``on_policy_revoked``-clears-
    # a-``policies``-dict harness in ``test_approval_modes_enforcement`` and the
    # gate's own ``approval_policy == "auto"`` read.

    def _derived_trust_model(self):
        """The two stores the gateway's ``_clear_override_derived_trust`` clears: a
        per-slot ``approval_policy`` map (what ``admission.parent_trusted`` reads) and
        the shared channel-trust mapping. Returns them plus a ``_clear`` callback with
        the same effect the real hook has, and a list recording revalidation order."""
        policies = {"dashboard:s1": "auto", "channel:telegram:owner": "auto"}
        channel_mapping = {"channel:telegram:owner"}
        order: list[str] = []

        def _clear(_source):
            order.append("clear")
            for key in list(policies):
                policies[key] = ""
            channel_mapping.clear()

        return policies, channel_mapping, order, _clear

    def test_the_flip_clears_slot_and_channel_derived_trust_synchronously(self, crew_home):

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, channel_mapping, _order, _clear = self._derived_trust_model()
        so.on_policy_revoked = _clear

        revoked = _revalidate("off")

        assert revoked is True
        # By the time the trusted applier returns, a spawn admission read of the
        # parent slot's policy sees no "auto" -- the exact value gate.py consults.
        assert policies["dashboard:s1"] == ""
        # The shared channel-trust mapping (the CHANNEL half a subagent reads) is gone.
        assert policies["channel:telegram:owner"] == ""
        assert channel_mapping == set()
        assert so.is_active() is False

    def test_teardown_runs_before_the_grant_is_deactivated(self, crew_home):
        """Ordering MUST take the grant flag DOWN first (at suspend, before the mask
        read), then run the derived-trust teardown, then re-drop in the finally. The
        grant-first-only ordering was the unrecoverable one -- ``is_active()`` reporting
        no grant while the slots still carry "auto", or the flag live across the read --
        and suspend-before-read closes both."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        _policies, _channel_mapping, order, _clear = self._derived_trust_model()

        # Record when the grant flag actually drops, relative to the teardown.
        real_deactivate = so.deactivate

        def _recording_deactivate(*a, **kw):
            order.append("deactivate")
            return real_deactivate(*a, **kw)

        so.deactivate = _recording_deactivate  # type: ignore[method-assign]
        so.on_policy_revoked = _clear

        _revalidate("off")

        # suspend drops the flag first; then the derived-trust teardown; then the finally
        # re-drops it (idempotent belt-and-suspenders). The teardown is never the LAST
        # step, so no spawn in the gap reads a live "auto".
        assert order == ["deactivate", "clear", "deactivate"], (
            "grant flag must drop at suspend (before the read), the teardown must run, "
            f"and the finally must re-drop; saw {order}"
        )
        assert so.is_active() is False

    def test_a_retained_grant_does_not_tear_down_derived_trust(self, crew_home):
        """When the new mode still masks (grant retained), the teardown must NOT run --
        the operator's inherited trust is still legitimate."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, channel_mapping, order, _clear = self._derived_trust_model()
        so.on_policy_revoked = _clear

        revoked = _revalidate("strict")

        assert revoked is False
        assert order == []
        assert policies["dashboard:s1"] == "auto"
        assert channel_mapping == {"channel:telegram:owner"}

    def test_a_raising_teardown_fails_closed_and_re_raises(self, crew_home):
        """The trusted ConfigWatch applier retries on failure, so a teardown that
        raises must propagate (not be swallowed), like the rest of this fn -- AND the
        grant flag must still be dropped (in a ``finally``), or a raised teardown would
        preserve the global grant that the revocation exists to remove."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        assert so.is_active() is True

        def _boom(_source):
            raise RuntimeError("derived-trust store unreachable")

        so.on_policy_revoked = _boom

        with pytest.raises(RuntimeError, match="derived-trust store unreachable"):
            _revalidate("off")

        # The teardown raised, but the grant flag MUST be gone: the finally dropped it,
        # so no reader sees a live grant after a failed cleanup.
        assert so.is_active() is False
        assert so.is_declared is False


class TestRevalidationSuspendsTrustBeforeReadingTheMask:
    """GPT F2: the revalidation must suspend override-derived authorization SYNCHRONOUSLY
    before it yields to read the mask, restoring it only after a successful masked
    re-check -- never leaving standing authority live across the read or a cleanup failure.

    The gateway applier reads ``is_declared`` (a file open) off the event loop via
    ``asyncio.to_thread``. If the inherited ``approval_policy="auto"`` on the slots stayed
    live across that yield, a spawn admitted while the worker thread reads the mask would be
    auto-approved under the very mode being revalidated; and if the final teardown raised,
    the old ordering had never suspended anything, so the authority survived the failure.
    ``suspend_standing_override_trust`` + ``_finish_standing_override_revalidation`` split the
    work so the suspend happens first, on the dispatch thread, before any yield.
    """

    @pytest.fixture(autouse=True)
    def _fresh_override(self):
        from kiro_crew import safety_override

        safety_override.reset_singleton()
        yield
        safety_override.reset_singleton()

    def _arm_declared_grant(self, crew_home, mode: str = "auto"):
        from kiro_crew import safety_override

        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
        _activate_grant(mode=mode)
        assert standing_approval.is_declared(mode) is True
        result = safety_override.grant_declared_yolo()
        assert result.active is True
        so = safety_override.safety_override()
        assert so.is_declared is True
        return so

    def _suspend_model(self):
        """A slot-policy map with a suspend hook that clears it (returning a restore that
        puts it back) and a revoke hook that clears it for good, plus an ``order`` list and
        a marker for whether the mask was read while trust was still live."""
        policies = {"dashboard:s1": "auto"}
        order: list[str] = []
        state = {"live_at_read": None}

        def _suspend():
            order.append("suspend")
            snapshot = dict(policies)
            for key in list(policies):
                policies[key] = ""

            def _restore():
                order.append("restore")
                policies.update(snapshot)

            return _restore

        def _revoke(_source):
            order.append("revoke")
            for key in list(policies):
                policies[key] = ""

        return policies, order, state, _suspend, _revoke

    def test_suspend_runs_before_the_mask_is_read(self, crew_home, monkeypatch):
        """The inherited trust must be down BEFORE ``is_declared`` is read, so a spawn
        admitted during the read never sees a live 'auto'."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        real_is_declared = standing_approval.is_declared

        def _observing_is_declared(mode):
            # Record what a concurrent spawn admission would read AT the mask read.
            order.append(f"read({'live' if policies['dashboard:s1'] == 'auto' else 'suspended'})")
            return real_is_declared(mode)

        monkeypatch.setattr(standing_approval, "is_declared", _observing_is_declared)

        _revalidate("off")

        # Suspend precedes the read; the read sees the slot already suspended.
        assert order[0] == "suspend"
        assert order[1] == "read(suspended)", f"trust was live during the mask read: {order}"

    def test_a_still_masked_mode_restores_the_suspended_trust(self, crew_home):
        """Retain path: the mask still holds, so the grant is RE-ACTIVATED and the
        suspended inherited trust is RESTORED -- the operator's session keeps
        auto-approving. The global flag goes DOWN at suspend and back UP on the
        permitting recheck."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        revoked = _revalidate("strict")

        assert revoked is False
        assert order == ["suspend", "restore"]
        assert policies["dashboard:s1"] == "auto"  # trust is back
        assert so.is_declared is True  # grant re-activated
        assert so.is_active() is True

    def test_suspend_takes_the_global_grant_flag_down_before_the_read(self, crew_home, monkeypatch):
        """GPT F (gateway:15402): the global declared grant must be INACTIVE across the
        off-loop read, not just the inherited slot trust. A reader of ``is_active()`` at
        the mask read sees no live standing grant."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        assert so.is_active() is True
        seen = {}

        real_is_declared = standing_approval.is_declared

        def _observing_is_declared(mode):
            seen["active_at_read"] = so.is_active()
            return real_is_declared(mode)

        monkeypatch.setattr(standing_approval, "is_declared", _observing_is_declared)

        _revalidate("strict")

        assert seen["active_at_read"] is False, "global grant was live during the mask read"
        # Permitting recheck re-activated it.
        assert so.is_active() is True

    def test_an_unmasked_mode_revokes_and_never_restores(self, crew_home):
        """Revoke path: suspend, then (mask gone) revoke for good -- no restore."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        revoked = _revalidate("off")

        assert revoked is True
        assert "restore" not in order
        assert order[0] == "suspend"
        assert "revoke" in order
        assert policies["dashboard:s1"] == ""
        assert so.is_active() is False

    def test_a_raising_suspend_hook_still_revokes_never_leaves_trust_live(self, crew_home):
        """If the suspend hook raises, the finish step treats trust as suspended and
        REVOKES (fail closed) rather than restoring -- authority is never left live."""

        so = self._arm_declared_grant(crew_home, mode="auto")
        order: list[str] = []

        def _boom_suspend():
            order.append("suspend-raise")
            raise RuntimeError("suspend store unreachable")

        def _revoke(_source):
            order.append("revoke")

        so.on_policy_suspend = _boom_suspend
        so.on_policy_revoked = _revoke

        # A masking mode would normally RETAIN -- but a failed suspend must not let the
        # finish step restore; it carries no restore callable, so a still-masked re-check
        # simply keeps the grant with nothing to put back, and an unmasked one revokes.
        revoked = _revalidate("off")

        assert revoked is True
        assert order == ["suspend-raise", "revoke"]
        assert so.is_active() is False

    def test_a_grant_activated_mid_revalidation_is_not_clobbered(self, crew_home):
        """Suspend takes the grant DOWN. If the operator activates a NEW grant during the
        off-loop read, a permitting recheck must NOT re-activate the suspended one over
        it -- the finish re-activates only when nothing is live (``not is_active()``)."""
        from kiro_crew import safety_override

        so = self._arm_declared_grant(crew_home, mode="auto")
        suspension = safety_override.suspend_standing_override_trust()
        assert suspension is not None
        # Suspend already deactivated the declared grant.
        assert so.is_active() is False

        # Operator establishes a fresh ad-hoc grant in the window.
        so.activate(source="dashboard", ttl=3600)
        assert so.is_active() is True
        assert so.is_declared is False  # it is the ad-hoc grant, not the declared one

        # Finishing against a still-masking mode must leave the operator's new grant alone.
        revoked = safety_override._finish_standing_override_revalidation("strict", suspension)

        assert revoked is False
        assert so.is_active() is True
        assert so.is_declared is False  # the declared grant was NOT re-activated over it

    def test_a_policy_denial_during_the_window_is_not_restored(self, crew_home, monkeypatch):
        """GPT (safety_override:2390): restore must go through the POLICY gate. If the
        enterprise ceiling denies YOLO between suspend and the permitting mask recheck,
        the declared grant must NOT be restored (a bare ``activate_declared`` would ignore
        the denial)."""
        from kiro_crew import safety_override

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        # Policy now forbids YOLO outright: grant_declared_yolo yields no live grant.
        monkeypatch.setattr(safety_override, "declared_grant_permitted", lambda: False)
        monkeypatch.setattr(safety_override, "yolo_policy_permits", lambda: False)

        # The mask still holds, so without the policy gate the grant would be restored.
        _revalidate("strict")

        # The grant was NOT restored, and the suspended inherited trust was torn down for
        # good rather than put back over a denied grant.
        assert so.is_active() is False
        assert "restore" not in order
        assert policies["dashboard:s1"] == ""

    def test_an_operator_revoke_during_the_window_is_not_re_armed(self, crew_home):
        """GPT F2 (safety_override:2402): suspend takes the grant DOWN, so an operator who
        selects Normal in the window revokes an already-inactive grant -- a no-op that
        moves no ``is_active`` and no epoch. The finish step must NOT re-arm it: it tracks
        the explicit-revocation generation and restores only when it is unchanged."""
        from kiro_crew import safety_override

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        # Suspend takes the declared grant and its inherited trust down.
        suspension = safety_override.suspend_standing_override_trust()
        assert suspension is not None
        assert so.is_active() is False

        # Operator selects Normal in the window: deactivating an already-suspended grant is
        # a no-op for state, but it bumps the explicit-revocation generation.
        so.deactivate("dashboard")

        # Finish against a still-masking mode must NOT re-arm the revoked grant.
        safety_override._finish_standing_override_revalidation("strict", suspension)

        assert so.is_active() is False
        assert so.is_declared is False
        assert "restore" not in order
        assert policies["dashboard:s1"] == ""

    def test_a_slack_off_during_the_window_is_not_re_armed(self, crew_home):
        """GPT 6.1 F3 (safety_override:2400): the operator's revoke during the suspend
        window must be seen even when it arrives through Slack's ``disable_yolo``.

        ``disable_yolo`` must not early-return on ``has_grant()`` -- which reads
        ``_active``, False while the declared grant is SUSPENDED for the ``agent.sandbox``
        re-check -- because that skips the ``deactivate`` that bumps the explicit-revocation
        generation, and the finish step then re-arms the grant the operator turned off.
        ``disable_yolo`` always routes through ``deactivate("slack")``, so the generation
        bump happens during the window and the finish step refuses to re-arm.
        """
        from kiro_crew import safety_override
        from kiro_crew.slack import handler as h

        so = self._arm_declared_grant(crew_home, mode="auto")
        policies, order, _state, _suspend, _revoke = self._suspend_model()
        so.on_policy_suspend = _suspend
        so.on_policy_revoked = _revoke

        suspension = safety_override.suspend_standing_override_trust()
        assert suspension is not None
        assert so.is_active() is False
        # has_grant() reads _active, which is False while suspended -- the state an
        # off-path must still record a revocation against.
        assert so.has_grant() is False

        # Operator sends Slack "yolo off" DURING the window, through the real caller path.
        h.disable_yolo()

        # Finish against a still-masking mode must NOT re-arm the Slack-revoked grant.
        safety_override._finish_standing_override_revalidation("strict", suspension)

        assert so.is_active() is False
        assert so.is_declared is False
        assert "restore" not in order
        assert policies["dashboard:s1"] == ""
