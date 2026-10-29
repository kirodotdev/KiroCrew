"""``kirocrew doctor``'s install checks and install advice.

Which install owns the ``kirocrew`` command, the source checkout, leftover
kiro-cli installers, the faiss and AWS voice hints, the bare-install-command
invariant and the venv deps probe.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew import cli_doctor, extras


class TestPathLauncherOwnership:
    """`kirocrew doctor` names which install owns the `kirocrew` command.

    A gateway deliberately never takes the name from another install's working
    launcher, so the two can diverge silently: the documented Linux pairing puts
    a cli.sh wheel and a deb/rpm desktop install on one machine, and the desktop
    app has no terminal to show the decline. This is where that is visible.
    """

    def test_matching_launcher_is_reported_clean(self, monkeypatch, tmp_path, capsys) -> None:
        exe = tmp_path / "opt" / "bin" / "kirocrew"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(exe))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(exe))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "kirocrew CLI: ✅" in out
        assert "different install" not in out

    def test_matching_launcher_prints_the_symlink_resolved_spelling(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # The clean row prints the same canonical form the mismatch rows and the
        # Data Home row print, not the PATH spelling through a ``current`` link.
        versioned = tmp_path / "install" / "1.2.3"
        (versioned / "bin").mkdir(parents=True)
        exe = versioned / "bin" / "kirocrew"
        exe.write_text("")
        current = tmp_path / "install" / "current"
        make_dir_link(current, versioned)
        via_link = current / "bin" / "kirocrew"
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(via_link))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(exe))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert f"kirocrew CLI: ✅ {os.path.realpath(exe)}" in out
        assert str(via_link) not in out

    def test_divergent_launcher_names_both_paths(self, monkeypatch, tmp_path, capsys) -> None:
        wheel = tmp_path / "crew-venv" / "bin" / "kirocrew"
        wheel.parent.mkdir(parents=True)
        wheel.write_text("")
        package = tmp_path / "opt" / "KiroCrew" / "kirocrew"  # brand-ok: real /opt path
        package.parent.mkdir(parents=True)
        package.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(wheel))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(package))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        # Both sides must be named, or the user cannot tell which is which.
        # Compare like with like: the check prints realpath, and on Windows a
        # realpath can differ in form (short vs long name, case) from str(path).
        assert os.path.realpath(wheel) in out and os.path.realpath(package) in out
        assert "kirocrew setup" in out

    def test_no_launcher_on_path_is_informational(self, monkeypatch, capsys) -> None:
        """The desktop app runs its bundled backend directly, so an absent
        terminal command is a state, not a fault."""
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: None)

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⏹ not on PATH" in out
        assert "⚠" not in out

    def test_unresolvable_install_does_not_cry_wolf(self, monkeypatch, tmp_path, capsys) -> None:
        """A bare "kirocrew" sentinel is not a path, so there is nothing to
        compare and no divergence to claim."""
        found = tmp_path / "bin" / "kirocrew"
        found.parent.mkdir(parents=True)
        found.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(found))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: "kirocrew")

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "kirocrew CLI: ✅" in out


class TestToolboxPathLauncher:
    """A Toolbox install's `kirocrew` on PATH resolves to Toolbox's shared
    `toolbox-exec` dispatcher, never to this install's binary, so the row asks the
    launcher for its version instead of comparing paths."""

    def _layout(self, monkeypatch, tmp_path, *, toolbox_install=True, probe=None):
        """PATH launcher = the dispatcher file itself (no link needed: the check reads
        the realpath's basename), the running install under tools/kirocrew."""
        dispatcher = tmp_path / ".toolbox" / "tools" / "toolbox" / "1.0" / "toolbox-exec"
        dispatcher.parent.mkdir(parents=True)
        dispatcher.write_text("")
        running = tmp_path / ".toolbox" / "tools" / "kirocrew" / "9.9.9" / "bin" / "kirocrew"
        running.parent.mkdir(parents=True)
        running.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(dispatcher))
        monkeypatch.setattr("kiro_crew.agent._resolve_kirocrew_bin", lambda: str(running))
        monkeypatch.setattr("kiro_crew.env.is_toolbox_install", lambda: toolbox_install)
        # The probe goes through the sandbox chokepoint: record what it was asked to
        # wrap, and hand back a marked argv and env so the spawn can be checked.
        calls: list[list[str]] = []
        self.wrapped: list[list[str]] = []
        self.spawn_env: list[dict] = []
        cleanup = tmp_path / "launcher.tmp"
        cleanup.write_text("")
        self.cleanup = cleanup

        def fake_wrap(argv, mode="standard", **kwargs):
            calls.append(list(argv))
            return ["<sandbox>", *argv], {"SCRUBBED": "1"}, str(cleanup)

        def fake_run_limited(argv, **kwargs):
            self.wrapped.append(list(argv))
            self.spawn_env.append(kwargs.get("env"))
            if isinstance(probe, BaseException):
                raise probe
            returncode, stdout = probe
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(cli_doctor.sandbox, "sandboxed_spawn_argv", fake_wrap)
        monkeypatch.setattr(cli_doctor.sandbox, "run_limited", fake_run_limited)
        return dispatcher, calls

    def test_same_version_through_the_dispatcher_is_clean(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        from kiro_crew import version_line

        dispatcher, calls = self._layout(monkeypatch, tmp_path, probe=(0, version_line() + "\n"))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert f"kirocrew CLI: ✅ {dispatcher} (Toolbox launcher, same version)" in out
        assert "different install" not in out
        # The PATH spelling is run, since the dispatcher picks the tool from argv[0].
        assert calls == [[str(dispatcher), "--version"]]

    def test_the_probe_runs_inside_the_sandbox(self, monkeypatch, tmp_path, capsys) -> None:
        """The launcher is whatever PATH found first, which an agent can plant, so it
        is spawned wrapped, with the scrubbed env, and the wrapper file is removed."""
        from kiro_crew import version_line

        dispatcher, _ = self._layout(monkeypatch, tmp_path, probe=(0, version_line() + "\n"))

        cli_doctor._doctor_path_launcher()

        assert self.wrapped == [["<sandbox>", str(dispatcher), "--version"]]
        assert self.spawn_env == [{"SCRUBBED": "1"}]
        assert not self.cleanup.exists()

    def test_a_sandbox_that_cannot_be_built_keeps_the_warning(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        from kiro_crew import version_line

        self._layout(monkeypatch, tmp_path, probe=(0, version_line() + "\n"))

        def refuse(argv, mode="standard", **kwargs):
            raise RuntimeError("no sandbox backend")

        monkeypatch.setattr(cli_doctor.sandbox, "sandboxed_spawn_argv", refuse)

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        assert self.wrapped == []

    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param((0, "kirocrew 0.0.0-other\n"), id="different-version"),
            pytest.param((0, "toolbox: no such tool\n"), id="malformed-output"),
            pytest.param((0, ""), id="empty-output"),
            pytest.param((1, ""), id="nonzero-exit"),
            pytest.param(subprocess.TimeoutExpired(["kirocrew"], 5), id="timeout"),
            pytest.param(OSError("exec failed"), id="cannot-run"),
        ],
    )
    def test_any_other_probe_answer_keeps_the_warning(
        self, monkeypatch, tmp_path, capsys, probe
    ) -> None:
        self._layout(monkeypatch, tmp_path, probe=probe)

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        assert "kirocrew CLI: ✅" not in out

    def test_a_non_toolbox_install_is_not_probed(self, monkeypatch, tmp_path, capsys) -> None:
        from kiro_crew import version_line

        _, calls = self._layout(
            monkeypatch, tmp_path, toolbox_install=False, probe=(0, version_line() + "\n")
        )

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        assert calls == []

    def test_a_launcher_that_is_not_the_dispatcher_is_not_probed(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        from kiro_crew import version_line

        _, calls = self._layout(monkeypatch, tmp_path, probe=(0, version_line() + "\n"))
        wheel = tmp_path / "crew-venv" / "bin" / "kirocrew"
        wheel.parent.mkdir(parents=True)
        wheel.write_text("")
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda c, **kw: str(wheel))

        cli_doctor._doctor_path_launcher()

        out = capsys.readouterr().out
        assert "⚠ kirocrew CLI on PATH belongs to a different install" in out
        assert calls == [] and self.wrapped == []


class TestSourceCheckout:
    """`kirocrew doctor` Source Checkout section — stale/off-branch source tree.

    Guards _doctor_source_checkout: an editable install parked on a stale
    feature branch runs old code (merged security fixes included) while every
    other doctor section reports healthy. These tests drive the probe through
    the _git_line seam — no real repository needed.
    """

    @staticmethod
    def _fake_git(answers: dict[tuple[str, ...], str | None]):
        def fake(repo, *args):
            return answers.get(tuple(args))

        return fake

    def _repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        return tmp_path

    def test_on_default_up_to_date_passes(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "0",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "✅ main (up to date" in out
        assert "⚠️" not in out

    def test_on_default_behind_warns_with_count(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "42",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "42 commit(s) behind" in out
        assert "update + restart" in out

    def test_feature_branch_behind_warns_with_fix(self, monkeypatch, tmp_path, capsys) -> None:
        # The incident shape: gateway source parked on a feature branch for
        # days, hundreds of commits behind — doctor must name the branch, the
        # distance, and the recovery path.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "fix/some-feature",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "798",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "fix/some-feature" in out
        assert "798 commit(s) behind origin/main" in out
        assert "NOT active" in out
        assert "check out the default branch" in out

    def test_remediation_never_renders_ref_inside_a_command(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        """A hostile ref name must not become a pasteable command payload.

        Branch names come from the repository — agent-writable on this threat
        model — so a ref like ``$(touch${IFS}/tmp/pwn)`` rendered into a
        suggested ``git checkout ...`` line would execute when the operator
        pastes it. Remediation must stay prose: no line may combine a command
        word with the interpolated ref.
        """
        evil = "$(touch${IFS}/tmp/pwn)"
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): evil,
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): "3",
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        # The state is still reported (prose may name the ref) ...
        assert evil in out
        # ... but never on a line shaped like a runnable git command.
        for line in out.splitlines():
            if "git -C" in line or "git checkout" in line:
                raise AssertionError(f"pasteable command rendered: {line!r}")

    def test_on_default_failed_count_reports_could_not_check(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # rev-list failing on the default branch must NOT masquerade as a
        # verified-fresh checkout — "up to date" is a claim the probe could
        # not establish.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "✅" not in out
        assert "could not count commits behind" in out

    def test_feature_branch_unknown_distance_still_warns(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # rev-list failing (e.g. origin/main ref pruned) must not hide the
        # off-branch state itself.
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "fix/some-feature",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): "origin/main",
                    ("rev-list", "--count", "HEAD..origin/main"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "⚠️" in out
        assert "on 'fix/some-feature' — not the default branch" in out
        assert "behind" not in out.split("not the default branch")[1].splitlines()[0]

    def test_missing_origin_head_reports_branch_without_guessing(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        # No origin/HEAD → report what we know, never assume the default is
        # "main" (could mislabel a repo whose default genuinely differs).
        monkeypatch.setattr(
            cli_doctor,
            "_git_line",
            self._fake_git(
                {
                    ("rev-parse", "--abbrev-ref", "HEAD"): "develop",
                    ("rev-parse", "--abbrev-ref", "origin/HEAD"): None,
                }
            ),
        )
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "develop" in out
        assert "could not determine default branch" in out
        assert "main" not in out

    def test_not_a_git_checkout_is_not_applicable(self, monkeypatch, tmp_path, capsys) -> None:
        # Tarball installs (cloud/EC2) have no .git — mirror the update
        # handler's guard and stay quiet rather than warning.
        cli_doctor._doctor_source_checkout(tmp_path)
        out = capsys.readouterr().out
        assert "⏹ not a git checkout" in out
        assert "⚠️" not in out

    def test_git_failure_reports_could_not_check(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli_doctor, "_git_line", self._fake_git({}))
        cli_doctor._doctor_source_checkout(self._repo(tmp_path))
        out = capsys.readouterr().out
        assert "could not check" in out

    def test_git_line_returns_none_on_nonzero_exit(self, monkeypatch, tmp_path) -> None:
        import subprocess as _sp

        def fake_run(*a, **k):
            return _sp.CompletedProcess(a, 128, stdout="", stderr="fatal: not a repo")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_git_line_returns_first_line_stripped(self, monkeypatch, tmp_path) -> None:
        import subprocess as _sp

        def fake_run(*a, **k):
            return _sp.CompletedProcess(a, 0, stdout="  main  \nextra\n", stderr="")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_git_line_returns_none_on_blank_output(self, monkeypatch, tmp_path) -> None:
        """A zero exit that prints only whitespace is no answer, not an empty line."""
        import subprocess as _sp

        def fake_run(*a, **k):
            return _sp.CompletedProcess(a, 0, stdout="  \n\n", stderr="")

        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_git_bin", lambda: "/usr/bin/git")
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_git_line_returns_none_on_oserror(self, monkeypatch, tmp_path) -> None:
        def fake_run(*a, **k):
            raise OSError("git not found")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None

    def test_git_line_survives_non_utf8_output(self, monkeypatch, tmp_path) -> None:
        """A non-UTF-8 ref name must not crash doctor.

        ``text=True`` decodes strictly unless an ``errors=`` policy is given:
        a branch named with latin-1 bytes would raise ``UnicodeDecodeError``
        inside ``_git_line`` — which the OSError/SubprocessError handler does
        not catch — terminating the whole doctor run. The call passes
        ``errors="replace"`` so undecodable bytes degrade to U+FFFD instead.
        The fake below decodes with whatever policy the call supplies, so
        removing ``errors="replace"`` makes this test crash exactly as the
        real doctor would.
        """
        import subprocess as _sp

        raw = b"exp\xe9rimental\n"  # latin-1 e-acute: invalid as UTF-8

        def fake_run(argv, *a, **k):
            errors = k.get("errors")
            stdout = raw.decode("utf-8", errors=errors) if errors else raw.decode("utf-8")
            return _sp.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda _n: "/usr/bin/git"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
        line = cli_doctor._git_line(tmp_path, "rev-parse", "--abbrev-ref", "HEAD")
        assert line == "exp\ufffdrimental"

    def test_git_line_pins_git_and_returns_none_when_untrusted(self, monkeypatch, tmp_path) -> None:
        """git resolves via trusted_git_bin; a miss means no subprocess at all.

        Doctor runs with operator privileges, so a ``git`` shim planted in an
        agent-writable PATH directory must never execute: when the trusted
        resolver declines, _git_line collapses to None without spawning. When it
        resolves, the pinned absolute path -- not the bare name -- reaches argv[0].

        The resolver itself (system dirs plus the Windows install-root fallback)
        is tested in `test_platform_compat_pinned_tools`; this asserts what the doctor does
        with each OUTCOME, which is why it patches the resolver rather than the
        directories behind it.
        """
        import subprocess as _sp

        calls: list[list[str]] = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0, stdout="main\n", stderr="")

        monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)

        # Miss: no trusted git -> None, and no process spawned.
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_git_bin", lambda: None)
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") is None
        assert calls == []

        # Hit: the resolved absolute path is argv[0], never the bare "git".
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_git_bin", lambda: "/usr/bin/git")
        assert cli_doctor._git_line(tmp_path, "rev-parse", "HEAD") == "main"
        assert calls and calls[0][0] == "/usr/bin/git"


class TestCliInstallerResidue:
    """Detection of leftover kiro-cli auto-update installers in the temp dir.

    kiro-cli checks for updates on every process start, and Crew spawns a fresh
    kiro-cli per session. On Windows the running binary cannot be replaced, so
    each check leaves an installer behind that is never cleaned up (upstream
    kirodotdev/Kiro#10970). These guard the doctor surface that makes the
    resulting disk usage visible.
    """

    def _installer(self, directory: Path, name: str, size: int = 1024) -> Path:
        path = directory / name
        path.write_bytes(b"\0" * size)
        return path

    def test_scan_counts_matching_files_and_sums_bytes(self, tmp_path: Path) -> None:
        self._installer(tmp_path, "kiro-installer-2.14.0.msi", size=2048)
        self._installer(tmp_path, "kiro-installer-2.15.0.msi", size=1024)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (2, 3072)

    def test_scan_ignores_unrelated_files(self, tmp_path: Path) -> None:
        # Must not sweep in every temp file that happens to mention kiro.
        self._installer(tmp_path, "kiro-installer-2.14.0.msi")
        self._installer(tmp_path, "kiro-log.txt")
        self._installer(tmp_path, "some-other-installer.msi")
        count, _ = cli_doctor._scan_cli_installer_residue(tmp_path)
        assert count == 1

    def test_scan_ignores_directories(self, tmp_path: Path) -> None:
        # A directory whose name matches must not be counted as a reclaimable
        # file, nor make stat() sizes meaningless.
        (tmp_path / "kiro-installer-dir").mkdir()
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_is_non_recursive(self, tmp_path: Path) -> None:
        # The installer lands at the top level; descending would make the scan
        # unbounded over a shared temp dir.
        nested = tmp_path / "nested"
        nested.mkdir()
        self._installer(nested, "kiro-installer-2.14.0.msi")
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_returns_zero_for_missing_dir(self, tmp_path: Path) -> None:
        # Note: glob() on a missing directory yields nothing rather than
        # raising, so this pins the missing-dir OUTCOME, not the OSError
        # handler — that branch is covered by the unreadable-dir test below.
        assert cli_doctor._scan_cli_installer_residue(tmp_path / "gone") == (0, 0)

    def test_scan_returns_zero_for_unreadable_dir(self, tmp_path: Path, monkeypatch) -> None:
        # A temp dir the process cannot list (permissions, or a racing rmtree)
        # must degrade to "nothing found" rather than crashing the doctor run.
        def boom(self: Path, _pattern: str):  # type: ignore[no-untyped-def]
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "glob", boom)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (0, 0)

    def test_scan_skips_entry_that_races_a_delete(self, tmp_path: Path, monkeypatch) -> None:
        # The updater (or a cleanup script) can remove a file mid-scan; one
        # unreadable entry must not abort the diagnostic.
        self._installer(tmp_path, "kiro-installer-a.msi", size=512)
        self._installer(tmp_path, "kiro-installer-b.msi", size=512)
        real_stat = Path.stat

        def flaky_stat(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
            if self.name == "kiro-installer-a.msi":
                raise OSError("vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        assert cli_doctor._scan_cli_installer_residue(tmp_path) == (1, 512)

    def test_scan_stops_at_cap(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor, "_CLI_INSTALLER_SCAN_CAP", 3)
        for i in range(6):
            self._installer(tmp_path, f"kiro-installer-{i}.msi", size=10)
        count, _ = cli_doctor._scan_cli_installer_residue(tmp_path)
        assert count == 3

    def test_single_file_is_silent(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # One file can be a download still in flight — not residue.
        self._installer(tmp_path, "kiro-installer-2.14.0.msi")
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_clean_host_is_silent(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_residue_is_reported_and_recorded(self, tmp_path: Path, monkeypatch, capsys) -> None:
        self._installer(tmp_path, "kiro-installer-2.14.0.msi", size=1048576)
        self._installer(tmp_path, "kiro-installer-2.15.0.msi", size=1048576)
        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", lambda: str(tmp_path))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "kiro-cli installer residue" in out
        assert "2 in" in out
        assert "2.0 MiB" in out
        # The remedy must name the setting AND its cost, so a user is not talked
        # into silently disabling their own security updates.
        assert "app.disableAutoupdates true" in out
        assert "per-user" in out
        assert issues == ["kiro-cli installer residue in temp"]

    def test_unusable_temp_volume_does_not_crash_doctor(self, monkeypatch, capsys) -> None:
        # gettempdir() raises when no candidate temp dir is usable. A diagnostic
        # must degrade to silence rather than abort the whole doctor run with a
        # traceback on exactly the host that most needs the rest of it.
        def boom() -> str:
            raise FileNotFoundError("No usable temporary directory found")

        monkeypatch.setattr(cli_doctor.tempfile, "gettempdir", boom)
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        assert issues == []
        assert capsys.readouterr().out == ""

    def test_large_total_renders_gib(self, monkeypatch, capsys) -> None:
        # Formatting only: writing gigabytes to disk in a test is not acceptable.
        monkeypatch.setattr(
            cli_doctor, "_scan_cli_installer_residue", lambda _d: (700, 80 * 1073741824)
        )
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "80.00 GiB" in out
        # 700 is past the cap, so BOTH the count and the size are floors: the scan
        # stopped summing at the cap, so an exact-looking size would contradict
        # the "700+" beside it.
        assert "700+" in out
        assert "≥ 80.00 GiB" in out

    def test_uncapped_size_is_not_marked_as_a_floor(self, monkeypatch, capsys) -> None:
        # Below the cap the scan saw everything, so the figure is exact and must
        # NOT be hedged -- otherwise every host reads as approximate.
        monkeypatch.setattr(cli_doctor, "_scan_cli_installer_residue", lambda _d: (4, 4 * 1048576))
        issues: list[str] = []
        cli_doctor._doctor_cli_installer_residue(issues)
        out = capsys.readouterr().out
        assert "4.0 MiB" in out
        assert "≥" not in out
        assert "4+" not in out


def _report_source() -> str:
    """The source of every function that prints a row of the doctor report.

    The orchestrator plus each ``kiro_crew.doctor_checks`` family, so an invariant
    asserted over the report holds wherever a section lives.
    """
    import importlib
    import inspect
    import pkgutil

    from kiro_crew import doctor_checks

    families = [
        importlib.import_module(f"{doctor_checks.__name__}.{info.name}")
        for info in pkgutil.iter_modules(doctor_checks.__path__)
    ]
    return "\n".join(
        [inspect.getsource(cli_doctor._doctor)] + [inspect.getsource(m) for m in families]
    )


#: The install-channel guard, spelled bare in the orchestrator and through the
#: facade in a family module.
_CHANNEL_GUARD = re.compile(r"if (cli_doctor\.)?pip_install_channel_available\(\):")


def _line_above(lines: list[str], index: int) -> str:
    """The statement line above *index*, stepping over a wrapped ``print(`` opener."""
    above = index - 1
    while lines[above].strip() == "print(":
        above -= 1
    return lines[above]


class TestFaissHint:
    """The absent-faiss advice has to name the interpreter that would import it.

    A bare ``pip install faiss-cpu`` resolves to whatever ``pip`` the user's
    PATH offers, which on a packaged or minimal install is not the gateway's
    python -- so the wheel lands where this process never imports from, and the
    next doctor run prints the identical line with nothing saying the install
    missed. The command itself is rendered by
    ``extras.pip_install_command_for``, tested directly in ``test_extras.py``;
    what is guarded here is that doctor calls it instead of embedding a literal.
    ``_doctor()`` spawns subprocesses, probes the network and calls ``sys.exit``,
    so its source is read rather than run -- the same approach the WhatsApp
    call-site guard above takes.
    """

    def _source(self) -> str:
        return _report_source()

    def test_the_hint_is_rendered_for_this_interpreter(self) -> None:
        assert "pip_install_command_for('faiss-cpu')" in self._source()

    def test_no_bare_pip_command_is_printed(self) -> None:
        """The literal this section replaced. Kept as its own assertion because a
        re-added bare form would sit happily beside the correct call."""
        assert "`pip install faiss-cpu`" not in self._source()

    def test_the_renderer_names_the_running_interpreter(self) -> None:
        """Ties the call site to real output: whatever doctor prints for that
        call carries this process's own interpreter."""
        assert sys.executable in extras.pip_install_command_for("faiss-cpu")

    def test_the_command_is_printed_only_where_it_can_run(self) -> None:
        """The command names the gateway's own interpreter, so on the bundled
        desktop build running it would write into the code-signed bundle, break
        later launches and be discarded on the next app update. Naming it there
        is worse than naming nothing, which is what the dashboard's own install
        card does in the same state."""
        source = self._source()
        call = source.index("pip_install_command_for('faiss-cpu')")
        guards = [m.start() for m in _CHANNEL_GUARD.finditer(source)]

        assert guards, "the render must sit inside the guard, not beside it"
        assert min(guards) < call, "the render must sit inside the guard, not beside it"

    def test_the_bundled_interpreter_yields_no_install_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behavioural half of the guard above, so the two cannot drift."""
        monkeypatch.setattr(extras.platform_compat, "is_bundled_interpreter", lambda: True)

        assert cli_doctor.pip_install_channel_available() is False


class TestVoiceAwsHint:
    """The optional AWS voice packages need the same treatment as faiss.

    ``_doctor`` imports ``amazon_transcribe`` and ``boto3`` in this process, so a
    bare ``pip install`` on those two lines misses for exactly the reason it
    missed for faiss: it resolves to whatever ``pip`` the user's PATH offers, and
    the wheel lands where this process never imports from. ``install_hint``'s own
    docstring reserves the bare form for output "where the surrounding text
    already says which environment is meant" and routes the copied-blind case to
    ``pip_install_command`` -- which is what a doctor ``Install:`` line is.
    """

    def _source(self) -> str:
        return _report_source()

    def test_both_voice_aws_lines_name_this_interpreter(self) -> None:
        assert self._source().count("pip_install_command('voice-aws')") == 2

    def test_no_bare_install_hint_remains_in_doctor(self) -> None:
        """The form these two lines replaced. Asserted across the whole function,
        so a re-added bare hint anywhere in doctor fails here rather than only at
        the two sites this change touched."""
        assert "install_hint(" not in self._source()

    def test_the_renderer_names_the_running_interpreter(self) -> None:
        """Ties the call sites to real output: voice-aws is a declared extra, so
        the existing ``pip_install_command`` renders it."""
        assert sys.executable in extras.pip_install_command("voice-aws")

    def test_each_line_is_printed_only_where_it_can_run(self) -> None:
        """Same bundled-interpreter hazard as the faiss line: naming the gateway's
        interpreter there would write into the code-signed bundle. Both renders
        must sit inside the guard, not beside it."""
        lines = self._source().splitlines()
        renders = [i for i, ln in enumerate(lines) if "pip_install_command('voice-aws')" in ln]

        assert len(renders) == 2
        for index in renders:
            assert _CHANNEL_GUARD.search(_line_above(lines, index))


class TestDoctorPrintsNoBareInstallCommand:
    """The invariant, asserted once over the whole function.

    Every install command ``_doctor`` prints is for a module THIS process
    imports, so a bare ``pip`` can resolve to an interpreter the gateway never
    imports from, and the wheel lands out of reach. A per-site guard says
    nothing about a site that does not exist yet, so the property is asserted
    over the whole function instead: any printed line carrying a bare
    ``pip install`` fails here.
    """

    def _source(self) -> str:
        return _report_source()

    def test_no_printed_line_carries_a_bare_pip_install(self) -> None:
        """Scoped to printed lines, so the surrounding code comments that mention
        ``pip install -e`` in prose stay legal."""
        offenders = [
            line.strip()
            for line in self._source().splitlines()
            if "print(" in line and "pip install" in line
        ]

        assert offenders == []

    def test_the_editable_install_fix_names_this_interpreter_and_is_gated(self) -> None:
        lines = self._source().splitlines()
        renders = [i for i, ln in enumerate(lines) if "pip_install_command_for('-e', '.')" in ln]

        assert len(renders) == 1
        assert _CHANNEL_GUARD.search(_line_above(lines, renders[0]))

    def test_the_fts5_fix_names_this_interpreter_and_is_gated(self) -> None:
        lines = self._source().splitlines()
        renders = [
            i for i, ln in enumerate(lines) if "pip_install_command_for('pysqlite3-binary')" in ln
        ]

        assert len(renders) == 1
        assert _CHANNEL_GUARD.search(_line_above(lines, renders[0]))

    def test_the_fts5_alternative_survives_the_gate(self) -> None:
        """The one place gating must NOT hide the whole message. Where pip cannot
        run, using a different Python is the only remaining fix, so that sentence
        has to print in exactly the case the command is withheld. Checked by
        indentation: the alternative sits outside the ``if``, not inside it."""
        alternatives = [
            ln
            for ln in self._source().splitlines()
            if "Or use a Python whose SQLite" in ln and ln.startswith(" " * 12 + "print(")
        ]

        assert len(alternatives) == 1, "the fts5 alternative must print unconditionally"


class TestVenvDepsProbe:
    """The deps probe answers for the VENV, never the doctor's own process.

    ``python -c`` puts the child's CWD at ``sys.path[0]`` and inherits
    ``PYTHONPATH``, so an unisolated probe imports whatever decoy package
    sits on either route -- making the doctor's verdict describe the
    caller's environment instead of the venv under test (the false-healthy
    the isolated ``dep_sync._probe_interpreter`` closes). The decoys here
    raise on import: a probe that can still see them fails against an
    interpreter that genuinely serves the real modules, so each test proves
    the route is closed in a way that does not depend on which direction the
    decoy lies in. The probe children run a fixed read-only import with the
    cwd the code under test pins (the interpreter's own bin dir) -- nothing
    is written, so the tmp-cwd rule for file-creating children does not
    apply, and pointing them at ``tmp_path`` would test nothing.
    """

    _DEP_NAMES = ("websockets", "slack_sdk", "aiohttp")

    def _plant_raising_decoys(self, root: Path) -> Path:
        decoy = root / "decoy-path"
        for name in self._DEP_NAMES:
            pkg = decoy / name
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(
                "raise ImportError('decoy package imported')", encoding="utf-8"
            )
        return decoy

    def test_decoy_on_pythonpath_is_invisible_to_the_probe(self, tmp_path, monkeypatch) -> None:
        """PYTHONPATH entries rank ahead of site-packages, so an unisolated
        probe imports the raising decoys and misreports this healthy
        interpreter as missing its deps."""
        decoy = self._plant_raising_decoys(tmp_path)
        monkeypatch.setenv("PYTHONPATH", str(decoy))

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is True

    def test_decoy_in_the_callers_cwd_is_invisible_to_the_probe(
        self, tmp_path, monkeypatch
    ) -> None:
        """The second route: the caller's CWD lands at ``sys.path[0]`` for an
        unisolated ``python -c``, ranking the decoys above site-packages."""
        decoy = self._plant_raising_decoys(tmp_path)
        monkeypatch.chdir(decoy)

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is True

    def test_missing_modules_still_report_missing(self, monkeypatch) -> None:
        """Isolation must not soften the verdict: a probe exiting nonzero is
        exactly the missing-deps answer the doctor section exists to show."""
        monkeypatch.setattr(
            cli_doctor.dep_sync,
            "_probe_interpreter",
            lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1),
        )

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is False

    def test_a_wedged_interpreter_reports_missing(self, monkeypatch) -> None:
        """A hung venv python must surface as a deps failure, not hang the
        operator's doctor run or escape as a traceback."""

        def _hang(*a, **k):
            raise subprocess.TimeoutExpired(cmd="python", timeout=5)

        monkeypatch.setattr(cli_doctor.dep_sync, "_probe_interpreter", _hang)

        assert cli_doctor._venv_deps_ok(Path(sys.executable)) is False

    def test_an_unspawnable_interpreter_reports_missing(self, tmp_path) -> None:
        assert cli_doctor._venv_deps_ok(tmp_path / "no-such-venv" / "python") is False

    def test_the_probe_asks_the_venv_for_all_three_core_deps(self, monkeypatch) -> None:
        """Pins the probe's question itself: the decoy tests above pass any
        probe that ignores PYTHONPATH, including one that stopped importing a
        module the gateway needs."""
        seen: dict = {}

        def record(target_py, code, timeout=None):
            seen.update(target=target_py, code=code, timeout=timeout)
            return subprocess.CompletedProcess(args=[], returncode=0)

        monkeypatch.setattr(cli_doctor.dep_sync, "_probe_interpreter", record)

        assert cli_doctor._venv_deps_ok(Path("/v/bin/python")) is True
        assert seen["code"] == "import websockets, slack_sdk, aiohttp"
        assert seen["target"] == Path("/v/bin/python")
        assert seen["timeout"] == 15

    def test_the_probe_is_wired_into_the_doctor_run(self) -> None:
        """Guards the call site: every other test drives the helper directly,
        so a deleted call would leave them green while the doctor silently
        skipped the check. ``_doctor()`` spawns subprocesses and calls
        ``sys.exit``, so its source is read rather than run."""
        import inspect

        source = inspect.getsource(cli_doctor._doctor)
        assert "_venv_deps_ok(venv_py)" in source
