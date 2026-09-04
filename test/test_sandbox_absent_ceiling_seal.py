"""An ABSENT crew-home ceiling is still sealed read-only by the Linux launcher.

``mount(2)`` cannot target a path that does not exist, so the ``READONLY_DIRS`` loop's
``if os.path.exists(target)`` guard silently skips a ceiling that has never been written
— and on a default install that is most of them, which left the data home writable at
exactly the names the seal exists to protect. ``_materialize_sealable_ceilings()`` closes
that by creating the absent ceiling first, but only for the leaves that clear both tests
the production comment states: an empty document must mean what an absent file means, and
a STALE read of it must fail toward refusal.

The load-bearing test here is
:meth:`TestSealAppliesToAPreviouslyAbsentCeiling.test_bind_and_remount_pair_is_emitted`:
it executes the launcher's own seal loop (extracted from the generated script, so the
production source is what runs) against a real filesystem, with ``_mount_or_die``
replaced by a recorder. Delete the materialiser and that loop records nothing, because
the guard falls through — which is the whole defect.

:class:`TestCeilingsThatMustNotBeMaterialized` is the fence in the opposite direction,
and it is the one to read before adding a leaf: three ceilings read a present-but-empty
file as something other than absent, and a fourth would be pinned stale in the dangerous
direction by the bind mount itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from kiro_crew import sandbox

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

#: Flag values the launcher defines for itself; mirrored so the extracted loop can run.
_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 4096


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """Point ``config_dir()`` — the live data home — at a scratch tree."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    return home


def _seal_loop_source() -> str:
    """The launcher's ``READONLY_DIRS`` loop body, ready to run.

    Pulled out of the generated script rather than restated, so this test cannot pass
    against a loop the launcher no longer contains.
    """
    script = sandbox._build_launcher_script("strict")
    loop = (
        "for d in READONLY_DIRS:"
        + script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]
    )
    return textwrap.dedent(loop)


def _run_seal_loop(targets: list[str]) -> list[tuple[str, int]]:
    """Execute the launcher's seal loop over *targets*, recording every mount call."""
    calls: list[tuple[str, int]] = []

    def _record(source, target, flags, what):
        assert source == target, "a ceiling is bound over ITSELF, not over an empty source"
        calls.append((os.fsdecode(target), flags))

    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(  # noqa: S102 - running the launcher's OWN generated source is the assertion
        _seal_loop_source(),
        {
            "os": os,
            "READONLY_DIRS": targets,
            "_mount_or_die": _record,
            "_MS_BIND": _MS_BIND,
            "_MS_REMOUNT": _MS_REMOUNT,
            "_MS_RDONLY": _MS_RDONLY,
            # Stubbed to 0 so this test keeps asserting the bind+remount PAIR
            # exactly; the flag re-assertion itself is covered by
            # test_sandbox_seal_locked_flags.py against the real helper.
            "_locked_mount_flags": lambda _target: 0,
        },
    )
    return calls


@_POSIX_ONLY
class TestSealAppliesToAPreviouslyAbsentCeiling:
    def test_bind_and_remount_pair_is_emitted(self, crew_home):
        """The seal reaches a ceiling that did not exist when the spawn started.

        Both calls are asserted, not just the first: ``MS_RDONLY`` is ignored on the
        initial ``MS_BIND``, so a bind without the remount grants exactly the write
        access the loop exists to withhold.
        """
        target = str(crew_home / "computer_use.json")
        assert not os.path.exists(target)

        assert target in sandbox._materialize_sealable_ceilings()
        calls = _run_seal_loop([target])

        assert calls == [
            (target, _MS_BIND),
            (target, _MS_REMOUNT | _MS_BIND | _MS_RDONLY),
        ]

    def test_loop_skips_the_ceiling_when_it_was_never_materialized(self, crew_home):
        """The defect, pinned: no target on disk means no seal at all."""
        target = str(crew_home / "computer_use.json")

        assert _run_seal_loop([target]) == []

    def test_every_sealable_leaf_is_created(self, crew_home):
        created = sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert json.loads(path.read_text(encoding="utf-8")) == {}
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

        for leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_the_hidden_records_dir_is_materialised(self, crew_home):
        """The MASK's counterpart to the ceiling case above.

        A hidden leaf has the same existence requirement as a read-only ceiling and
        the opposite reason for it: on Linux the mask is a bind mount whose loop
        guards on ``isdir``, so an ABSENT directory is silently skipped -- and
        skipped precisely on the fresh install where the agent could create it
        first and write what the gateway later reads back as authoritative.

        Derived from the tuple, so a second hidden leaf is covered without editing
        this test.
        """
        created = set(sandbox._materialize_sealable_ceilings())

        assert sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES, "empty tuple would be vacuous"
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created, f"{leaf} was not materialised, so its mask is skipped"
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_created_ceilings_are_in_the_launcher_readonly_list(self, crew_home):
        """Creating a path is only useful if the seal loop is handed it.

        Reconciled PER DISPOSITION rather than against one list, because two kinds
        of path are materialised for opposite reasons and each has its own loop:

        * a read-only ceiling is created so the SEAL can apply -> ``READONLY_DIRS``;
        * a hidden leaf is created so the MASK can -> ``SENSITIVE_DIRS``.

        Asserting every created path against ``READONLY_DIRS`` alone would demand
        that a directory meant to be invisible in the sandbox be exposed read-only
        instead -- the exact inversion of its purpose. Derived from the two
        precreate tuples, so a leaf added to either one must appear in the matching
        launcher list rather than in whichever list this test happened to name.
        """
        created = set(sandbox._materialize_sealable_ceilings())
        script = sandbox._build_launcher_script("strict")

        def _launcher_list(name: str) -> set[str]:
            match = re.search(rf"{name} = (\[.*?\])\n", script, re.S)
            assert match, f"{name} is not emitted by the launcher script"
            return set(json.loads(match.group(1)))

        readonly = _launcher_list("READONLY_DIRS")
        masked = _launcher_list("SENSITIVE_DIRS")

        # Every created path is handed to exactly the loop its disposition needs.
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = str(crew_home / leaf)
            if path not in created:
                continue  # covered by test_the_hidden_records_dir_is_materialised
            assert path in masked, f"{leaf} is created to be masked but is not in SENSITIVE_DIRS"
            assert path not in readonly, (
                f"{leaf} is exposed READ-ONLY as well as masked; a hidden leaf that "
                "is also readable is not hidden"
            )

        # And nothing created is left with no disposition at all -- the original
        # invariant, widened rather than weakened.
        unaccounted = created - readonly - masked
        assert not unaccounted, f"created but handed to no seal loop: {sorted(unaccounted)}"

    def test_namespace_argv_materializes_before_the_launcher_runs(self, crew_home):
        """The production wiring, not just the helper.

        The seal happens in the launcher CHILD after ``namespace_argv`` returns, so the
        creation has to be on this path — a helper nobody calls seals nothing.
        """
        sandbox.namespace_argv(["/bin/true"])

        assert (crew_home / "computer_use.json").is_file()
        assert (crew_home / "profiles").is_dir()

    def test_relocated_data_home_is_covered(self, tmp_path, monkeypatch):
        """A data home that escapes ``$HOME`` gets the same treatment.

        Without this the fleets that relocate the data home — the ones most likely to
        care about a governance ceiling — would be the only ones left unsealed.
        """
        relocated = tmp_path / "srv" / "crew"
        relocated.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: relocated)

        assert str(relocated / "computer_use.json") in sandbox._materialize_sealable_ceilings()

    def test_deprecated_home_spelling_gains_no_stubs(self, crew_home, tmp_path):
        """Only the LIVE data home is materialised.

        The deny lists cover both ``_CREW_HOME_PREFIXES`` because either tree may hold
        bytes, but creation is the opposite case: a stub under a migrated host's leftover
        ``~/.kirocrew`` is a file nothing will ever read.
        """
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()

        sandbox._materialize_sealable_ceilings()

        assert list(legacy.iterdir()) == []


@_POSIX_ONLY
class TestALinkedProtectedLeafRefusesTheSpawn:
    """A disposition attaches to a NAME; following a link voids it.

    The fourth probe of the same fence, and the same shape as the other three: the
    path the launcher covers and the path the bytes reach diverged. Here
    ``.resolve()`` followed the link, so the store wrote through to the target while
    the bind-mask -- which guards on ``isdir`` of the leaf -- attached to the link.
    The link name stays in the writable data home, so a sandboxed process can unlink
    it and drop a directory of its own.

    Refused rather than warned, unlike every other ceiling: see
    ``sandbox._CREW_NO_ALIAS_LEAVES`` for why the chezmoi/stow argument that earns
    the warning elsewhere does not apply to these two.
    """

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_a_symlinked_leaf_refuses(self, crew_home, tmp_path, leaf):
        elsewhere = tmp_path / f"elsewhere-{leaf}"
        elsewhere.mkdir()
        link = crew_home / leaf
        if link.exists() or link.is_symlink():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as caught:
            sandbox._materialize_sealable_ceilings()
        assert "SYMLINK" in str(caught.value)
        assert leaf in str(caught.value)

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_the_refusal_beats_the_warning_path(self, crew_home, tmp_path, leaf, caplog):
        """It must REFUSE, not log that the path was covered and continue.

        Warning and continuing is what made this silent: the log claimed a seal that
        the bytes never got.
        """
        elsewhere = tmp_path / f"elsewhere2-{leaf}"
        elsewhere.mkdir()
        link = crew_home / leaf
        if link.exists() or link.is_symlink():
            link.unlink() if link.is_symlink() else shutil.rmtree(link)
        link.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()
        # Nothing was written through the link either.
        assert not list(elsewhere.iterdir())

    @pytest.mark.parametrize("leaf", sorted(sandbox._CREW_NO_ALIAS_LEAVES))
    def test_a_real_directory_is_accepted(self, crew_home, leaf):
        """The refusal must not reject the ordinary case."""
        (crew_home / leaf).mkdir(parents=True, exist_ok=True)
        sandbox._materialize_sealable_ceilings()  # does not raise

    def test_the_no_alias_set_covers_both_panel_leaves(self):
        """Derived, so a third protected leaf has to be added here too."""
        assert "crew-panels" in sandbox._CREW_NO_ALIAS_LEAVES
        assert "panel-templates" in sandbox._CREW_NO_ALIAS_LEAVES
        # Every no-alias leaf is actually materialised, or the guard never runs.
        materialised = set(sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES) | set(
            sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        )
        assert (
            sandbox._CREW_NO_ALIAS_LEAVES <= materialised
        ), "a no-alias leaf that is never materialised is never checked"


@_POSIX_ONLY
class TestPublishIsAllOrNothing:
    """The ceiling path never appears holding anything but the complete document."""

    def test_partial_write_publishes_nothing(self, crew_home, monkeypatch):
        """A zero-length ``*.json`` would read as CORRUPT, not as absent."""

        def _boom(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(os, "write", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            assert not (crew_home / leaf).exists()

    def test_no_temp_file_is_left_behind(self, crew_home, monkeypatch):
        """The temp sibling is reclaimed on the failure path as well as the success one."""

        def _boom(src, dst):
            raise OSError("cross-device link")

        monkeypatch.setattr(os, "link", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert [p.name for p in crew_home.iterdir() if p.name.startswith(".kirocrew-ceiling")] == []

    def test_publish_never_clobbers_a_racing_writer(self, crew_home):
        """``os.link`` fails with EEXIST rather than overwriting.

        The upstream ``os.path.exists`` check is an optimisation, not the guard — a
        second spawn, or an operator writing the real document, can land between it and
        the publish.
        """
        target = crew_home / "computer_use.json"
        target.write_text('{"enabled": true}', encoding="utf-8")

        assert sandbox._publish_empty_ceiling(str(target), str(crew_home)) is False
        assert target.read_text(encoding="utf-8") == '{"enabled": true}'

    def test_existing_ceiling_is_left_byte_for_byte_alone(self, crew_home):
        """Never a truncate: an operator's document outranks the absent default."""
        target = crew_home / "aws_service_consent.json"
        target.write_text('{"s3": "confirmed"}', encoding="utf-8")

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.read_text(encoding="utf-8") == '{"s3": "confirmed"}'


@_POSIX_ONLY
class TestAShortWriteNeverPublishes:
    """``os.write`` may consume part of the buffer and report it as success."""

    def test_partial_progress_is_completed_not_published_short(self, crew_home):
        """The loop finishes the document; a one-byte-at-a-time write still lands whole."""
        real_write = os.write
        calls: list[int] = []

        def _one_byte(fd, data):
            calls.append(len(data))
            return real_write(fd, bytes(data[:1]))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "write", _one_byte)
            assert sandbox._publish_empty_ceiling(
                str(crew_home / "computer_use.json"), str(crew_home)
            )

        assert len(calls) > 1, "a short write must be retried, not accepted"
        assert (crew_home / "computer_use.json").read_bytes() == sandbox._EMPTY_CEILING_DOCUMENT

    def test_zero_progress_publishes_nothing(self, crew_home, monkeypatch):
        """A filesystem accepting nothing must fail, not spin forever."""
        monkeypatch.setattr(os, "write", lambda fd, data: 0)

        assert (
            sandbox._publish_empty_ceiling(str(crew_home / "computer_use.json"), str(crew_home))
            is False
        )
        assert not (crew_home / "computer_use.json").exists()


@_POSIX_ONLY
class TestAnUnsealedCeilingIsNeverSilent:
    """A seal that could not be established is logged, because nothing else reports it.

    Raising instead would reach far past this seal: ``namespace_argv`` runs under
    ``wrap_argv``, whose callers catch narrowly and degrade their own operation, so an
    additive control would take every sandboxed spawn on the host down with it.
    """

    def test_failed_dir_creation_warns(self, crew_home, monkeypatch, caplog):
        monkeypatch.setattr(
            os, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs"))
        )

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._materialize_sealable_ceilings()

        assert "REFUSING to launch" in caplog.text
        assert "profiles" in caplog.text

    def test_failed_publish_warns(self, crew_home, monkeypatch, caplog):
        monkeypatch.setattr(
            os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks"))
        )

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._materialize_sealable_ceilings()

        # Only the FIRST unsealable ceiling is reached: the refusal is immediate, which is
        # the point -- the loop must not carry on creating the rest behind a known hole.
        assert "REFUSING to launch" in caplog.text
        assert sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES[0] in caplog.text

    def test_success_is_quiet(self, crew_home, caplog):
        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "REFUSING to launch" not in caplog.text


@_POSIX_ONLY
class TestACreationFailureRefusesTheSpawn:
    """A ceiling that cannot be created is a ceiling that will not be sealed.

    Earlier revisions warned and continued here. That is the shape where the launcher's
    ``exists`` guard silently skips the path and the agent runs with a writable keystone,
    so the failure is fatal to the spawn instead — the ``_mount_or_die`` posture. No
    ``wrap_argv`` caller falls back to running the command unconfined, so refusing costs
    the operation, never the confinement.
    """

    def test_unwritable_data_home_refuses(self, crew_home, monkeypatch):
        def _boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(os, "mkdir", _boom)
        monkeypatch.setattr(tempfile, "mkstemp", _boom)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_failed_publish_refuses(self, crew_home, monkeypatch):
        monkeypatch.setattr(
            os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks"))
        )

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_a_race_that_another_writer_won_is_benign(self, crew_home, monkeypatch):
        """``EEXIST`` means the path now exists, so the launcher seals it: nothing to do."""
        target = crew_home / "computer_use.json"

        def _link_but_someone_won(src_path, dst):
            # Honour the dst actually being published: every ceiling in the loop goes
            # through this stub, and a stub that only ever creates ONE of them leaves the
            # others genuinely unsealable -- which is a different scenario.
            Path(os.fsdecode(dst)).write_text('{"enabled": false}', encoding="utf-8")
            raise FileExistsError("raced")

        monkeypatch.setattr(os, "link", _link_but_someone_won)

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.read_text(encoding="utf-8") == '{"enabled": false}'

    def test_an_existing_dir_ceiling_is_benign(self, crew_home):
        (crew_home / "profiles").mkdir()

        created = sandbox._materialize_sealable_ceilings()

        assert str(crew_home / "profiles") not in created

    def test_absent_data_home_is_not_created(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope" / "crew"
        monkeypatch.setattr(sandbox, "config_dir", lambda: missing)

        assert sandbox._materialize_sealable_ceilings() == []
        assert not missing.exists()

    def test_unresolvable_data_home_yields_nothing(self, monkeypatch):
        def _boom():
            raise RuntimeError("no data home")

        monkeypatch.setattr(sandbox, "config_dir", _boom)

        assert sandbox._sealable_absent_ceilings() == ([], [])
        assert sandbox._materialize_sealable_ceilings() == []


@_POSIX_ONLY
class TestADanglingSymlinkRefusesTheSpawn:
    """The one state that defeats every ``os.path.exists`` guard on this path at once.

    ``exists`` FOLLOWS symlinks, so a dangling link reports as absent: the materialiser
    tries to create and ``os.link`` refuses with ``EEXIST``, the launcher's seal loop
    emits no mount, and the sandboxed process's write then follows the link to a file the
    host afterwards reads through the ceiling path. The link is plantable through the very
    hole this change closes, so an agent on an earlier build could pre-arm it.

    It is REFUSED, not removed. ``islink`` followed by ``unlink`` is not atomic and the
    dashboard publishes a real keystone over that same name with ``atomic_write``, so a
    removal racing a validated operator write would delete the operator's new settings —
    and POSIX has no unlink-only-if-still-a-symlink to close that window with.
    """

    def test_the_unguarded_chain_really_is_exploitable(self, crew_home):
        """Pin the mechanism itself, so the refusal below is not guarding a phantom."""
        target = crew_home / "computer_use.json"
        victim = crew_home / "elsewhere.json"
        target.symlink_to(victim)

        assert os.path.lexists(target) is True
        assert os.path.exists(target) is False, "exists() follows the link -> reads as absent"
        # The launcher's own guard therefore skips it: no bind, no remount.
        assert _run_seal_loop([str(target)]) == []
        # And a write through the link lands where the host will read it back.
        target.write_text('{"enabled": true}', encoding="utf-8")
        assert victim.exists()
        assert target.read_text(encoding="utf-8") == '{"enabled": true}'

    def test_a_file_ceiling_squatter_refuses(self, crew_home):
        target = crew_home / "computer_use.json"
        target.symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_sealable_ceilings()

        assert "computer_use.json" in str(err.value)
        assert "elsewhere.json" in str(err.value), "the destination is the diagnostic value"

    def test_a_dir_ceiling_squatter_refuses(self, crew_home):
        target = crew_home / "profiles"
        target.symlink_to(crew_home / "no-such-dir")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

    def test_the_squatter_is_never_removed(self, crew_home):
        """Removing it is the data-loss path this refusal exists to avoid."""
        target = crew_home / "computer_use.json"
        target.symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert target.is_symlink(), "the link is the operator's to resolve, not ours to delete"
        assert not (crew_home / "elsewhere.json").exists(), "nothing written through the link"

    def test_namespace_argv_refuses_rather_than_launching(self, crew_home):
        """The refusal has to reach the spawn path, or it protects nothing."""
        (crew_home / "computer_use.json").symlink_to(crew_home / "elsewhere.json")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.namespace_argv(["/bin/true"])

    def test_a_resolving_symlink_is_left_alone(self, crew_home):
        """It reads as present, so the launcher seals the inode it resolves to.

        The remaining exposure — the link NAME stays replaceable in a writable parent — is
        pre-existing for every ceiling and cannot be closed without sealing the data-home
        root, so this change must neither refuse nor delete on account of it.
        """
        real = crew_home / "elsewhere.json"
        real.write_text('{"enabled": false}', encoding="utf-8")
        target = crew_home / "computer_use.json"
        target.symlink_to(real)

        created = sandbox._materialize_sealable_ceilings()

        assert str(target) not in created
        assert target.is_symlink()
        assert real.read_text(encoding="utf-8") == '{"enabled": false}'


@_POSIX_ONLY
class TestAnAliasBackedCeilingIsReported:
    """``MS_RDONLY`` binds a MOUNT, not an inode, so a second name survives the seal.

    Both shapes are PRE-EXISTING -- the ceilings this module publishes end at
    ``st_nlink == 1`` and are never symlinks (pinned below) -- so they are reported rather
    than refused: a dotfile manager or a snapshot tool giving a config file a second name
    is ordinary, and failing the spawn over it is a far wider blast radius than the hole.
    """

    def test_what_this_module_publishes_is_never_alias_backed(self, crew_home):
        """The premise of reporting rather than refusing: we never create the shape."""
        sandbox._materialize_sealable_ceilings()

        for leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES:
            path = crew_home / leaf
            assert path.stat().st_nlink == 1, "a published ceiling must have no alias"
            assert not path.is_symlink()
        assert [p.name for p in crew_home.iterdir() if p.name.startswith(".kirocrew-ceiling")] == []

    def test_a_symlinked_ceiling_is_reported(self, crew_home, caplog):
        real = crew_home / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        (crew_home / "computer_use.json").symlink_to(real)

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "is a SYMLINK" in caplog.text
        assert "computer_use.json" in caplog.text

    def test_a_hardlinked_ceiling_is_reported(self, crew_home, caplog):
        target = crew_home / "computer_use.json"
        target.write_text("{}", encoding="utf-8")
        os.link(target, crew_home / "alias.json")
        assert target.stat().st_nlink == 2

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "hardlinks" in caplog.text
        assert "computer_use.json" in caplog.text

    def test_a_symlinked_dir_ceiling_is_reported(self, crew_home, caplog):
        real = crew_home / "real-profiles"
        real.mkdir()
        (crew_home / "profiles").symlink_to(real)

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "is a SYMLINK" in caplog.text
        assert "profiles" in caplog.text

    def test_reporting_never_refuses_and_never_removes(self, crew_home):
        """The whole point of warning: an ordinary dotfile-manager host still runs."""
        real = crew_home / "elsewhere.json"
        real.write_text('{"enabled": false}', encoding="utf-8")
        link = crew_home / "computer_use.json"
        link.symlink_to(real)

        sandbox._materialize_sealable_ceilings()  # must not raise

        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == '{"enabled": false}'

    def test_an_ordinary_single_link_ceiling_is_quiet(self, crew_home, caplog):
        (crew_home / "computer_use.json").write_text("{}", encoding="utf-8")

        with caplog.at_level("WARNING", logger="kiro_crew.sandbox"):
            sandbox._materialize_sealable_ceilings()

        assert "SYMLINK" not in caplog.text
        assert "hardlinks" not in caplog.text


@_POSIX_ONLY
class TestCeilingsThatMustNotBeMaterialized:
    """Four ceilings the seal deliberately does not reach, for two distinct reasons.

    ``denied_commands.json`` reads ``{}`` as its absent default, but a bind mount pins
    the INODE while every dashboard writer publishes a NEW one through ``atomic_write``,
    so a sealed stub would report "nothing is denied" to in-sandbox ``mcp_cron`` for the
    rest of the sandbox's life. The other three read a present-but-empty file as
    something other than absent: ``security_policy.json`` raises
    ``PlatformCompositionError`` out of ``governance.load_security_policy`` (which reruns
    at boot and per app callback), ``app_admission.json`` flips ``open_default()`` into
    deny-all, and ``admission_policy.json`` is already seeded at first run.
    """

    EXCLUDED = (
        "denied_commands.json",
        "security_policy.json",
        "app_admission.json",
        "admission_policy.json",
    )

    @pytest.mark.parametrize("leaf", EXCLUDED)
    def test_leaf_is_not_in_the_precreate_set(self, leaf):
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert leaf not in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES

    @pytest.mark.parametrize("leaf", EXCLUDED)
    def test_leaf_is_not_written_to_disk(self, leaf, crew_home):
        sandbox._materialize_sealable_ceilings()

        assert not (crew_home / leaf).exists()
