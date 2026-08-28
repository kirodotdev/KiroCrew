"""Unit tests for the Pull+Build pre-merge installability probe.

The probe's value is entirely in WHICH answer it gives: the exit code decides
whether the sync stops, and the classification decides which sentence the
dashboard shows in place of npm's log-file pointer. So these pin the
classification and the operator-facing message, not the plumbing.
"""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import npm_preflight as np


class TestClassify:
    """npm's own error CODES are the signal, so the verdict is the same
    whichever registry is configured."""

    @pytest.mark.parametrize(
        "blob",
        [
            "npm error code E401\nnpm error Unable to authenticate",
            "npm error code E403",
            "npm ERR! 401 Unauthorized - GET https://example.invalid/x",
            "HttpErrorAuthUnknown: Unable to authenticate, need: Bearer realm=...",
            "npm error your authentication token seems to be invalid",
            "npm error code ENEEDAUTH",
        ],
    )
    def test_auth_failures(self, blob):
        assert np.classify(blob) == np.EXIT_AUTH

    @pytest.mark.parametrize(
        "blob",
        [
            "npm error code E404\nnpm error 404 Not Found - GET .../left-pad",
            "npm error 404 Not Found",
        ],
    )
    def test_missing_version(self, blob):
        """A curated mirror answers a blocked version with 404. That is NOT an
        auth problem, and a credential refresh cannot fix it -- which is the
        whole reason it gets its own code."""
        assert np.classify(blob) == np.EXIT_UNAVAILABLE

    @pytest.mark.parametrize(
        "blob",
        [
            "npm error code ETIMEDOUT",
            "npm error network timeout at: https://example.invalid",
            "npm error code ENOTFOUND",
            "npm error code ECONNRESET",
            "npm error code EAI_AGAIN",
        ],
    )
    def test_network_failures(self, blob):
        assert np.classify(blob) == np.EXIT_TRANSIENT

    def test_unrecognized_is_not_called_transient(self):
        """Calling an unknown failure transient invites a retry that cannot
        help and hides the real cause."""
        assert np.classify("npm error something entirely new") == np.EXIT_FAILED

    def test_auth_wins_over_a_co_occurring_network_signal(self):
        """Ordering matters: a run that 401s often also logs a socket error
        afterwards, and the auth failure is the actionable half."""
        assert np.classify("npm error code E401\nnpm error code ECONNRESET") == np.EXIT_AUTH

    def test_every_nonzero_code_has_a_registry_neutral_explanation(self):
        for code in (
            np.EXIT_AUTH,
            np.EXIT_UNAVAILABLE,
            np.EXIT_TRANSIENT,
            np.EXIT_NO_SPACE,
            np.EXIT_FAILED,
        ):
            text = np.explain_exit(code)
            assert text and text[0].islower()
            for leak in ("npm", "codeartifact", "amazon", "harmony"):
                assert (
                    leak not in text.lower()
                ), "the operator-facing explanation must name no vendor or tool"


class TestFirstErrorLine:
    """npm prints its diagnosis FIRST and its log-file pointer LAST, which is
    why 'the last output line' was the least informative thing to show."""

    def test_prefers_the_diagnosis_over_the_log_pointer(self):
        blob = (
            "npm warn deprecated foo@1.0.0\n"
            "npm error code E401\n"
            "npm error Unable to authenticate, your token seems to be invalid\n"
            "npm error A complete log of this run can be found in: "
            "/home/u/.npm/_logs/2026-08-28T11_21_33_570Z-debug-0.log\n"
        )
        assert np._first_error_line(blob) == "npm error code E401"

    def test_never_returns_the_log_pointer_even_when_it_is_the_only_match(self):
        blob = (
            "npm error A complete log of this run can be found in: "
            "/home/u/.npm/_logs/x-debug-0.log\n"
        )
        assert np._first_error_line(blob) == ""

    def test_is_bounded(self):
        assert len(np._first_error_line("npm error " + "x" * 5000)) <= 400


class TestProbe:
    """The probe must isolate itself from the checkout's own node_modules."""

    def test_missing_lockfile_in_the_ref_is_a_failure_not_a_pass(self, monkeypatch):
        """Fail closed. A ref whose lockfile cannot be read is exactly the case
        where proceeding would delete node_modules for nothing."""

        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, b"", b"path does not exist")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        code, detail = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main"
        )
        assert code == np.EXIT_FAILED
        assert "package-lock.json" in detail

    def test_mirrors_the_real_step_and_skips_lifecycle_scripts(self, monkeypatch):
        """A probe that resolves differently from the install is worse than no
        probe: it either passes what will fail or fails what would have worked.
        And it must not execute the tree's install hooks."""
        seen: list[list[str]] = []

        def fake_run(argv, **kw):
            seen.append(list(argv))
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, b"{}", b"")
            return subprocess.CompletedProcess(argv, 0, b"added 1 package", b"")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        code, _ = np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main")
        assert code == np.EXIT_OK
        ci = [a for a in seen if "ci" in a]
        assert ci, seen
        assert "--ignore-scripts" in ci[0]
        # Flags that would change RESOLUTION must not be added.
        for changer in ("--legacy-peer-deps", "--force", "--omit", "--registry"):
            assert changer not in ci[0], f"{changer} makes the probe answer a different question"

    def test_must_not_use_dry_run(self, monkeypatch):
        """``--dry-run`` does not attempt retrieval, so it cannot answer this
        question at all.

        Measured against a lockfile pinning a tarball that 404s:
        ``npm ci --dry-run --ignore-scripts`` exits 0 and reports "added 1
        package", while the same command WITHOUT ``--dry-run`` exits 1 on the
        missing tarball. A dry run would therefore pass exactly the case this
        module exists to catch -- an uncached package the registry will not hand
        over -- and the sync would go on to empty node_modules regardless.
        """
        seen: list[list[str]] = []

        def fake_run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"{}", b"")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main")
        ci = [a for a in seen if "ci" in a]
        assert ci, seen
        assert "--dry-run" not in ci[0], (
            "a dry run reports success without fetching, which is the one "
            "outcome this probe must never produce"
        )

    def test_reads_the_settings_that_change_resolution(self, monkeypatch):
        """.npmrc carries resolution-affecting settings (a minimum-release-age
        gate, for one), so omitting it would make the probe disagree with the
        install it guards."""
        assert set(np._PROBE_FILES) >= {"package-lock.json", "package.json", ".npmrc"}

    def test_timeout_is_transient_not_a_hard_failure(self, monkeypatch):
        def fake_run(argv, **kw):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, b"{}", b"")
            raise subprocess.TimeoutExpired(argv, 1)

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        code, detail = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main", timeout=1
        )
        assert code == np.EXIT_TRANSIENT
        assert "timed out" in detail

    def test_leaves_no_scratch_directory_behind(self, monkeypatch, tmp_path):
        made: list[str] = []
        real_mkdtemp = np.tempfile.mkdtemp

        def spy(*a, **kw):
            # Confine the REAL directory under this test's tmp_path. probe()
            # cleans up with ignore_errors=True, so a cleanup failure is silent:
            # without this the residue would survive in the shared temp dir
            # instead of somewhere pytest reclaims, and the only signal would be
            # this test's own assertion.
            kw.setdefault("dir", str(tmp_path))
            path = real_mkdtemp(*a, **kw)
            made.append(path)
            return path

        monkeypatch.setattr(np.tempfile, "mkdtemp", spy)
        monkeypatch.setattr(
            np.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"{}", b""),
        )
        np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main")
        assert made, "probe never created a scratch directory"
        assert made[0].startswith(str(tmp_path)), (
            f"{made[0]} is outside this test's tmp_path; probe cleans up with "
            "ignore_errors=True, so a silent cleanup failure would leave residue "
            "in the shared temp directory"
        )
        assert not Path(made[0]).exists(), f"probe left {made[0]} behind"


class TestCli:
    """The exit code is the only channel the diagnosis travels on.

    stdout is shared with worktree-controlled build output, so nothing the
    gateway promotes may be parsed out of it: an install script could print any
    marker and then fail, and the dashboard would show the forgery as
    authoritative -- remedy included.
    """

    def test_success_says_so_without_a_promotable_marker(self, monkeypatch, capsys):
        monkeypatch.setattr(np, "probe", lambda **kw: (np.EXIT_OK, ""))
        rc = np.main(["--git", "g", "--npm", "n", "--repo", "r", "--ref", "x"])
        assert rc == np.EXIT_OK
        out = capsys.readouterr().out
        assert "installable" in out
        assert "::" not in out, "stdout must carry no in-band marker at all"

    @pytest.mark.parametrize(
        "code",
        [
            np.EXIT_AUTH,
            np.EXIT_UNAVAILABLE,
            np.EXIT_TRANSIENT,
            np.EXIT_NO_SPACE,
            np.EXIT_FAILED,
        ],
    )
    def test_failure_propagates_the_code_and_logs_a_plain_detail(self, monkeypatch, capsys, code):
        monkeypatch.setattr(np, "probe", lambda **kw: (code, "npm error code E401"))
        rc = np.main(["--git", "g", "--npm", "n", "--repo", "r", "--ref", "x"])
        assert rc == code, "the code IS the diagnosis"
        out = capsys.readouterr().out
        assert out.startswith(np.DETAIL_PREFIX), out
        assert "::" not in out, "stdout must carry no in-band marker at all"
        assert np.explain_exit(code) in out

    def test_the_gateway_maps_only_codes_this_module_owns(self):
        """``explain_exit`` must not invent a cause for an ordinary failure.

        Its input is the exit code of an ARBITRARY step -- ``npm ci`` exiting 1,
        a compile error, a killed process -- so falling back to "the incoming
        lockfile could not be installed" would state a cause never established.
        """
        for code in (
            np.EXIT_AUTH,
            np.EXIT_UNAVAILABLE,
            np.EXIT_TRANSIENT,
            np.EXIT_NO_SPACE,
            np.EXIT_FAILED,
            np.EXIT_TREE_AMBIGUOUS,
            np.EXIT_RESTORE_FAILED,
        ):
            assert np.explain_exit(code), code
        for foreign in (1, 2, 127, 130, 137, -1):
            assert np.explain_exit(foreign) == "", foreign
        assert np.explain_exit(np.EXIT_OK) == ""


class TestScratchFilesystemFailures:
    """The probe performs a REAL install, so its scratch directory can fill.

    Every one of its own filesystem operations is mapped to a classified code in
    one place. An uncaught OSError anywhere here would kill the step with a
    traceback and NO cause, which puts the dashboard back to showing whatever
    npm's last output line happened to be -- the exact defect this module exists
    to remove. So the coverage is per-SITE, because the class of bug is "one site
    was missed".
    """

    def test_a_full_scratch_dir_maps_to_the_out_of_space_code(self):
        assert np._os_error_code(OSError(errno.ENOSPC, "No space left")) == np.EXIT_NO_SPACE

    def test_other_os_errors_are_not_reported_as_out_of_space(self):
        assert np._os_error_code(OSError(errno.EACCES, "denied")) == np.EXIT_FAILED
        assert np._os_error_code(OSError()) == np.EXIT_FAILED

    def test_mkdtemp_failure_is_classified_not_raised(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(np.tempfile, "mkdtemp", boom)
        code, detail = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main"
        )
        assert code == np.EXIT_NO_SPACE
        assert "scratch" in detail

    def test_write_failure_during_extraction_is_classified_not_raised(self, monkeypatch):
        """The site the reviewer named: the lockfile write itself."""
        monkeypatch.setattr(
            np.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"{}", b""),
        )
        real = np.Path.write_bytes

        def boom(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(np.Path, "write_bytes", boom)
        try:
            code, detail = np.probe(
                git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main"
            )
        finally:
            monkeypatch.setattr(np.Path, "write_bytes", real)
        assert code == np.EXIT_NO_SPACE
        assert "scratch dir" in detail

    def test_git_spawn_failure_during_extraction_is_classified(self, monkeypatch):
        def boom(argv, **kw):
            raise OSError(errno.ENOENT, "No such file")

        monkeypatch.setattr(np.subprocess, "run", boom)
        code, detail = np.probe(
            git="/nope/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main"
        )
        assert code == np.EXIT_FAILED
        assert "could not run git" in detail

    def test_extraction_timeout_is_transient(self, monkeypatch):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)

        monkeypatch.setattr(np.subprocess, "run", boom)
        code, detail = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo="/repo", ref="origin/main"
        )
        assert code == np.EXIT_TRANSIENT
        assert "timed out" in detail
