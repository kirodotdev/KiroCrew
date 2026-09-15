"""``sandbox.launcher_refusal``: the launcher's own refusal line as a typed verdict.

``wrap_argv`` raises before a spawn it cannot sandbox. The Linux launcher can
also refuse AFTER the spawn — a host that passed the probe still denies a
control at spawn time — and that refusal reaches its caller only as exit 1 plus
one ``sandbox:``-prefixed line on stderr. Read as a plain non-zero exit, a
present, signed-in kiro-cli was reported as not installed. These tests pin the
classifier to the launcher's real wording, so a drift in either side reds here
rather than in a container.
"""

from __future__ import annotations

import errno
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox as sb

#: The exact line a container under its runtime's default AppArmor profile
#: produces: both unshares succeed, the launcher's first mount is refused.
_MOUNT_REFUSED = (
    "sandbox: BLOCKED -- making mount propagation private on / failed: errno 13 "
    "(Permission denied). The sandbox could not establish this control, so the "
    "agent would run with the path visible. Lower sandbox_level to run without it "
    "deliberately."
)


class TestLauncherRefusalClassification:
    def test_the_container_mount_refusal_is_a_no_backend_verdict_with_the_mount_remedy(
        self,
    ) -> None:
        kind, detail, remedy = sb.launcher_refusal(_MOUNT_REFUSED)  # type: ignore[misc]

        assert kind == "no_backend"
        assert detail == _MOUNT_REFUSED
        assert remedy == sb.REMEDY_MOUNT_DENIED

    def test_the_line_is_found_anywhere_in_the_captured_output(self) -> None:
        # The supervisor and a launcher warning can precede it; the child's own
        # output cannot follow it (the launcher exits without exec).
        output = "some supervisor note\n" + _MOUNT_REFUSED + "\n"
        assert sb.launcher_refusal(output) is not None

    def test_a_refused_hiding_mount_shares_the_mount_remedy(self) -> None:
        # A bind mount refused with EACCES is the same policy that refuses the
        # propagation mount; the remedy is the same container change.
        line = "sandbox: BLOCKED -- hiding /home/dev/.aws failed: errno 13 (Permission denied)."
        assert sb.launcher_refusal(line) == ("no_backend", line, sb.REMEDY_MOUNT_DENIED)

    def test_a_refused_mount_with_an_unmapped_errno_carries_no_remedy(self) -> None:
        line = "sandbox: BLOCKED -- hiding /home/dev/.aws failed: errno 5 (Input/output error)."
        assert sb.launcher_refusal(line) == ("no_backend", line, "")

    def test_the_unshare_steps_classify_exactly_as_the_probe_does(self) -> None:
        newns = "sandbox: unshare(NEWNS) failed: errno 1"
        newuser = "sandbox: unshare(NEWUSER) failed: errno 1"

        assert sb.launcher_refusal(newns) == ("no_backend", newns, sb.REMEDY_APPARMOR_USERNS)
        assert sb.launcher_refusal(newuser) == ("no_backend", newuser, sb.REMEDY_USERNS_DENIED)

    @pytest.mark.parametrize(
        "line",
        [
            "sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned -1)",
            "sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned -1)",
            "sandbox: BLOCKED — no seccomp syscall table for machine sparc64",
            "sandbox: BLOCKED — libc exposes no prctl(2), so neither the NO_NEW_PRIVS nor "
            "the seccomp step can run",
        ],
    )
    def test_the_filter_installs_are_no_backend_with_no_mechanism_token(self, line: str) -> None:
        # The em dash is the launcher's own spelling at these sites.
        assert sb.launcher_refusal(line) == ("no_backend", line, "")

    def test_a_broken_handshake_is_transient(self) -> None:
        line = "sandbox: FATAL - child did not publish its namespace readiness"
        assert sb.launcher_refusal(line) == ("transient", line, "")

    def test_a_hardlinked_credential_is_not_a_sandbox_failure(self) -> None:
        # The sandbox WORKED and found host state it must not expose. Calling that
        # "no backend" would push the operator to disable isolation instead of
        # fixing the file the line names.
        line = (
            "sandbox: BLOCKED — found hardlink(s) to protected credential path(s): "
            "/home/dev/.ssh/id_ed25519 -> /home/dev/backup/key"
        )
        assert sb.launcher_refusal(line) is None

    def test_an_unreadable_known_hosts_is_host_state_not_a_verdict(self) -> None:
        line = (
            "sandbox: FATAL — cannot read /home/dev/.ssh/known_hosts (Permission denied). "
            "Refusing to continue: proceeding without it would leave host-key "
            "verification accepting any new key."
        )
        assert sb.launcher_refusal(line) is None

    def test_a_launcher_invoked_with_no_command_is_the_callers_defect(self) -> None:
        # ``sandbox_launcher: no command given`` is a usage error in the spawn
        # itself; it says nothing about the host and must not become a verdict.
        assert sb.launcher_refusal("sandbox_launcher: no command given") is None

    def test_an_advisory_warning_the_launcher_continued_past_is_not_a_refusal(self) -> None:
        line = (
            "sandbox: WARNING -- widening /run/x failed (errno 13); continuing with the path sealed"
        )
        assert sb.launcher_refusal(line) is None

    @pytest.mark.parametrize(
        "output",
        [
            "",
            "kiro-cli 1.18.0\n",
            "Error: not logged in\n",
            "Kiro sandbox: enabled (internal)\n",  # a mid-line mention is not the launcher
            "the sandbox: BLOCKED story\n",
        ],
    )
    def test_a_child_that_failed_on_its_own_is_never_a_sandbox_verdict(self, output: str) -> None:
        assert sb.launcher_refusal(output) is None


class _CorroborationWiring:
    """What the corroboration fakes recorded for one call."""

    def __init__(self) -> None:
        self.spawn_argv: list[str] | None = None
        self.spawn_kwargs: dict[str, object] | None = None
        self.runs = 0
        self.cleanup_path: str | None = None


class TestCorroboration:
    """The candidate's line is a hint; the verdict is a trusted launcher run's.

    The child is the unverified candidate itself, so its stderr can carry the
    launcher's exact line. A caller that trusted it would let a planted binary
    make the gate announce a sandbox failure and offer the isolation opt-out.
    ``corroborate_launcher_refusal`` therefore re-runs the REAL launcher around
    a trusted no-op with the refused spawn's own options and reports only what
    THAT run's own stderr says -- text no child wrote, and covering every
    launcher step, not just the three the boot probe mirrors.
    """

    _NOOP = "/usr/bin/true"

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        noop: str | None,
        platform: str = "linux",
        run_result: tuple[int, str] | None = None,
        run_error: BaseException | None = None,
        spawn_error: BaseException | None = None,
    ) -> _CorroborationWiring:
        """Replace the three seams the corroborator drives.

        ``sandboxed_spawn_argv`` returns fake argv/env plus a REAL temp file so
        the unlink is exercised; ``run_limited`` yields *run_result*
        or raises *run_error*; ``trusted_system_bin`` resolves ``true`` to
        *noop* (``None`` models a host without it).
        """
        w = _CorroborationWiring()

        monkeypatch.setattr(sb.sys, "platform", platform)
        monkeypatch.setattr(
            sb.platform_compat,
            "trusted_system_bin",
            lambda name: noop if name == "true" else None,
        )

        def fake_spawn(
            argv: list[str], **kwargs: object
        ) -> tuple[list[str], dict[str, str], str | None]:
            w.spawn_argv = list(argv)
            w.spawn_kwargs = kwargs
            if spawn_error is not None:
                raise spawn_error
            cleanup = tmp_path / "trusted-launcher.py"
            cleanup.write_text("# trusted launcher\n", encoding="utf-8")
            w.cleanup_path = str(cleanup)
            return ([sys.executable, str(cleanup)], {"PATH": "/usr/bin"}, str(cleanup))

        monkeypatch.setattr(sb, "sandboxed_spawn_argv", fake_spawn)

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            w.runs += 1
            if run_error is not None:
                raise run_error
            assert run_result is not None, "the trusted launcher must not run for this input"
            returncode, stderr = run_result
            return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

        monkeypatch.setattr(sb, "run_limited", fake_run)
        return w

    def test_a_forged_line_with_a_working_launcher_is_not_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(0, ""))

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 1, "the line must trigger exactly one trusted run"
        assert w.cleanup_path is not None
        assert not Path(w.cleanup_path).exists(), "the launcher temp is unlinked on exit 0"

    def test_a_seccomp_install_refusal_from_the_trusted_run_is_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A LATER launcher step than the probe mirrors: running the real
        # launcher is what lets this refusal reach a caller as a typed verdict.
        # The em dash is the launcher's own spelling at the seccomp site.
        line = "sandbox: BLOCKED \u2014 failed to install seccomp-BPF filter (prctl returned 1)"
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(1, line + "\n"))

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) == ("no_backend", line, "")
        assert w.runs == 1

    def test_a_mount_refusal_from_the_trusted_run_carries_the_mount_remedy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        line = "sandbox: BLOCKED -- making mount propagation private on / failed: errno 13"
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(1, line + "\n"))

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) == (
            "no_backend",
            line,
            sb.REMEDY_MOUNT_DENIED,
        )
        assert w.cleanup_path is not None
        assert not Path(w.cleanup_path).exists(), "the launcher temp is unlinked on exit 1"

    def test_a_trusted_run_with_no_launcher_line_is_not_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(1, "true: unexpected\n"))

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 1

    def test_a_host_state_refusal_from_the_trusted_run_stays_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The trusted launcher ran and found host state (a hardlinked
        # credential), not a sandbox failure: left as the child's own complaint.
        line = "sandbox: BLOCKED \u2014 found hardlink(s) to protected credential path(s): /x -> /y"
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(1, line + "\n"))

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 1

    def test_a_trusted_run_that_times_out_is_not_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(
            monkeypatch,
            tmp_path,
            noop=self._NOOP,
            run_error=subprocess.TimeoutExpired(cmd="true", timeout=15.0),
        )

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 1
        assert w.cleanup_path is not None
        assert not Path(w.cleanup_path).exists(), "the launcher temp is unlinked on timeout"

    def test_no_trusted_true_on_the_host_means_no_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(monkeypatch, tmp_path, noop=None)

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 0
        assert w.spawn_argv is None, "nothing is built without a trusted no-op"

    def test_no_launcher_line_means_no_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP)

        assert sb.corroborate_launcher_refusal("Error: not logged in\n") is None
        assert w.runs == 0
        assert w.spawn_argv is None

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_off_linux_a_matching_line_never_runs_a_launcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
    ) -> None:
        # The prefixes are the Linux launcher's; elsewhere a matching line can
        # only be the child's text, and a Linux-only launcher run would refuse
        # for reasons of its own.
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, platform=platform)

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) is None
        assert w.runs == 0
        assert w.spawn_argv is None

    def test_a_prebuild_sandbox_unavailable_is_its_own_triple(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The launcher could not be built for the no-op at all: the pre-spawn
        # typed error's own triple, with no trusted run.
        error = sb.SandboxUnavailableError(
            "no backend",
            kind="no_backend",
            detail="unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)",
            remedy=sb.REMEDY_USERNS_DENIED,
        )
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, spawn_error=error)

        assert sb.corroborate_launcher_refusal(_MOUNT_REFUSED) == (
            "no_backend",
            "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)",
            sb.REMEDY_USERNS_DENIED,
        )
        assert w.runs == 0, "a sandbox that cannot be built never runs the no-op"

    def test_the_refused_spawns_options_reach_the_launcher_build_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        w = self._wire(monkeypatch, tmp_path, noop=self._NOOP, run_result=(0, ""))

        sb.corroborate_launcher_refusal(
            _MOUNT_REFUSED,
            mode="standard",
            extra_hidden_dirs=("/home/dev/.aws",),
            extra_visible_dirs=("/home/dev/.config/kiro",),
        )

        assert w.spawn_argv == [self._NOOP]
        assert w.spawn_kwargs == {
            "mode": "standard",
            "strip_python_env": True,
            "extra_hidden_dirs": ("/home/dev/.aws",),
            "extra_visible_dirs": ("/home/dev/.config/kiro",),
        }


class TestOneOwnerForTheLauncherPrefixes:
    def test_the_clone_probe_classifier_reads_the_sandbox_tuple(self) -> None:
        # Two spellings of what the launcher says would drift apart the first time
        # the launcher changed; the module that generates the launcher owns the one.
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        assert clone_setup._LAUNCHER_EXIT_PREFIXES is sb.LAUNCHER_EXIT_PREFIXES


@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
class TestClassifierMatchesTheLauncher:
    """The wording lives in the launcher template; the classifier must keep up."""

    def test_every_launcher_line_is_a_known_refusal_or_the_advisory_prefix(self) -> None:
        source = sb._build_launcher_script("strict")
        spellings = set(re.findall(r'"(sandbox: [^"%{]+)', source))
        assert spellings, "the launcher template no longer spells its lines this way"
        for spelling in spellings:
            recognized = spelling.startswith(sb.LAUNCHER_EXIT_PREFIXES) or spelling.startswith(
                "sandbox: WARNING"
            )
            assert recognized, f"a new launcher line the classifier does not know: {spelling!r}"

    def test_the_mount_or_die_wording_parses_to_the_mount_step(self) -> None:
        # Rendered the way ``_mount_or_die`` renders it, with the errno the
        # container case produces.
        rendered = (
            "sandbox: BLOCKED -- %s failed: errno %d (%s). The sandbox could not "
            "establish this control, so the agent would run with the path "
            "visible. Lower sandbox_level to run without it deliberately."
        ) % ("making mount propagation private on /", errno.EACCES, "Permission denied")
        assert "_mount_or_die" in sb._build_launcher_script("strict")
        assert sb.launcher_refusal(rendered) == ("no_backend", rendered, sb.REMEDY_MOUNT_DENIED)
