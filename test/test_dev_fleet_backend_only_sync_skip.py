"""A backend-only Pull + Build must not reinstall node_modules or rebuild.

Dev Fleet's sync must not append the ``npm ci`` and ``npm build + stage``
steps unconditionally on every non-edition sync, so a sync that moved only
Python still paid a full ``npm ci`` (which DELETES ``website/node_modules``
before reinstalling from an unchanged lockfile) and a full vite build that
reproduced a byte-identical bundle.

The skip decision is made POST-FETCH by the preflight step -- the only window
where ``git diff --name-only <ref> -- website/`` means "the incoming ref does
not touch the frontend" (after ``fetch`` pinned the ref, before ``merge`` made
the worktree equal to it). The preflight signals it by exiting a RESERVED code
(:data:`npm_preflight.EXIT_FRONTEND_SKIP`) that the runner trusts ONLY from the
preflight step's label; the runner then suppresses the two frontend steps,
holding the verdict in its own state -- never a file a same-UID worktree step
could forge (the security property GPT 5.6 Review flagged on the marker-file
draft).

These pin the three seams:
* the preflight exits EXIT_FRONTEND_SKIP only when :func:`_install_already_proven`
  fires;
* the runner suppresses the frontend labels when the trusted preflight step
  exits that code, WITHOUT running their node_modules transaction;
* a forged EXIT_FRONTEND_SKIP from any OTHER step is demoted to a plain failure,
  so an untrusted step cannot skip the build and ship stale assets.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import npm_preflight as np
from kiro_crew.apps.builtins.dev_fleet import sync_runner

PREFLIGHT_LABEL = "Verify dependencies"
FRONTEND = frozenset({"npm ci", "npm build + stage"})


def _tree(root: Path, *, populated: bool = True) -> str:
    nm = root / "website" / "node_modules"
    nm.mkdir(parents=True, exist_ok=True)
    if populated:
        (nm / ".package-lock.json").write_bytes(b"{}")
    return str(root)


def _fingerprint(root: Path, tree_id: str) -> None:
    """Write the build-source fingerprint the skip requires, beside static/dist."""
    d = Path(root, "src", "kiro_crew", "static", "dist")
    d.mkdir(parents=True, exist_ok=True)
    (d / np._BUILD_SOURCE_FINGERPRINT).write_text(tree_id, encoding="utf-8")


def _git(
    *,
    changed: list[str] | None = None,
    tree_id: str = "treesha_current",
    rc: int = 0,
    ls_rc: int = 0,
    status_dirty: bool = False,
    boom: Exception | None = None,
):
    """A ``git``/``npm`` stub answering the subtree diff, the skip-time
    ``git status --porcelain`` cleanliness check, the rev-parse of the incoming
    ref's website tree, and the ``npm ls --all`` integrity check.

    ``ls_rc`` 0 = complete tree; ``status_dirty`` True = an uncommitted/untracked
    website file is present at skip time."""

    def fake_run(argv, **kw):
        if boom is not None:
            raise boom
        if "ls" in argv:  # npm ls --all integrity check
            return subprocess.CompletedProcess(argv, ls_rc, b"", b"missing: x" if ls_rc else b"")
        if "status" in argv:  # skip-time worktree cleanliness (untracked included)
            out = b"?? website/src/new.tsx\n" if status_dirty else b""
            return subprocess.CompletedProcess(argv, rc, out, b"")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, rc, (tree_id + "\n").encode(), b"")
        out = "\n".join(changed or []).encode()  # diff
        return subprocess.CompletedProcess(argv, rc, out, b"")

    return fake_run


# -- the preflight emits the frontend-skip verdict only when proven --


class TestPreflightEmitsFrontendSkipOnlyWhenProven:
    def _run_main(
        self,
        monkeypatch,
        repo: str,
        changed: list[str],
        tree_id="treesha_current",
        ls_rc=0,
        status_dirty=False,
    ):
        # The probe's own npm ci must never run here; short-circuit it so only
        # the --emit-frontend-skip path is under test.
        monkeypatch.setattr(np, "probe", lambda **kw: (np.EXIT_OK, ""))
        monkeypatch.setattr(
            np.subprocess,
            "run",
            _git(changed=changed, tree_id=tree_id, ls_rc=ls_rc, status_dirty=status_dirty),
        )
        return np.main(
            [
                "--git",
                "/usr/bin/git",
                "--npm",
                "/usr/bin/npm",
                "--repo",
                repo,
                "--ref",
                "origin/main",
                "--emit-frontend-skip",
            ]
        )

    def test_emits_skip_on_a_backend_only_sync_when_the_build_is_current(
        self, tmp_path, monkeypatch
    ):
        """Unchanged subtree + populated node_modules + a build fingerprint that
        matches the incoming website/ tree -> skip both frontend steps."""
        repo = _tree(tmp_path)
        _fingerprint(tmp_path, "treesha_current")  # matches the git stub's rev-parse
        rc = self._run_main(monkeypatch, repo, changed=[], tree_id="treesha_current")
        assert rc == np.EXIT_FRONTEND_SKIP

    def test_does_not_emit_skip_when_the_build_fingerprint_is_stale(self, tmp_path, monkeypatch):
        """The stale-bundle hole: a prior frontend sync merged new source but its npm ci
        failed, so the transaction restored the OLD node_modules. The subtree
        stops changing, but static/dist was built from the OLD tree. The
        fingerprint (old) does not match the incoming website/ tree (new), so the
        build MUST run rather than ship a stale bundle."""
        repo = _tree(tmp_path)
        _fingerprint(tmp_path, "treesha_OLD_source")  # built from a different tree
        rc = self._run_main(monkeypatch, repo, changed=[], tree_id="treesha_current")
        assert rc != np.EXIT_FRONTEND_SKIP, "a stale build fingerprint must force a rebuild"

    def test_does_not_emit_skip_when_no_build_fingerprint_exists(self, tmp_path, monkeypatch):
        """A bundle built before this feature (or a stamp that failed to write)
        proves nothing about what the dist was built from -> rebuild."""
        repo = _tree(tmp_path)  # no _fingerprint() written
        rc = self._run_main(monkeypatch, repo, changed=[], tree_id="treesha_current")
        assert rc != np.EXIT_FRONTEND_SKIP

    @pytest.mark.parametrize(
        "changed",
        [
            ["website/package-lock.json"],
            ["website/package.json"],
            ["website/.npmrc"],
            ["website/src/App.tsx"],
            ["website/index.html"],
        ],
    )
    def test_does_not_emit_skip_when_the_frontend_changed(self, tmp_path, monkeypatch, changed):
        repo = _tree(tmp_path)
        _fingerprint(tmp_path, "treesha_current")  # even with a matching fingerprint
        rc = self._run_main(monkeypatch, repo, changed=changed, tree_id="treesha_current")
        assert rc != np.EXIT_FRONTEND_SKIP, changed

    def test_does_not_emit_skip_when_node_modules_is_unpopulated(self, tmp_path, monkeypatch):
        repo = _tree(tmp_path, populated=False)
        _fingerprint(tmp_path, "treesha_current")
        rc = self._run_main(monkeypatch, repo, changed=[], tree_id="treesha_current")
        assert rc != np.EXIT_FRONTEND_SKIP

    def test_does_not_emit_skip_when_the_tree_is_partial(self, tmp_path, monkeypatch):
        """The GPT partial-tree gap: node_modules is non-empty (passes "populated")
        and the source fingerprint matches, but `npm ls --all` reports missing
        packages (rc!=0). The skip MUST be refused so the sync does not succeed on
        an incomplete dependency tree."""
        repo = _tree(tmp_path)
        _fingerprint(tmp_path, "treesha_current")
        rc = self._run_main(monkeypatch, repo, changed=[], tree_id="treesha_current", ls_rc=1)
        assert rc != np.EXIT_FRONTEND_SKIP, "a partial node_modules must force a rebuild"

    def test_does_not_emit_skip_when_an_untracked_frontend_file_is_present(
        self, tmp_path, monkeypatch
    ):
        """The untracked-inputs gap: the tracked diff and the tree fingerprint
        both ignore an untracked website/ file added since the build, but a skip
        would serve a bundle that omits it. `git status --porcelain` (untracked
        included) being non-empty must refuse the skip."""
        repo = _tree(tmp_path)
        _fingerprint(tmp_path, "treesha_current")
        rc = self._run_main(
            monkeypatch, repo, changed=[], tree_id="treesha_current", status_dirty=True
        )
        assert rc != np.EXIT_FRONTEND_SKIP, "an untracked website/ file must force a rebuild"

    def test_without_the_flag_the_skip_code_is_never_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np, "probe", lambda **kw: (np.EXIT_OK, "ok"))
        called = {"n": 0}
        monkeypatch.setattr(
            np,
            "_frontend_build_already_current",
            lambda *a: (called.__setitem__("n", called["n"] + 1), None)[1],
        )
        rc = np.main(["--git", "/g", "--npm", "/n", "--repo", "/r", "--ref", "x"])
        assert rc == np.EXIT_OK
        assert called["n"] == 0, "no flag -> the skip predicate must not be consulted"

    def test_the_skip_code_is_reserved(self):
        """It must be in the reserved set or the runner would not demote a forged
        one from an untrusted step -- the whole security property."""
        assert np.EXIT_FRONTEND_SKIP in np.RESERVED_EXIT_CODES


# -- the runner suppresses the frontend steps on the trusted verdict --


def _echo_step(label, rc, tmp_path, stash=None):
    """A step that writes a sentinel file then exits rc, so a skip is observable
    by the sentinel's ABSENCE.

    Invoked through ``sys.executable`` rather than a literal ``python3``: an
    ordinary Windows install has no ``python3`` on PATH (only ``python.exe``), so
    the step failed with cmd's 9009 "not recognized" and every verdict below read
    as a plain failure. The interpreter running the suite is the one program
    guaranteed to exist on every host."""
    ran = tmp_path / f"ran-{label.replace(' ', '_')}"
    st = {
        "label": label,
        "env": {},
        "argv": [
            sys.executable,
            "-c",
            "import sys,pathlib;pathlib.Path(sys.argv[1]).write_text('x');sys.exit(int(sys.argv[2]))",
            str(ran),
            str(rc),
        ],
    }
    if stash:
        st["stash"] = stash
    return st, ran


def _run(steps, cwd):
    return sync_runner.run_steps(
        steps,
        cwd,
        frozenset({np.EXIT_FRONTEND_SKIP}),  # reserved
        PREFLIGHT_LABEL,
        np.EXIT_RESTORE_FAILED,
        np.EXIT_FRONTEND_SKIP,
        FRONTEND,
    )


class TestRunnerSuppressesFrontendOnTheTrustedVerdict:
    def test_preflight_skip_verdict_suppresses_both_frontend_steps(self, tmp_path):
        pf, pf_ran = _echo_step(PREFLIGHT_LABEL, np.EXIT_FRONTEND_SKIP, tmp_path)
        ci, ci_ran = _echo_step("npm ci", 0, tmp_path)
        build, build_ran = _echo_step("npm build + stage", 0, tmp_path)
        rc = _run([pf, ci, build], str(tmp_path))
        assert rc == 0, "the skip verdict is a success, not a failure"
        assert pf_ran.exists(), "the preflight itself must have run"
        assert not ci_ran.exists(), "npm ci must be suppressed by the skip verdict"
        assert not build_ran.exists(), "build + stage must be suppressed by the skip verdict"

    def test_preflight_ok_runs_both_frontend_steps(self, tmp_path):
        pf, _ = _echo_step(PREFLIGHT_LABEL, 0, tmp_path)
        ci, ci_ran = _echo_step("npm ci", 0, tmp_path)
        build, build_ran = _echo_step("npm build + stage", 0, tmp_path)
        rc = _run([pf, ci, build], str(tmp_path))
        assert rc == 0
        assert ci_ran.exists() and build_ran.exists(), "a normal preflight leaves the build to run"

    def test_suppressed_npm_ci_does_not_run_the_transaction(self, tmp_path):
        """The safety property: a suppressed npm ci must leave node_modules
        untouched. The transaction moves the tree aside on entry and DROPS the
        backup on a zero exit, so a suppressed step must not enter it or the tree
        is deleted."""
        repo = Path(_tree(tmp_path))
        stash = str(repo / "website" / "node_modules")
        before = sorted(p.name for p in (repo / "website" / "node_modules").iterdir())
        pf, _ = _echo_step(PREFLIGHT_LABEL, np.EXIT_FRONTEND_SKIP, tmp_path)
        ci, _ = _echo_step("npm ci", 0, tmp_path, stash=stash)
        rc = _run([pf, ci], str(tmp_path))
        assert rc == 0
        assert (
            repo / "website" / "node_modules"
        ).is_dir(), (
            "a suppressed npm ci must NOT run the transaction that would delete node_modules"
        )
        after = sorted(p.name for p in (repo / "website" / "node_modules").iterdir())
        assert after == before

    def test_an_unsuppressed_npm_ci_failure_still_restores_via_the_transaction(self, tmp_path):
        """The saving must not disarm the protection: an npm ci that actually
        runs and FAILS is still restored by the transaction."""
        repo = Path(_tree(tmp_path))
        stash = str(repo / "website" / "node_modules")
        pf, _ = _echo_step(PREFLIGHT_LABEL, 0, tmp_path)  # normal, no skip
        ci, _ = _echo_step("npm ci", 3, tmp_path, stash=stash)  # runs and fails
        rc = _run([pf, ci], str(tmp_path))
        assert rc == 3
        assert (repo / "website" / "node_modules").is_dir()


# -- an untrusted step cannot FORGE the skip verdict (the GPT security finding) --


class TestUntrustedStepCannotForgeTheSkipVerdict:
    def test_a_non_preflight_step_exiting_the_skip_code_is_demoted_to_failure(self, tmp_path):
        """A pip lifecycle script (worktree-controlled) exiting EXIT_FRONTEND_SKIP
        must NOT be honoured as a skip. demote_reserved turns it into a plain
        failure, so the sync fails loudly instead of silently shipping a stale
        build.
        """
        out = sync_runner.demote_reserved(
            np.EXIT_FRONTEND_SKIP,
            "pip install",
            frozenset({np.EXIT_FRONTEND_SKIP}),
            PREFLIGHT_LABEL,
        )
        assert out == 1, "a forged skip code from an untrusted step must be demoted to failure"

    def test_a_forged_skip_from_pip_does_not_suppress_the_frontend(self, tmp_path):
        """End to end through run_steps: a pip step forging the code fails the
        run (fail-fast), and the frontend steps are never reached -- the opposite
        of being skipped-as-success."""
        pf, _ = _echo_step(PREFLIGHT_LABEL, 0, tmp_path)  # honest preflight, no skip
        pip, _ = _echo_step("pip install", np.EXIT_FRONTEND_SKIP, tmp_path)  # forges the code
        ci, ci_ran = _echo_step("npm ci", 0, tmp_path)
        rc = _run([pf, pip, ci], str(tmp_path))
        assert rc == 1, "the forged skip code must be demoted to a failure"
        assert not ci_ran.exists(), "fail-fast: npm ci is never reached, not skipped-as-success"

    def test_the_preflight_label_keeps_its_trusted_skip_code(self, tmp_path):
        """Control for the demotion test: the SAME code from the preflight label
        is NOT demoted."""
        out = sync_runner.demote_reserved(
            np.EXIT_FRONTEND_SKIP,
            PREFLIGHT_LABEL,
            frozenset({np.EXIT_FRONTEND_SKIP}),
            PREFLIGHT_LABEL,
        )
        assert out == np.EXIT_FRONTEND_SKIP


# -- the assembled steps are the plain npm ci / build + stage, unchanged --


class TestFrontendBuildStepsShape:
    def test_both_steps_present_in_order(self):
        from kiro_crew.apps.builtins.dev_fleet import worktree_ops

        steps = worktree_ops._frontend_build_steps(
            npm_bin="/usr/bin/npm", git_bin="/usr/bin/git", repo="/repo"
        )
        labels = [label for (_argv, _mode, _env, label) in steps]
        assert labels == ["npm ci", "npm build + stage"]
        assert steps[0][0] == ["/usr/bin/npm", "ci", "--prefix", "website"]
        assert "build_and_stage" in steps[1][0][2]
        # The labels the runner is told to suppress must match these exactly.
        assert set(labels) == FRONTEND


# -- the fingerprint is stamped only when website/ was clean (GPT finding) --


class TestBuildSourceFingerprintCleanTreeGuard:
    """`_write_build_source_fingerprint` records HEAD:website, which describes the
    COMMITTED tree. The build compiles the WORKING tree, so a dirty website/ must
    NOT be stamped -- else a reverted dirty edit would let a later sync skip on a
    fingerprint that matches HEAD while the bundle was built from content no
    longer on disk."""

    def _git_status(self, *, porcelain: bytes, tree_id: bytes = b"tree_head\n", rc: int = 0):
        from kiro_crew import frontend as fe

        def fake_run(argv, **kw):
            if "status" in argv:
                return subprocess.CompletedProcess(argv, rc, porcelain, b"")
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, tree_id, b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        return fe, fake_run

    def test_clean_website_is_stamped(self, tmp_path, monkeypatch):
        fe, fake = self._git_status(porcelain=b"")
        (tmp_path / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
        monkeypatch.setattr(fe.subprocess, "run", fake)
        fe._write_build_source_fingerprint(tmp_path, "/usr/bin/git", log=lambda *_: None)
        fp = tmp_path / "src" / "kiro_crew" / "static" / "dist" / fe._BUILD_SOURCE_FINGERPRINT
        assert fp.exists() and fp.read_text().strip() == "tree_head"

    def test_dirty_website_is_not_stamped(self, tmp_path, monkeypatch):
        fe, fake = self._git_status(porcelain=b" M website/src/App.tsx\n")
        (tmp_path / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
        monkeypatch.setattr(fe.subprocess, "run", fake)
        fe._write_build_source_fingerprint(tmp_path, "/usr/bin/git", log=lambda *_: None)
        fp = tmp_path / "src" / "kiro_crew" / "static" / "dist" / fe._BUILD_SOURCE_FINGERPRINT
        assert (
            not fp.exists()
        ), "a dirty website/ must leave no fingerprint, so the next sync rebuilds"

    def test_unresolvable_status_is_not_stamped(self, tmp_path, monkeypatch):
        fe, fake = self._git_status(porcelain=b"", rc=128)
        (tmp_path / "src" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
        monkeypatch.setattr(fe.subprocess, "run", fake)
        fe._write_build_source_fingerprint(tmp_path, "/usr/bin/git", log=lambda *_: None)
        fp = tmp_path / "src" / "kiro_crew" / "static" / "dist" / fe._BUILD_SOURCE_FINGERPRINT
        assert not fp.exists()


# -- npm ls is the tree-completeness signal (GPT partial-tree finding) --


class TestFrontendTreeComplete:
    def test_complete_tree_is_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(ls_rc=0))
        assert np._frontend_tree_complete("/usr/bin/npm", str(tmp_path)) is True

    def test_partial_tree_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(ls_rc=1))
        assert np._frontend_tree_complete("/usr/bin/npm", str(tmp_path)) is False

    def test_missing_npm_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(boom=OSError("no npm")))
        assert np._frontend_tree_complete("/usr/bin/npm", str(tmp_path)) is False

    def test_timeout_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(boom=subprocess.TimeoutExpired("npm", 120)))
        assert np._frontend_tree_complete("/usr/bin/npm", str(tmp_path)) is False


class TestFrontendWorktreeClean:
    def test_clean_is_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(status_dirty=False))
        assert np._frontend_worktree_clean("/usr/bin/git", str(tmp_path)) is True

    def test_untracked_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(status_dirty=True))
        assert np._frontend_worktree_clean("/usr/bin/git", str(tmp_path)) is False

    def test_missing_git_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(np.subprocess, "run", _git(boom=OSError("no git")))
        assert np._frontend_worktree_clean("/usr/bin/git", str(tmp_path)) is False
