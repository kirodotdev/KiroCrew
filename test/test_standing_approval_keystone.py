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
import subprocess
import sys

import pytest

from kiro_crew import platform_compat, sandbox, standing_approval
from kiro_crew.config import loader
from kiro_crew.security import paths as security_paths
from kiro_crew.subprocess_utf8 import UTF8_TEXT

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
        assert standing_approval.is_declared("auto") is True

    def test_an_explicit_boolean_false_does_not_grant(self, crew_home):
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: False})
        assert standing_approval.is_declared("auto") is False

    def test_an_unreadable_document_is_no_grant(self, crew_home):
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs POSIX permission bits and a non-root uid")
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
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
        assert standing_approval.is_declared("auto") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard-link semantics")
    def test_a_grant_with_a_second_hard_link_is_refused(self, crew_home):
        """A second name for this inode is the shape the whole keystone denies.

        The mask covers a path rather than an inode, so a document reachable under
        another name is writable through that name whatever the mask says.
        """
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
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


class TestTheMigrationCommandRuns:
    """The notice's whole value is that the operator can act on it.

    Both renderings are asserted from THIS platform via the ``windows`` parameter,
    because the defect these tests exist for is a string verified only where it was
    written: the POSIX one-liner reached CI and failed on Windows, where ``mkdir -p``
    and ``printf`` are not commands and POSIX quotes are not quoting characters.
    """

    def test_the_command_uses_the_resolved_path_not_an_env_var(self, crew_home):
        for on_windows in (False, True):
            notice = standing_approval.migration_notice("auto", windows=on_windows)
            # $KIROCREW_HOME is UNSET on a default installation, where the data home
            # comes from config_dir() -- an env-var form expands to /standing-approval
            # and fails at the filesystem root.
            assert "$KIROCREW_HOME" not in notice
            assert str(loader.standing_approval_path()) in notice

    def test_the_windows_rendering_uses_no_posix_shell_builtin(self, crew_home):
        """No single spelling writes this JSON in both cmd and PowerShell, so the
        Windows text states the path and the line rather than pretending to run.
        """
        notice = standing_approval.migration_notice("auto", windows=True)
        assert "mkdir -p" not in notice
        assert "printf" not in notice
        assert "'" not in notice.split("containing this one line:")[1]
        assert f'{{"{standing_approval.GRANT_FIELD}": true}}' in notice

    def test_the_posix_rendering_gives_a_runnable_command(self, crew_home):
        notice = standing_approval.migration_notice("auto", windows=False)
        assert "mkdir -p" in notice
        assert "printf" in notice

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell builtins")
    def test_running_the_posix_command_produces_a_document_that_grants(self, crew_home):
        """End to end: extract the command from the notice, run it, read the grant back.

        This is the assertion a path-shape mistake cannot pass -- it fails if the command
        targets the wrong directory, whatever the text looks like.
        """
        notice = standing_approval.migration_notice("auto", windows=False)
        assert standing_approval.is_declared("auto") is False
        start = notice.index("mkdir -p")
        end = notice.index("  (then remove")
        command = notice[start:end]

        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, **UTF8_TEXT)

        assert completed.returncode == 0, completed.stderr
        assert loader.standing_approval_path().is_file()
        assert standing_approval.is_declared("auto") is True

    def test_the_default_rendering_follows_the_host(self, crew_home):
        """Omitting the parameter must not silently pick one platform's text."""
        assert standing_approval.migration_notice("auto") == standing_approval.migration_notice(
            "auto", windows=platform_compat.IS_WINDOWS
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
        server._apply_startup_yolo(object(), self._cfg(sandbox_mode="auto"))
        assert calls == ["granted"]

    def test_a_floor_that_clamps_off_back_up_still_grants(self, startup, crew_home, monkeypatch):
        server, calls = startup
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "cc")
        _write_grant(crew_home, {standing_approval.GRANT_FIELD: True})
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

    def test_a_maskable_host_gets_the_posix_remedy(self, crew_home):
        text = standing_approval.migration_notice("auto", masked=True, windows=False)
        assert "mkdir -p" in text
        assert "UNAVAILABLE" not in text

    def test_a_maskable_host_gets_the_windows_wording(self, crew_home):
        text = standing_approval.migration_notice("auto", masked=True, windows=True)
        assert "create the file" in text
        assert "mkdir -p" not in text

    @pytest.mark.parametrize("windows", [False, True])
    def test_an_unmasked_host_is_told_the_grant_is_unavailable(self, crew_home, windows):
        text = standing_approval.migration_notice("auto", masked=False, windows=windows)
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
        assert "mkdir -p" in standing_approval.migration_notice("auto", windows=False)
        assert "UNAVAILABLE" in standing_approval.migration_notice("off", windows=False)
