"""SELinux pre-flight for the Linux system unit (#7165).

Pure-logic tests: every kernel interface the module reads is redirected at a
tmp_path fake or monkeypatched, so nothing here reads the host's real policy,
label set, or enforcing state. That matters twice over — the suite must produce
the same verdicts on an AppArmor-only CI runner as on an SELinux-enforcing
workstation, and a test must never be in a position to write to selinuxfs.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.service import linux as svc_linux
from kiro_crew.service import selinux as sel
from kiro_crew.service.common import SERVICE_NAME

# Contexts as they appear on a Fedora-family targeted policy. Only used as
# opaque strings — the module never parses or matches on a type name.
INIT_T = "system_u:system_r:init_t:s0"
HOME_T = "unconfined_u:object_r:user_home_t:s0"
BIN_T = "system_u:object_r:bin_t:s0"

EXECUTE_BIT = 15  # 1-based bit number as selinuxfs reports it; mask 0x4000.
EXECUTE_MASK = 1 << (EXECUTE_BIT - 1)
GETATTR_MASK = 1 << (4 - 1)


def _fake_selinuxfs(tmp_path, *, enforce="1", execute_bit=EXECUTE_BIT, index=6):
    """Build a selinuxfs-shaped tree and point the module at it."""
    root = tmp_path / "selinux"
    (root / "class" / "file" / "perms").mkdir(parents=True)
    if enforce is not None:
        (root / "enforce").write_text(enforce)
    (root / "class" / "file" / "index").write_text(str(index))
    if execute_bit is not None:
        (root / "class" / "file" / "perms" / "execute").write_text(str(execute_bit))
    return root


@pytest.fixture
def selinuxfs(tmp_path, monkeypatch):
    """An enforcing SELinux host whose policy answers are set per-test."""
    root = _fake_selinuxfs(tmp_path)
    monkeypatch.setattr(sel, "SELINUX_FS", root)
    monkeypatch.setattr(sel, "_ENFORCE_PATH", root / "enforce")
    monkeypatch.setattr(sel, "_CLASS_DIR", root / "class")
    monkeypatch.setattr(sel, "_ACCESS_PATH", root / "access")
    return root


@pytest.fixture
def policy(monkeypatch):
    """Control ``_compute_av``'s answer without touching a kernel interface."""

    def configure(allowed: int, flags: int = 0):
        monkeypatch.setattr(sel, "_compute_av", lambda _s, _t, _c: (allowed, flags))

    return configure


class TestEnforcingDetection:
    """Only an actively enforcing kernel can turn a denial into a start failure."""

    def test_enforce_one_is_enforcing(self, selinuxfs):
        assert sel.is_enforcing() is True

    def test_permissive_is_not_enforcing(self, selinuxfs):
        """In permissive mode the denial is logged and the execve succeeds."""
        (selinuxfs / "enforce").write_text("0")
        assert sel.is_enforcing() is False

    def test_absent_selinuxfs_is_not_enforcing(self, selinuxfs):
        """No SELinux at all — the overwhelmingly common case — must read False."""
        (selinuxfs / "enforce").unlink()
        assert sel.is_enforcing() is False

    def test_unparseable_enforce_is_not_enforcing(self, selinuxfs):
        (selinuxfs / "enforce").write_text("banana")
        assert sel.is_enforcing() is False


class TestLabelReading:
    """The label of the file the kernel actually executes, not of the symlink."""

    def test_unreadable_xattr_reads_as_no_label(self, tmp_path, monkeypatch):
        """A filesystem that cannot answer must not be read as a label.

        Patched rather than relying on an unlabelled temp file: on a real
        SELinux host every file HAS a label (a tmp file comes back as
        ``default_t``), so a host-dependent version of this test passes on CI
        and fails on exactly the machines the feature is for.
        """

        def refuse(*_a):
            raise OSError("xattr not supported")

        # raising=False: os.getxattr is Linux-only and absent on Windows, where
        # this file is still collected. Setting it there is exactly right — the
        # parse logic under test is platform-independent, and _file_context()
        # reaches it through getattr(os, "getxattr", None).
        monkeypatch.setattr(sel.os, "getxattr", refuse, raising=False)
        target = tmp_path / "kirocrew"
        target.write_text("#!/bin/sh\n")
        assert sel._file_context(str(target)) is None

    def test_absent_path_reads_as_no_label(self, tmp_path):
        assert sel._file_context(str(tmp_path / "nope")) is None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="symlink creation needs privilege on Windows and realpath anchors "
        "a POSIX path to a drive letter, so neither the setup nor the asserted "
        "path is meaningful there; the module only ever runs on Linux",
    )
    def test_label_is_read_through_a_symlink_chain(self, tmp_path, monkeypatch):
        """~/.local/bin/kirocrew is a symlink to the venv entry point.

        SELinux checks the label of the resolved file, so reading the LINK's
        label would answer a question the kernel never asks.
        """
        real = tmp_path / "venv" / "bin" / "kirocrew"
        real.parent.mkdir(parents=True)
        real.write_text("#!/usr/bin/python3\n")
        link = tmp_path / "kirocrew"
        link.symlink_to(real)

        seen: list[str] = []

        def fake_getxattr(path, name):
            seen.append(os.fspath(path))
            assert name == "security.selinux"
            return HOME_T.encode() + b"\x00"

        monkeypatch.setattr(sel.os, "getxattr", fake_getxattr, raising=False)
        assert sel._file_context(str(link)) == HOME_T
        assert seen == [str(real)], "must read the resolved target's label"

    def test_trailing_nul_is_stripped(self, tmp_path, monkeypatch):
        """The kernel NUL-terminates the value; a bare strip leaves it behind
        and the policy query would then fail to parse the context."""
        monkeypatch.setattr(sel.os, "getxattr", lambda *_a: BIN_T.encode() + b"\x00", raising=False)
        target = tmp_path / "kirocrew"
        target.write_text("x")
        assert sel._file_context(str(target)) == BIN_T


class TestPermBitDecoding:
    """selinuxfs exposes a 1-based BIT NUMBER, not a mask."""

    def test_bit_number_becomes_a_mask(self, selinuxfs):
        assert sel._perm_bit("file", "execute") == EXECUTE_MASK == 0x4000

    def test_out_of_range_bit_is_rejected(self, selinuxfs):
        (selinuxfs / "class" / "file" / "perms" / "execute").write_text("99")
        assert sel._perm_bit("file", "execute") is None

    def test_missing_perm_file_is_none(self, selinuxfs):
        (selinuxfs / "class" / "file" / "perms" / "execute").unlink()
        assert sel._perm_bit("file", "execute") is None


class TestBlocksSystemUnit:
    """The gate: fires ONLY on a proven denial, fails open on everything else."""

    @staticmethod
    def _binary(tmp_path, shebang: str | None = None) -> str:
        path = tmp_path / "kirocrew"
        path.write_text(f"{shebang}\n" if shebang else "\x7fELF")
        path.chmod(0o755)
        return str(path)

    def test_fires_when_policy_denies_execute(self, selinuxfs, policy, monkeypatch, tmp_path):
        binary = self._binary(tmp_path)
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: HOME_T)
        # getattr allowed, execute NOT — the exact shape that makes the file look
        # fine to every ordinary check while systemd still gets 203/EXEC.
        policy(GETATTR_MASK)

        blocked, reason = sel.blocks_system_unit(binary)
        assert blocked is True
        assert INIT_T in reason and HOME_T in reason and binary in reason

    def test_fires_on_any_denied_label_not_just_user_home_t(
        self, selinuxfs, policy, monkeypatch, tmp_path
    ):
        """The gate must key on the POLICY VERDICT, never on a type name.

        A checkout outside /home (``/local/home/...``, a bind-mounted work area,
        ``/opt``) does not match the home fcontext regex and comes back as
        ``default_t`` — verified on the development host, where the gate
        correctly fired on ``default_t`` rather than the ``user_home_t`` the
        issue reports. Matching on the reported type name would have missed it.
        """
        other_label = "unconfined_u:object_r:default_t:s0"
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: other_label)
        policy(GETATTR_MASK)

        blocked, reason = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is True
        assert other_label in reason

    def test_quiet_when_policy_allows_execute(self, selinuxfs, policy, monkeypatch, tmp_path):
        binary = self._binary(tmp_path)
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: BIN_T)
        policy(GETATTR_MASK | EXECUTE_MASK)

        blocked, reason = sel.blocks_system_unit(binary)
        assert blocked is False
        assert "allows" in reason

    def test_quiet_when_not_enforcing(self, selinuxfs, policy, monkeypatch, tmp_path):
        """Permissive: the denial is logged, the unit starts. Must not refuse."""
        (selinuxfs / "enforce").write_text("0")
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: HOME_T)
        policy(GETATTR_MASK)

        blocked, reason = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is False
        assert "not enforcing" in reason

    def test_quiet_when_source_domain_is_permissive(self, selinuxfs, policy, monkeypatch, tmp_path):
        """A per-domain permissive source still executes despite the denial.

        Global mode is enforcing here, so only the reply's flags word
        distinguishes this from a real failure.
        """
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: HOME_T)
        policy(GETATTR_MASK, flags=sel._AVD_FLAG_PERMISSIVE)

        blocked, _ = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is False

    def test_quiet_when_pid1_context_is_unreadable(self, selinuxfs, policy, monkeypatch, tmp_path):
        monkeypatch.setattr(sel, "_system_manager_context", lambda: None)
        policy(GETATTR_MASK)
        blocked, reason = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is False
        assert "system manager" in reason

    def test_quiet_when_the_file_has_no_label(self, selinuxfs, policy, monkeypatch, tmp_path):
        """An unlabelled or unreadable path yields no verdict, so no refusal."""
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: None)
        policy(GETATTR_MASK)
        blocked, _ = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is False

    def test_quiet_when_the_policy_query_fails(self, selinuxfs, monkeypatch, tmp_path):
        """A kernel that refuses _compute_av must not be read as a denial."""
        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        monkeypatch.setattr(sel, "_file_context", lambda _p: HOME_T)
        monkeypatch.setattr(sel, "_compute_av", lambda *_a: None)
        blocked, _ = sel.blocks_system_unit(self._binary(tmp_path))
        assert blocked is False

    @pytest.mark.skipif(
        os.name == "nt",
        reason="a shebang is a POSIX exec mechanism and the interpreter path it "
        "names is required to be absolute-POSIX; on Windows tmp_path yields "
        "C:\\... so _interpreter_of correctly declines it and there is nothing "
        "to assert",
    )
    def test_fires_on_a_denied_shebang_interpreter(self, selinuxfs, policy, monkeypatch, tmp_path):
        """The entry point is a script naming a venv interpreter under $HOME.

        Checking only the script would pass a case that still fails at the
        execve of the interpreter — a false negative that lets the doomed
        install proceed.
        """
        interpreter = tmp_path / "venv" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"\x7fELF")
        script = self._binary(tmp_path, shebang=f"#!{interpreter}")

        monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
        # The script itself is fine; only the interpreter is home-labelled.
        monkeypatch.setattr(
            sel,
            "_file_context",
            lambda p: HOME_T if p == str(interpreter) else BIN_T,
        )
        monkeypatch.setattr(
            sel,
            "_compute_av",
            lambda _s, target, _c: (
                (GETATTR_MASK, 0) if target == HOME_T else (GETATTR_MASK | EXECUTE_MASK, 0)
            ),
        )

        blocked, reason = sel.blocks_system_unit(script)
        assert blocked is True
        assert str(interpreter) in reason

    def test_non_absolute_shebang_is_ignored(self, selinuxfs, tmp_path):
        """`#!python3` names nothing the kernel can resolve for us to check."""
        script = self._binary(tmp_path, shebang="#!python3")
        assert sel._interpreter_of(script) is None

    def test_env_shebang_reports_env_as_the_executed_file(self, tmp_path):
        """`#!/usr/bin/env python3` execs env; env is what the kernel checks."""
        script = self._binary(tmp_path, shebang="#!/usr/bin/env python3")
        assert sel._interpreter_of(script) == "/usr/bin/env"

    def test_elf_binary_has_no_interpreter(self, tmp_path):
        assert sel._interpreter_of(self._binary(tmp_path)) is None


class TestComputeAvQuery:
    """Query construction and reply parsing, exercised through the ``_query_access``
    seam so no test ever monkeypatches ``os.open`` (which breaks pytest's own
    I/O) or comes within one edit of writing to real selinuxfs."""

    def test_query_is_written_and_reply_parsed(self, selinuxfs, monkeypatch):
        asked: list[str] = []

        def fake_query(query: str) -> str:
            asked.append(query)
            return "220990 ffffffff 0 ffffffff 1 0"

        monkeypatch.setattr(sel, "_query_access", fake_query)
        assert sel._compute_av(INIT_T, HOME_T, "file") == (0x220990, 0)
        # class id 6 from the faked index; ffffffff asks for the full vector.
        assert asked == [f"{INIT_T} {HOME_T} 6 ffffffff"]

    def test_permissive_flag_is_taken_from_the_last_field(self, selinuxfs, monkeypatch):
        """flags is field 6, not field 5 — an off-by-one here would silently
        read seqno as the permissive bit and disable the whole gate."""
        monkeypatch.setattr(sel, "_query_access", lambda _q: "220990 ffffffff 0 ffffffff 7 1")
        assert sel._compute_av(INIT_T, HOME_T, "file") == (0x220990, 1)

    def test_truncated_reply_is_no_answer(self, selinuxfs, monkeypatch):
        monkeypatch.setattr(sel, "_query_access", lambda _q: "220990 ffffffff")
        assert sel._compute_av(INIT_T, HOME_T, "file") is None

    def test_non_hex_reply_is_no_answer(self, selinuxfs, monkeypatch):
        """A reply shaped right but not parseable must not become a verdict."""
        monkeypatch.setattr(sel, "_query_access", lambda _q: "zzz ffffffff 0 ffffffff 1 0")
        assert sel._compute_av(INIT_T, HOME_T, "file") is None

    def test_unavailable_transport_is_no_answer(self, selinuxfs, monkeypatch):
        """A kernel that refuses _compute_av, or no selinuxfs at all."""
        monkeypatch.setattr(sel, "_query_access", lambda _q: None)
        assert sel._compute_av(INIT_T, HOME_T, "file") is None

    def test_unopenable_access_node_yields_no_reply(self, selinuxfs):
        """The real transport against a path that does not exist."""
        assert sel._query_access("anything") is None

    def test_missing_class_index_is_no_answer(self, selinuxfs, monkeypatch):
        monkeypatch.setattr(sel, "_query_access", lambda _q: "0 0 0 0 0 0")
        (selinuxfs / "class" / "file" / "index").unlink()
        assert sel._compute_av(INIT_T, HOME_T, "file") is None


class TestUserScopeUnitRendering:
    """The remedy is rendered by the real renderer, so it cannot drift."""

    @staticmethod
    def _render(monkeypatch, user_scope):
        monkeypatch.setenv("USER", "tester")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/tester/.local/bin/kirocrew",
        ):
            return svc_linux.render_unit(user_scope=user_scope)

    def test_user_unit_omits_user_and_group(self, monkeypatch):
        """A user manager rejects User=/Group=, making the unit unloadable."""
        unit = self._render(monkeypatch, True)
        assert "\nUser=" not in unit
        assert "\nGroup=" not in unit

    def test_system_unit_still_carries_user_and_group(self, monkeypatch):
        unit = self._render(monkeypatch, False)
        assert "\nUser=tester\n" in unit
        assert "\nGroup=" in unit

    def test_default_scope_is_system(self, monkeypatch):
        """Callers that pass nothing must get exactly today's unit."""
        monkeypatch.setenv("USER", "tester")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/tester/.local/bin/kirocrew",
        ):
            assert svc_linux.render_unit() == svc_linux.render_unit(user_scope=False)

    def test_user_unit_wants_default_target(self, monkeypatch):
        """multi-user.target is a system target the user manager does not have."""
        unit = self._render(monkeypatch, True)
        assert "WantedBy=default.target" in unit
        assert "multi-user.target" not in unit

    def test_system_unit_wants_multi_user_target(self, monkeypatch):
        unit = self._render(monkeypatch, False)
        assert "WantedBy=multi-user.target" in unit

    def test_both_scopes_share_exec_start_and_environment(self, monkeypatch):
        """The pasted unit must run the same thing with the same environment."""
        system = self._render(monkeypatch, False)
        user = self._render(monkeypatch, True)
        exec_line = [ln for ln in system.splitlines() if ln.startswith("ExecStart=")]
        assert exec_line and exec_line[0] in user.splitlines()
        for line in system.splitlines():
            if line.startswith("Environment="):
                assert line in user.splitlines()


class TestStartFailureCoversWhatThePreflightCannotProve:
    """The pre-flight only judges the file systemd itself execs.

    A `KIROCREW_SERVICE_BIN` override naming a system-labelled WRAPPER that later
    runs a binary under $HOME passes the gate and still cannot serve -- as the
    shell's exit 126, not 203/EXEC, because the wrapper execs fine. Following the
    delegation is not decidable and scanning the wrapper for path literals would
    refuse over a path in a comment, so the residue is caught where the unit
    actually failed instead.
    """

    @staticmethod
    def _restart_fails(monkeypatch, *, enforcing):
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        # The gate finds nothing: this is precisely the wrapper case.
        monkeypatch.setattr(svc_linux.selinux, "blocks_system_unit", lambda _p: (False, "allowed"))
        monkeypatch.setattr(svc_linux.selinux, "is_enforcing", lambda: enforcing)

        def run(argv, **_kw):
            if argv[:3] == ["sudo", "systemctl", "restart"]:
                return MagicMock(returncode=1, stdout="", stderr="Job failed")
            return MagicMock(returncode=0, stdout="", stderr="")

        return run

    def test_enforcing_host_gets_the_selinux_hypothesis_and_remedy(self, monkeypatch):
        run = self._restart_fails(monkeypatch, enforcing=True)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew-wrapper",
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        # Prose assertions run against whitespace-collapsed text: the message is
        # hard-wrapped, so a literal substring can straddle a newline and a test
        # pinned to the exact wrap breaks on any reflow.
        flat = " ".join(msg.split())
        # The original failure is still reported first.
        assert "systemctl restart` failed" in flat
        # Then SELinux as a CANDIDATE, with the command that settles it.
        assert "SELinux is enforcing" in flat
        assert "hypothesis, not a finding" in flat
        assert "ausearch -m avc" in flat
        # And it must be honest that the gate looked and found nothing.
        assert "found no proven denial" in flat
        assert "/usr/local/bin/kirocrew-wrapper" in flat
        # It points at the documented remedy; it does NOT paste one. This fires on
        # every failed restart on every enforcing host -- all of RHEL/Fedora --
        # including hosts the pre-flight positively proved ALLOW for, so a port
        # conflict must not be answered with a pasteable unit for a denial nobody
        # has observed.
        assert "docs/guides/install.md" in flat
        assert "KIROCREW_UNIT" not in msg
        assert "systemctl --user enable --now" not in msg
        assert "loginctl enable-linger" not in msg

    def test_the_hint_is_much_shorter_than_the_proven_refusal(self, monkeypatch):
        """A hypothesis must not cost the operator as much text as a finding."""
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.selinux, "is_enforcing", lambda: True)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            hint = svc_linux.selinux_start_failure_hint()
            refusal = svc_linux.selinux_refusal("a proven denial")

        # Measured on this tree: 15 lines vs 68. The bound is what stops the
        # remedy (or another wall of prose) drifting back onto the guess path.
        assert len(hint.splitlines()) <= 20, "the hypothesis path must stay compact"
        assert len(refusal.splitlines()) > len(hint.splitlines()) * 3

    def test_non_enforcing_host_gets_no_selinux_noise(self, monkeypatch):
        """A restart failure on a host without SELinux must read exactly as before."""
        run = self._restart_fails(monkeypatch, enforcing=False)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        assert "systemctl restart` failed" in msg
        assert "SELinux" not in msg
        assert "ausearch" not in msg

    def test_hint_is_empty_off_the_enforcing_path(self, monkeypatch):
        monkeypatch.setattr(svc_linux.selinux, "is_enforcing", lambda: False)
        assert svc_linux.selinux_start_failure_hint() == ""

    def test_only_the_proven_refusal_carries_the_remedy(self, monkeypatch):
        """The remedy belongs behind a proven denial, not ahead of a guess."""
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.selinux, "is_enforcing", lambda: True)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            remedy = svc_linux._user_scope_remedy()
            assert remedy in svc_linux.selinux_refusal("some proven denial")
            assert remedy not in svc_linux.selinux_start_failure_hint()


class TestInstallRefusesAnUnstartableSystemUnit:
    """install() must stop BEFORE the first write, not after enabling a
    crash-looping unit."""

    @staticmethod
    def _blocked(monkeypatch, blocked=True):
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(
            svc_linux.selinux,
            "blocks_system_unit",
            lambda _p: (blocked, "policy denies init_t execute on user_home_t"),
        )

    def test_install_raises_before_touching_the_host(self, monkeypatch):
        self._blocked(monkeypatch)
        ok = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", return_value=ok) as run,
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        assert run.call_args_list == [], (
            "a proven-unstartable unit must be refused before any subprocess "
            f"runs; got {[list(c.args[0]) for c in run.call_args_list]}"
        )
        msg = str(exc.value)
        assert "203/EXEC" in msg
        assert "policy denies init_t execute on user_home_t" in msg

    def test_refusal_carries_a_pasteable_user_unit(self, monkeypatch):
        self._blocked(monkeypatch)
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/tester/.local/bin/kirocrew",
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        # The remedy must be the user-scope unit, complete and self-contained.
        assert "systemctl --user enable --now" in msg
        assert "loginctl enable-linger" in msg
        assert str(svc_linux.USER_UNIT_SUBDIR) in msg
        assert "WantedBy=default.target" in msg
        assert "ExecStart=" in msg
        # Every command named must exist. `kirocrew service` has only
        # install/uninstall/status, so the remedy may not invent a subcommand.
        assert "kirocrew service print-unit" not in msg

    def test_refusal_names_only_real_service_subcommands(self, monkeypatch):
        """Guards the class of bug where a remedy prints a command we do not
        ship, which sends the operator into `invalid choice`."""
        self._blocked(monkeypatch)
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/home/tester/.local/bin/kirocrew",
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        real = {"install", "uninstall", "status"}
        for token in str(exc.value).split("kirocrew service ")[1:]:
            word = token.split()[0].strip("`.,\n")
            # A pipe-joined hint like `status|uninstall` names several at once.
            assert set(word.split("|")) <= real, f"unknown subcommand: {word}"

    def test_refusal_never_lets_the_pasting_shell_pick_the_account(self, monkeypatch):
        """A user unit has no User=, so whoever's manager loads it runs the agent.

        `service install` runs under sudo, so the shell reading this refusal is
        usually root's. If the remedy said `~` or `$USER` it would name /root and
        root, and the operator would end up running untrusted agent tools as root
        -- the exact thing install() refuses outright a few lines earlier. Nothing
        in the remedy may be resolved by the shell that pastes it.
        """
        self._blocked(monkeypatch)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        assert "$USER" not in msg, "the pasting shell must not choose the account"
        assert "~/" not in msg, "the pasting shell must not choose the home"
        # Joined the same way the code joins it rather than hard-coded with
        # forward slashes: this file is collected on Windows, where Path renders
        # separators as backslashes, and a POSIX-spelled literal here fails there
        # for a reason that has nothing to do with the property being tested.
        expected_unit = (
            Path("/home/tester") / svc_linux.USER_UNIT_SUBDIR / (f"{SERVICE_NAME}.service")
        )
        assert str(expected_unit) in msg
        assert "loginctl enable-linger tester" in msg

    def test_refusal_warns_that_a_root_shell_would_run_the_agent_as_root(self, monkeypatch):
        """The path names alone are not enough -- an operator pasting into the
        wrong shell must be told what goes wrong, since a user unit gives no
        error, it just silently runs as the wrong account."""
        self._blocked(monkeypatch)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        assert "AS tester" in msg
        assert "ROOT" in msg
        # `sudo -u` looks like the obvious way to run as another account and
        # cannot work here (no session, so no user manager to talk to).
        assert "sudo -u tester` is NOT" in msg
        assert "machinectl shell tester@" in msg

    def test_remedy_quotes_a_home_containing_a_space(self, monkeypatch):
        """The remedy is copy-pasted verbatim, so an unquoted path word-splits.

        `mkdir -p /home/tester with space/.config/systemd/user` would create two
        wrong directories and the redirect would land the unit where systemd never
        looks -- an operator following the instructions exactly would end up with
        no service and no error explaining why.
        """
        self._blocked(monkeypatch)
        spaced_home = "/home/tester with space"
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value=spaced_home),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        msg = str(exc.value)
        quoted = shlex.quote(str(Path(spaced_home) / svc_linux.USER_UNIT_SUBDIR))
        assert f"mkdir -p {quoted}" in msg
        # The bare, splittable form must be gone entirely.
        assert f"mkdir -p {spaced_home}" not in msg

    @pytest.mark.skipif(
        os.name == "nt",
        reason="shlex.quote is POSIX and quotes the backslashes in a Windows path, "
        "so 'needs no quoting' is only meaningful on POSIX -- and what this renders "
        "is a Linux shell snippet either way",
    )
    def test_ordinary_paths_are_not_needlessly_quoted(self, monkeypatch):
        """shlex.quote leaves simple paths alone; the common case must stay clean."""
        self._blocked(monkeypatch)
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc:
                svc_linux.install()

        assert "'" not in str(exc.value).split("mkdir -p ")[1].splitlines()[0]

    def test_install_proceeds_normally_when_not_blocked(self, monkeypatch):
        """The overwhelmingly common host must be completely unaffected."""
        self._blocked(monkeypatch, blocked=False)
        ok = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", return_value=ok) as run,
        ):
            svc_linux.install()

        called = [list(c.args[0]) for c in run.call_args_list]
        assert ["sudo", "systemctl", "enable", f"{SERVICE_NAME}.service"] in called
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in called

    def test_gate_is_asked_about_the_unit_exec_path(self, monkeypatch):
        """It must judge the SAME binary render_unit puts in ExecStart."""
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        asked: list[str] = []
        monkeypatch.setattr(
            svc_linux.selinux,
            "blocks_system_unit",
            lambda path: (asked.append(path), (False, "allowed"))[1],
        )
        ok = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", return_value=ok),
        ):
            svc_linux.install()

        assert asked == ["/home/tester/.local/bin/kirocrew"]
