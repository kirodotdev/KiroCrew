"""Auto-improvement regressions: the clone, and the repository the agent edits.

Retargeting clears repository-scoped config, the clone is on the configured branch
before the profile is built, a non-default branch is really checked out, the stored
push destination is validated, the app's own host-side git never runs a hook, filter
or gitdir the repository planted, a failed head checkout refuses the clone, distinct
repositories get distinct workspace keys, and a long repository-scoped session key
still persists.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
    synthetic_clone_is_safe,
)

#: Windows' extended-length path prefix. ``os.readlink`` returns an absolute target with
#: it attached, and ``pathlib`` reads the prefix as part of the drive, so it survives
#: ``resolve()`` too -- it has to be stripped explicitly to compare paths across
#: platforms.
_EXTENDED_LENGTH_PREFIX = "\\\\?\\"


def _without_extended_prefix(target: str) -> str:
    """*target* with the Windows extended-length prefix removed; unchanged elsewhere."""
    if target.startswith(_EXTENDED_LENGTH_PREFIX):
        return target[len(_EXTENDED_LENGTH_PREFIX) :]
    return target


class TestRetargetClearsRepoScopedConfig:
    """Found by executing docs/system-specs/modules/auto-improvement.md against a SECOND repository.

    ``setup-clone`` rewrote ``clone``/``target_url``/``target_display`` but left
    ``branch`` untouched, so after retargeting from Kiro Crew to chess_test the config
    still named ``origin/feat/auto-improvement-app`` — a branch that does not exist in
    the new clone. The picker then showed a value with no matching option, and a run
    would try to check out a missing ref.
    """

    def test_retarget_drops_the_previous_repos_branch(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import routes, store

        monkeypatch.setattr(store, "data_dir", lambda: tmp_path)
        store.write_json_atomic(
            store.config_path(),
            {
                "target_url": "https://github.com/old/repo",
                "clone": "/tmp/old",
                "branch": "origin/feat/only-in-old",
                "scopeDiffBase": "origin/main",
                "maxCycles": 7,
            },
        )
        src = Path(routes.__file__).read_text(encoding="utf-8")
        assert 'retargeted = str(current.get("target_url") or "") != url' in src
        assert 'for stale in ("branch", "scopeDiffBase")' in src

    def test_a_same_repo_setup_keeps_the_branch(self) -> None:
        """Re-running setup on the SAME url must not wipe a deliberate branch choice."""
        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        src = Path(routes.__file__).read_text(encoding="utf-8")
        # The pop is guarded by the retarget check, not unconditional.
        assert src.index("retargeted =") < src.index('for stale in ("branch"')
        assert "if retargeted:" in src


@pytest.mark.usefixtures("synthetic_clone_is_safe")
class TestCheckoutPrecedesProfileBuild:
    """The profile resolves ``scopeDiffBase`` in its CONSTRUCTOR, so the clone must
    already be on the configured branch when it is built.

    Raised by review of this branch. Built first, HEAD is still the repo default, so
    ``scoped_relpaths(clone, base)`` diffs ``base...HEAD`` on the WRONG branch, gets an
    empty result, returns None ("no scope"), and the edit fence silently widens from
    "what this branch changed" to the whole repository — the opposite of what setting a
    diff scope is for.
    """

    def test_checkout_happens_before_build_profile(self, monkeypatch, tmp_path) -> None:
        import kiro_crew.apps.builtins.auto_improvement.profiles as profiles_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import RunSupervisor

        order: list[str] = []

        def _checkout(path, branch):
            order.append("checkout")
            return True, "ok"

        def _build(cfg):
            order.append("build_profile")
            raise ValueError("stop here — ordering is what this asserts")

        monkeypatch.setattr(clone_setup, "checkout_branch", _checkout)
        monkeypatch.setattr(profiles_mod, "build_profile", _build)

        with pytest.raises(ValueError):
            RunSupervisor()._build_driver(
                {"clone": str(tmp_path), "branch": "feat/x", "scopeDiffBase": "origin/main"}
            )
        assert order == ["checkout", "build_profile"], order

    def test_failed_checkout_refuses_to_start_even_without_a_diff_scope(
        self, monkeypatch, tmp_path
    ) -> None:
        """RAISE on ANY failed checkout, scoped or not. `checkout_branch` already tries the
        remote-tracking ref AND a local ref, so a False means the configured branch exists
        NOWHERE — starting an edit-and-push loop against whatever HEAD the clone holds would
        operate on the wrong revision. A scopeDiffBase makes it worse (mis-scoped fence) but
        the base case is already unsafe."""
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.backend.runner import RunSupervisor

        monkeypatch.setattr(clone_setup, "checkout_branch", lambda p, b: (False, "no such ref"))
        # UNSCOPED must now refuse too.
        with pytest.raises(RuntimeError, match="wrong revision"):
            RunSupervisor()._build_driver({"clone": str(tmp_path), "branch": "feat/x"})
        # SCOPED still refuses, and the message notes the mis-scoped fence.
        with pytest.raises(RuntimeError, match="scopeDiffBase"):
            RunSupervisor()._build_driver(
                {"clone": str(tmp_path), "branch": "feat/x", "scopeDiffBase": "origin/main"}
            )


class TestTheStoredPushDestinationIsValidated:
    """`origin_url` was returned VERBATIM while only the legacy `target_url` was validated.

    This is the single place the push destination is resolved for all three exits — the
    draft-PR push, the F10 direct push, and one-click commit — so an unvalidated value here
    redirects every one of them. Measured before the fix:
    `{"origin_url": "https://attacker.example.com/exfil.git"}` came back unchanged, while the
    identical string under `target_url` was correctly refused. The function's own docstring
    already claimed a hand-edited config "cannot smuggle in an arbitrary push destination";
    that promise was false for the preferred path.

    The security guidance on untrusted URL destinations asks for exactly this — allowlist the
    destination rather than trusting persisted input. Raised by the GPT review of this branch.
    """

    @pytest.fixture(autouse=True)
    def _public_dns(self, _public_dns: None) -> None:
        """Every case here takes the tests-conftest ``_public_dns`` pin.

        ``resolve_origin_url``'s identity-pinning step re-runs ``validate_target_url``,
        which (1) does a LIVE ``socket.getaddrinfo`` resolve that fails CLOSED on a
        runner DNS hiccup, and (2) shells out to ``gh config get git_protocol`` to pick
        a clone transport. An earlier copy of this fixture re-implemented only (1), so
        the four network-remote cases below ran the developer's REAL ``gh`` -- an
        authenticated CLI, spawned with the worker's cwd inherited -- on every run.
        The conftest fixture pins both seams (``_gh_prefers_ssh`` to https); this
        override exists only to make it autouse for the class, because the foreign-
        remote cases short-circuit on the allowlist before either seam and a case
        that forgets to request it would flake or spawn silently.
        """

    @pytest.mark.parametrize(
        "url",
        [
            "https://attacker.example.com/exfil.git",
            "https://evilgithub.com/o/r.git",  # suffix confusion
            "https://github.com.attacker.net/o/r.git",  # subdomain confusion
            "git@evil.com:o/r.git",
            "http://github.com/o/r.git",  # cleartext is never our transport
            "git://github.com/o/r.git",
            "https://github.com/",  # no repo path
            clone_setup.DISABLED_NO_PUSH,  # a marker, not a destination
        ],
    )
    def test_a_foreign_or_malformed_remote_yields_no_push_target(self, url) -> None:
        assert (
            clone_setup.resolve_origin_url({"origin_url": url}) == ""
        ), f"{url!r} would have become the push destination"

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:owner/repo.git",  # what setup writes when gh prefers ssh
            "https://github.com/owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "https://x-access-token:TOK@github.com/owner/repo.git",  # authenticated form
            "/tmp/upstream.git",  # local bare repo: no network host to redirect to
            "file:///tmp/upstream.git",
        ],
    )
    def test_a_legitimate_remote_still_resolves(self, url) -> None:
        """The validation must not degrade working installs to queue-only.

        `validate_target_url` accepts only `https://` INPUT, but setup persists the SSH form —
        so re-running it here would have refused every ssh-configured install's own remote.
        Measured: that regression broke 3 existing tests before the host check replaced it.

        A NETWORK remote must additionally name the same `owner/repo` as the configured
        target (host-allowlisting alone let a tampered path redirect the push — GPT review),
        so the real config shape is passed here: both keys, same repo, every transport form.
        A LOCAL path has no identity to pin and resolves on its own.
        """
        cfg = {"origin_url": url}
        if clone_setup._remote_slug(url):
            cfg["target_url"] = "https://github.com/owner/repo"
        assert clone_setup.resolve_origin_url(cfg) == url

    @pytest.mark.parametrize(
        "url",
        [
            r"C:\work\mirror\repo.git",
            "C:/work/mirror/repo.git",
            r"d:\repo.git",
        ],
    )
    def test_a_windows_drive_path_is_a_local_origin_on_windows(self, monkeypatch, url) -> None:
        """urlparse reads `C:` as a one-letter scheme; git on Windows reads it as a local path."""
        monkeypatch.setattr(clone_setup, "IS_WINDOWS", True)
        assert clone_setup.resolve_origin_url({"origin_url": url}) == url

    @pytest.mark.parametrize(
        "url",
        [
            r"C:\work\mirror\repo.git",
            "C:/work/mirror/repo.git",
            "C:repo.git",  # drive-relative: not accepted, needs an explicit separator
            "C:/",  # no path after the drive
            "x:attacker/repo",  # scp-like host `x`, not a drive path
        ],
    )
    def test_a_drive_shaped_origin_stays_refused_where_git_reads_it_as_ssh(
        self, monkeypatch, url
    ) -> None:
        """On POSIX git parses `C:path` as ssh to host `C`, so the drive form must stay refused.

        The Windows-only cases above and these POSIX cases differ only in ``IS_WINDOWS``;
        the drive-relative and bare-drive rows stay refused on Windows too.
        """
        monkeypatch.setattr(clone_setup, "IS_WINDOWS", False)
        assert clone_setup.resolve_origin_url({"origin_url": url}) == ""
        if url in ("C:repo.git", "C:/", "x:attacker/repo"):
            monkeypatch.setattr(clone_setup, "IS_WINDOWS", True)
            assert clone_setup.resolve_origin_url({"origin_url": url}) == ""


class TestTheAddressDecisionItself:
    """The SSRF screen the class above stubs out gets its own direct coverage.

    ``TestTheStoredPushDestinationIsValidated`` pins ``getaddrinfo``, so it does not
    exercise ``_host_is_blocked`` incidentally — and that incidental exercise is the
    helper's ONLY other coverage in the repo. Losing the pinned-away coverage without an
    equivalent is how
    a later edit to the screen (say, dropping the ``is_private`` predicate, or turning
    the fail-closed ``except`` into fail-open) would land silently. Same split as the
    ``public_dns`` precedent in ``test/test_meetings_providers.py``: the scheme tests
    stub resolution, and the address decision is tested on its own.
    """

    def _pin(self, monkeypatch: pytest.MonkeyPatch, result) -> None:
        if isinstance(result, Exception):

            def _resolve(*_a, **_kw):
                raise result

        else:

            def _resolve(*_a, **_kw):
                return [(2, 1, 6, "", (result, 443))]

        monkeypatch.setattr(clone_setup.socket, "getaddrinfo", _resolve)

    def test_a_public_address_is_allowed(self, monkeypatch) -> None:
        self._pin(monkeypatch, "93.184.216.34")
        assert clone_setup._host_is_blocked("github.com") is False

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # loopback
            "10.0.0.7",  # private
            "169.254.169.254",  # link-local (the cloud metadata range)
            "0.0.0.0",  # unspecified
        ],
    )
    def test_an_internal_address_is_blocked(self, monkeypatch, address) -> None:
        """An allowlisted NAME pointed at an internal ADDRESS is the rebinding case."""
        self._pin(monkeypatch, address)
        assert clone_setup._host_is_blocked("github.com") is True

    def test_a_resolver_error_fails_closed(self, monkeypatch) -> None:
        """The contract the fixture above leans on: a DNS error means BLOCKED.

        This is what makes a resolver hiccup a test-hermeticity problem and not a
        production one — pin it so the fail-closed ``except`` cannot quietly become
        fail-open.
        """
        self._pin(monkeypatch, OSError("resolver down"))
        assert clone_setup._host_is_blocked("github.com") is True


class TestANonDefaultBranchIsActuallyCheckedOut:
    """`checkout_branch` silently left the run on the DEFAULT branch.

    Same root cause as the one-click-commit fetch bug: inside a clone whose origin is
    neutralized, `git fetch origin <branch>` always exits 128 — so the fetch-failed
    fallback is the normal path, not an edge case. That fallback only looked for a LOCAL
    branch, and a fresh clone has a local branch for the default branch ONLY. Every
    non-default target therefore returned `(False, "could not fetch ...")`, and the
    caller's non-scoped path logs a warning and starts anyway: the run then discovers,
    edits and measures `main` while the operator believes it is on their branch.

    No network is needed to do this correctly — `origin/<branch>` is already in the clone
    from the initial clone. Raised by the GPT review of this branch.
    """

    @staticmethod
    def _git(*args: str, cwd: Path) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "-C", str(cwd), *args], cwd=cwd, check=True, capture_output=True, env=env
        )

    def _remote_with_two_branches(self, tmp_path: Path) -> Path:
        upstream = tmp_path / "up.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(upstream)], cwd=upstream.parent, check=True
        )
        seed = tmp_path / "seed"
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(seed)],
            cwd=seed.parent,
            check=True,
            capture_output=True,
        )
        (seed / "f.txt").write_text("on-default\n", encoding="utf-8")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-qm", "default", cwd=seed)
        self._git("branch", "-M", "main", cwd=seed)
        self._git("push", "-q", "origin", "main", cwd=seed)
        self._git("checkout", "-q", "-b", "feature", cwd=seed)
        (seed / "f.txt").write_text("on-feature\n", encoding="utf-8")
        (seed / "ONLY_ON_FEATURE.md").write_text("x\n", encoding="utf-8")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-qm", "feature", cwd=seed)
        self._git("push", "-q", "origin", "feature", cwd=seed)
        return upstream

    def _disabled_clone(self, tmp_path: Path, upstream: Path) -> Path:
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(clone)],
            cwd=clone.parent,
            check=True,
            capture_output=True,
        )
        clone_setup._disable_push(clone)  # exactly what production leaves behind
        return clone

    def test_a_remote_only_branch_is_checked_out_without_a_fetch(self, tmp_path: Path) -> None:
        upstream = self._remote_with_two_branches(tmp_path)
        clone = self._disabled_clone(tmp_path, upstream)

        # Preconditions that make this test meaningful: no LOCAL feature branch, but the
        # remote-tracking ref is present, and the origin is genuinely unreachable.
        locals_ = subprocess.run(
            ["git", "-C", str(clone), "branch", "--format=%(refname:short)"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.split()
        assert "feature" not in locals_, "a local feature branch would mask the bug"
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", "origin/feature"],
                cwd=clone,
                capture_output=True,
            ).returncode
            == 0
        ), "origin/feature must exist for the fix to have anything to use"
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "fetch", "--quiet", "origin", "feature"],
                cwd=clone,
                capture_output=True,
            ).returncode
            != 0
        ), "origin is still reachable — _disable_push regressed"

        ok, note = clone_setup.checkout_branch(clone, "origin/feature")
        assert ok is True, f"checkout refused a branch that is present in the clone: {note}"

        # The load-bearing assertion: the TREE is the feature branch's, not the default's.
        head = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        assert head == "feature", f"the run would have measured {head!r}, not 'feature'"
        assert (clone / "f.txt").read_text(encoding="utf-8").strip() == "on-feature"
        assert (clone / "ONLY_ON_FEATURE.md").is_file(), "the working tree is the wrong branch"

    def test_a_bare_branch_name_works_too(self, tmp_path: Path) -> None:
        """Config stores `origin/x`, but the bare form must resolve identically."""
        clone = self._disabled_clone(tmp_path, self._remote_with_two_branches(tmp_path))
        ok, _note = clone_setup.checkout_branch(clone, "feature")
        assert ok is True
        assert (clone / "ONLY_ON_FEATURE.md").is_file()

    def test_a_branch_on_no_remote_and_no_local_ref_still_fails(self, tmp_path: Path) -> None:
        """The fix must not turn a genuinely missing branch into a false success — that
        would put the run on the default branch with an `ok` verdict, which is worse than
        the bug it replaces."""
        clone = self._disabled_clone(tmp_path, self._remote_with_two_branches(tmp_path))
        ok, note = clone_setup.checkout_branch(clone, "origin/no-such-branch")
        assert ok is False
        assert "no-such-branch" in note

    def test_already_on_the_branch_is_a_no_op(self, tmp_path: Path) -> None:
        clone = self._disabled_clone(tmp_path, self._remote_with_two_branches(tmp_path))
        assert clone_setup.checkout_branch(clone, "main")[0] is True
        ok, note = clone_setup.checkout_branch(clone, "main")
        assert ok is True and "already on" in note


class TestRepoControlledGitHooksDoNotExecuteHostSide:
    """The agent runs sandboxed, but the app's own git commands (`add`/`commit`/`push`) run on
    the HOST as the gateway user, in the SAME worktree/clone the agent edits. So a repository
    instruction that has the auto-approved shell write a hook and point `core.hooksPath` at it
    would get that hook EXECUTED host-side — outside the sandbox — on the next commit/push. Every
    host-side git helper now injects trusted `-c core.hooksPath=<devnull> -c core.fsmonitor=false`
    overrides (on OUR argv, which beat the repo's config), so a planted hook never fires and a
    repo-set fsmonitor program is never spawned. Raised by the GPT review.
    """

    def test_a_planted_pre_commit_hook_does_not_run(self, tmp_path) -> None:
        """Behavioral, against a REAL repo: a `pre-commit` hook the repo installs must NOT
        execute when the app commits through its hardened helper."""
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv

        repo = tmp_path / "repo"
        repo.mkdir()
        sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        # A repository-controlled hook that fires on commit and drops a sentinel.
        hooks = repo / ".githooks"
        hooks.mkdir()
        sentinel = tmp_path / "PWNED"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
        hook.chmod(0o755)
        # The attacker step: point core.hooksPath at the planted hook (as an injected
        # `git config` / a checked-in `.git/config` would).
        sp.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], cwd=repo, check=True
        )
        (repo / "f.txt").write_text("x\n", encoding="utf-8")

        drv._git(["add", "-A"], repo)
        drv._git(["commit", "-q", "-m", "app commit"], repo)

        assert not sentinel.exists(), (
            "a repository-planted pre-commit hook executed host-side — core.hooksPath was not "
            "neutralized on the app's git commit"
        )
        # And the commit still landed (the override does not break normal operation).
        head = sp.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert head.returncode == 0 and head.stdout.strip(), "the hardened commit did not land"

    def test_the_pin_refuses_a_symlink_and_fails_closed(self, tmp_path) -> None:
        """The attributes pin lands INSIDE a tree the agent can write, so the agent could
        replace `.git/info/attributes` (or `info`, or `.git`) with a SYMLINK. Following it would
        let the pin write our content THROUGH the link (corrupting an arbitrary host file) or be
        aimed somewhere that re-enables a driver. The pin must (a) refuse a link rather than
        follow it, and (b) FAIL CLOSED — raise `GitSafetyError` so the git call is refused, not
        run undefended. Raised by the GPT review of `git_safety`.
        """
        import os

        from kiro_crew.apps.builtins.auto_improvement.spine import git_safety as gs

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess as sp

        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
        # A victim file OUTSIDE the repo the agent tries to clobber via a symlinked pin target.
        victim = tmp_path / "VICTIM"
        victim.write_text("original\n", encoding="utf-8")
        info = repo / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        attr = info / "attributes"
        attr.unlink(missing_ok=True)
        os.symlink(str(victim), str(attr))

        with pytest.raises(gs.GitSafetyError):
            gs.require_pinned(repo)
        assert (
            victim.read_text(encoding="utf-8") == "original\n"
        ), "the pin wrote THROUGH a symlink and corrupted a file outside the repo"

        # `git_argv` — the shared builder — must also fail closed, not return an argv.
        with pytest.raises(gs.GitSafetyError):
            gs.git_argv(repo, "add", "-A")

    def test_a_repointed_worktree_gitdir_is_refused(self, tmp_path) -> None:
        """A linked worktree's `.git` is a FILE (`gitdir: <path>`) whose CONTENTS the agent can
        rewrite. If it repoints at ANOTHER repository's gitdir, the pin would `O_TRUNC` that
        repo's `info/attributes` — an arbitrary-write primitive. Git's backpointer is
        bidirectional (the gitdir carries a `gitdir` file pointing back at THIS worktree's
        `.git`); the resolver validates that round-trip and refuses a mismatch. Raised by the
        GPT review.
        """
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.spine import git_safety as gs

        main = tmp_path / "main"
        main.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=main, check=True)
        (main / "f").write_text("x\n", encoding="utf-8")
        sp.run(["git", "-C", str(main), "add", "-A"], cwd=main, check=True)
        sp.run(
            [
                "git",
                "-C",
                str(main),
                "-c",
                "user.email=t@t.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "base",
            ],
            cwd=main,
            check=True,
        )
        wt = tmp_path / "linked"
        sp.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], cwd=main, check=True)
        # A legitimate linked worktree pins without complaint.
        gs.require_pinned(wt)

        # A second, unrelated repo whose attributes must NOT be touched.
        victim = tmp_path / "victim"
        victim.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=victim, check=True)
        (victim / ".git" / "info").mkdir(exist_ok=True)
        (victim / ".git" / "info" / "attributes").write_text("PRECIOUS\n", encoding="utf-8")

        # The agent repoints the linked worktree's `.git` file at the victim's gitdir.
        (wt / ".git").write_text(f"gitdir: {victim / '.git'}\n", encoding="utf-8")
        with pytest.raises(gs.GitSafetyError):
            gs.require_pinned(wt)
        assert (victim / ".git" / "info" / "attributes").read_text(
            encoding="utf-8"
        ) == "PRECIOUS\n", (
            "the pin truncated an unrelated repository's attributes via a repointed .git file"
        )

    def test_the_pin_survives_a_normal_repo(self, tmp_path) -> None:
        """The fail-closed path must NOT fire on a healthy repo: `require_pinned` returns
        normally and the pin file holds the expected content."""
        from kiro_crew.apps.builtins.auto_improvement.spine import git_safety as gs

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess as sp

        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
        gs.require_pinned(repo)  # must not raise
        pin = (repo / ".git" / "info" / "attributes").read_text(encoding="utf-8")
        assert (
            "-filter" in pin and " diff" in pin
        ), "the pin content is not the driver-unbinding line"

    def test_the_pin_atomically_replaces_a_hardlink(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import git_safety as gs

        repo = tmp_path / "repo"
        info = repo / ".git" / "info"
        info.mkdir(parents=True)
        external = tmp_path / "external"
        external.write_text("PRECIOUS\n", encoding="utf-8")
        pin = info / "attributes"
        os.link(external, pin)
        external_inode = external.stat().st_ino

        gs.require_pinned(repo)

        assert external.read_text(encoding="utf-8") == "PRECIOUS\n"
        assert external.stat().st_ino == external_inode
        assert pin.read_text(encoding="utf-8") == gs._ATTRIBUTES_PIN
        assert pin.stat().st_ino != external_inode
        assert pin.stat().st_nlink == 1

    def test_every_host_side_git_helper_injects_the_safe_config(self) -> None:
        """Structural: each helper that runs git over the agent-writable tree must carry the
        hook/fsmonitor overrides, so a new call site cannot quietly omit them."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup as clone_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
        from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as pw_mod
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import (
            pr_recipe as recipe_mod,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine import agent_discovery as disc_mod
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as drv
        from kiro_crew.apps.builtins.auto_improvement.spine import gate as gate_mod
        from kiro_crew.apps.builtins.auto_improvement.spine import proposer as prop_mod

        for label, fn in (
            ("driver._git", drv._git),
            ("gate._git_argv", gate_mod._git_argv),
            ("commit._git", commit_mod._git),
            ("pr_recipe.GitHubPRRecipe._git", recipe_mod.GitHubPRRecipe._git),
            # The watcher's OWN host-side git helper — missed in the first hardening pass and
            # caught by the Opus 5 review; its calls (`remote get-url`, `status`, `diff`) run
            # host-side over the agent-writable clone just like the others.
            ("pr_watchers._git", pw_mod._git),
            # The proposer stages/diffs the agent's worktree, and discovery diffs/greps the
            # clone — both host-side. Swept in for completeness.
            ("proposer._git", prop_mod._git),
            # `_capture_diff` runs `git add -A` + `diff --cached` DIRECTLY (not through `_git`,
            # for capture_output + a custom pathspec), so it needs its own pin — checking only
            # `proposer._git` let this call site slip. Raised by the Opus review.
            ("proposer.Proposer._capture_diff", prop_mod.Proposer._capture_diff),
            ("agent_discovery._git", disc_mod._git),
            # `checkout_branch`'s nested `_run` runs `checkout -B`/`fetch` host-side over the
            # clone and was the ONE helper this list omitted — so its inline `-c` pair drifted
            # from the shared config and missed the attribute-pin vector. `_run` is a closure,
            # so assert against the enclosing function's source. Raised by the Opus 5 review.
            ("clone_setup.checkout_branch", clone_mod.checkout_branch),
        ):
            src = inspect.getsource(fn)
            assert "_GIT_SAFE_CONFIG" in src, f"{label} does not inject the git safe-config"
            # AND the attributes pin: the `-c` flags alone do NOT stop an attribute-bound
            # `filter.<n>.clean` / `diff.<n>.textconv` driver (measured — a planted clean filter
            # still executed on `git add` with only hooksPath+fsmonitor set). Every helper must
            # refresh `.git/info/attributes` too, because the agent can wipe it between calls.
            # It must use the FAIL-CLOSED `require_pinned` (raises if the pin cannot be
            # established) rather than the best-effort `pin_attributes`, so a symlink-swap or an
            # unwritable gitdir REFUSES the git call instead of running it undefended.
            assert "require_pinned(" in src, (
                f"{label} does not fail-closed-pin the attributes — a repository-controlled "
                f"filter/diff driver would still execute host-side, or the pin could fail open"
            )

        # The config itself must disable BOTH hook execution and the fsmonitor exec vector.
        for mod in (drv, gate_mod, commit_mod, pw_mod, prop_mod, disc_mod, clone_mod):
            cfg = " ".join(mod._GIT_SAFE_CONFIG)
            assert "core.hooksPath=" in cfg, f"{mod.__name__} does not neutralize core.hooksPath"
            assert "core.fsmonitor=false" in cfg, f"{mod.__name__} does not disable fsmonitor"
            assert (
                "core.attributesFile=/dev/null" in cfg
            ), f"{mod.__name__} permits an external attributes path"
            assert (
                "core.excludesFile=/dev/null" in cfg
            ), f"{mod.__name__} permits an external excludes path"
            assert "push.recurseSubmodules=no" in cfg, (
                f"{mod.__name__} permits recursive submodule pushes — the egress guard only "
                f"validates the superproject origin, so a recursive push could ship unscanned "
                f"submodule objects to an attacker-controlled submodule remote"
            )

        # ONE shared definition, not a per-module copy: this finding class recurred six times
        # because each new host-side git surface re-declared its own constant and a later one
        # omitted a vector. Every module must alias the shared tuple.
        from kiro_crew.apps.builtins.auto_improvement.spine import git_safety

        for mod in (drv, gate_mod, commit_mod, pw_mod, prop_mod, disc_mod, clone_mod):
            assert mod._GIT_SAFE_CONFIG is git_safety.GIT_SAFE_CONFIG, (
                f"{mod.__name__} re-declares its own git safe-config instead of aliasing the "
                f"shared one — that drift is how a vector gets missed"
            )

    def test_a_planted_clean_filter_does_not_run(self, tmp_path) -> None:
        """Behavioral, against a REAL repo: a repository-controlled `filter.<n>.clean` driver
        bound by `.gitattributes` must NOT execute when the app stages the tree.

        This is a SEPARATE vector from hooks. Measured before fixing (git 2.47): with only
        `-c core.hooksPath=<devnull> -c core.fsmonitor=false`, a planted clean filter still
        executed on `git add` — the agent writes the driver into repo-local `.git/config`
        (`git config filter.pwn.clean …`) and binds it with an in-tree `.gitattributes`, and
        neither `core.attributesFile=<devnull>` nor `GIT_CONFIG_GLOBAL/SYSTEM=<devnull>` blocks
        it. What does: `.git/info/attributes` holding `* -filter diff`, which outranks the in-tree
        file and unbinds any driver without needing its name. `diff` is SET, not `-diff`:
        `-diff` marks every path binary and blinded this app's own credential scanner
        (`_scan_pushable_content`), which the suite caught. Raised by the GPT review.
        """
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.spine import proposer as prop_mod

        repo = tmp_path / "repo"
        repo.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
        sentinel = tmp_path / "FILTER_RAN"
        # The attack, both halves: the driver in repo-local config, the binding in-tree.
        sp.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "filter.pwn.clean",
                f"sh -c 'touch {sentinel}; cat'",
            ],
            cwd=repo,
            check=True,
        )
        (repo / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
        (repo / "f.txt").write_text("data\n", encoding="utf-8")

        # Stage through the PRODUCTION helper.
        prop_mod._git(["add", "-A"], repo)
        assert not sentinel.exists(), (
            "a repository-planted clean filter executed host-side during `git add` — the "
            "attributes pin did not unbind it"
        )

        # And it must stay blocked after the agent WIPES the pin (it can: the file lives in a
        # tree the agent writes). The helper re-pins on every call for exactly this reason.
        sentinel.unlink(missing_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("# wiped\n", encoding="utf-8")
        prop_mod._git(["add", "-A"], repo)
        assert not sentinel.exists(), (
            "wiping `.git/info/attributes` re-opened the filter vector — the pin must be "
            "refreshed before each host-side git call, not written once at setup"
        )

    def test_a_planted_clean_filter_does_not_run_in_a_linked_worktree(self, tmp_path) -> None:
        """The SAME filter attack, but staged in a linked `git worktree` — which is where
        candidates actually run.

        Git redirects a linked worktree's `info/` to the COMMON gitdir, so a pin written to the
        per-worktree `.git/worktrees/<id>/info/attributes` is NOT the file git reads — the in-tree
        `.gitattributes` binding survives and the clean filter executes on `git add`. Measured on
        git 2.47: pin in the per-worktree copy → `check-attr filter` still `pwn`; pin in the common
        `info` → `unset`. `_resolve_gitdir` must therefore return the COMMON gitdir for a linked
        worktree, not the per-worktree one. Raised by the GPT review; this asserts the behavior,
        not the code shape, so it fails RED against a per-worktree pin.
        """
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.spine import proposer as prop_mod

        main = tmp_path / "main"
        main.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=main, check=True)
        (main / "seed").write_text("x\n", encoding="utf-8")
        sp.run(["git", "-C", str(main), "add", "-A"], cwd=main, check=True)
        sp.run(
            [
                "git",
                "-C",
                str(main),
                "-c",
                "user.email=t@t.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "base",
            ],
            cwd=main,
            check=True,
        )
        wt = tmp_path / "linked"
        sp.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], cwd=main, check=True)

        sentinel = tmp_path / "WT_FILTER_RAN"
        # The driver lands in repo-local config (shared across worktrees); the binding is the
        # worktree's own in-tree `.gitattributes`.
        sp.run(
            ["git", "-C", str(wt), "config", "filter.pwn.clean", f"sh -c 'touch {sentinel}; cat'"],
            cwd=wt,
            check=True,
        )
        (wt / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
        (wt / "f.txt").write_text("data\n", encoding="utf-8")

        # Stage through the PRODUCTION helper, from inside the linked worktree.
        prop_mod._git(["add", "-A"], wt)
        assert not sentinel.exists(), (
            "a clean filter planted in a LINKED WORKTREE executed host-side during `git add` — "
            "the pin was written to the per-worktree gitdir, but git reads `info/attributes` from "
            "the COMMON gitdir, so the in-tree `.gitattributes` binding was never unbound"
        )

    def test_clone_setup_checkout_pins_the_attributes(self, tmp_path, monkeypatch) -> None:
        """`clone_setup.checkout_branch` runs `checkout -B <bare>` host-side over the clone, and
        `checkout` runs the SMUDGE filter as it writes the working tree — so a repo-planted
        `filter.<n>.smudge` bound by an in-tree `.gitattributes` would execute outside the
        sandbox as the gateway user. The helper carried only the two `-c` flags and no
        attribute pin, which the module documents do NOT stop an attribute-bound driver.

        Asserts the OBSERVABLE effect of the fix: after the production helper runs, the clone's
        `.git/info/attributes` holds the driver-unbinding pin (proving `require_pinned` fired
        on the checkout path). A pin-content assertion rather than a sentinel because whether
        git re-materializes a blob — and thus runs smudge — depends on checkout internals, so
        the sentinel is a flaky witness; the pin's presence is the deterministic one. Verified
        RED against the inline pre-fix `_run` (no pin written). Raised by the Opus 5 review."""
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup as clone_mod

        monkeypatch.setattr(clone_mod, "_push_disabled", lambda _clone: True)
        clone = tmp_path / "clone"
        clone.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=clone, check=True)
        sp.run(
            ["git", "-C", str(clone), "config", "user.email", "t@t.invalid"], cwd=clone, check=True
        )
        sp.run(["git", "-C", str(clone), "config", "user.name", "t"], cwd=clone, check=True)
        (clone / "f.txt").write_text("data\n", encoding="utf-8")
        sp.run(["git", "-C", str(clone), "add", "-A"], cwd=clone, check=True)
        sp.run(["git", "-C", str(clone), "commit", "-qm", "base"], cwd=clone, check=True)
        sp.run(["git", "-C", str(clone), "branch", "feature", "main"], cwd=clone, check=True)

        pin = clone / ".git" / "info" / "attributes"
        assert not pin.exists(), "precondition: no pin before the helper runs"

        ok, _msg = clone_mod.checkout_branch(clone, "feature", timeout_s=30)
        assert ok, "the production checkout helper failed outright"
        assert pin.is_file() and "-filter" in pin.read_text(encoding="utf-8"), (
            "clone_setup checkout did not fail-closed-pin `.git/info/attributes` — a "
            "repository-controlled smudge/textconv driver would execute host-side"
        )

    def test_the_direct_push_calls_are_also_hardened(self) -> None:
        """The driver's push helper (`_push_with_rebase`) builds its push argv inline, not
        through `_git`, and `git push` runs the `pre-push` hook — so it must carry the config
        too, on both the first attempt and the post-rebase retry."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._push_with_rebase)
        assert src.count("_GIT_SAFE_CONFIG") >= 2, (
            "a direct `git push` bypasses the hook/fsmonitor overrides — a repo pre-push hook "
            "would execute host-side"
        )

    def test_the_publish_helper_has_no_symbolic_source_default(self) -> None:
        """`src` must be REQUIRED, so no caller can publish a symbolic ref by omission.

        It shipped as `src: str = "HEAD"` "for callers that have nothing more specific". There
        were none: one production caller, which resolves the committed object and passes it.
        The default kept three arms alive that fired only for a symbolic source -- skipping the
        pre-fetch tamper check, replaying the BRANCH instead of the object, and re-reading HEAD
        after the push for the ledger. Each was the check-then-use shape this whole change
        exists to remove, reachable by anyone who later added a caller and omitted the argument.
        A default is invisible at the call site, which is why this is pinned on the SIGNATURE:
        reviving it changes nothing observable until someone omits the argument, and then it
        changes what gets published. Asked for by the first-principles review, which wanted the
        subtraction rather than a fourth guard."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        param = inspect.signature(Driver._push_with_rebase).parameters["src"]
        assert (
            param.default is inspect.Parameter.empty
        ), f"`src` has a default again ({param.default!r}) -- a symbolic source is publishable"
        body = inspect.getsource(Driver._push_with_rebase)
        assert '"HEAD"' not in body, "a `HEAD` literal is back in the publish helper"

    def test_the_replay_is_authorized_against_an_exact_tree(self) -> None:
        """The publish gate must compare TREES, never a patch identity.

        `git patch-id` ignores hunk positions -- that is what lets it recognise a change
        replayed onto a moved base -- so a commit whose added and removed lines match the
        authorized change while sitting elsewhere in the file carries the same identity. A
        tree names the exact content of every path, so relocation is a different tree.

        Pinned on the SOURCE because the failure is silent. Both forms produce a well-formed
        value, the gate keeps passing honest replays, and nothing observable changes until a
        relocated substitution arrives -- there is no behavioural test that fails if this
        regresses to a patch identity. Raised by the GPT review.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        body = inspect.getsource(D._expected_replay_tree)
        assert '"merge-tree", "--write-tree"' in body, (
            "_expected_replay_tree no longer states the expected tree from the two immutable "
            "ids, so the replay is not bound to the authorized content"
        )
        gate = inspect.getsource(D.Driver._push_with_rebase)
        assert "_expected_replay_tree(base, src" in gate, "the publish gate lost the tree check"
        assert "_commit_tree(rebased_id" in gate, "the gate no longer reads the replay's tree"
        # The ARGV element, not the word: the comments here name patch-id to explain why a
        # tree is used instead, and flagging prose would make this assertion unmaintainable.
        assert '"patch-id"' not in gate and '"patch-id"' not in body, (
            "a patch identity is back in the publish path; it ignores hunk positions, so the "
            "same lines relocated would authorize"
        )

    def test_the_post_agent_status_probes_are_hardened(self) -> None:
        """`author_bug_fix`/`author_perf_fix` run `git status --porcelain` on the HOST right
        after the agent turn to see whether it produced work. `git status` consults (and can
        SPAWN) `core.fsmonitor`, so an agent that set it to a program gets host-side execution
        outside the sandbox. Both probes must carry the safe config."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as ar

        cfg = " ".join(ar._GIT_SAFE_CONFIG)
        assert (
            "core.hooksPath=" in cfg and "core.fsmonitor=false" in cfg
        ), "agent_runner._GIT_SAFE_CONFIG does not neutralize hooks + fsmonitor"
        for fn_name in ("author_bug_fix", "author_perf_fix"):
            src = inspect.getsource(getattr(ar, fn_name))
            # Every git invocation in these functions must carry the config — assert there is no
            # bare `["git", "-C", ...]` that skipped it.
            assert 'status", "--porcelain"' in src, f"{fn_name} lost its status probe"
            assert (
                "_GIT_SAFE_CONFIG" in src
            ), f"{fn_name}'s post-agent `git status` bypasses the hook/fsmonitor overrides"

    def test_red_base_staging_does_not_dereference_a_credential_symlink(self, tmp_path) -> None:
        """`Gate._stage_test_only_base` snapshots the agent-editable worktree into a sibling
        RED tree with `shutil.copytree`. The worktree is agent-written, and a candidate may
        (within the edit allowlist) plant a test SYMLINK pointing OUT of the tree — at, say,
        `$HOME/.aws/credentials`. `copytree`'s default (`symlinks=False`) DEREFERENCES it,
        copying the secret's CONTENTS into the RED tree, where the repo's own tests/conftest
        run and could read and exfiltrate it. It must copy the link AS A LINK (`symlinks=True`).

        Behavioral, verified RED against the default `copytree`: asserts the staged copy is a
        symlink and does NOT contain the secret bytes. Raised by the GPT review."""
        import subprocess as sp

        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            TRACK_BUG,
            Candidate,
            Proposal,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.gate import Gate

        # A secret OUTSIDE the worktree — the stand-in for `$HOME/.aws/credentials`.
        secret = tmp_path / "credentials"
        secret.write_text("aws_secret_access_key = TOPSECRET\n", encoding="utf-8")

        wt = tmp_path / "wt"
        wt.mkdir()
        sp.run(["git", "init", "-q", "-b", "main", "."], cwd=wt, check=True)
        sp.run(["git", "-C", str(wt), "config", "user.email", "t@t.invalid"], cwd=wt, check=True)
        sp.run(["git", "-C", str(wt), "config", "user.name", "t"], cwd=wt, check=True)
        (wt / "src").mkdir()
        (wt / "src" / "m.py").write_text("def f():\n    return 0\n", encoding="utf-8")
        sp.run(["git", "-C", str(wt), "add", "-A"], cwd=wt, check=True)
        sp.run(["git", "-C", str(wt), "commit", "-qm", "base"], cwd=wt, check=True)
        base_sha = sp.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            cwd=wt,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        # The candidate adds a NEW test file (so RED staging proceeds) AND plants a symlink
        # to the out-of-tree secret, then commits both so they are "changed vs base".
        (wt / "tests").mkdir()
        (wt / "tests" / "test_repro.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        link = wt / "tests" / "aws_creds"
        link.symlink_to(secret)
        sp.run(["git", "-C", str(wt), "add", "-A"], cwd=wt, check=True)
        sp.run(["git", "-C", str(wt), "commit", "-qm", "candidate"], cwd=wt, check=True)

        gate = Gate.__new__(Gate)
        proposal = Proposal(
            cand_id="c1",
            candidate=Candidate(kind=TRACK_BUG, target="src/m.py::f"),
            worktree=wt,
            branch="main",
            description="",
            diff="",
        )
        staged_src = gate._stage_test_only_base(proposal, base_sha)
        base_tree = staged_src.parent
        staged_link = base_tree / "tests" / "aws_creds"

        assert staged_link.is_symlink(), (
            "the RED-base staging dereferenced an agent-planted symlink — a credential the "
            "link pointed at would have been copied into a tree the repo's tests can read"
        )
        # Copied verbatim as a link (its stored target is the original path), NOT resolved
        # into a real file that materialized the secret bytes inside the RED tree.
        #
        # Compared as PATHS with the Windows extended-length prefix stripped, not as the
        # raw string `os.readlink` returns. Windows stores an absolute symlink target as
        # `\\?\C:\...`, and neither a string comparison nor `Path.resolve()` closes that
        # gap -- `pathlib` reads `\\?\C:` as the drive and keeps it. Comparing `Path`
        # objects also gets Windows' case-insensitivity for free. The assertion still says
        # what it did: a target rewritten to point inside the RED tree is a different path,
        # and the sibling `is_symlink()` check above is what catches a dereferenced copy.
        assert (
            Path(_without_extended_prefix(os.readlink(staged_link))) == secret
        ), "the staged link's target was rewritten"


class TestDistinctRepositoriesGetDistinctWorkspaces:
    """`_slugify` maps every non-alphanumeric run to `_`, so DIFFERENT repositories collapsed
    onto one workspace key. Measured: `owner/a-b`, `owner/a_b`, `owner/a.b`, `owner/A-B`,
    `owner/a--b` and `owner/a b` all produced `owner_a_b`.

    GitHub allows both `-` and `_` in a repository name, so `owner/a-b` and `owner/a_b` are
    two real, unrelated repositories. The ledger, ruler, results and — worst — the PR QUEUE
    all hang off this key, so the two shared a queue: a manual draft could apply and push the
    FIRST repository's queued diff into the second. The same collapse applies to branches
    (`feat/x-1` vs `feat/x_1`).

    Fixed by appending a short digest of the UNSLUGGED repo (and normalized branch) to the
    key: the readable slug stays for humans, the digest carries the distinction. Raised by
    the GPT review.
    """

    def test_dash_and_underscore_repositories_do_not_share_a_workspace(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        a = store.workspace_key({"target_display": "owner/a-b"})
        b = store.workspace_key({"target_display": "owner/a_b"})
        assert a != b, f"two different repositories share the workspace {a!r}"

    def test_every_measured_collision_is_now_distinct(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        names = ["owner/a-b", "owner/a_b", "owner/a.b", "owner/A-B", "owner/a--b", "owner/a b"]
        keys = [store.workspace_key({"target_display": n}) for n in names]
        # `owner/A-B` vs `owner/a-b` is the one pair that SHOULD collide: GitHub repository
        # names are case-insensitive, so those are the same repository.
        assert len(set(keys)) == len(names) - 1, f"still colliding: {sorted(set(keys))}"

    def test_branches_are_distinguished_too(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        a = store.workspace_key({"target_display": "o/r", "branch": "feat/x-1"})
        b = store.workspace_key({"target_display": "o/r", "branch": "feat/x_1"})
        assert a != b, f"two different branches share the workspace {a!r}"

    def test_the_key_is_stable_and_filesystem_safe(self) -> None:
        """It names a directory and is read on every path lookup, so it must not drift
        between calls and must not contain path syntax."""
        import re

        from kiro_crew.apps.builtins.auto_improvement.backend import store

        cfg = {"target_display": "owner/repo.name", "branch": "origin/feat/x"}
        first = store.workspace_key(cfg)
        assert first == store.workspace_key(cfg), "the key is not stable across calls"
        assert re.fullmatch(r"[a-z0-9_]+", first), f"key is not filesystem-safe: {first!r}"
        # The readable part survives — a directory a human cannot recognize is its own bug.
        assert "owner_repo_name" in first, f"key lost its readable prefix: {first!r}"

    def test_the_remote_prefix_still_normalizes(self) -> None:
        """`origin/main` and a local `main` must remain ONE workspace — that is existing,
        deliberate behavior and the digest must not break it."""
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        assert store.workspace_key(
            {"target_display": "o/r", "branch": "origin/main"}
        ) == store.workspace_key({"target_display": "o/r", "branch": "main"})


class TestAFailedHeadCheckoutRefusesTheClone:
    """`setup_isolated_clone` logged a failed `git checkout <head-branch>` at DEBUG and
    carried on, leaving the clone on whatever the shared clone's HEAD was — normally the
    BASE branch.

    Every other failure in that function fails closed (a failed clone returns an error, an
    un-neutralizable origin deletes the tree and returns an error). This one did not, and it
    is the same class of harm: the watcher then reads the base tree, "fixes" code that is not
    what the PR changed, and exports a patch computed against the wrong revision. Measured on
    a real repo: after `git checkout <missing-branch>` the clone still reports HEAD `main`
    with the base file content, so nothing downstream can tell it got the wrong tree.

    This is reachable, not hypothetical: the shared clone is reset by the loop's own
    `_reset_provisional`/`_discard_staged` paths, so a generated bug-PR branch it once held
    can be absent by the time a watcher clones from it. Raised by the GPT review.
    """

    def test_a_missing_head_branch_is_an_error_not_a_silent_base_checkout(self, tmp_path) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend.pr_watchers import (
            setup_isolated_clone,
        )

        shared = tmp_path / "shared"
        shared.mkdir()

        def _git(*args, cwd=shared):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

        _git("init", "-q")
        _git("config", "user.email", "t@example.invalid")
        _git("config", "user.name", "T")
        (shared / "f.txt").write_text("base\n", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-qm", "base")

        dest = tmp_path / "iso"
        path, err = setup_isolated_clone(
            str(shared), str(dest), branch="pr-head-that-does-not-exist", base_ref=""
        )
        assert err, "a missing PR head branch was accepted, so the watcher gets the base tree"
        assert path == "", "a refused clone must not return a usable path"
        assert not dest.exists(), "the unusable clone was left on disk"

    def test_an_existing_head_branch_still_works(self, tmp_path) -> None:
        """The refusal must not break the normal path."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend.pr_watchers import (
            setup_isolated_clone,
        )

        shared = tmp_path / "shared2"
        shared.mkdir()

        def _git(*args):
            subprocess.run(["git", *args], cwd=shared, check=True, capture_output=True)

        _git("init", "-q")
        _git("config", "user.email", "t@example.invalid")
        _git("config", "user.name", "T")
        (shared / "f.txt").write_text("base\n", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-qm", "base")
        _git("checkout", "-q", "-b", "feature-head")
        (shared / "f.txt").write_text("head\n", encoding="utf-8")
        _git("commit", "-qam", "head work")

        from pathlib import Path

        dest = tmp_path / "iso2"
        path, err = setup_isolated_clone(str(shared), str(dest), branch="feature-head", base_ref="")
        assert err == "", f"a valid head branch was refused: {err}"
        assert path, "no clone path returned"
        # The point of the checkout: the agent must see the PR's content, not the base's.
        assert (Path(path) / "f.txt").read_text(encoding="utf-8") == "head\n"


class TestALongRepoKeyCanStillPersist:
    """`_SAFE_KEY_RE` capped the session key at 128 chars. D-75 made the client key
    `kind-repo-id` (was `kind-id`) to stop cross-repository collisions — which means the key
    now embeds `owner/repo`, and GitHub allows owner<=39 + repo<=100. Measured: a PR key like
    `pr-<39>-<100>-1` is 145 chars, past the bound.

    `save_session` then raises `unsafe session record key`, but `openSession()` has already
    seeded the chat slot before it calls `save_session`, so the link is lost and every retry
    spawns another orphaned chat — the exact "each retry creates another" failure. The bound
    exists for a real reason (the record is a `<key>.json` file, and a path component cannot
    exceed 255), so it is raised to a value that clears the longest legitimate key while
    staying safely under the filesystem limit, not removed. Raised by the GPT review.
    """

    def test_a_max_length_github_pr_key_is_accepted(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        owner = "o" * 39
        repo = "r" * 100
        key = f"pr-{owner}-{repo}-1"  # what `sessionKey('pr', 1, 'owner/repo')` produces
        assert len(key) > 128, "fixture no longer exceeds the OLD bound — revisit"
        # Must not raise.
        assert store._validate_key(key) == key

    def test_the_bound_stays_below_the_filesystem_component_limit(self) -> None:
        """The cap is not removed — a `<key>.json` filename must fit in one path component
        (255 on ext4/APFS/NTFS). So the longest ACCEPTED key plus '.json' must clear it."""
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        longest_ok = "a" * 250
        too_long = "a" * 260
        assert store._validate_key(longest_ok) == longest_ok
        try:
            store._validate_key(too_long)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "a 260-char key was accepted; `<key>.json` overflows a path component"
            )

    def test_an_unsafe_shape_is_still_rejected(self) -> None:
        """Raising the length must not loosen the CHARACTER fence — a key becomes a filename."""
        from kiro_crew.apps.builtins.auto_improvement.backend import store

        for bad in ("../etc/passwd", "a/b", ".hidden", "has space", ""):
            try:
                store._validate_key(bad)
            except ValueError:
                continue
            raise AssertionError(f"unsafe key accepted: {bad!r}")


class TestFixtureGitHasAutoMaintenanceOff:
    """The repo-root conftest's git floor reaches this testpath too.

    `test_red_base_staging_does_not_dereference_a_credential_symlink` commits and then
    copies the worktree, `.git` included. A commit with auto maintenance on leaves a
    detached `git maintenance run --auto` adding and removing lock files there, so the
    copy could list a file that is gone by the time it is read. What git resolves from
    these is pinned in `test/test_host_isolation_floor.py`; this reads the environment
    alone, so it spawns nothing.
    """

    _OFF = {"maintenance.auto": "false", "gc.auto": "0"}

    def test_the_environment_entries_turn_auto_maintenance_off(self) -> None:
        last: dict[str, str] = {}
        for index in range(int(os.environ.get("GIT_CONFIG_COUNT") or 0)):
            key = os.environ.get(f"GIT_CONFIG_KEY_{index}", "").lower()
            last[key] = os.environ.get(f"GIT_CONFIG_VALUE_{index}", "")
        assert {key: last.get(key) for key in self._OFF} == self._OFF

    def test_the_global_config_file_turns_auto_maintenance_off(self) -> None:
        text = Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
        tail = text[text.rindex("[maintenance]") :]
        assert tail == "[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"
