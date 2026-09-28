"""Unit tests for the Pull+Build pre-merge installability probe.

The probe's value is entirely in WHICH answer it gives: the exit code decides
whether the sync stops, and the classification decides which sentence the
dashboard shows in place of npm's log-file pointer. So these pin the
classification and the operator-facing message, not the plumbing.
"""

from __future__ import annotations

import ast
import errno
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import npm_preflight as np

#: ``EDQUOT`` is not defined on every platform (Windows' CRT omits it), so it is
#: looked up rather than imported -- the same shape the production module uses to
#: build its out-of-room set, and the shape the rest of this repo's errno tests
#: use. ``None`` here means the platform cannot raise it, so the cases that need
#: it skip instead of failing to collect.
_EDQUOT = getattr(errno, "EDQUOT", None)


def _real_git(monkeypatch) -> str:
    """The host's git, ABSOLUTE, with its environment made hermetic -- or skip.

    The tests below ask a real ``git check-ignore`` because ignore resolution is
    the one thing this module refuses to reimplement, so the binary is genuine.
    Two things about how it runs are pinned. The path is what ``shutil.which``
    resolves rather than the bare name production is handed in the field: a shim
    earlier on PATH would answer for the wrong tool. And the operator's global and
    system config are pointed away, with any inherited ``GIT_*`` location override
    dropped: a ``core.hooksPath`` or ``init.templateDir`` there would put the
    operator's hooks into every ``git init`` these tests perform, and a stray
    ``GIT_DIR`` would make ``-C <repo>`` answer about some other repository.
    """
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is unavailable")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    return found


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
            path = real_mkdtemp(*a, **kw)
            made.append(path)
            return path

        monkeypatch.setattr(np.tempfile, "mkdtemp", spy)
        monkeypatch.setattr(
            np.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"{}", b""),
        )
        # A REAL repo directory, so the scratch lands inside it (see
        # TestScratchLivesOnTheRepoFilesystem) rather than in the shared temp
        # dir. probe() cleans up with ignore_errors=True, so a cleanup failure
        # is silent: confining the directory under this test's tmp_path is what
        # keeps residue somewhere pytest reclaims, leaving this test's own
        # assertion as the only signal that has to work.
        np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo=str(tmp_path), ref="origin/main")
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


def _tree(root: Path, *, populated: bool = True) -> str:
    """A checkout whose frontend half has a dependency tree. Returns the repo path."""
    nm = root / "website" / "node_modules"
    nm.mkdir(parents=True, exist_ok=True)
    if populated:
        (nm / ".package-lock.json").write_bytes(b"{}")
    return str(root)


def _git(*, changed: list[str] | None = None, rc: int = 0, boom: Exception | None = None):
    """A ``git`` stub for the subtree comparison. ``changed`` is what diff reports."""

    def fake_run(argv, **kw):
        if boom is not None:
            raise boom
        out = "\n".join(changed or []).encode()
        return subprocess.CompletedProcess(argv, rc, out, b"")

    return fake_run


class TestSkipWhenTheFrontendIsUntouched:
    """The probe pays a real install to be honest; it should pay only when the
    answer is not already on disk.

    Most syncs are backend-only and change nothing under ``website/``, so
    re-deriving "is this lockfile installable" costs a full scratch install for
    an answer the populated tree beside those same files already gives.
    """

    def test_skips_when_the_frontend_subtree_is_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(changed=[]))
        reason = np._install_already_proven("/usr/bin/git", _tree(tmp_path), "origin/main")
        assert reason and "changes nothing under" in reason
        assert "website/" in reason

    @pytest.mark.parametrize(
        "changed,why",
        [
            (["website/package-lock.json"], "a new lockfile is a new resolution"),
            (["website/package.json"], "npm ci verifies the lockfile against package.json"),
            (["website/.npmrc"], ".npmrc carries settings that change resolution"),
            (
                ["website/src/App.tsx"],
                "changed SOURCE owes a new bundle: with the three resolution inputs "
                "identical but source changed, a skipped probe lets the merge land and "
                "a failing npm ci afterwards leaves new source beside the "
                "previously-built bundle -- the gap that made comparing only the "
                "resolution files insufficient",
            ),
            (["website/index.html"], "any frontend path at all is a frontend change"),
        ],
    )
    def test_probes_when_anything_under_the_frontend_changed(
        self, tmp_path, monkeypatch, changed, why
    ):
        monkeypatch.setattr(np.subprocess, "run", _git(changed=changed))
        assert (
            np._install_already_proven("/usr/bin/git", _tree(tmp_path), "origin/main") is None
        ), why

    def test_probes_when_there_is_no_dependency_tree(self, tmp_path, monkeypatch):
        """With nothing installed there is no evidence, so a fresh checkout's
        first sync still probes -- which is when the answer is least known.

        The subtree is reported UNCHANGED here, so the missing tree is the only
        thing that can make this probe.
        """
        monkeypatch.setattr(np.subprocess, "run", _git(changed=[]))
        (tmp_path / "website").mkdir(parents=True)
        assert np._install_already_proven("/usr/bin/git", str(tmp_path), "origin/main") is None

    def test_probes_when_the_tree_is_present_but_empty(self, tmp_path, monkeypatch):
        """An interrupted `npm ci` leaves an empty directory behind, and an empty
        tree proves nothing about installability."""
        monkeypatch.setattr(np.subprocess, "run", _git(changed=[]))
        repo = _tree(tmp_path, populated=False)
        assert np._install_already_proven("/usr/bin/git", repo, "origin/main") is None

    def test_probes_when_node_modules_is_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(changed=[]))
        web = tmp_path / "website"
        web.mkdir(parents=True)
        (web / "node_modules").write_bytes(b"not a tree")
        assert np._install_already_proven("/usr/bin/git", str(tmp_path), "origin/main") is None

    @pytest.mark.parametrize(
        "kwargs,why",
        [
            ({"rc": 128}, "a failed comparison establishes nothing"),
            ({"boom": OSError("no git")}, "a missing git establishes nothing"),
            ({"boom": subprocess.TimeoutExpired("git", 60)}, "a timeout establishes nothing"),
        ],
    )
    def test_an_unanswerable_comparison_probes(self, tmp_path, monkeypatch, kwargs, why):
        """The unknown case must cost an install, never a guarantee."""
        monkeypatch.setattr(np.subprocess, "run", _git(**kwargs))
        assert (
            np._install_already_proven("/usr/bin/git", _tree(tmp_path), "origin/main") is None
        ), why

    def test_the_skip_makes_the_install_not_run_at_all(self, tmp_path, monkeypatch):
        """The point of the decision is the cost it removes, so pin that no npm
        process is started -- not merely that the verdict is OK."""
        repo = _tree(tmp_path)

        def fake_run(argv, **kw):
            if "ci" in argv:
                pytest.fail(f"npm must not run when the install is already proven: {argv}")
            if "diff" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"{}", b"")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        code, detail = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo=repo, ref="origin/main"
        )
        assert code == np.EXIT_OK
        assert "skipped the install" in detail

    def test_a_touched_frontend_still_pays_for_the_install(self, tmp_path, monkeypatch):
        """The saving must not become a hole: when the frontend DID change, the
        install runs exactly as before."""
        repo = _tree(tmp_path)
        ran: list[list[str]] = []

        def fake_run(argv, **kw):
            if "ci" in argv:
                ran.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, b"added 1 package", b"")
            if "diff" in argv:
                return subprocess.CompletedProcess(argv, 0, b"website/package-lock.json\n", b"")
            return subprocess.CompletedProcess(argv, 0, b"{}", b"")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        code, _ = np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo=repo, ref="origin/main")
        assert code == np.EXIT_OK
        assert ran, "a touched frontend must still be probed by a real install"

    def test_the_comparison_is_scoped_to_the_frontend_half(self, tmp_path, monkeypatch):
        """A backend-only sync must not be disqualified by its own backend diff,
        so the diff has to be pathspec-limited rather than repo-wide.

        This assertion is also what pins the pathspec WIDE enough, and it carries
        that alone. The stub returns whatever ``changed`` says regardless of the
        pathspec, so the source-file row in the parametrized set above would keep
        passing if the pathspec were narrowed back to the three resolution files
        -- mutation-checked, and only this test reddened. The pair is what covers
        the gap: that row pins that a source change in the diff means probe, this
        one pins that the diff is asked about the whole subtree.
        """
        seen: list[list[str]] = []

        def fake_run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(np.subprocess, "run", fake_run)
        np._install_already_proven("/usr/bin/git", _tree(tmp_path), "origin/main")
        assert seen and "--" in seen[0] and seen[0][-1] == np._FRONTEND_SUBDIR, seen

    def test_the_cli_reports_a_skip_rather_than_the_generic_pass_line(self, monkeypatch, capsys):
        """A skipped safety step must be visible in the run log. Otherwise the
        only trace is a missing pause, which nobody reads as a decision."""
        monkeypatch.setattr(np, "probe", lambda **kw: (np.EXIT_OK, "skipped the install: because"))
        rc = np.main(
            ["--git", "/usr/bin/git", "--npm", "/usr/bin/npm", "--repo", "/repo", "--ref", "r"]
        )
        assert rc == np.EXIT_OK
        out = capsys.readouterr().out
        assert "skipped the install: because" in out
        assert "incoming lockfile is installable" not in out

    def test_the_cli_still_reports_a_real_pass_when_the_install_ran(self, monkeypatch, capsys):
        monkeypatch.setattr(np, "probe", lambda **kw: (np.EXIT_OK, ""))
        assert (
            np.main(
                ["--git", "/usr/bin/git", "--npm", "/usr/bin/npm", "--repo", "/repo", "--ref", "r"]
            )
            == np.EXIT_OK
        )
        assert "incoming lockfile is installable" in capsys.readouterr().out


class TestScratchLivesOnTheRepoFilesystem:
    """The probe rehearses the real install, so it must rehearse on the real
    filesystem.

    The incident: ``TMPDIR`` was unset, so the probe installed into ``/tmp`` --
    a memory-backed filesystem mounted with a fixed inode count and shared with
    every other process on the host. Litter left there by unrelated work took
    the file count down to fewer slots than a dependency tree needs, the install
    hit ENOSPC with 33 GiB of bytes still free, and Pull+Build refused with a
    message about space. The repo's own filesystem had 259 million free inodes
    at that moment.
    """

    @staticmethod
    def _stub_git(monkeypatch):
        """Make every `git show` succeed and no `npm ci` run for real.

        Every subprocess returns 0, which includes the `git check-ignore` the scratch
        location is gated on -- so under this stub the checkout is treated as hiding the
        scratch name. That is the merged-repo case these tests are about; the gate's own
        behaviour is covered in `TestTheRepoRootIsUsedOnlyWhenGitHidesIt`.
        """
        monkeypatch.setattr(
            np.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"{}", b""),
        )

    def _scratch_paths(self, monkeypatch, repo) -> list[str]:
        made: list[str] = []
        real = np.tempfile.mkdtemp

        def spy(*a, **kw):
            path = real(*a, **kw)
            made.append(path)
            return path

        monkeypatch.setattr(np.tempfile, "mkdtemp", spy)
        self._stub_git(monkeypatch)
        np.probe(git="/usr/bin/git", npm="/usr/bin/npm", repo=str(repo), ref="origin/main")
        return made

    def test_the_install_goes_inside_the_repo_not_the_temp_dir(self, monkeypatch, tmp_path):
        """The whole point: the rehearsal shares a filesystem with the real
        `npm ci`, so its room budget is the one that actually applies."""
        repo = tmp_path / "checkout"
        repo.mkdir()
        elsewhere = tmp_path / "tmpdir"
        elsewhere.mkdir()
        # Prove the location is chosen from the repo and not merely inherited:
        # TMPDIR points somewhere else entirely, and the scratch must ignore it.
        monkeypatch.setenv("TMPDIR", str(elsewhere))
        monkeypatch.setattr(np.tempfile, "tempdir", None)

        made = self._scratch_paths(monkeypatch, repo)

        assert made, "probe never created a scratch directory"
        assert Path(made[0]).parent == repo, (
            f"scratch landed in {Path(made[0]).parent}, not the repo -- a probe on "
            "another filesystem answers a different question than the install"
        )
        assert not list(elsewhere.iterdir()), "the probe still used TMPDIR"

    def test_the_scratch_name_is_covered_by_gitignore(self, monkeypatch):
        """A killed process leaves the directory behind, and an untracked
        leftover in the checkout root fail-closes Dev Fleet's "Prune merged"
        -- the same hazard the static/dist staging entries were added for.

        Asked of GIT, on a name shaped like one ``mkdtemp`` actually produces,
        rather than by comparing the rule's text to the prefix: a rule that lost
        its trailing ``*`` still starts with the prefix but matches no generated
        directory, so a textual check can stay green over a broken ignore.

        A read-only question about THIS checkout, which is why it is asked of the
        checkout rather than a scratch repository.
        """
        root = Path(np.__file__).resolve().parents[5]
        if not (root / ".git").exists():
            pytest.skip("not a git checkout")
        git = _real_git(monkeypatch)
        generated = f"{np._SCRATCH_PREFIX}ab12cd34"
        proc = subprocess.run(
            [git, "-C", str(root), "check-ignore", "--no-index", "-q", "--", generated],
            cwd=root,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, (
            f"git does not ignore {generated!r}, so a scratch directory left by a "
            f"killed probe would make the checkout read as dirty -- add a rule "
            f"covering {np._SCRATCH_PREFIX!r} to {root / '.gitignore'}"
        )

    @pytest.mark.parametrize(
        "make_repo, why",
        [
            (lambda base: base / "missing", "a repo path that does not exist"),
            (lambda base: base / "file", "a repo path that is a file"),
        ],
    )
    def test_an_unusable_repo_falls_back_to_the_temp_dir(self, tmp_path, make_repo, why):
        """A checkout that cannot hold a scratch directory still gets a probe."""
        (tmp_path / "file").write_text("x", encoding="utf-8")
        assert np._scratch_parent(str(make_repo(tmp_path))) is None, why

    def test_a_read_only_checkout_falls_back_to_the_temp_dir(self, tmp_path, monkeypatch):
        repo = tmp_path / "checkout"
        repo.mkdir()
        monkeypatch.setattr(np.os, "access", lambda p, mode: False)
        assert np._scratch_parent(str(repo)) is None

    def test_a_writable_repo_is_used(self, tmp_path):
        repo = tmp_path / "checkout"
        repo.mkdir()
        assert np._scratch_parent(str(repo)) == str(repo)

    @pytest.mark.parametrize(
        "code, name",
        [
            (errno.ENOSPC, "a full filesystem"),
            pytest.param(
                _EDQUOT,
                "an exhausted per-user quota",
                marks=pytest.mark.skipif(
                    _EDQUOT is None, reason="EDQUOT is not defined on this platform"
                ),
            ),
        ],
    )
    def test_no_room_on_the_repo_filesystem_is_the_verdict_not_a_fallback(
        self, tmp_path, monkeypatch, code, name
    ):
        """Out of room on the repo's filesystem says the filesystem the real
        install targets has none. That IS the probe's answer, so it must not
        retry in TMPDIR: a rehearsal there would certify a filesystem the
        install never touches and pass a sync that cannot install.

        Both errnos, because a quota is how a managed host says the same thing,
        and only one of them is spelled ENOSPC.

        The checkout is a real repo carrying the ignore rule, because after
        `_scratch_name_is_ignored` the repo is the scratch host only where git
        hides the name -- so this property is stated under the condition that
        makes it apply. That condition has its own tests in
        `TestTheRepoRootIsUsedOnlyWhenGitHidesIt`.
        """
        git = _real_git(monkeypatch)
        repo = tmp_path / "checkout"
        repo.mkdir()
        subprocess.run([git, "init", "-q", str(repo)], check=True, timeout=60, cwd=str(repo))
        (repo / ".gitignore").write_text(f"/{np._SCRATCH_PREFIX}*\n", encoding="utf-8")
        calls: list[object] = []

        def boom(*a, **kw):
            calls.append(kw.get("dir"))
            raise OSError(code, name)

        monkeypatch.setattr(np.tempfile, "mkdtemp", boom)
        rc, detail = np.probe(git=git, npm="/usr/bin/npm", repo=str(repo), ref="origin/main")
        assert rc == np.EXIT_NO_SPACE
        assert "scratch" in detail
        assert calls == [str(repo)], f"{name} must not retry in TMPDIR"

    def test_a_quota_is_out_of_room_not_an_unclassified_failure(self):
        """The classifier and the fallback guard read the same set, so they
        cannot disagree about whether a condition is the target's own answer."""
        assert errno.ENOSPC in np._OUT_OF_ROOM_ERRNOS
        if _EDQUOT is None:
            pytest.skip("EDQUOT is not defined on this platform")
        assert _EDQUOT in np._OUT_OF_ROOM_ERRNOS
        assert np._os_error_code(OSError(_EDQUOT, "Disk quota exceeded")) == np.EXIT_NO_SPACE

    @pytest.mark.parametrize(
        "blob",
        [
            "npm error code ENOSPC",
            "npm error nospc ENOSPC: no space left on device",
            "npm error code EDQUOT",
            "npm error EDQUOT: disk quota exceeded, write",
        ],
    )
    def test_npm_reporting_out_of_room_reads_the_same_as_a_direct_errno(self, blob):
        """Two paths reach the same condition: a filesystem call the probe makes
        itself, and `npm ci` hitting it mid-install and printing its own diagnosis.
        They must classify alike, or an operator gets the actionable sentence for a
        quota the probe met and the generic failure for the identical quota npm met.
        """
        assert np.classify(blob) == np.EXIT_NO_SPACE, f"{blob!r} was not read as out of room"

    def test_a_host_condition_on_the_repo_does_fall_back(self, tmp_path, monkeypatch):
        """A permission or platform refusal is not an answer about the lockfile,
        so the probe still runs -- in TMPDIR."""
        repo = tmp_path / "checkout"
        repo.mkdir()
        fallback = tmp_path / "tmpdir"
        fallback.mkdir()
        real = np.tempfile.mkdtemp
        attempts: list[object] = []

        def picky(*a, **kw):
            attempts.append(kw.get("dir"))
            if kw.get("dir") == str(repo):
                raise OSError(errno.EACCES, "Permission denied")
            return real(*a, **{**kw, "dir": str(fallback)})

        monkeypatch.setattr(np.tempfile, "mkdtemp", picky)
        self._stub_git(monkeypatch)
        code, _ = np.probe(
            git="/usr/bin/git", npm="/usr/bin/npm", repo=str(repo), ref="origin/main"
        )
        assert code == np.EXIT_OK
        assert attempts == [str(repo), None], "the probe did not fall back to TMPDIR"


class TestAbandonedScratchIsSwept:
    """Moving the scratch into the repo took away the host's age-based cleanup.

    `probe` deletes its own scratch in a `finally`, so what survives is a run that never
    reached it -- a SIGKILL, an OOM kill, a reboot. In `/tmp` those leftovers were
    eventually age-cleaned by the host. The repo root is cleaned by nobody, and the
    directory name is git-ignored, so without a sweeper an abandoned `node_modules` tree
    sits there invisibly and forever -- holding exactly the bytes and file slots the next
    probe is measured against.
    """

    def _stale(self, parent, name: str, age_secs: float, *, owned: bool = True):
        d = parent / name
        (d / "node_modules" / "pkg").mkdir(parents=True)
        (d / "node_modules" / "pkg" / "index.js").write_text("//\n")
        if owned:
            (d / np._SCRATCH_MARKER).touch()
        when = time.time() - age_secs
        os.utime(d, (when, when))
        return d

    def test_a_scratch_dir_older_than_the_window_is_removed(self, tmp_path):
        gone = self._stale(tmp_path, f"{np._SCRATCH_PREFIX}killed", np._SCRATCH_STALE_SECS + 60)
        np._sweep_stale_scratch(str(tmp_path))
        assert not gone.exists(), "an abandoned scratch tree was left to accumulate"

    def test_an_unmarked_look_alike_is_never_deleted(self, tmp_path):
        """A name prefix is a convention, not proof of authorship, and the ignore
        rule this change adds keeps such a directory out of `git status` too. The
        sweep is a recursive delete in the operator's checkout root, so it acts only
        on a directory carrying the marker this module wrote."""
        foreign = self._stale(
            tmp_path, f"{np._SCRATCH_PREFIX}notmine", np._SCRATCH_STALE_SECS * 10, owned=False
        )
        np._sweep_stale_scratch(str(tmp_path))
        assert foreign.exists(), "the sweep deleted a directory it never created"

    def test_a_symlinked_marker_does_not_authorize_the_delete(self, tmp_path):
        """Otherwise whatever planted the link decides what the sweep destroys."""
        elsewhere = tmp_path / "real-marker"
        elsewhere.touch()
        d = self._stale(
            tmp_path, f"{np._SCRATCH_PREFIX}linked", np._SCRATCH_STALE_SECS * 10, owned=False
        )
        (d / np._SCRATCH_MARKER).symlink_to(elsewhere)
        np._sweep_stale_scratch(str(tmp_path))
        assert d.exists(), "a symlinked marker authorized the delete"

    def test_a_created_scratch_carries_the_marker(self, tmp_path, monkeypatch):
        """The marker has to be written where the directory is made, or the sweep
        can never reclaim anything and the leak this class exists for returns."""
        monkeypatch.setattr(np, "_scratch_parent", lambda repo: None)
        monkeypatch.setattr(np.tempfile, "mkdtemp", lambda *a, **kw: str(tmp_path / "s"))
        (tmp_path / "s").mkdir()
        path, failure = np._make_scratch("git", str(tmp_path / "checkout"))
        assert failure is None and path is not None
        assert (path / np._SCRATCH_MARKER).is_file(), "a new scratch dir has no ownership marker"

    def test_a_scratch_dir_a_running_probe_could_own_is_left_alone(self, tmp_path):
        """The sweep takes no lock, so the window is what keeps a CONCURRENT Pull + Build
        safe. A live probe is bounded by its own timeout plus its fixed-timeout helpers,
        so anything inside the window may still be in use and deleting it would break
        another operator's verification."""
        live = self._stale(tmp_path, f"{np._SCRATCH_PREFIX}inflight", 60)
        np._sweep_stale_scratch(str(tmp_path))
        assert live.exists(), "the sweep deleted a scratch dir a running probe could own"

    def test_it_touches_nothing_that_is_not_a_scratch_dir(self, tmp_path):
        """The sweep runs in the REPO ROOT, so a name filter that is too loose deletes
        the user's work rather than litter."""
        keep = tmp_path / "node_modules"
        keep.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        old = time.time() - (np._SCRATCH_STALE_SECS * 10)
        os.utime(keep, (old, old))
        os.utime(src, (old, old))
        np._sweep_stale_scratch(str(tmp_path))
        assert keep.exists() and src.exists(), "the sweep reached outside its own prefix"

    def test_a_marked_directory_without_the_prefix_is_still_not_swept(self, tmp_path, monkeypatch):
        """The prefix filter is defence in depth and has to be pinned on its own.

        Once the marker became the ownership proof, every other test in this class was
        satisfied by the marker alone -- a mutation run that removed the prefix filter
        turned nothing red. So this case gives a directory the marker and the age but NOT
        the name, which is the only shape that can fail when the filter goes missing.
        """
        d = tmp_path / "node_modules"
        d.mkdir()
        (d / np._SCRATCH_MARKER).touch()
        later = time.time() + np._SCRATCH_STALE_SECS * 10
        monkeypatch.setattr(np.time, "time", lambda: later)
        np._sweep_stale_scratch(str(tmp_path))
        assert d.exists(), "the sweep reached a directory outside its own prefix"

    def test_a_symlink_named_like_a_scratch_dir_never_destroys_its_target(
        self, tmp_path, monkeypatch
    ):
        """An OUTCOME test, and its docstring says so on purpose.

        Anything that can create a name in the repo root could otherwise choose what the
        sweep destroys. Two layers refuse that today, and this pins the result rather
        than either mechanism: `shutil.rmtree` rejects a symlink argument outright, and
        the sweep reads `is_dir`/`stat` with `follow_symlinks=False`. Removing the flags
        alone leaves this test passing -- verified by mutation -- because `rmtree` still
        refuses. The flags stay because the staleness question they answer is a different
        one: a fresh link to an old directory, and an old link to a fresh one, both give
        the wrong answer when the mtime is read through the link.

        Age comes from moving the sweep's CLOCK forward, not from back-dating the link:
        `os.utime(..., follow_symlinks=False)` raises `NotImplementedError` on Windows, and
        aging the target instead would let the entry be skipped on its own fresh mtime --
        passing for the wrong reason. Shifting the clock ages every entry at once, on
        every platform.
        """
        target = tmp_path / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("x\n")
        link = tmp_path / f"{np._SCRATCH_PREFIX}link"
        link.symlink_to(target, target_is_directory=True)
        later = time.time() + np._SCRATCH_STALE_SECS * 10
        monkeypatch.setattr(np.time, "time", lambda: later)
        np._sweep_stale_scratch(str(tmp_path))
        assert (target / "keep.txt").exists(), "the sweep destroyed a symlink's target"

    def test_an_unreadable_parent_does_not_raise(self, tmp_path, monkeypatch):
        """Housekeeping must never be the reason a verification does not run."""

        def boom(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(np.os, "scandir", boom)
        np._sweep_stale_scratch(str(tmp_path))  # must not raise

    def test_the_sweep_runs_before_the_new_scratch_is_created(self, tmp_path, monkeypatch):
        """Order is what makes the sweep a remedy and not only hygiene: the room an
        abandoned tree holds is charged to the same budget the incoming install needs,
        so it has to be returned BEFORE the probe measures it."""
        repo = tmp_path / "checkout"
        repo.mkdir()
        gone = self._stale(repo, f"{np._SCRATCH_PREFIX}killed", np._SCRATCH_STALE_SECS + 60)
        order: list[str] = []
        real_mkdtemp = np.tempfile.mkdtemp
        real_rmtree = np.shutil.rmtree

        def spy(*a, **kw):
            order.append("create")
            return real_mkdtemp(*a, **kw)

        def watched(path, **kw):
            if str(path) == str(gone):
                order.append("sweep")
            return real_rmtree(path, **kw)

        monkeypatch.setattr(np.tempfile, "mkdtemp", spy)
        monkeypatch.setattr(np.shutil, "rmtree", watched)
        monkeypatch.setattr(np, "_scratch_name_is_ignored", lambda git, repo: True)
        path, failure = np._make_scratch("/usr/bin/git", str(repo))
        assert failure is None and path is not None
        assert order[:2] == ["sweep", "create"], f"wrong order: {order}"

    def test_a_fallback_to_tmpdir_does_not_sweep_it(self, tmp_path, monkeypatch):
        """TMPDIR is the host's to age-clean and is shared with unrelated work, so a
        directory this module did not choose is not one it deletes in."""
        swept: list[str] = []
        (tmp_path / "x").mkdir()
        monkeypatch.setattr(np, "_scratch_parent", lambda repo: None)
        monkeypatch.setattr(np, "_sweep_stale_scratch", lambda parent, **kw: swept.append(parent))
        monkeypatch.setattr(np.tempfile, "mkdtemp", lambda *a, **kw: str(tmp_path / "x"))
        np._make_scratch("/usr/bin/git", str(tmp_path / "checkout"))
        assert swept == [], "the sweep ran against TMPDIR"


class TestTheRepoRootIsUsedOnlyWhenGitHidesIt:
    """The probe code and the ignore rule ship separately, so the rule may be absent.

    `npm_preflight` arrives with the installed gateway; the rule covering its scratch
    name is a commit in the checkout's own history. Right after an upgrade a fleet
    checkout parked on an older ref runs this code with no rule for it, and a probe
    killed in that window leaves an UNTRACKED directory in the checkout root -- which
    reads as dirty and fail-closes "Prune merged". That is the same
    operator-unactionable refusal this module exists to remove, arriving from the other
    side, so the repo-root choice is conditional on git actually hiding the name.
    """

    def _repo(self, tmp_path, *, ignored: bool):
        repo = tmp_path / "checkout"
        repo.mkdir()
        subprocess.run([self.git, "init", "-q", str(repo)], check=True, timeout=60, cwd=str(repo))
        if ignored:
            (repo / ".gitignore").write_text(f"/{np._SCRATCH_PREFIX}*\n", encoding="utf-8")
        return repo

    @pytest.fixture(autouse=True)
    def _needs_git(self, monkeypatch):
        """The real oracle, absolute and hermetic -- see ``_real_git``."""
        self.git = _real_git(monkeypatch)

    def test_a_checkout_carrying_the_rule_hosts_the_scratch(self, tmp_path):
        repo = self._repo(tmp_path, ignored=True)
        assert np._scratch_name_is_ignored(self.git, str(repo)) is True
        path, failure = np._make_scratch(self.git, str(repo))
        assert failure is None and path is not None
        assert path.parent == repo, "a checkout that hides the name should host the scratch"

    def test_a_checkout_without_the_rule_falls_back_to_tmpdir(self, tmp_path, monkeypatch):
        """The whole point of the gate: better to rehearse on the wrong filesystem --
        this module's previous behaviour -- than to leave a checkout reading dirty."""
        repo = self._repo(tmp_path, ignored=False)
        elsewhere = tmp_path / "tmpdir"
        elsewhere.mkdir()
        monkeypatch.setenv("TMPDIR", str(elsewhere))
        monkeypatch.setattr(np.tempfile, "tempdir", None)
        assert np._scratch_name_is_ignored(self.git, str(repo)) is False
        path, failure = np._make_scratch(self.git, str(repo))
        assert failure is None and path is not None
        assert path.parent == elsewhere, f"scratch landed in {path.parent}, not TMPDIR"

    def test_an_unanswerable_question_is_read_as_not_ignored(self, tmp_path):
        """A missing git, a timeout and a non-repo all leave the question open. The
        conservative reading costs a probe on TMPDIR; the other one costs a checkout
        that silently reads dirty."""
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert np._scratch_name_is_ignored(self.git, str(plain)) is False
        assert np._scratch_name_is_ignored(str(tmp_path / "no-such-git"), str(plain)) is False

    def test_the_sweep_still_runs_when_the_gate_sends_the_probe_to_tmpdir(
        self, tmp_path, monkeypatch
    ):
        """Litter left while the rule was absent is exactly what an un-ignored checkout
        needs cleared, so the sweep is not conditional on the gate."""
        repo = self._repo(tmp_path, ignored=False)
        gone = repo / f"{np._SCRATCH_PREFIX}killed"
        gone.mkdir()
        # Marked, because it stands in for a directory a killed PROBE created --
        # an unmarked look-alike is deliberately not sweepable and has its own test.
        (gone / np._SCRATCH_MARKER).touch()
        old = time.time() - (np._SCRATCH_STALE_SECS + 60)
        os.utime(gone, (old, old))
        elsewhere = tmp_path / "tmpdir"
        elsewhere.mkdir()
        monkeypatch.setenv("TMPDIR", str(elsewhere))
        monkeypatch.setattr(np.tempfile, "tempdir", None)
        path, failure = np._make_scratch(self.git, str(repo))
        assert failure is None and path is not None and path.parent == elsewhere
        assert not gone.exists(), "litter from the un-ignored window was left behind"

    def test_the_generated_name_is_the_one_git_is_asked_about(self, tmp_path):
        """A rule that lost its trailing `*` still starts with the prefix but matches
        no generated directory, so the question has to carry a suffix."""
        repo = self._repo(tmp_path, ignored=False)
        (repo / ".gitignore").write_text(f"/{np._SCRATCH_PREFIX}\n", encoding="utf-8")
        assert np._scratch_name_is_ignored(self.git, str(repo)) is False


class TestTheOutOfRoomMessageNamesBothBudgets:
    """The message an operator acts on has to name the budget that actually ran
    out.

    "not enough room ... free space in the temporary directory" sent the
    operator to a free-BYTES figure that read 33 GiB free, so the message looked
    wrong and the real cause -- a memory-backed filesystem's fixed file limit --
    was invisible. Same errno, two budgets.
    """

    def test_it_names_the_file_count_and_not_only_bytes(self):
        text = np.explain_exit(np.EXIT_NO_SPACE)
        assert "df -i" in text, "the message must name the free-file-count check"
        assert "df -h" in text, "the message must keep the free-bytes check too"
        assert "file" in text.lower()

    def test_it_does_not_send_the_operator_to_bytes_alone(self):
        """The regression pin. The original sentence said only "free space in the
        temporary directory", and an operator who checked exactly that saw 33 GiB
        free and concluded the message was wrong."""
        text = np.explain_exit(np.EXIT_NO_SPACE).lower()
        assert "free space in the temporary directory" not in text

    def test_it_blames_neither_filesystem_nor_one_budget(self):
        """The install writes to two filesystems that need not be the same one --
        the scratch directory and the package cache -- and either can run out of
        bytes or of file slots. Naming any single one of those four would
        reintroduce the same defect from another side, so the message names none
        and sends the operator to check them all."""
        text = np.explain_exit(np.EXIT_NO_SPACE).lower()
        for overclaim in ("memory-backed", "tmpfs", "inode", "the scratch filesystem"):
            assert overclaim not in text, (
                f"the message asserts {overclaim!r} as the cause, but which budget "
                "and which filesystem ran out is not known at this point"
            )

    def test_it_names_both_places_the_install_writes(self):
        """A full package cache raises the same errno as a full scratch, so an
        operator who only checks where the scratch lives finds nothing wrong."""
        text = np.explain_exit(np.EXIT_NO_SPACE).lower()
        assert "checkout" in text
        assert "cache" in text

    def test_it_stays_registry_and_tool_neutral(self):
        """Same contract the other explanations are held to."""
        text = np.explain_exit(np.EXIT_NO_SPACE).lower()
        for leak in ("npm", "codeartifact", "amazon", "harmony"):
            assert leak not in text


def _declared_timeouts() -> tuple[int, list[int]]:
    """Every subprocess deadline this module declares, read out of its own source.

    Returns ``probe()``'s default timeout and the fixed ``timeout=`` literals passed
    at the call sites. Reading the source is what makes the guard below hold for a
    deadline nobody has written yet: a new helper, or a raised default, changes this
    sum without anyone having to remember the sweep exists.
    """
    tree = ast.parse(Path(np.__file__).read_text(encoding="utf-8"))
    default = 0
    fixed: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "probe":
            for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if arg.arg == "timeout" and isinstance(value, ast.Constant):
                    default = int(value.value)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, int):
                        fixed.append(int(kw.value.value))
    return default, fixed


class TestTheSweepWindowStaysWiderThanAProbe:
    """The lockless sweep is safe only because no live probe can be as old as the
    window.

    A live probe's scratch is genuinely prefix-named and genuinely carries the
    ownership marker, so neither of those two guards separates it from an abandoned
    tree -- the age check alone does. That makes ``_SCRATCH_STALE_SECS`` and the
    probe's deadline a single invariant held in two places, and raising the deadline
    is a one-line change. Past the window, the sweep would delete the scratch of a
    Pull + Build still running: the very class of unactionable failure this module
    exists to remove, arriving from the inside.

    The relationship is stated in prose above the constant. Prose is not a guard.
    """

    def test_the_window_exceeds_every_deadline_the_module_declares(self):
        default, fixed = _declared_timeouts()
        assert default > 0, "probe()'s default timeout was not found in the source"
        assert fixed, "no fixed subprocess deadlines were found in the source"
        # Conservative: charge one probe with its own deadline plus every fixed
        # helper deadline, as though all of them ran back to back in one run.
        worst_case = default + sum(fixed)
        assert np._SCRATCH_STALE_SECS > worst_case, (
            f"the sweep window is {np._SCRATCH_STALE_SECS}s but one probe can run for "
            f"up to {worst_case}s ({default}s default + {sum(fixed)}s of helpers), so "
            "the sweep can delete a live probe's scratch. Raise _SCRATCH_STALE_SECS "
            "with the deadline, or the sweep needs a lock instead of a clock."
        )

    def test_the_margin_is_wide_and_not_merely_positive(self):
        """The constant's own comment says the margin is deliberately far wider than
        the bound rather than close to it, because sweeping too late only delays and
        sweeping too early destroys another operator's in-flight work. A window that
        merely cleared the bound would satisfy the test above while leaving no room
        for a probe that overruns its deadline, which is exactly what a hung child
        process does."""
        default, fixed = _declared_timeouts()
        worst_case = default + sum(fixed)
        assert np._SCRATCH_STALE_SECS >= worst_case * 4, (
            f"the window ({np._SCRATCH_STALE_SECS}s) is less than 4x the worst-case "
            f"probe lifetime ({worst_case}s); the constant documents a wide margin"
        )
