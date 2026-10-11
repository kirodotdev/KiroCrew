"""Tests for :mod:`kiro_crew.agent_scratch`.

Everything runs against a monkeypatched data home under ``tmp_path``; the
real ``<data home>/scratch`` is never touched.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import platform_compat
from kiro_crew.config import paths


class _FakeComponentSecurity:
    """Minimal stand-in for ``windows_acl.ComponentSecurity`` carrying an owner_sid."""

    def __init__(self, owner_sid: str) -> None:
        self.owner_sid = owner_sid


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
        # The validator refuses the foreign-owned root with the specific reason.
        assert paths.valid_scratch_root_override() is None
        assert paths.scratch_root_override_refusal_reason() == "not owned by current user"
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

    def test_relocated_root_with_unreadable_owner_is_refused_fail_closed(
        self, tmp_path, monkeypatch
    ) -> None:
        # If the root's owner cannot be read, the validator refuses fail-closed rather than
        # adopt an owner it cannot verify, and scratch_root() returns the default.
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
        assert paths.scratch_root_override_refusal_reason() == "owner unreadable"
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
