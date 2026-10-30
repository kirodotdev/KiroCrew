"""Tests for :mod:`kiro_crew.agent_scratch`.

Everything runs against a monkeypatched data home under ``tmp_path``; the
real ``<data home>/scratch`` is never touched.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew import agent_scratch as sc
from kiro_crew import platform_compat
from kiro_crew.config import paths


class _FakeComponentSecurity:
    """Stand-in for ``windows_acl.ComponentSecurity``.

    Carries every field the shared trust check reads
    (``github_runner.check_provider_path_component_windows``), defaulting to a clean
    local-volume descriptor with no NULL DACL, no foreign writers and no unparsable ACEs --
    so the accept/refuse decision turns only on ``owner_sid`` unless a case overrides a field.
    """

    def __init__(
        self,
        owner_sid: str,
        *,
        owner_name: str = "fake-owner",
        null_dacl: bool = False,
        writers: tuple = (),
        unparsable_ace_types: tuple = (),
        volume_is_local: bool = True,
    ) -> None:
        self.owner_sid = owner_sid
        self.owner_name = owner_name
        self.null_dacl = null_dacl
        self.writers = writers
        self.unparsable_ace_types = unparsable_ace_types
        self.volume_is_local = volume_is_local


@pytest.fixture
def scratch_root(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


class TestAllocate:
    def test_creates_private_dir_under_managed_root(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("chat-31")

        assert path.parent == scratch_root
        assert path.is_dir()
        assert path.name.startswith("chat-31-")
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    def test_label_is_sanitized_and_bounded(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("weird/label with spaces!" + "x" * 100)
        assert "/" not in path.name and " " not in path.name
        assert len(path.name) <= 40 + 1 + 8  # label cap + dash + token

    def test_two_allocations_are_distinct(self, scratch_root: Path) -> None:
        assert sc.allocate_scratch("a") != sc.allocate_scratch("a")


class TestScratchRootOverride:
    """``KIROCREW_SCRATCH_ROOT`` relocates the managed scratch root."""

    @pytest.fixture(autouse=True)
    def _reset_warned(self, _floor_monkeypatch) -> None:
        # The ignore warning fires once per distinct raw value when the override pin is
        # computed; reset the pin so each test starts fresh regardless of order. Uses
        # _floor_monkeypatch (undone independently) rather than the shared monkeypatch,
        # which a test calling monkeypatch.undo() would lift.
        from kiro_crew.config import paths as _paths

        _floor_monkeypatch.setattr(_paths, "_scratch_root_override_pin", _paths._UNSET)

    def test_default_is_data_home_scratch(self, scratch_root: Path, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
        assert sc.scratch_root() == scratch_root

    def test_valid_override_is_used_as_the_root(self, tmp_path: Path, monkeypatch) -> None:
        # A real directory on another "drive" becomes the managed root itself,
        # with no ``scratch`` subcomponent appended -- and allocations land
        # under it, so the override reaches the allocator, not just the getter.
        # HOME is pinned off the real tree: pytest's tmp_path is UNDER the real
        # ``~/.kiro/crew``, and the validator refuses an override inside any crew-home
        # spelling, so without this the "off-drive" root would be refused as in-home.
        # The override applies on Windows only, so pin ``win32``.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        # Patch Path.home() too: on Windows Path.home() ignores HOME, so the default home
        # would otherwise resolve to the real crew home and refuse the override as in-home.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        # The current user owns the root, so the foreign-owner guard passes (the Windows ACL
        # layer is not available on the test runner, so stub the owner read).
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(sc.platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-ME")
        )
        assert sc.scratch_root() == other.resolve()
        allocated = sc.allocate_scratch("chat-7")
        assert allocated.parent == other.resolve()
        assert allocated.is_dir()

    def test_relocated_root_is_locked_to_the_owner(self, tmp_path: Path, monkeypatch) -> None:
        # A relocated override root points at an arbitrary directory whose ACL comes from
        # its parent, and ``mkdir(mode=0o700)`` is ignored on Windows, so the root would
        # keep the drive's default (world-writable) ACL. Allocation must lock the root to
        # the owner, as the default data home is. Assert the owner-only tighten is applied
        # to the relocated root (the default root is already restricted by ensure_data_home,
        # so it is not re-tightened here).
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)  # exists, so the validator's owner check runs on it
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        # The validator reads the root's owner; the current user owns it, so it is accepted.
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-ME")
        )
        restricted: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )
        sc.allocate_scratch("chat-9")
        assert str(other.resolve()) in restricted, "the relocated root was not locked to the owner"

    def test_relocated_root_owned_by_another_user_is_refused_by_the_validator(
        self, tmp_path, monkeypatch
    ) -> None:
        # A relocated root a DIFFERENT local user already created and owns must be refused by
        # the validator, so scratch_root() -- the single decider -- falls back to the default
        # for allocation, the sweep, the log cap and the shared join alike. The owner tighten
        # is never applied to the foreign root.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-OTHER")
        )
        # The validator refuses the foreign-owned root.
        assert paths.valid_scratch_root_override() is None
        restricted: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )
        # scratch_root() is the single decider: it returns the default, so allocation lands
        # under the default and the foreign root is never tightened.
        assert sc.scratch_root() == sc.config_dir() / "scratch"
        allocated = sc.allocate_scratch("chat-11")
        assert allocated.parent == sc.config_dir() / "scratch"
        assert restricted == [], "a foreign-owned root must not be locked (adopted)"

    def test_relocated_root_owned_by_administrators_is_accepted(
        self, tmp_path, monkeypatch
    ) -> None:
        # A directory an ELEVATED session creates on Windows is owned by
        # ``BUILTIN\\Administrators`` (``S-1-5-32-544``), not the user's own SID. A strict
        # owner==current-user compare would refuse it and silently keep scratch on C:. The
        # owner check adopts an Administrators-owned root WHEN the volume is local -- the SAME
        # rule the workflow allocator applies (``workflow_memory._allocator_owner``) -- so the
        # root is adopted, locked to the owner, and allocated under. (``_FakeComponentSecurity``
        # defaults ``volume_is_local=True``.)
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl,
            "describe",
            lambda p: _FakeComponentSecurity(
                owner_sid="S-1-5-32-544", owner_name="BUILTIN\\Administrators"
            ),
        )
        restricted: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )
        # Accepted: scratch_root() returns the override and allocation lands under it.
        assert paths.valid_scratch_root_override() == other.resolve()
        allocated = sc.allocate_scratch("chat-adm")
        assert allocated.parent == other.resolve()
        assert str(other.resolve()) in restricted, "an accepted relocated root must be locked"

    def test_relocated_root_owned_by_administrators_on_a_share_is_refused(
        self, tmp_path, monkeypatch
    ) -> None:
        # An Administrators-owned root on a NON-LOCAL volume (a UNC/mapped share) is the FILE
        # SERVER's Administrators, not this host's -- the identical ``S-1-5-32-544`` SID means a
        # different machine. Adopting it would hand that server control of every session's
        # scratch, including the staged self-update installers the override exists for. The
        # owner check requires ``volume_is_local`` for the Administrators case, the SAME rule the
        # workflow allocator applies (``workflow_memory._allocator_owner``), so a share root
        # owned by Administrators is refused and ``scratch_root()`` falls back to the default.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl,
            "describe",
            lambda p: _FakeComponentSecurity(
                owner_sid="S-1-5-32-544",
                owner_name="BUILTIN\\Administrators",
                volume_is_local=False,
            ),
        )
        monkeypatch.setattr(
            sc.platform_compat,
            "restrict_dir_to_owner",
            lambda p: pytest.fail("a refused share root must never be locked"),
        )
        # Refused by owner: the pre-adopt check names the share, scratch_root() returns the
        # default and allocation lands there, not under the share.
        assert paths.scratch_root_owner_refusal(other.resolve()) == "not owned by current user"
        default_root = sc.config_dir() / "scratch"
        allocated = sc.allocate_scratch("chat-adm-share")
        assert allocated.parent == default_root
        assert allocated.parent != other.resolve()

    def test_inherited_auth_users_root_is_accepted_after_the_lock(
        self, tmp_path, monkeypatch
    ) -> None:
        # The documented D:\kirocrew-scratch case. A fresh directory under a secondary NTFS
        # drive inherits an ``Authenticated Users: Modify`` ACE (a replaceable-writer right).
        # The owner check (owner-only) accepts it, THEN the lock removes that ACE, THEN the
        # post-lock writer check sees a clean descriptor and allocation lands under the root.
        # Running the writer check BEFORE the lock would refuse it and demote to C:.
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")

        locked = {"done": False}
        auth_users = windows_acl.Writer(
            sid="S-1-5-11", name="Authenticated Users", rights=("DELETE",)
        )

        def _describe(_p):
            # Before the lock the inherited Authenticated Users writer is present; after the
            # owner-only lock the descriptor is clean.
            if locked["done"]:
                return _FakeComponentSecurity(owner_sid="S-1-5-21-ME")
            return _FakeComponentSecurity(owner_sid="S-1-5-21-ME", writers=(auth_users,))

        def _lock(_p):
            locked["done"] = True

        monkeypatch.setattr(windows_acl, "describe", _describe)
        monkeypatch.setattr(sc.platform_compat, "restrict_dir_to_owner", _lock)

        allocated = sc.allocate_scratch("chat-dh")
        assert allocated.parent == other.resolve(), "D:-style root must be adopted after lock"
        assert locked["done"], "the root must be locked"

    def test_peer_writable_ancestor_of_a_nested_override_is_refused(
        self, tmp_path, monkeypatch
    ) -> None:
        # Locking the scratch LEAF owner-only protects nothing above it. A nested override
        # like ``D:\shared\scratch`` under an operator-created ``D:\shared`` leaves that
        # PARENT with a replaceable-writer ACE, so another local user who can replace
        # ``D:\shared`` renames it aside after validation and recreates the path, redirecting
        # every later child-scratch read/write into peer-controlled storage. The writer check
        # therefore walks the resolved ancestor chain, not just the leaf: a parent a foreign
        # principal can swap refuses and allocation falls back to the guarded default. (This
        # test fails on the leaf-only predecessor, which validated only the scratch leaf.)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        shared = tmp_path / "data-drive" / "shared"
        other = shared / "scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")

        # ``FILE_DELETE_CHILD`` on the PARENT is a child-swap vector that is NOT dropped on a
        # non-volume-root node, so a foreign principal holding it can replace the override's
        # parent directory.
        peer = windows_acl.Writer(sid="S-1-5-21-PEER", name="Peer", rights=("FILE_DELETE_CHILD",))
        shared_resolved = shared.resolve()

        def _describe(p):
            # Leaf and every ancestor read clean (owner me, no offenders) EXCEPT the override's
            # own parent ``…/data-drive/shared``, which a peer can swap.
            if Path(p) == shared_resolved:
                return _FakeComponentSecurity(owner_sid="S-1-5-21-ME", writers=(peer,))
            return _FakeComponentSecurity(owner_sid="S-1-5-21-ME")

        monkeypatch.setattr(windows_acl, "describe", _describe)
        monkeypatch.setattr(sc.platform_compat, "restrict_dir_to_owner", lambda p: None)

        # Owner check accepts (leaf is owned by me), the lock runs, but the post-lock writer
        # walk refuses the swappable parent, so allocation falls back to the default.
        default_root = sc.config_dir() / "scratch"
        allocated = sc.allocate_scratch("chat-ancestor")
        assert allocated.parent == default_root
        assert allocated.parent != other.resolve()
        # The single-decider property: scratch_root() is demoted to match the fallback.
        assert sc.scratch_root() == default_root

    def test_lock_failure_falls_back_to_the_default_root(self, tmp_path, monkeypatch) -> None:
        # ``restrict_dir_to_owner`` is fail-loud: it raises ``OSError`` when the owner-only
        # ACL cannot be rewritten. A lock we cannot complete leaves the inherited
        # replaceable-writer ACE in place, so it must fall back fail-closed to the guarded
        # default exactly like the owner and writer refusals -- never propagate the error and
        # abort every spawn while the override is set. Assert allocation lands under the
        # default and ``scratch_root()`` (the single decider) is demoted to match.
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-ME")
        )

        def _lock_raises(_p):
            raise OSError("cannot rewrite ACL")

        monkeypatch.setattr(sc.platform_compat, "restrict_dir_to_owner", _lock_raises)

        default_root = sc.config_dir() / "scratch"
        allocated = sc.allocate_scratch("chat-lockfail")
        # Falls back to the guarded default instead of aborting the spawn.
        assert allocated.parent == default_root
        assert allocated.parent != other.resolve()
        # The single-decider property: the sweep / log-cap / shared-join root all read
        # scratch_root(), which must now be the default the allocation used.
        assert sc.scratch_root() == default_root

    def test_concurrent_demote_does_not_skip_adoption_of_the_selected_root(
        self, tmp_path, monkeypatch
    ) -> None:
        # Race: ``allocate_scratch`` selects the override as its root, then a CONCURRENT
        # allocation refuses its own owner re-check and demotes the shared pin. A guard that
        # read the pin a SECOND time (``valid_scratch_root_override()``) after root selection
        # would now see ``None`` and skip the owner/lock/writer checks, allocating under the
        # override with NO adoption. The adoption decision is instead ``root != default``, read
        # from the single ``scratch_root()`` call, so a demote landing BETWEEN the reads cannot
        # make this thread skip adoption. Simulate exactly that: ``scratch_root`` returns the
        # override AND demotes the pin as a side effect, so any later re-read is ``None``. This
        # test fails on a two-read guard and passes on the ``root != default`` one.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-ME")
        )

        resolved_override = other.resolve()

        def _scratch_root_then_demote():
            # Return the override as this allocation's root, then a concurrent allocation's
            # demote lands -- clearing the pin BEFORE any second read in allocate_scratch.
            sc.demote_scratch_root_override("concurrent demote")
            return resolved_override

        monkeypatch.setattr(sc, "scratch_root", _scratch_root_then_demote)

        owner_checked: list[str] = []
        restricted: list[str] = []
        monkeypatch.setattr(
            sc,
            "scratch_root_owner_refusal",
            lambda root: owner_checked.append(str(root)) or None,
        )
        monkeypatch.setattr(sc, "scratch_root_writer_refusal", lambda root: None)
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )

        allocated = sc.allocate_scratch("chat-race")
        # Adoption was NOT skipped despite the demote landing before any re-read: the owner
        # check and the lock both ran on the override root, and allocation landed under it.
        assert owner_checked == [str(resolved_override)]
        assert str(resolved_override) in restricted
        assert allocated.parent == resolved_override

    def test_system_owned_root_is_refused(self, tmp_path, monkeypatch) -> None:
        # A directory owned by NT AUTHORITY\\SYSTEM must be refused at the owner step, never
        # adopted or locked (locking would rewrite a system ACL under an elevated gateway).
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        # A SYSTEM owner is not the current user and not Administrators, so it is refused.
        monkeypatch.setattr(
            windows_acl,
            "describe",
            lambda p: _FakeComponentSecurity(owner_sid="S-1-5-18", owner_name="SYSTEM"),
        )
        restricted: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )
        assert paths.valid_scratch_root_override() is None
        allocated = sc.allocate_scratch("chat-sys")
        assert allocated.parent == sc.config_dir() / "scratch"
        assert restricted == [], "a SYSTEM-owned root must never be locked"

    def test_programdata_override_is_refused_as_a_system_directory(self, monkeypatch) -> None:
        # The Windows system directories (%SystemRoot%/%WINDIR%, %ProgramData%,
        # %ProgramFiles%, %ProgramFiles(x86)%) are owned by SYSTEM/Administrators/
        # TrustedInstaller; the validator must refuse each one (and anything under it) BEFORE
        # any adopt or lock, so an elevated gateway never rewrites its ACL. _is_unsafe_home is
        # POSIX-shaped and misses them.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("SystemRoot", "C:\\Windows")
        monkeypatch.setenv("ProgramData", "C:\\ProgramData")
        monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
        monkeypatch.setenv("ProgramFiles(x86)", "C:\\Program Files (x86)")
        cases = [
            "C:\\Windows",
            "C:\\Windows\\Temp\\kirocrew-scratch",
            "C:\\ProgramData",
            "c:\\programdata\\kirocrew-scratch",  # case-insensitive
            "C:\\Program Files\\kirocrew-scratch",
            "C:\\Program Files (x86)\\kirocrew-scratch",
        ]
        for value in cases:
            monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", value)
            monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
            assert paths.valid_scratch_root_override() is None, value
            assert sc.scratch_root() == sc.config_dir() / "scratch", value

    def test_relocated_root_with_unreadable_owner_is_refused_fail_closed(
        self, tmp_path, monkeypatch
    ) -> None:
        # A volume whose security descriptor cannot be read (a FAT/exFAT data drive) is
        # refused fail-closed: it has no access control to verify the owner against and
        # cannot be owner-locked either, so adopting it would leave scratch world-writable.
        # scratch_root() returns the default.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        def _boom(_p):
            raise windows_acl.AclUnavailable("cannot read the security descriptor")

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(windows_acl, "describe", _boom)
        assert paths.valid_scratch_root_override() is None
        assert sc.scratch_root() == sc.config_dir() / "scratch"

    def test_scratch_root_returns_default_when_owner_check_fails(
        self, tmp_path, monkeypatch
    ) -> None:
        # The single-decider property: when the owner check refuses the override,
        # scratch_root() (which the sweep, log cap and shared join all read) returns the
        # guarded default, not the override.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        other.mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-OTHER")
        )
        assert sc.scratch_root() == sc.config_dir() / "scratch"
        assert sc.scratch_root() != other.resolve()

    def test_root_absent_at_validation_then_foreign_owned_is_refused_at_allocation(
        self, tmp_path, monkeypatch
    ) -> None:
        # TOCTOU: the validator runs (and CACHES its accept) while the override root does
        # NOT yet exist -- the boot sweep calls scratch_root() before any mkdir -- so there
        # is no owner to check and the cached accept pins. Before allocation, another local
        # user creates the directory and owns it. allocate_scratch must re-check the owner
        # AFTER its own mkdir and, finding it foreign, fall back to the guarded default
        # instead of tightening (and thereby cementing) the foreign owner's control.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        # NOTE: `other` is deliberately NOT created here -- it is absent when the validator
        # resolves and caches the override.
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        # Validation while absent: p.exists() is False, so no owner check runs and the
        # override is accepted (and cached).
        assert paths.valid_scratch_root_override() == other.resolve()
        # Now the root "appears" owned by another user before allocation.
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-OTHER")
        )
        restricted: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: restricted.append(str(p))
        )
        allocated = sc.allocate_scratch("chat-12")
        # The allocation-time owner re-check refused the foreign root and fell back to the
        # guarded default; the foreign root was never tightened (adopted).
        assert allocated.parent == sc.config_dir() / "scratch"
        assert restricted == [], "a foreign-owned root created after validation must not be locked"

    def test_sweep_root_follows_the_allocation_fallback_when_owner_recheck_refuses(
        self, tmp_path, monkeypatch
    ) -> None:
        # The allocation-time owner re-check (foreign root created after a cached accept)
        # falls allocation back to config_dir()/scratch. But the override pin, computed
        # while the root was absent, still accepts -- so without a demotion scratch_root()
        # keeps returning the override, and the sweep, the log cap and the shared-window
        # join all operate on the refused directory while allocations land in the default.
        # The fallback must demote the pin so scratch_root() -- the single decider every
        # one of those reads -- returns the SAME default allocation uses. Assert the sweep
        # root equals the allocation root after the fallback.
        monkeypatch.setattr(sys, "platform", "win32")
        realhome = tmp_path / "realhome"
        monkeypatch.setenv("HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: realhome))
        other = tmp_path / "data-drive" / "kirocrew-scratch"
        # Absent at validation: the validator caches an accept with no owner to check.
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)
        import kiro_crew.windows_acl as windows_acl

        monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-ME")
        assert paths.valid_scratch_root_override() == other.resolve()
        # The root now reads as foreign-owned, so the allocation-time re-check refuses it.
        monkeypatch.setattr(
            windows_acl, "describe", lambda p: _FakeComponentSecurity(owner_sid="S-1-5-21-OTHER")
        )
        monkeypatch.setattr(sc.platform_compat, "restrict_dir_to_owner", lambda p: None)

        allocated = sc.allocate_scratch("chat-13")

        default_root = sc.config_dir() / "scratch"
        assert allocated.parent == default_root
        # The single-decider property after the fallback: the sweep root (and the log cap
        # root, and the shared-window join root) all read scratch_root(), which must now be
        # the SAME default the allocation used, not the refused override.
        assert sc.scratch_root() == default_root
        assert sc.scratch_root() == allocated.parent

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="exercises the real Windows ACL owner read and owner-only lock; "
        "no stub, so it is meaningful only on a real Windows host",
    )
    def test_a_real_windows_root_is_accepted_and_locked_unstubbed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The one path CI's stubbed tests cannot cover: on a real Windows runner, a directory
        # the test user creates is read by the REAL ``windows_acl.describe`` /
        # ``current_user_sid`` (no stub, no elevation) and must be accepted by the shared
        # trust check and then locked owner-only. This runs on the Backend Tests (Windows)
        # shards. Cross-volume ``$TMPDIR`` writes inside kiro-cli's delegated Windows sandbox
        # cannot be driven from a unit test here and are not asserted.
        import kiro_crew.agent_scratch as agent_scratch

        realhome = tmp_path / "realhome" / ".kiro" / "crew"
        monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
        monkeypatch.setenv("KIROCREW_HOME", str(realhome))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "realhome"))
        other = tmp_path / "data-root" / "kirocrew-scratch"
        other.mkdir(parents=True)  # owned by the test user on the runner
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", str(other))
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET)

        # Accepted by the real owner read, allocated under, and the root is locked owner-only.
        assert paths.valid_scratch_root_override() == other.resolve()
        allocated = agent_scratch.allocate_scratch("chat-win")
        assert allocated.parent == other.resolve()
        assert allocated.is_dir()
        assert (allocated / sc.OWNER_FILENAME).is_file()

    def test_default_root_is_not_re_restricted(self, scratch_root: Path, monkeypatch) -> None:
        # With no override, the root is the default data-home scratch, already owner-only
        # via ensure_data_home; allocation must not call the owner tighten again.
        monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
        called: list[str] = []
        monkeypatch.setattr(
            sc.platform_compat, "restrict_dir_to_owner", lambda p: called.append(str(p))
        )
        sc.allocate_scratch("chat-10")
        assert called == [], "the default root should not be re-restricted on each allocation"

    def test_unsafe_override_is_ignored_with_one_warning(
        self, scratch_root: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A drive/filesystem root is refused by the shared ``_is_unsafe_home``
        # predicate and falls back to the default, logging exactly as an unsafe
        # ``KIROCREW_HOME`` does. ``Path.cwd().anchor`` is the drive root on
        # Windows (``C:\``) and ``/`` on POSIX -- the ``p == p.parent`` case the
        # predicate refuses on every OS, so the file is never created.
        # Pin ``win32``: the override applies on Windows only, so a non-Windows
        # platform refuses it earlier with the "unsupported platform" reason, which this
        # system-dir wording assertion must not see.
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", Path.cwd().anchor)
        with caplog.at_level("WARNING"):
            assert sc.scratch_root() == scratch_root
        warnings = [r for r in caplog.records if "KIROCREW_SCRATCH_ROOT" in r.getMessage()]
        assert len(warnings) == 1
        assert "is a system directory, ignoring" in warnings[0].getMessage()

    def test_unsafe_override_warns_only_once_across_calls(
        self, scratch_root: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # scratch_root() runs on every allocation/sweep/log-cap pass; a steadily
        # misconfigured value must not re-warn each time (the pin computes once per value).
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("KIROCREW_SCRATCH_ROOT", Path.cwd().anchor)
        with caplog.at_level("WARNING"):
            for _ in range(5):
                assert sc.scratch_root() == scratch_root
        warnings = [r for r in caplog.records if "KIROCREW_SCRATCH_ROOT" in r.getMessage()]
        assert len(warnings) == 1

    def test_unset_override_does_not_warn(
        self, scratch_root: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("KIROCREW_SCRATCH_ROOT", raising=False)
        with caplog.at_level("WARNING"):
            sc.scratch_root()
        assert not [r for r in caplog.records if "KIROCREW_SCRATCH_ROOT" in r.getMessage()]


class TestEnv:
    def test_exports_temp_triple_and_scratch_alias(self, tmp_path: Path, monkeypatch) -> None:
        # Where the cap can bound the log, the pin rides along with the triple.
        monkeypatch.setattr(sc, "_CAN_CAP_LOGS", True)
        env = sc.scratch_env(tmp_path)
        value = str(tmp_path)
        assert env == {
            "TMPDIR": value,
            "TMP": value,
            "TEMP": value,
            "KIROCREW_SCRATCH": value,
            "KIRO_CHAT_LOG_FILE": str(tmp_path / "kiro-log" / "kiro-chat.log"),
        }
        # The default NAME is load-bearing: kiro-cli writes mcp.log/lsp.log
        # beside the chat log only when it is called kiro-chat.log.
        assert Path(env["KIRO_CHAT_LOG_FILE"]).name == "kiro-chat.log"

    def test_log_pin_is_omitted_where_the_cap_cannot_run(self, tmp_path: Path, monkeypatch) -> None:
        # A pinned log nothing rotates is unbounded, so a platform without the
        # cap (Windows) leaves kiro-cli at its own default location.
        monkeypatch.setattr(sc, "_CAN_CAP_LOGS", False)
        env = sc.scratch_env(tmp_path)
        value = str(tmp_path)
        assert env == {
            "TMPDIR": value,
            "TMP": value,
            "TEMP": value,
            "KIROCREW_SCRATCH": value,
        }
        assert "KIRO_CHAT_LOG_FILE" not in env


class TestLivenessSweep:
    @staticmethod
    def _age(path: Path, seconds: float) -> None:
        past = time.time() - seconds
        os.utime(path, (past, past))

    def test_dead_and_idle_reclaimed_live_or_active_kept(self, scratch_root: Path) -> None:
        dead_idle = sc.allocate_scratch("dead")
        sc.record_owner(dead_idle, 2**22 - 1)  # almost surely dead
        self._age(dead_idle, 2 * sc._UNOWNED_GRACE_SECONDS)
        self._age(dead_idle / sc.OWNER_FILENAME, 2 * sc._UNOWNED_GRACE_SECONDS)
        dead_active = sc.allocate_scratch("active")
        sc.record_owner(dead_active, 2**22 - 1)  # dead owner, fresh mtime
        live = sc.allocate_scratch("live")
        # POSIX: our own process GROUP id -- the probe is group-scoped (the
        # launcher pid doubles as the pgid under start_new_session), and
        # under pytest-xdist os.getpid() is not a group leader. Windows: the
        # probe routes through pid_exists, so a live PID is the right ref.
        live_ref = os.getpid() if sys.platform == "win32" else os.getpgrp()
        sc.record_owner(live, live_ref)
        self._age(live, 2 * sc._UNOWNED_GRACE_SECONDS)

        removed = sc.sweep_dead_scratch()

        assert not dead_idle.exists(), "dead owner + idle content is reclaimed"
        assert dead_active.exists(), "fresh mtime reads as in-use: kept"
        assert live.exists(), "a live owner is never touched, however idle"
        assert removed == 1

    def test_deep_fresh_write_keeps_the_dir(self, scratch_root: Path) -> None:
        # A live process writing through an already-open fd never touches the
        # top DIRECTORY's mtime -- the idle signal must be tree-newest, so a
        # fresh file deep inside keeps the dir even when the top is aged and
        # the recorded owner is dead (the Windows-reachable wrapper case,
        # where no group probe exists).
        path = sc.allocate_scratch("deep")
        sc.record_owner(path, 2**22 - 1)  # dead owner
        nested = path / "clone" / "src"
        nested.mkdir(parents=True)
        (nested / "live-output.log").write_text("still writing")
        # Age every PARENT dir (top + intermediate); the deep file stays fresh.
        for p in (path, path / "clone", nested, path / sc.OWNER_FILENAME):
            self._age(p, 2 * sc._UNOWNED_GRACE_SECONDS)
        os.utime(nested / "live-output.log")  # the one fresh entry

        assert sc.sweep_dead_scratch() == 0
        assert path.exists()

    def test_allocation_records_a_provisional_owner(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("prov")
        assert (path / sc.OWNER_FILENAME).read_text() == str(os.getpid())

    def test_unowned_dir_is_never_deleted(self, scratch_root: Path) -> None:
        # Allocation writes a provisional owner atomically-with-creation, so
        # an ownerless dir indicates a state this code did not produce --
        # deleting on absence of evidence is how live work gets lost.
        path = sc.allocate_scratch("unowned")
        (path / sc.OWNER_FILENAME).unlink()
        self._age(path, 10 * sc._UNOWNED_GRACE_SECONDS)

        assert sc.sweep_dead_scratch() == 0
        assert path.exists()

    def test_garbled_owner_file_is_left_for_a_human(self, scratch_root: Path) -> None:
        path = sc.allocate_scratch("garbled")
        (path / sc.OWNER_FILENAME).write_text("not-a-pid")
        self._age(path, 2 * sc._UNOWNED_GRACE_SECONDS)

        removed = sc.sweep_dead_scratch()

        assert path.exists() and removed == 0

    def test_out_of_range_owner_is_kept_without_blocking_later_cleanup(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        real_scandir = os.scandir
        reaped: list[Path] = []
        corrupt = sc.allocate_scratch("a-out-of-range")
        sc.record_owner(corrupt, 10**100)
        self._age(corrupt, 2 * sc._UNOWNED_GRACE_SECONDS)
        self._age(corrupt / sc.OWNER_FILENAME, 2 * sc._UNOWNED_GRACE_SECONDS)
        dead = sc.allocate_scratch("z-dead-after-corrupt")
        sc.record_owner(dead, 2**22 - 1)  # almost surely dead
        self._age(dead, 2 * sc._UNOWNED_GRACE_SECONDS)
        self._age(dead / sc.OWNER_FILENAME, 2 * sc._UNOWNED_GRACE_SECONDS)

        with monkeypatch.context() as ordered:
            ordered.setattr(
                os,
                "scandir",
                lambda path: sorted(real_scandir(path), key=lambda entry: entry.name),
            )

            # The sweep reaps through ``platform_compat.rmtree_force`` and only
            # counts a directory it confirms is GONE afterwards, so a stub that
            # merely records the path would read back as a failed delete and
            # never be counted. Record what was reaped and perform the real
            # removal (with the real ``os.scandir`` restored, since
            # ``shutil.rmtree`` enters it as a context manager) so the
            # production success contract is exercised.
            def _record_and_remove(path, **_kwargs):
                reaped.append(Path(path))
                with monkeypatch.context() as unordered:
                    unordered.setattr(os, "scandir", real_scandir)
                    return real_rmtree_force(path)

            real_rmtree_force = sc.platform_compat.rmtree_force
            ordered.setattr(sc.platform_compat, "rmtree_force", _record_and_remove)
            removed = sc.sweep_dead_scratch()

        assert corrupt.exists(), "an invalid identity is not evidence that its owner is dead"
        assert dead in reaped, "one corrupt marker must not abort cleanup of later entries"
        assert removed == 1

    def test_missing_root_returns_zero(self, scratch_root: Path) -> None:
        assert sc.sweep_dead_scratch() == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
    def test_symlink_child_is_never_followed(self, scratch_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep").write_text("keep")
        scratch_root.mkdir(parents=True, exist_ok=True)
        (scratch_root / "evil").symlink_to(victim)

        removed = sc.sweep_dead_scratch()

        assert removed == 0
        assert victim.exists() and (victim / "keep").exists()

    #: Large enough that a capped walk would misread the tree as active;
    #: a local constant so the test stands on its own.
    LARGE_TREE_ENTRIES = 10_001

    @classmethod
    def _build_large_tree(cls, path: Path) -> Path:
        """Populate *path* with an over-cap ``target/`` tree; return one deep file."""
        target = path / "target"
        deep = target / "debug" / "deps"
        deep.mkdir(parents=True)
        for i in range(cls.LARGE_TREE_ENTRIES):
            (target / f"unit-{i}.o").touch()
        deep_file = deep / "artifact.rlib"
        deep_file.touch()
        return deep_file

    def test_dead_idle_tree_larger_than_former_scan_cap_is_reclaimed(
        self, scratch_root: Path
    ) -> None:
        # A capped walk bails to the sweep-timestamp fallback on a tree this
        # large and misreads it as permanently active; the uncapped walk must
        # reclaim a dead owner's cargo-target-sized residue.
        path = sc.allocate_scratch("bigdead")
        sc.record_owner(path, 2**22 - 1)  # almost surely dead
        self._build_large_tree(path)
        now = time.time() + 2 * sc._UNOWNED_GRACE_SECONDS

        assert sc.sweep_dead_scratch(now=now) == 1
        assert not path.exists()

    def test_live_owner_large_tree_is_kept(self, scratch_root: Path) -> None:
        # Size must not cause deletion: a live owner keeps its tree however
        # large and however idle.
        path = sc.allocate_scratch("biglive")
        live_ref = os.getpid() if sys.platform == "win32" else os.getpgrp()
        sc.record_owner(path, live_ref)
        self._build_large_tree(path)
        now = time.time() + 2 * sc._UNOWNED_GRACE_SECONDS

        assert sc.sweep_dead_scratch(now=now) == 0
        assert path.exists()

    def test_fresh_deep_write_in_large_dead_tree_keeps_it(self, scratch_root: Path) -> None:
        # The uncapped walk must still see a fresh write deep inside a large
        # dead-owner tree: one recent file anywhere reads as in-use.
        path = sc.allocate_scratch("bigfresh")
        sc.record_owner(path, 2**22 - 1)  # dead owner
        deep_file = self._build_large_tree(path)
        now = time.time() + 2 * sc._UNOWNED_GRACE_SECONDS
        os.utime(deep_file, (now, now))  # the one fresh entry

        assert sc.sweep_dead_scratch(now=now) == 0
        assert path.exists()


_GIT = platform_compat.trusted_git_bin()


def _git(
    cwd: Path, *args: str, trace_to: Path | None = None, auto_maintenance: bool = False
) -> str:
    """Run git for a fixture, with no host config and a fixed identity.

    Auto-maintenance and auto-gc are off: either can start a background git that
    creates and removes ``.git/objects/maintenance.lock`` while a test walks the
    tree, so a snapshot could list the file and then fail to read it. ``-c`` is
    carried to every git the command starts, the detached one included.

    *trace_to* names a file that receives git's trace2 event stream, so a test
    can see which child processes a fixture command started. *auto_maintenance*
    turns git's auto maintenance back on; only the regression test's control
    command sets it.
    """
    assert _GIT is not None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Scratch Test",
        GIT_AUTHOR_EMAIL="scratch-test@example.invalid",
        GIT_COMMITTER_NAME="Scratch Test",
        GIT_COMMITTER_EMAIL="scratch-test@example.invalid",
    )
    if trace_to is not None:
        env["GIT_TRACE2_EVENT"] = str(trace_to)
    # A commit otherwise starts a detached ``git maintenance run --auto`` that
    # outlives this call and creates and removes lock files under .git, so a
    # walk of the tree right after can list a file that is gone when it is
    # touched. Stripping GIT_* dropped the suite's maintenance entries, so they
    # go back on for every fixture command.
    env.update(git_maintenance_off_entries(env))
    flags = [
        "-c",
        "init.defaultBranch=main",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
    ]
    if auto_maintenance:
        # Explicit, so the control starts maintenance over the entries and the
        # flag above: the later -c wins.
        flags += ["-c", "maintenance.auto=true"]
    result = subprocess.run(
        [_GIT, *flags, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=True,
        encoding="utf-8",
    )
    return result.stdout


def _snapshot(tree: Path) -> dict[str, tuple[str, object]]:
    """Every entry under *tree* by relative path: its kind, and its bytes and mode or target."""
    seen: dict[str, tuple[str, object]] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(path)
            relative = path.relative_to(tree).as_posix()
            if stat.S_ISLNK(info.st_mode):
                seen[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                seen[relative] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                seen[relative] = ("file", (path.read_bytes(), stat.S_IMODE(info.st_mode)))
    return seen


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


@pytest.mark.skipif(_GIT is None, reason="needs a git binary")
class TestGitTreesAreQuarantined:
    """A dead, idle tree holding a git repository is moved whole, never deleted."""

    LATER = 2 * sc._UNOWNED_GRACE_SECONDS

    @pytest.fixture
    def upstream(self, tmp_path: Path) -> Path:
        repo = tmp_path / "upstream"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("one\n")
        (repo / ".gitignore").write_text("build/\n")
        _git(repo, "add", "a.txt", ".gitignore")
        _git(repo, "commit", "-q", "-m", "first")
        return repo

    @staticmethod
    def _dead_scratch() -> Path:
        path = sc.allocate_scratch("gitwork")
        sc.record_owner(path, 2**22 - 1)  # almost surely dead
        return path

    def _clone_into(self, upstream: Path, inside: str = "clone") -> tuple[Path, Path]:
        path = self._dead_scratch()
        clone = path / inside
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(clone.parent, "clone", "-q", str(upstream), clone.name)
        return path, clone

    def _sweep_later(self, extra: float = 0.0) -> int:
        return sc.sweep_dead_scratch(now=time.time() + self.LATER + extra)

    @staticmethod
    def _moved(path: Path) -> Path:
        """Where the sweep moved *path* to; asserts it left the managed root."""
        assert not os.path.lexists(path)
        moved = [p for p in sc.quarantine_dir().iterdir() if p.name.endswith(f"-{path.name}")]
        assert len(moved) == 1, moved
        assert sc._quarantined_at(moved[0].name) is not None
        return moved[0]

    def _swept(self, path: Path) -> Path:
        """Sweep the dead, idle *path*, assert it was moved whole, and return where to."""
        before = _snapshot(path)
        assert self._sweep_later() == 0
        moved = self._moved(path)
        assert _snapshot(moved) == before
        return moved

    def _local_feature(self, upstream: Path) -> tuple[Path, Path]:
        """A dead tree holding a clone with one unpushed commit on ``feature``."""
        path, clone = self._clone_into(upstream)
        _git(clone, "checkout", "-q", "-b", "feature")
        (clone / "feature.py").write_text("committed, never pushed\n")
        _git(clone, "add", "feature.py")
        _git(clone, "commit", "-q", "-m", "feature")
        return path, clone

    # -- the fixtures ------------------------------------------------------

    @staticmethod
    def _background_children(events: Path) -> tuple[list[list[str]], list[list[str]]]:
        """Every child a traced git command started, and the auto-maintenance ones."""
        started = [
            event["argv"]
            for event in map(json.loads, events.read_text(encoding="utf-8").splitlines())
            if event.get("event") == "child_start"
        ]
        return started, [argv for argv in started if argv[1:2] in (["maintenance"], ["gc"])]

    def test_fixture_git_leaves_no_process_writing_into_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tests below walk, snapshot and move a tree right after building
        # it, so no git command may leave a detached child (auto maintenance or
        # auto gc) still adding and removing lock files in it. The suite's own
        # maintenance.auto=false entry is dropped here, so this checks the
        # helper's flags and not whatever the environment happens to carry.
        monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
        control_events = tmp_path / "control.json"
        control = tmp_path / "control"
        control.mkdir()
        _git(control, "init", "-q")
        _git(
            control,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
            trace_to=control_events,
            auto_maintenance=True,
        )
        control_started, control_background = self._background_children(control_events)
        # Searched in every argv word, independent of the filter below, so a
        # filter that stops recognising the child fails here instead of passing.
        if not any({"maintenance", "gc"} & set(argv) for argv in control_started):
            pytest.fail(
                "prerequisite missing: the control git started no auto maintenance, "
                f"so this test cannot show the helper's flags suppress it: {control_started}"
            )
        assert control_background, f"the filter missed the control's maintenance: {control_started}"

        events = tmp_path / "trace2.json"
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", trace_to=events)
        (repo / "a.txt").write_text("one\n")
        _git(repo, "add", "a.txt", trace_to=events)
        _git(repo, "commit", "-q", "-m", "first", trace_to=events)
        _git(tmp_path, "clone", "-q", str(repo), "clone", trace_to=events)
        clone = tmp_path / "clone"
        _git(clone, "checkout", "-q", "-b", "feature", trace_to=events)
        (clone / "b.txt").write_text("two\n")
        _git(clone, "add", "b.txt", trace_to=events)
        _git(clone, "commit", "-q", "-m", "feature", trace_to=events)
        (clone / "a.txt").write_text("stashed\n")
        _git(clone, "stash", "-q", trace_to=events)
        _git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "wt"), trace_to=events)

        started, background = self._background_children(events)
        assert started, "the trace recorded no child processes at all"
        assert background == []

    # -- what is moved -----------------------------------------------------

    def test_unsaved_work_comes_through_the_move_whole(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        # An unpushed commit, a stash and an uncommitted file: the moved tree
        # holds each of them and still works as a repository.
        path, clone = self._local_feature(upstream)
        (clone / "a.txt").write_text("stashed\n")
        _git(clone, "stash", "-q")
        (clone / "draft.txt").write_text("uncommitted\n")

        moved = self._swept(path) / "clone"
        assert _git(moved, "log", "-1", "--format=%s") == "feature\n"
        assert _git(moved, "stash", "list") != ""
        assert "draft.txt" in _git(moved, "status", "--porcelain")

    def test_clean_clone_is_moved_too(self, scratch_root: Path, upstream: Path) -> None:
        # Ignored build output is not work, and the clone matches its remote,
        # but nothing outside the agent can prove that, so it is moved as well.
        path, clone = self._clone_into(upstream)
        (clone / "build").mkdir()
        (clone / "build" / "out.o").write_text("object")

        self._swept(path)

    def test_repository_nested_in_a_workspace_is_moved(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        path, _clone = self._clone_into(upstream, inside="workspace/src/Package")

        self._swept(path)

    def test_linked_worktree_is_moved(self, scratch_root: Path, upstream: Path) -> None:
        # Its ``.git`` is a file naming a repository outside the tree.
        path = self._dead_scratch()
        _git(upstream, "worktree", "add", "-q", "-b", "side", str(path / "wt"))

        self._swept(path)

    @pytest.mark.parametrize("where", ["backup.git", "."])
    def test_a_bare_repository_is_moved(self, scratch_root: Path, where: str) -> None:
        # A bare repository has no work tree, but it can hold the only copy of
        # commits pushed into it.
        path = self._dead_scratch()
        _git(path, "init", "-q", "--bare", where)

        assert sc.holds_git_repository(path)
        self._swept(path)

    @pytest.mark.parametrize("linked", ["objects", "refs"])
    def test_a_bare_repository_with_linked_storage_is_moved(
        self, scratch_root: Path, linked: str
    ) -> None:
        # The link points at a directory in the same tree, so deleting the
        # tree would delete what it stores.
        path = self._dead_scratch()
        _git(path, "init", "-q", "--bare", "backup.git")
        store = path / f"{linked}-store"
        os.rename(path / "backup.git" / linked, store)
        _symlink_or_skip(path / "backup.git" / linked, Path("..") / store.name)

        assert sc.holds_git_repository(path)
        self._swept(path)

    def test_a_git_dir_whose_head_is_gone_is_moved(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        # Git does not open it without a HEAD, but its refs and objects still
        # hold the commits.
        path, clone = self._local_feature(upstream)
        (clone / ".git" / "HEAD").unlink()

        assert sc.holds_git_repository(path)
        self._swept(path)

    def test_a_git_file_counts_whatever_it_names(self, scratch_root: Path) -> None:
        path = self._dead_scratch()
        (path / "wt").mkdir()
        (path / "wt" / ".git").write_text("gitdir: a\0b\n")

        assert sc.holds_git_repository(path)
        self._swept(path)

    def test_a_linked_git_entry_counts_as_a_repository(self, scratch_root: Path) -> None:
        path = self._dead_scratch()
        (path / "work").mkdir()
        _symlink_or_skip(path / "work" / ".git", path / "elsewhere")

        assert sc.holds_git_repository(path)

    def test_an_unlistable_tree_counts_as_holding_a_repository(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        path = self._dead_scratch()

        def _refuse(_path):
            raise PermissionError("unreadable")

        monkeypatch.setattr(sc.os, "scandir", _refuse)
        assert sc.holds_git_repository(path)

    # -- what is deleted at once -------------------------------------------

    def test_tree_without_a_repository_is_deleted(self, scratch_root: Path) -> None:
        path = self._dead_scratch()
        (path / "notes.txt").write_text("residue")
        (path / "HEAD").write_text("a file named like a git one, on its own\n")

        assert not sc.holds_git_repository(path)
        assert self._sweep_later() == 1
        assert not path.exists()
        assert not sc.quarantine_dir().exists()

    def test_a_git_dir_holding_only_objects_is_residue(self, scratch_root: Path) -> None:
        # Object files with nothing else beside them are what a partial delete
        # strands on Windows; git does not open such a directory.
        path = self._dead_scratch()
        (path / ".git" / "objects" / "ab").mkdir(parents=True)
        (path / ".git" / "objects" / "ab" / "cdef").write_bytes(b"blob")

        assert not sc.holds_git_repository(path)
        assert self._sweep_later() == 1
        assert not path.exists()

    def test_repositories_only_in_rebuildable_dirs_are_not_work(self, scratch_root: Path) -> None:
        # Test suites ``git init`` throwaway repositories in pytest's basetemp,
        # which lands at the top of the scratch tree; package installs can
        # carry a ``.git`` too.
        path = self._dead_scratch()
        basetemp = path / "pytest-of-agent" / "pytest-3" / "test_commit0"
        basetemp.mkdir(parents=True)
        _git(basetemp, "init", "-q", "repo")
        (path / "work" / "node_modules" / "dep").mkdir(parents=True)
        _git(path / "work" / "node_modules" / "dep", "init", "-q")

        assert not sc.holds_git_repository(path)
        assert self._sweep_later() == 1
        assert not path.exists()
        assert not sc.quarantine_dir().exists()

    def test_a_basetemp_name_below_the_top_is_searched(self, scratch_root: Path) -> None:
        # Only pytest's own spot, the top of the tree, is a basetemp.
        path = self._dead_scratch()
        (path / "work").mkdir()
        _git(path / "work", "init", "-q", "pytest-of-agent")

        assert sc.holds_git_repository(path)
        self._swept(path)

    # -- what the quarantine keeps -----------------------------------------

    @staticmethod
    def _rebuildable(tree: Path) -> None:
        """Fill *tree* with what tools rebuild: installs, a venv, caches, a basetemp."""
        for relative in (
            "clone/node_modules/dep/index.js",
            "clone/__pycache__/mod.cpython-312.pyc",
            "clone/.pytest_cache/v/cache/lastfailed",
            "clone/.mypy_cache/3.12/mod.json",
            "clone/.ruff_cache/0.1/cache",
            "clone/.tox/py312/log",
            "clone/.nox/tests/log",
            "clone/.venv/pyvenv.cfg",
            "clone/.venv/lib/site.py",
            "clone/env312/pyvenv.cfg",
            "pytest-of-agent/pytest-0/test_x0/repo/.git/HEAD",
        ):
            (tree / relative).parent.mkdir(parents=True, exist_ok=True)
            (tree / relative).write_text("rebuilt\n")

    def test_the_quarantine_keeps_the_work_and_drops_what_tools_rebuild(
        self, scratch_root: Path, upstream: Path, caplog
    ) -> None:
        caplog.set_level("INFO", logger=sc.__name__)
        path, clone = self._clone_into(upstream)
        (clone / "draft.txt").write_text("uncommitted\n")
        # Named like a venv, but no pyvenv.cfg: somebody's files.
        (clone / "work" / ".venv").mkdir(parents=True)
        (clone / "work" / ".venv" / "notes.txt").write_text("not a venv\n")
        # A link named pyvenv.cfg does not make a venv either.
        _symlink_or_skip(clone / "work" / "pyvenv.cfg", Path(".venv"))
        # A repository is work whatever its directory is called.
        _git(path, "clone", "-q", str(upstream), "node_modules")
        kept = _snapshot(path)
        self._rebuildable(path)

        assert self._sweep_later() == 0
        moved = self._moved(path)
        assert _snapshot(moved) == kept
        assert "?? draft.txt" in _git(moved / "clone", "status", "--porcelain")
        assert f"removed 10 rebuildable dir(s) from {moved}" in caplog.text

    def test_a_venv_holding_a_repository_is_work(self, scratch_root: Path, upstream: Path) -> None:
        # A venv used as a project folder, or one ``pip install -e git+...``
        # cloned into, holds work below its pyvenv.cfg.
        path = self._dead_scratch()
        (path / "work").mkdir()
        (path / "work" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (path / "work" / "src").mkdir()
        _git(path / "work" / "src", "clone", "-q", str(upstream), "pkg")

        assert sc.holds_git_repository(path)
        self._swept(path)

    def test_a_basetemp_name_inside_a_venv_is_work(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        path, _clone = self._clone_into(upstream, inside="env/pytest-of-agent/repo")
        (path / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")

        self._swept(path)

    def test_a_quarantined_venv_keeps_a_repository_below_a_basetemp_name(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        path, _clone = self._clone_into(upstream, inside="env/pytest-of-agent/repo")
        moved = self._swept(path)
        (moved / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        kept = _snapshot(moved)

        assert self._sweep_later(extra=3600) == 0
        assert (moved / "env").is_dir()
        assert _snapshot(moved) == kept

    def test_nested_venvs_are_searched_once(
        self, scratch_root: Path, upstream: Path, monkeypatch
    ) -> None:
        # Each venv is judged by searching below it; searching it again as the
        # walk descends made the work double with every level of nesting.
        path = self._dead_scratch()
        venv = path
        for level in range(8):
            venv = venv / f"e{level}"
            venv.mkdir()
            (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        # A ``.git`` file, not a clone: a clone's object paths this deep pass
        # Windows' 260-character path limit.
        (venv / "pkg").mkdir()
        (venv / "pkg" / ".git").write_text("gitdir: elsewhere\n")
        dirs = 1 + sum(len(names) for _root, names, _files in os.walk(path))

        listed: list[str] = []
        real_scandir = os.scandir

        def _counting(target):
            listed.append(os.fspath(target))
            return real_scandir(target)

        with monkeypatch.context() as patch:
            patch.setattr(sc.os, "scandir", _counting)
            assert sc.holds_git_repository(path)
        assert len(listed) <= dirs, (len(listed), dirs)
        assert len(listed) == len(set(listed))
        self._swept(path)

    def test_nothing_inside_a_git_directory_is_stripped(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        path, clone = self._clone_into(upstream)
        _git(path, "init", "-q", "--bare", "backup.git")
        for relative in (
            "clone/.git/node_modules/dep/index.js",
            "clone/.git/hooks/__pycache__/hook.pyc",
            "clone/.git/env/pyvenv.cfg",
            "backup.git/node_modules/dep/index.js",
            "backup.git/hooks/.venv/pyvenv.cfg",
        ):
            (path / relative).parent.mkdir(parents=True, exist_ok=True)
            (path / relative).write_text("inside a git directory\n")

        self._swept(path)

    def test_a_linked_install_is_never_followed(
        self, scratch_root: Path, upstream: Path, tmp_path: Path
    ) -> None:
        # A link holds no bytes of its own, so it stays, and what it names is
        # never entered, whether in the tree or outside it.
        path, clone = self._clone_into(upstream)
        (clone / "store" / "dep").mkdir(parents=True)
        (clone / "store" / "dep" / "index.js").write_text("in the tree\n")
        outside = tmp_path / "shared-modules"
        (outside / "dep").mkdir(parents=True)
        (outside / "dep" / "index.js").write_text("outside the tree\n")
        (outside / "__pycache__").mkdir()
        (clone / "web").mkdir()
        _symlink_or_skip(clone / "node_modules", Path("store"))
        _symlink_or_skip(clone / "web" / "node_modules", outside)
        _symlink_or_skip(clone / "web" / "lib", outside)
        _symlink_or_skip(path / "pytest-of-agent", clone / "store")
        kept, outside_before = _snapshot(path), _snapshot(outside)

        assert self._sweep_later() == 0
        moved = self._moved(path)
        assert _snapshot(moved) == kept
        assert _snapshot(outside) == outside_before

    def test_a_tree_already_in_the_quarantine_is_stripped_on_a_later_sweep(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        # A tree moved before the strip existed, or whose strip was cut short.
        path, _clone = self._local_feature(upstream)
        moved_at = time.time() + self.LATER
        assert sc.sweep_dead_scratch(now=moved_at) == 0
        moved = self._moved(path)
        kept = _snapshot(moved)
        self._rebuildable(moved)

        assert sc.sweep_dead_scratch(now=moved_at + 3600) == 0
        assert _snapshot(moved) == kept
        assert sc.sweep_dead_scratch(now=moved_at + 7200) == 0
        assert _snapshot(moved) == kept
        assert _git(moved / "clone", "log", "-1", "--format=%s") == "feature\n"

    # -- the move ----------------------------------------------------------

    def test_every_move_logs_one_info_line_naming_where_it_went(
        self, scratch_root: Path, upstream: Path, caplog
    ) -> None:
        caplog.set_level("INFO", logger=sc.__name__)
        clean, _clone = self._clone_into(upstream)
        unsaved, _feature = self._local_feature(upstream)
        now = time.time() + self.LATER

        assert sc.sweep_dead_scratch(now=now) == 0

        expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now + sc.QUARANTINE_TTL_SECONDS))
        for path in (clean, unsaved):
            moved = self._moved(path)
            [line] = [r for r in caplog.records if str(moved) in r.getMessage()]
            assert line.levelname == "INFO"
            assert f"moved {path.name} to {moved} instead of deleting it" in line.getMessage()
            assert expires in line.getMessage()

    def test_a_tree_stays_in_place_when_the_quarantine_cannot_receive_it(
        self, scratch_root: Path, upstream: Path, caplog
    ) -> None:
        caplog.set_level("WARNING", logger=sc.__name__)
        path, _clone = self._local_feature(upstream)
        sc.quarantine_dir().write_text("not a directory")

        assert self._sweep_later() == 0
        assert path.exists()
        assert f"keeping {path} in place" in caplog.text

        sc.quarantine_dir().unlink()
        self._swept(path)

    @pytest.mark.parametrize("error", [errno.EXDEV, errno.EACCES])
    def test_a_failed_move_keeps_the_tree_in_place_and_copies_nothing(
        self, scratch_root: Path, upstream: Path, monkeypatch, caplog, error: int
    ) -> None:
        # A quarantine on another filesystem refuses the rename with EXDEV; the
        # sweep never falls back to copying the tree and deleting it.
        caplog.set_level("WARNING", logger=sc.__name__)
        path, _clone = self._local_feature(upstream)
        before = _snapshot(path)

        def _refuse(_source, _target):
            raise OSError(error, os.strerror(error))

        monkeypatch.setattr(sc.os, "rename", _refuse)

        assert self._sweep_later() == 0
        assert _snapshot(path) == before
        assert list(sc.quarantine_dir().iterdir()) == []
        assert "could not be moved" in caplog.text

    def test_a_tree_marked_active_before_the_final_look_is_not_moved(
        self, scratch_root: Path, upstream: Path, monkeypatch
    ) -> None:
        # A spawn that mounts the tree while the sweep lists it refreshes its
        # mtime, and the sweep's final look under the lock sees that.
        path, _clone = self._local_feature(upstream)
        idle_since = time.time() - self.LATER
        for dirpath, dirnames, filenames in os.walk(path):
            for name in dirnames + filenames:
                os.utime(Path(dirpath) / name, (idle_since, idle_since))
        os.utime(path, (idle_since, idle_since))
        real_check = sc.holds_git_repository

        def _spawn_meanwhile(tree: Path) -> bool:
            assert sc.shared_scratch_window(tree) == tree
            return real_check(tree)

        monkeypatch.setattr(sc, "holds_git_repository", _spawn_meanwhile)

        assert sc.sweep_dead_scratch() == 0
        assert path.exists()
        assert not sc.quarantine_dir().exists()

    # -- the expiry --------------------------------------------------------

    def test_a_quarantined_tree_is_deleted_only_after_its_ttl(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        path, _clone = self._local_feature(upstream)
        moved_at = time.time() + self.LATER
        assert sc.sweep_dead_scratch(now=moved_at) == 0
        moved = self._moved(path)

        assert sc.sweep_dead_scratch(now=moved_at + sc.QUARANTINE_TTL_SECONDS - 60) == 0
        assert moved.exists()
        assert sc.sweep_dead_scratch(now=moved_at + sc.QUARANTINE_TTL_SECONDS + 1) == 1
        assert not moved.exists()

    def test_each_quarantined_tree_expires_by_its_own_move_time(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        week = 7 * 24 * 3600.0
        early, _clone = self._clone_into(upstream)
        first_sweep = time.time() + self.LATER
        assert sc.sweep_dead_scratch(now=first_sweep) == 0
        late, _clone = self._clone_into(upstream)
        assert sc.sweep_dead_scratch(now=first_sweep + week) == 0
        early_moved, late_moved = self._moved(early), self._moved(late)

        assert sc.sweep_dead_scratch(now=first_sweep + sc.QUARANTINE_TTL_SECONDS + 1) == 1
        assert not early_moved.exists()
        assert late_moved.exists()
        assert sc.sweep_dead_scratch(now=first_sweep + week + sc.QUARANTINE_TTL_SECONDS + 1) == 1
        assert not late_moved.exists()

    def test_a_write_in_the_quarantine_defers_its_expiry(
        self, scratch_root: Path, upstream: Path
    ) -> None:
        # Someone working inside a quarantined tree past its date keeps it
        # until the tree has been idle for the grace window.
        path, _clone = self._local_feature(upstream)
        moved_at = time.time() + self.LATER
        assert sc.sweep_dead_scratch(now=moved_at) == 0
        moved = self._moved(path)
        expired = moved_at + sc.QUARANTINE_TTL_SECONDS + 1
        recovering = moved / "clone" / "notes.txt"
        recovering.write_text("written during recovery\n")
        os.utime(recovering, (expired - 60, expired - 60))

        assert sc.sweep_dead_scratch(now=expired) == 0
        assert recovering.exists()
        assert sc.sweep_dead_scratch(now=expired + sc._UNOWNED_GRACE_SECONDS) == 1
        assert not moved.exists()

    def test_entries_the_sweep_did_not_write_are_left_in_the_quarantine(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        holding = sc.quarantine_dir()
        holding.mkdir(parents=True)
        (holding / "kept-by-hand").mkdir()
        (holding / "20200101T000000Z-a-file").write_text("not a directory")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not the sweep's\n")
        _symlink_or_skip(holding / "20200101T000000Z-a-link", outside)

        assert sc.sweep_dead_scratch(now=time.time() + 10 * sc.QUARANTINE_TTL_SECONDS) == 0
        assert (holding / "kept-by-hand").is_dir()
        assert (holding / "20200101T000000Z-a-file").is_file()
        assert os.path.lexists(holding / "20200101T000000Z-a-link")
        assert (outside / "keep.txt").is_file()

    def test_a_stamped_name_outside_the_quarantine_is_not_expired(self, scratch_root: Path) -> None:
        # The move time is read only from entries in the quarantine; a
        # directory in the root with the same kind of name is judged like any
        # other scratch directory, and one with no owner record is kept.
        stray = scratch_root / "20200101T000000Z-gitwork-deadbeef"
        stray.mkdir(parents=True)
        (stray / "work.txt").write_text("no owner record\n")

        assert sc.sweep_dead_scratch(now=time.time() + 10 * sc.QUARANTINE_TTL_SECONDS) == 0
        assert (stray / "work.txt").is_file()


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the log cap needs dir_fd opens and O_NOFOLLOW"
)


@posix_only
class TestKiroCliLogCap:
    CAP = 4096
    KEEP = 1024

    @staticmethod
    def _log_dir(scratch_root: Path, label: str = "s") -> Path:
        path = sc.allocate_scratch(label)
        log_dir = path / sc.KIRO_CLI_LOG_SUBDIR
        log_dir.mkdir()
        return log_dir

    def _cap(self) -> int:
        return sc.cap_kiro_cli_logs(cap_bytes=self.CAP, keep_bytes=self.KEEP)

    def test_oversized_log_is_tail_kept_and_truncated_while_open(self, scratch_root: Path) -> None:
        log_dir = self._log_dir(scratch_root)
        log = log_dir / "kiro-chat.log"
        body = b"".join(b"%08d\n" % i for i in range(1000))  # 9000 bytes
        log.write_bytes(body)
        # The writer is kiro-cli, alive, appending -- model it with an open
        # O_APPEND descriptor that keeps writing after the rotation.
        writer = os.open(log, os.O_WRONLY | os.O_APPEND)
        try:
            assert self._cap() == 1
            assert log.stat().st_size == 0
            assert (log_dir / "kiro-chat.log.1").read_bytes() == body[-self.KEEP :]
            assert stat.S_IMODE((log_dir / "kiro-chat.log.1").stat().st_mode) == 0o600
            os.write(writer, b"after\n")
        finally:
            os.close(writer)
        # O_APPEND lands the next record at the new end: no hole, no lost writer.
        assert log.read_bytes() == b"after\n"

    def test_all_three_members_are_bounded_and_small_ones_kept(self, scratch_root: Path) -> None:
        log_dir = self._log_dir(scratch_root)
        (log_dir / "kiro-chat.log").write_bytes(b"x" * (self.CAP + 1))
        (log_dir / "mcp.log").write_bytes(b"y" * (self.CAP + 1))
        (log_dir / "lsp.log").write_bytes(b"z" * self.CAP)  # exactly at the cap: kept

        assert self._cap() == 2
        assert (log_dir / "kiro-chat.log").stat().st_size == 0
        assert (log_dir / "mcp.log").stat().st_size == 0
        assert (log_dir / "lsp.log").stat().st_size == self.CAP

    def test_under_cap_untouched_and_missing_root_is_zero(self, scratch_root: Path) -> None:
        assert sc.cap_kiro_cli_logs() == 0  # no root yet
        log_dir = self._log_dir(scratch_root)
        (log_dir / "kiro-chat.log").write_bytes(b"small")
        assert self._cap() == 0
        assert (log_dir / "kiro-chat.log").read_bytes() == b"small"

    def test_symlinked_log_is_never_truncated(self, scratch_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"v" * (self.CAP + 1))
        log_dir = self._log_dir(scratch_root)
        (log_dir / "kiro-chat.log").symlink_to(victim)

        assert self._cap() == 0
        assert victim.stat().st_size == self.CAP + 1

    def test_hard_linked_log_is_never_truncated(self, scratch_root: Path, tmp_path: Path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"v" * (self.CAP + 1))
        log_dir = self._log_dir(scratch_root)
        os.link(victim, log_dir / "kiro-chat.log")

        assert self._cap() == 0
        assert victim.stat().st_size == self.CAP + 1

    def test_symlinked_log_dir_is_never_followed(self, scratch_root: Path, tmp_path: Path) -> None:
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        (victim_dir / "kiro-chat.log").write_bytes(b"v" * (self.CAP + 1))
        path = sc.allocate_scratch("linked")
        (path / sc.KIRO_CLI_LOG_SUBDIR).symlink_to(victim_dir)

        assert self._cap() == 0
        assert (victim_dir / "kiro-chat.log").stat().st_size == self.CAP + 1

    def test_symlinked_scratch_child_is_never_followed(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim" / sc.KIRO_CLI_LOG_SUBDIR
        victim.mkdir(parents=True)
        (victim / "kiro-chat.log").write_bytes(b"v" * (self.CAP + 1))
        scratch_root.mkdir(parents=True, exist_ok=True)
        (scratch_root / "evil").symlink_to(victim.parent)

        assert self._cap() == 0
        assert (victim / "kiro-chat.log").stat().st_size == self.CAP + 1

    def test_planted_rotated_name_is_unlinked_not_written_through(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"precious")
        log_dir = self._log_dir(scratch_root)
        (log_dir / "kiro-chat.log").write_bytes(b"x" * (self.CAP + 1))
        (log_dir / "kiro-chat.log.1").symlink_to(victim)

        assert self._cap() == 1
        assert victim.read_bytes() == b"precious"
        rotated = log_dir / "kiro-chat.log.1"
        assert not rotated.is_symlink() and rotated.read_bytes() == b"x" * self.KEEP

    def test_truncate_still_bounds_when_tail_copy_fails(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        log_dir = self._log_dir(scratch_root)
        log = log_dir / "kiro-chat.log"
        log.write_bytes(b"x" * (self.CAP + 1))

        def boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(sc, "_write_rotated", boom)

        assert self._cap() == 1
        assert log.stat().st_size == 0
        assert not (log_dir / "kiro-chat.log.1").exists()

    def test_linked_root_caps_nothing(self, scratch_root: Path, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "s" / sc.KIRO_CLI_LOG_SUBDIR).mkdir(parents=True)
        (real / "s" / sc.KIRO_CLI_LOG_SUBDIR / "kiro-chat.log").write_bytes(b"x" * (self.CAP + 1))
        scratch_root.parent.mkdir(parents=True, exist_ok=True)
        scratch_root.symlink_to(real)

        assert self._cap() == 0
