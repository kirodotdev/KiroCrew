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

import errno
import json
import os
import re
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

    def test_created_ceilings_are_in_the_launcher_readonly_list(self, crew_home):
        """Creating a path is only useful if the seal loop is handed it."""
        created = sandbox._materialize_sealable_ceilings()
        script = sandbox._build_launcher_script("strict")
        match = re.search(r"READONLY_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        readonly = set(json.loads(match.group(1)))

        assert set(created) <= readonly

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


class TestMaskableDirsAreMaterializedBeforeTheSpawn:
    """An on-demand HIDDEN directory gets the mirror treatment of the ceilings above.

    The ``SENSITIVE_DIRS`` loop is guarded on ``isdir``, so a leaf the gateway creates
    lazily is unmasked in every sandbox spawned before its first use -- and once the
    gateway does create it, that running sandbox sees it. Creating it empty before the
    spawn is what gives the mask a name to bind over.
    """

    def test_every_maskable_leaf_is_created_owner_only(self, crew_home):
        created = sandbox._materialize_maskable_dirs()

        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            path = crew_home / leaf
            assert str(path) in created
            assert path.is_dir()
            # The mode is a POSIX property; this materialisation feeds the Linux
            # namespace launcher, and Windows ignores the mode argument.
            if os.name == "posix":
                assert stat.S_IMODE(path.stat().st_mode) == 0o700

    def test_the_leaf_is_created_directly_under_the_data_home(self, crew_home):
        # A top-level leaf on purpose: every intermediate directory between the
        # data home and the mask would be an agent-writable ancestor that a rename
        # could swap out from under a transfer (see storage.STAGING_DIR_LEAF).
        sandbox._materialize_maskable_dirs()
        for leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
            assert "/" not in leaf
            assert (crew_home / leaf).is_dir()

    def test_an_existing_directory_is_left_alone_and_not_reported(self, crew_home):
        target = crew_home / "aws-control-staging"
        target.mkdir(parents=True)
        marker = target / "drive-preview-live"
        marker.mkdir()

        assert sandbox._materialize_maskable_dirs() == []
        assert marker.is_dir()

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", ["standard", "cc", "strict"])
    def test_created_dirs_are_in_the_launcher_hidden_list(self, crew_home, mode):
        """Creating a path is only useful if the mask loop is handed it."""
        created = sandbox._materialize_maskable_dirs()
        script = sandbox._build_launcher_script(mode)
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        hidden = set(json.loads(match.group(1)))

        assert created
        assert set(created) <= hidden

    @_POSIX_ONLY
    def test_namespace_argv_materializes_the_masked_dirs(self, crew_home):
        sandbox.namespace_argv(["/bin/true"])

        assert (crew_home / "aws-control-staging").is_dir()

    def test_a_file_squatting_the_name_refuses_the_spawn(self, crew_home):
        # A plain file where the directory should be cannot be masked by the dir
        # loop (``isdir`` is false) and would be skipped silently; refuse instead.
        squat = crew_home / "aws-control-staging"
        squat.parent.mkdir(parents=True, exist_ok=True)
        squat.write_text("not a directory", encoding="utf-8")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()

    def test_an_unresolvable_data_home_refuses_the_spawn(self, monkeypatch):
        # Skipping the mask because the data home could not be resolved would
        # run the agent with the staging directory visible -- the exposure this
        # function exists to prevent. Fail closed, like every other reason.
        def boom():
            raise OSError("data home unavailable")

        monkeypatch.setattr(sandbox, "config_dir", boom)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()


@_POSIX_ONLY
class TestASymlinkMaskableLeafRefusesTheSpawn:
    """A HIDDEN-dir leaf that is a symlink is GPT's staging-mask bypass, and is refused.

    ``os.path.isdir`` follows a link, so a leaf that RESOLVES to a real directory reads as
    a directory and the mask loop would bind over the link's TARGET, not the leaf name.
    The name lives in the writable data home, so a sandboxed process can unlink it and put
    an agent-owned directory in its place -- and the pre-created staging directory the
    preview CLI writes into is then one the agent controls. ``_refuse_if_dangling_symlink``
    only rejects a link that resolves to nothing, so ``_refuse_if_symlink_leaf`` closes the
    RESOLVING case; the create-race re-check closes the swap-during-the-window case.
    """

    def test_the_unguarded_chain_really_is_exploitable(self, crew_home):
        """Pin the mechanism: isdir() follows the link, so it reads as a maskable dir."""
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        assert os.path.islink(target) is True
        assert os.path.isdir(target) is True, "isdir() follows the link -> reads as a dir"
        # Without the guard the loop would hit the isdir() branch and mask the link's
        # target, leaving the replaceable leaf name pointing at an agent-owned tree.

    def test_a_resolving_symlink_leaf_refuses(self, crew_home):
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_maskable_dirs()

        assert "aws-control-staging" in str(err.value)
        assert "agent-writable" in str(err.value), "the destination is the diagnostic value"

    def test_the_symlink_leaf_is_never_removed(self, crew_home):
        """Removing it is a data-loss path; the link is the operator's to resolve."""
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"
        target.symlink_to(victim)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_maskable_dirs()

        assert target.is_symlink(), "the link is not ours to delete"

    def test_a_symlink_swapped_in_after_a_lost_create_race_refuses(self, crew_home, monkeypatch):
        # The window between our checks and ``mkdir``: a sandboxed process wins the
        # create with a symlink to a tree it owns, so mkdir raises FileExistsError and a
        # plain isdir() re-check would follow the link and mask its target. The
        # no-follow re-validation must refuse instead.
        victim = crew_home / "agent-writable"
        victim.mkdir()
        target = crew_home / "aws-control-staging"

        real_mkdir = os.mkdir

        def racing_mkdir(path, mode=0o777, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(str(target)):
                # Simulate the racer landing a symlink at the leaf, then report the
                # collision the kernel would have raised.
                os.symlink(str(victim), str(target))
                raise FileExistsError(errno.EEXIST, "File exists", str(path))
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", racing_mkdir)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_maskable_dirs()

        assert "aws-control-staging" in str(err.value)
        assert target.is_symlink(), "the raced-in link is not ours to delete"

    def test_a_real_dir_winning_the_create_race_is_accepted(self, crew_home, monkeypatch):
        # The benign race: another spawn created the real directory first. mkdir raises
        # FileExistsError, the no-follow re-check finds a real dir, and the spawn proceeds.
        target = crew_home / "aws-control-staging"

        real_mkdir = os.mkdir

        def racing_mkdir(path, mode=0o777, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(str(target)):
                real_mkdir(str(target), 0o700)
                raise FileExistsError(errno.EEXIST, "File exists", str(path))
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", racing_mkdir)

        # Must not raise: a real directory won the race.
        sandbox._materialize_maskable_dirs()
        assert target.is_dir() and not target.is_symlink()
