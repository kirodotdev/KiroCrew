"""GitHub draft-PR recipe — the seam that replaced the upstream review CLI.

The safety-relevant assertions here are the ones that matter most: the recipe
must never publish, must never push to a protected branch, and must degrade to
the durable queue rather than losing a verified change.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import pr_recipe as pr
from kiro_crew.apps.builtins.auto_improvement.spine.profile import PRRecipe


def _broken_scanner(_text: str) -> str:
    """A redactor whose backend is unavailable. A typed function, not a throwing lambda:
    `security.redact` is annotated `Callable[[str], str]` and mypy rejects the lambda."""
    raise RuntimeError("scanner down")


def _recipe(tmp_path: Path, **kw) -> pr.GitHubPRRecipe:
    return pr.GitHubPRRecipe(
        user="zedmor",
        clone_path=tmp_path / "clone",
        pr_queue_dir=tmp_path / "queue",
        base_ref=kw.pop("base_ref", "origin/main"),
        **kw,
    )


class TestProtocolConformance:
    def test_satisfies_the_spine_seam(self, tmp_path: Path) -> None:
        """Structural typing: the spine consumes the profile only through this."""
        assert isinstance(_recipe(tmp_path), PRRecipe)

    def test_namespace_is_metadata_only(self, tmp_path: Path) -> None:
        assert _recipe(tmp_path).namespace == "github/zedmor"

    def test_base_ref_strips_remote_prefix(self, tmp_path: Path) -> None:
        """``gh --base`` wants a plain branch name."""
        assert _recipe(tmp_path, base_ref="origin/develop").base_branch == "develop"

    def test_bare_base_ref_passes_through(self, tmp_path: Path) -> None:
        assert _recipe(tmp_path, base_ref="develop").base_branch == "develop"


class TestExtractPrUrl:
    def test_finds_url_among_trailing_chatter(self) -> None:
        """Do NOT trust the last line: git hooks and agent stdout print after it.

        The upstream original recorded a hook's message ("breaking it up into smaller
        components.") as a review id because it took the last line.
        """
        out = "Warning: hook\nhttps://github.com/o/r/pull/7\nremote: done\n"
        assert pr.extract_pr_url(out) == "https://github.com/o/r/pull/7"

    def test_rejects_prose(self) -> None:
        assert pr.extract_pr_url("breaking it up into smaller components.") is None

    def test_rejects_a_non_github_url(self) -> None:
        assert pr.extract_pr_url("https://example.com/o/r/pull/7") is None


class TestBranchNaming:
    def test_branch_is_app_namespaced(self, tmp_path: Path) -> None:
        assert _recipe(tmp_path).branch_name(kind="perf", fingerprint="ab12") == (
            "auto-improvement/perf-ab12"
        )

    def test_odd_kind_is_slugified(self, tmp_path: Path) -> None:
        name = _recipe(tmp_path).branch_name(kind="Bug Fix!!", fingerprint="ff")
        assert name == "auto-improvement/bug-fix-ff"

    def test_generated_branch_can_never_be_protected(self, tmp_path: Path) -> None:
        """The prefix is what guarantees the denylist cannot match. Belt and
        braces: assert the authoritative gate agrees for the primary names."""
        recipe = _recipe(tmp_path)
        # Asserting the REAL protected branch names are denied.
        for protected in ("main", "master", "mainline", "release/1.0"):  # wokeignore:rule=master
            branch = recipe.branch_name(kind=protected, fingerprint="aa")
            ok, _ = recipe._authorize(branch)
            assert ok, f"generated branch from {protected!r} was refused: {branch}"


class TestDraftDegradation:
    def test_queue_copy_written_before_anything_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable record must survive a total drafting failure."""
        monkeypatch.setattr(pr.shutil, "which", lambda _n: None)  # no gh on PATH
        recipe = _recipe(tmp_path)
        result = recipe.draft(
            summary="perf: speed up the parser",
            description="# perf: speed up the parser\n\nEvidence...",
            diff="--- a\n+++ b\n",
            fingerprint="deadbeef",
        )
        assert result == "QUEUED:deadbeef"
        assert (tmp_path / "queue" / "deadbeef.diff").read_text() == "--- a\n+++ b\n"
        body = (tmp_path / "queue" / "deadbeef.pr.md").read_text()
        # The summary becomes the title, so the body's duplicate H1 is dropped.
        assert body.count("# perf: speed up the parser") == 1

    def test_no_pushable_remote_degrades_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fully push-disabled clone (the watcher clones) cannot open a PR."""
        monkeypatch.setattr(pr.shutil, "which", lambda _n: "/usr/bin/gh")
        recipe = _recipe(tmp_path, fetch_url="DISABLED_NO_PUSH")
        assert (
            recipe.draft(summary="fix: thing", description="body", diff="d", fingerprint="ff01")
            == "QUEUED:ff01"
        )

    def test_failed_push_degrades_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pr.shutil, "which", lambda _n: "/usr/bin/gh")

        def fake_git(self, *args, timeout=30.0):  # noqa: ANN001
            return subprocess.CompletedProcess(args, 1, "", "remote rejected")

        monkeypatch.setattr(pr.GitHubPRRecipe, "_git", fake_git)
        recipe = _recipe(tmp_path, fetch_url="https://github.com/o/r.git")
        assert (
            recipe.draft(summary="fix: thing", description="body", diff="d", fingerprint="ff02")
            == "QUEUED:ff02"
        )


class TestDraftOnlyPolicy:
    def test_command_is_draft_and_never_publishes(self) -> None:
        """``--draft`` is the mechanical half of the draft-only policy."""
        assert "--draft" in pr.DRAFT_CMD
        joined = " ".join(pr.DRAFT_CMD)
        for forbidden in ("--web", "merge", "ready", "--auto"):
            assert forbidden not in joined

    def test_successful_draft_returns_the_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pr.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(
            pr.GitHubPRRecipe,
            "_push_fix_branch",
            lambda self, *, branch: (True, branch),
        )
        seen: dict[str, list[str]] = {}

        def fake_run(cmd, **kw):  # noqa: ANN001
            seen["cmd"] = list(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/99\n", "")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        recipe = _recipe(tmp_path)
        out = recipe.draft(summary="perf: faster", description="body", diff="d", fingerprint="cafe")
        assert out == "https://github.com/o/r/pull/99"
        # The PR must target the configured base and carry the queue body file.
        assert "--base" in seen["cmd"] and "main" in seen["cmd"]
        assert "--body-file" in seen["cmd"]
        assert "--draft" in seen["cmd"]
        assert "--head" in seen["cmd"]


class TestStripLeadingH1:
    def test_drops_only_a_leading_h1(self) -> None:
        assert pr._strip_leading_h1("# Title\n\nBody") == "Body"

    def test_keeps_a_later_heading(self) -> None:
        assert pr._strip_leading_h1("Body\n\n# Later") == "Body\n\n# Later"

    def test_handles_empty(self) -> None:
        assert pr._strip_leading_h1("") == ""


class TestAuthenticatedRemote:
    """Regression: the first live run drafted nothing because the push went out
    over HTTPS while ``gh`` was authenticated for SSH on github.com (and git's
    global credential helper pointed at an unrelated provider), so the push could
    never authenticate and the PR silently degraded to the queue."""

    def test_https_rewritten_to_ssh_when_gh_prefers_ssh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pr, "_gh_prefers_ssh", lambda: True)
        assert (
            pr._prefer_authenticated_remote("https://github.com/o/r.git")
            == "git@github.com:o/r.git"
        )

    def test_https_kept_when_gh_prefers_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pr, "_gh_prefers_ssh", lambda: False)
        assert (
            pr._prefer_authenticated_remote("https://github.com/o/r.git")
            == "https://github.com/o/r.git"
        )

    def test_non_github_remote_is_never_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rewrite must only ever change the TRANSPORT of a github.com URL."""
        monkeypatch.setattr(pr, "_gh_prefers_ssh", lambda: True)
        for url in ("https://gitlab.com/o/r.git", "git@github.com:o/r.git", "nonsense"):
            assert pr._prefer_authenticated_remote(url) == url

    def test_host_scoped_setting_wins_over_the_global_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observed live: global default ``https``, github.com explicitly ``ssh``.
        Reading only the global value inverts the answer."""
        calls: list[list[str]] = []

        def fake_run(args, **kw):  # noqa: ANN001
            calls.append(list(args))
            host_scoped = "-h" in args
            return subprocess.CompletedProcess(args, 0, "ssh\n" if host_scoped else "https\n", "")

        monkeypatch.setattr(pr.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        assert pr._gh_prefers_ssh() is True
        assert "-h" in calls[0], "the host-scoped lookup must be tried first"


class TestPushedContentIsScanned:
    """Nothing scanned the CONTENT being pushed — only the commit message. A credential
    in an accepted fix would reach GitHub, where a pushed commit is as unwipeable as a
    commit message. Raised by review of this branch.

    DETECT-and-refuse rather than redact: rewriting a code diff would corrupt the very
    fix the gate proved, so a hit degrades the change to the durable local queue.
    """

    @staticmethod
    def _recipe(tmp_path, diff_text: str):
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        recipe = GitHubPRRecipe(
            user="u",
            clone_path=tmp_path,
            pr_queue_dir=tmp_path / "q",
            base_ref="origin/main",
        )

        def _fake_git(*args, timeout=30.0):
            # `_scan_pushable_content` now resolves the base first (`_scannable_base`), so the
            # stub must answer `rev-parse` distinctly: returning `diff_text` for EVERY call
            # made HEAD and the base look like the same sha, and the scan correctly refused.
            # HEAD and the base resolve to different shas here so the diff path is exercised.
            if args and args[0] == "rev-parse":
                sha = "b" * 40 if "HEAD" in args else "a" * 40
                return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=sha)
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout=diff_text)

        recipe._git = _fake_git  # type: ignore[method-assign]
        return recipe

    def test_a_credential_in_the_pushable_diff_refuses_the_push(self, tmp_path) -> None:
        leaked = "+AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
        ok, note = self._recipe(tmp_path, leaked)._scan_pushable_content()
        assert ok is False
        assert "credential" in note
        # The note must not quote the secret it is reporting.
        assert "wJalrXUtnFEMI" not in note

    def test_a_clean_diff_is_allowed(self, tmp_path) -> None:
        clean = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-return None\n+return 0\n"
        ok, note = self._recipe(tmp_path, clean)._scan_pushable_content()
        assert ok is True, note

    def test_unreadable_diff_fails_closed(self, tmp_path) -> None:
        """An unscannable push is indistinguishable from an unscanned one."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        recipe = GitHubPRRecipe(
            user="u", clone_path=tmp_path, pr_queue_dir=tmp_path / "q", base_ref="origin/main"
        )

        def _boom(*args, timeout=30.0):
            raise subprocess.SubprocessError("git exploded")

        recipe._git = _boom  # type: ignore[method-assign]
        ok, note = recipe._scan_pushable_content()
        # A raising `git` now trips the BASE resolution first (`_scannable_base`), which
        # refuses for the same fail-closed reason, so accept either refusal message — what
        # matters is that an unscannable push does not proceed.
        assert ok is False
        assert "could not read" in note or "could not resolve the base" in note
        # And nothing derived from git's output rides along in the logged note.
        assert "git exploded" not in note

    def test_pr_prose_is_redacted(self) -> None:
        """Prose CAN be rewritten safely, unlike a diff — so it is redacted, not refused."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            _redact_prose,
        )

        # The credential scanner anchors on an assignment, which is the shape an agent
        # actually pastes into a PR body when quoting the code it changed.
        body = "Repro steps:\naws_access_key_id=AKIAIOSFODNN7EXAMPLE\nthen run the suite.\n"
        out = _redact_prose(body)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "then run the suite." in out, "surrounding prose must survive"

    def test_a_scanner_that_cannot_run_refuses_rather_than_returning_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail CLOSED. This used to return the prose unscanned, on the reasoning that the
        diff beside it had passed a fail-closed scan and the PR is only a draft — but the
        prose is a separate artifact, it is the part the agent wrote most freely, and a
        published PR description cannot be un-published. Every other egress path in this
        app already fails closed. Raised by the GPT review of this branch."""
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            ProseRedactionUnavailable,
            _redact_prose,
        )

        secret = "aws_access_key_id=AKIAIOSFODNN7EXAMPLE"
        monkeypatch.setattr(security_mod, "redact", _broken_scanner)
        with pytest.raises(ProseRedactionUnavailable):
            _redact_prose(secret)

    def test_an_unscannable_draft_degrades_to_the_queue_instead_of_publishing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of failing closed: the verified fix is not LOST, it just is not
        published. The queue copy is still written (it never leaves the host, and a human
        needs to see what the agent actually wrote) and `gh` is never reached."""
        import kiro_crew.security as security_mod
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import pr_recipe as PR

        recipe = _recipe(tmp_path)

        gh_calls: list[list[str]] = []
        monkeypatch.setattr(PR.subprocess, "run", lambda argv, **kw: gh_calls.append(argv))
        # `gh` present on PATH, so reaching it would really publish — the assertion below
        # that it was NOT reached is only meaningful with this in place.
        monkeypatch.setattr(PR.shutil, "which", lambda _b: "/usr/bin/gh")

        monkeypatch.setattr(security_mod, "redact", _broken_scanner)
        out = recipe.draft(
            fingerprint="fp-unscannable",
            summary="fix: something",
            description="body with aws_access_key_id=AKIAIOSFODNN7EXAMPLE",
            diff="--- a\n+++ b\n",
        )

        assert out == "QUEUED:fp-unscannable", f"it published anyway: {out!r}"
        assert gh_calls == [], "gh was invoked with unscanned prose"
        # The evidence still survives on disk for the human.
        assert (recipe.pr_queue_dir / "fp-unscannable.pr.md").is_file()
        assert (recipe.pr_queue_dir / "fp-unscannable.diff").is_file()


class TestEveryPushPathScansContent:
    """All three exits share ONE scanner. A credential gate that guards only some of the
    exits is not a gate — and the exit that was missed is the one that publishes.

    Raised by review of this branch: the first pass guarded the PR-draft push and left
    the driver's F10 direct push and the operator's one-click commit unscanned.
    """

    def test_the_shared_scanner_is_the_single_implementation(self) -> None:
        """Each push path must call ``push_policy.scan_content_for_secrets``, not its own
        copy — three drifting copies is how one of them ends up without the fix."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for rel in (
            "profiles/github_repo/pr_recipe.py",
            "spine/driver.py",
            "backend/commit.py",
        ):
            src = (root / rel).read_text(encoding="utf-8")
            assert "scan_content_for_secrets" in src, f"{rel} does not scan pushed content"
            # No path may re-import the raw scanners and hand-roll the decision.
            assert "redact_credentials" not in src, f"{rel} should delegate, not re-implement"

    def test_scanner_refuses_a_credential(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            scan_content_for_secrets,
        )

        ok, note = scan_content_for_secrets("+aws_access_key_id=AKIAIOSFODNN7EXAMPLE\n")
        assert ok is False
        assert "AKIAIOSFODNN7EXAMPLE" not in note, "the note must not echo the secret"

    def test_scanner_allows_a_clean_diff_and_empty_input(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            scan_content_for_secrets,
        )

        clean = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-return None\n+return 0\n"
        assert scan_content_for_secrets(clean)[0] is True
        # An empty range is not a finding: nothing to publish means nothing to refuse.
        assert scan_content_for_secrets("")[0] is True

    def test_the_direct_push_scan_range_is_not_self_diffing(self) -> None:
        """A scanner that is CALLED but on an EMPTY range is not a gate. The driver's F10
        push diffed ``{dest}..HEAD`` where ``dest`` is the local branch HEAD sits on the
        tip of — so ``git diff <branch>..HEAD`` compared a ref to itself and returned
        nothing, and the fail-closed credential scan passed on a 0-byte input. Measured:
        a commit adding an AWS key gave a 0-byte ``<branch>..HEAD`` diff and a 144-byte
        ``HEAD~1..HEAD`` diff. Pin the source so the self-diffing range cannot return.
        Raised by the GPT review of this branch.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        src = inspect.getsource(Driver._direct_push)
        assert "HEAD~1..HEAD" in src, "the push scan no longer diffs the committed range"
        # The buggy construct was `scan_range = f"{dest}..HEAD"` fed to `git diff`. Match the
        # CODE, not a mention in a comment: an f-string building a `<var>..HEAD` diff range.
        import re

        code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        assert not re.search(
            r'f"\{[A-Za-z_]+\}\.\.HEAD"', code
        ), "a self-diffing f-string scan range is back in the code"

    def test_a_committed_secret_is_actually_in_the_scanned_range(self, tmp_path) -> None:
        """End to end against a real repo: the range the driver scans for a one-commit
        direct push (``HEAD~1..HEAD``) must CONTAIN that commit's content, so a secret in
        the verified fix reaches the scanner rather than a 0-byte diff."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            scan_content_for_secrets,
        )

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, env=env
            )

        git("init", "-q", "-b", "main", ".")
        (tmp_path / "f.txt").write_text("base\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        # The verified fix commits a credential (the exact thing the gate must catch).
        (tmp_path / "f.txt").write_text(
            "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8"
        )
        git("add", "-A")
        git("commit", "-qm", "the fix")

        # HEAD is the tip of 'main' — the buggy `main..HEAD` range would be empty here.
        assert git("rev-parse", "main").stdout.strip() == git("rev-parse", "HEAD").stdout.strip()
        assert git("diff", "main..HEAD").stdout == "", "precheck: the buggy range really is empty"

        # The fixed range carries the commit, and the scanner refuses it.
        scanned = git("diff", "HEAD~1..HEAD").stdout
        assert "AKIAIOSFODNN7EXAMPLE" in scanned, "the scanned range is empty — the gate is blind"
        assert scan_content_for_secrets(scanned)[0] is False, "the credential was not refused"

    def test_scanner_fails_closed_without_the_security_module(self, monkeypatch) -> None:
        """An unscannable push is indistinguishable from an unscanned one."""
        import builtins

        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            scan_content_for_secrets,
        )

        real_import = builtins.__import__

        def _no_security(name, *a, **kw):
            if name == "kiro_crew.security":
                raise ImportError("boom")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_security)
        ok, code = scan_content_for_secrets("anything")
        assert ok is False and code == "no_scanner"


class TestScanNotesCarryNoScannedContent:
    """A refusal note is LOGGED by its caller, so it must be provably independent of the
    text that was scanned.

    Raised by CodeQL on this branch as 5 high-severity clear-text-logging alerts: the
    query follows dataflow, and any path from the scanned text into the returned note
    makes those log calls look like they publish a secret. The property is real and worth
    pinning — a scanner warning can quote what it matched, which would write the secret
    into the very log the scan exists to keep it out of.
    """

    def test_the_hit_note_contains_only_a_count(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            SCAN_HIT,
            describe_scan,
            scan_content_for_secrets,
        )

        secret = "AKIAIOSFODNN7EXAMPLE"
        ok, code = scan_content_for_secrets(f"+aws_access_key_id={secret}\n")
        assert ok is False
        # A fixed CODE crosses out, not a message and not even the count: a count is
        # still a value derived from the scanned text, which a taint-tracking query
        # follows into the caller's log call. A constant cannot carry anything.
        assert code == SCAN_HIT
        note = describe_scan(code)
        assert secret not in note and secret.lower() not in note.lower()
        assert note == "content scan found credential/exfiltration finding(s)"

    def test_no_substring_of_the_input_longer_than_a_word_reaches_the_note(self) -> None:
        """A stronger form: no distinctive run of input characters appears in the note."""
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            describe_scan,
            scan_content_for_secrets,
        )

        marker = "ZqXvBnMwErTyUiOp"
        _, code = scan_content_for_secrets(f"+aws_secret_access_key={marker}0123456789\n")
        note = describe_scan(code)
        for size in (8, 12, 16):
            for i in range(0, len(marker) - size + 1):
                assert marker[i : i + size] not in note

    def test_scanner_import_failure_note_is_a_literal(self, monkeypatch) -> None:
        """The ImportError text is dropped, not interpolated: it can carry a path."""
        import builtins

        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            scan_content_for_secrets,
        )

        real = builtins.__import__

        def _no_security(name, *a, **kw):
            if name == "kiro_crew.security":
                raise ImportError("/home/someone/secret/path/kiro_crew/security.py missing")
            return real(name, *a, **kw)

        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            SCAN_NO_SCANNER,
            describe_scan,
        )

        monkeypatch.setattr(builtins, "__import__", _no_security)
        ok, code = scan_content_for_secrets("anything")
        assert ok is False
        assert code == SCAN_NO_SCANNER
        assert describe_scan(code) == "credential scanners unavailable"
        assert "/home/someone" not in describe_scan(code)

    def test_logged_code_locus_cannot_carry_a_credential(self) -> None:
        """The keeper logs a candidate's code locus, which is agent-supplied. A path and a
        Python symbol need no `=`, quotes or whitespace, so stripping those makes the
        no-credential property checkable while leaving a real locus untouched."""
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import _locus

        assert _locus("src/pkg/mod.py::Class.method") == "src/pkg/mod.py::Class.method"
        assert _locus('aws_secret_access_key="AKIAIOSFODNN7EXAMPLE"') == (
            "aws_secret_access_keyAKIAIOSFODNN7EXAMPLE"
        )
        assert "=" not in _locus("k=v")
        assert _locus("") == "?"
        assert len(_locus("x" * 500)) <= 160

    def test_guardrail_verdict_code_is_slug_shaped(self) -> None:
        """The keeper's verdict code is logged AND recorded in the ledger, so a guardrail
        metric name cannot smuggle credential-shaped punctuation into it."""
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import _metric_slug

        assert _metric_slug("aws_key=AKIA/123:x") == "aws_keyAKIA123x"
        assert _metric_slug("") == "unnamed"
        assert len(_metric_slug("m" * 200)) <= 40


class TestScannerReturnsCodesNotMessages:
    """The scanner's second return value must be one of a FIXED set of codes.

    Callers log it, so a string built inside the scanner — even one carrying only a count
    — is a value derived from the scanned text, and a taint-tracking query follows it into
    the log call and reports a leak. Returning a constant makes "the log line carries no
    scanned content" true by construction. Pinned because a future edit that helpfully
    formats the count back into the return value would silently undo it.
    """

    def test_every_return_is_a_known_code(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            SCAN_REASON_TEXT,
            scan_content_for_secrets,
        )

        for text in ("", "   ", "clean line\n", "+aws_access_key_id=AKIAIOSFODNN7EXAMPLE\n"):
            _, code = scan_content_for_secrets(text)
            assert code in SCAN_REASON_TEXT, f"unknown code {code!r} for {text!r}"

    def test_reason_table_is_all_literals(self) -> None:
        """No entry may contain a format placeholder — that would invite interpolation."""
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import SCAN_REASON_TEXT

        for code, text in SCAN_REASON_TEXT.items():
            assert "{" not in text and "%" not in text, f"{code} invites interpolation"

    def test_describe_scan_is_total(self) -> None:
        """An unknown code still yields a safe literal rather than raising."""
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import describe_scan

        assert describe_scan("some-future-code") == "content scan refused the push"


class TestLoggedStringsAreRebuiltFromConstants:
    """Logged strings derived from agent input are REBUILT from a fixed alphabet, not
    filtered out of the input.

    The distinction is invisible to a human and decisive to a taint-tracking query: a
    comprehension that filters characters still yields a string derived from the input,
    so CodeQL keeps reporting the log call. Drawing each character from a module constant
    is what actually severs it. Pinned because "simplify" would naturally rewrite these
    as a filter and silently undo it.
    """

    def test_locus_output_uses_only_its_alphabet(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import (
            _LOCUS_ALPHABET,
            _locus,
        )

        messy = "src/pkg/mod.py::Class.method aws_key='AKIAIOSFODNN7EXAMPLE'\n\t"
        out = _locus(messy)
        assert set(out) <= set(_LOCUS_ALPHABET), out
        # A real locus survives byte-identical — the sanitizer must not damage evidence.
        assert _locus("src/pkg/mod.py::Class.method") == "src/pkg/mod.py::Class.method"
        # The characters a credential assignment needs are gone.
        for ch in ("=", "'", '"', " ", "\n"):
            assert ch not in out

    def test_metric_slug_alphabet_is_tighter_than_the_locus_alphabet(self) -> None:
        """A metric name is an identifier: it has no business keeping path punctuation."""
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import (
            _LOCUS_ALPHABET,
            _METRIC_ALPHABET,
            _metric_slug,
        )

        assert set(_METRIC_ALPHABET) < set(_LOCUS_ALPHABET)
        assert "/" not in _METRIC_ALPHABET and ":" not in _METRIC_ALPHABET
        assert set(_metric_slug("aws/key:AKIA=1")) <= set(_METRIC_ALPHABET)

    def test_both_are_length_bounded(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import (
            _LOCUS_MAX,
            _METRIC_SLUG_MAX,
            _locus,
            _metric_slug,
        )

        assert len(_locus("z" * 5000)) == _LOCUS_MAX
        assert len(_metric_slug("z" * 5000)) == _METRIC_SLUG_MAX

    def test_empty_input_yields_a_placeholder_not_an_empty_field(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import _locus, _metric_slug

        assert _locus("") == "?"
        assert _metric_slug("") == "unnamed"


class TestAFailedDiffIsNeverVacuouslyClean:
    """A credential gate that reads an EMPTY diff must not conclude "clean".

    Raised by review of this branch against the driver's direct-push path. `_git` does not
    raise, so a diff against a ref with no local head exits non-zero with empty stdout —
    and `scan_content_for_secrets` reads blank text as "nothing to publish" and returns OK.
    The fail-closed gate was therefore skipped and the commit would be pushed unscanned.
    Reproduced before fixing: `git diff no-such-branch..HEAD` exits 128 with empty stdout.

    The scanner's blank-input shortcut is correct in itself — an empty range genuinely has
    nothing to refuse — so the guard belongs at every CALL SITE, which is why this asserts
    on all three rather than changing the scanner.
    """

    def test_all_three_push_paths_check_the_git_exit_status(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for rel, marker in (
            ("spine/driver.py", "proc.returncode != 0"),
            ("profiles/github_repo/pr_recipe.py", "proc.returncode != 0"),
            ("backend/commit.py", "scanned.returncode == 0"),
        ):
            src = (root / rel).read_text(encoding="utf-8")
            assert marker in src, f"{rel} does not check the diff's exit status"

    def test_blank_input_still_reads_as_clean(self) -> None:
        """Documents WHY the guard lives at the call sites: this shortcut is intended."""
        from kiro_crew.apps.builtins.auto_improvement.spine.push_policy import (
            SCAN_OK,
            scan_content_for_secrets,
        )

        assert scan_content_for_secrets("") == (True, SCAN_OK)
        assert scan_content_for_secrets("   \n") == (True, SCAN_OK)

    def test_a_failed_git_diff_really_does_exit_nonzero_with_empty_stdout(self, tmp_path) -> None:
        """Pins the premise the guard rests on, so a git behavior change is caught here."""
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        proc = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "no-such-branch..HEAD"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert proc.stdout == ""


class TestCloneCannotReachTheRemoteAtAll:
    """Disabling only the PUSH url leaves a live push target.

    ``git push --push`` is honored only when pushing BY REMOTE NAME;
    ``git push "$(git remote get-url origin)" HEAD`` ignores it and writes to the fetch
    url. The loop's agent runs auto-approved Bash inside this clone, so a repository
    instruction could do exactly that. Measured before fixing: pushing by name was
    refused, pushing to the fetch url landed a new branch upstream. Raised by review of
    this branch.

    The trusted publishers now take the real url from config (``origin_url``), which is
    deliberately NOT in ``_CONFIG_WRITABLE``.
    """

    def test_both_urls_are_neutralized_and_neither_push_route_works(self, tmp_path) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        up, work = tmp_path / "upstream.git", tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
        subprocess.run(["git", "clone", "-q", str(up), str(work)], capture_output=True)
        for k, v in (("user.email", "t@e"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
        (work / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)

        clone_setup._disable_push(work)

        fetch = subprocess.run(
            ["git", "-C", str(work), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "DISABLED" in fetch.upper(), "the FETCH url is a live push target"

        by_name = subprocess.run(
            ["git", "-C", str(work), "push", "origin", "HEAD"], capture_output=True, text=True
        )
        by_url = subprocess.run(
            ["git", "-C", str(work), "push", fetch, "HEAD:refs/heads/probe"],
            capture_output=True,
            text=True,
        )
        assert by_name.returncode != 0
        assert by_url.returncode != 0
        # The decisive assertion: nothing reached the remote.
        branches = subprocess.run(
            ["git", "-C", str(up), "branch"], capture_output=True, text=True
        ).stdout.strip()
        assert branches == "", f"a push escaped the sandbox: {branches!r}"

    def test_origin_url_is_not_client_writable(self) -> None:
        """It decides where a push can land, so it moves only through repository setup."""
        from kiro_crew.apps.builtins.auto_improvement.backend.routes import _CONFIG_WRITABLE

        assert "origin_url" not in _CONFIG_WRITABLE
        assert "clone" not in _CONFIG_WRITABLE
        assert "target_url" not in _CONFIG_WRITABLE


class TestTrustedPublisherStillWorksAfterNeutralizing:
    """The other half of the both-urls fix: neutralizing the clone must NOT break the
    publishers that legitimately push one generated ref.

    A security change that quietly disables the feature it guards is a regression, so this
    asserts the positive case end-to-end against a real bare repo: with both of the clone's
    urls neutralized, a recipe holding the config-carried url still lands its branch.
    """

    def test_a_recipe_holding_the_config_url_can_still_push(self, tmp_path) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        up, work = tmp_path / "up.git", tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
        subprocess.run(["git", "clone", "-q", str(up), str(work)], capture_output=True)
        for k, v in (("user.email", "t@e"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
        (work / "a.txt").write_text("x")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
        # A real upstream base, so the pre-push content scan's diff resolves.
        subprocess.run(
            ["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/main"],
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], capture_output=True)
        (work / "b.txt").write_text("fix")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-qm", "the fix"], check=True)

        clone_setup._disable_push(work)

        recipe = GitHubPRRecipe(
            user="u",
            clone_path=work,
            pr_queue_dir=tmp_path / "q",
            base_ref="origin/main",
            fetch_url=str(up),  # exactly what config's `origin_url` supplies
        )
        ok, note = recipe._push_fix_branch(branch="auto-improvement/bug-abc123")
        assert ok is True, note
        branches = subprocess.run(
            ["git", "-C", str(up), "branch"], capture_output=True, text=True
        ).stdout
        assert "auto-improvement/bug-abc123" in branches

    def test_without_the_config_url_the_neutralized_clone_degrades_to_the_queue(
        self, tmp_path
    ) -> None:
        """An older config (no `origin_url`) must degrade, never push unguarded."""
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        up, work = tmp_path / "up.git", tmp_path / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
        subprocess.run(["git", "clone", "-q", str(up), str(work)], capture_output=True)
        clone_setup._disable_push(work)
        recipe = GitHubPRRecipe(
            user="u", clone_path=work, pr_queue_dir=tmp_path / "q", base_ref="origin/main"
        )
        assert recipe._resolve_fetch_url() is None, "a neutralized clone must not yield a target"


class TestOriginUrlMigratesOldConfigs:
    """Neutralizing BOTH clone urls means the trusted publishers can no longer read the
    remote out of git — they take it from config's ``origin_url``. A config written before
    that change has no such key, and its clone still has a live fetch url.

    Found by inspecting the live dogfood configs on this host: both had ``clone`` set and
    no ``origin_url``. Without a migration they would silently degrade to queue-only after
    an upgrade, so the resolver falls back to the retained ``target_url``.
    """

    def test_a_config_without_origin_url_still_resolves_a_target(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
            resolve_origin_url,
        )

        legacy = {"clone": "/tmp/x", "target_url": "https://github.com/Zedmor/chess_test"}
        got = resolve_origin_url(legacy)
        assert got, "an existing install must not silently lose its push target"
        assert "chess_test" in got

    def test_origin_url_wins_when_present(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
            resolve_origin_url,
        )

        # `origin_url` is preferred over the legacy `target_url` — but only when the two name
        # the SAME repository. Host-allowlisting alone let an injected config keep
        # `github.com` and swap the path, redirecting the push to another owner's repo, so the
        # identity is pinned now. (This case previously used mismatched repos, which is
        # precisely the attack it must refuse — see the mismatch assertion below.)
        cfg = {"origin_url": "https://github.com/o/r.git", "target_url": "https://github.com/o/r"}
        assert resolve_origin_url(cfg) == "https://github.com/o/r.git"

    def test_an_origin_url_naming_another_repo_is_refused(self) -> None:
        """A tampered `origin_url` on an ALLOWED host must not redirect the push.

        `github.com` is allowlisted, so the host check passes and only the owner/repo
        differs — the exfiltration shape a host-only rule cannot see. Raised by the GPT
        review."""
        from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
            resolve_origin_url,
        )

        cfg = {
            "origin_url": "https://github.com/attacker/exfil.git",
            "target_url": "https://github.com/o/r",
        }
        assert resolve_origin_url(cfg) == "", "a repo-swapped origin_url became a push target"
        # And with nothing to pin against, it fails closed rather than trusting the string.
        assert resolve_origin_url({"origin_url": "https://github.com/attacker/exfil.git"}) == ""

    def test_the_fallback_revalidates_rather_than_trusting_the_stored_string(self) -> None:
        """A hand-edited `target_url` must not become an arbitrary push destination."""
        from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
            resolve_origin_url,
        )

        assert resolve_origin_url({"target_url": "https://evil.example.com/o/r"}) == ""
        assert resolve_origin_url({"target_url": "not a url"}) == ""

    def test_no_config_means_no_push_target(self) -> None:
        """Fail closed: every caller treats "" as "no target", degrading to the queue."""
        from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
            resolve_origin_url,
        )

        assert resolve_origin_url({}) == ""
        assert resolve_origin_url({"origin_url": "   "}) == ""


class TestPushedBranchActuallyContainsTheFix:
    """The end-to-end assertion the earlier rounds of this fix did not make.

    A first attempt staged the winner and I verified only that the tests passed — not that
    the PUSHED branch carried the fix. It did not: `git push HEAD:refs/heads/<b>` sends the
    COMMIT HEAD points at, and staging only touches the index. This drives the real recipe
    against a real bare repo and asserts on the CONTENT that reached the remote, which is
    the property the operator actually cares about.
    """

    @staticmethod
    def _repo(tmp_path):
        import subprocess

        up, work = tmp_path / "up.git", tmp_path / "w"
        subprocess.run(["git", "init", "-q", "--bare", str(up)], check=True)
        subprocess.run(["git", "clone", "-q", str(up), str(work)], capture_output=True)
        for k, v in (("user.email", "t@e"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True)
        (work / "a.txt").write_text("original\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
        subprocess.run(
            ["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/main"],
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], capture_output=True)
        return up, work

    def test_a_committed_fix_reaches_the_pushed_branch(self, tmp_path) -> None:
        import subprocess

        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        up, work = self._repo(tmp_path)
        (work / "a.txt").write_text("FIXED\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(work), "commit", "-qm", "wip(auto-improvement): staging c1"],
            check=True,
        )
        clone_setup._disable_push(work)

        recipe = GitHubPRRecipe(
            user="u",
            clone_path=work,
            pr_queue_dir=tmp_path / "q",
            base_ref="origin/main",
            fetch_url=str(up),
        )
        ok, note = recipe._push_fix_branch(branch="auto-improvement/bug-abc")
        assert ok is True, note
        got = subprocess.run(
            ["git", "-C", str(up), "show", "auto-improvement/bug-abc:a.txt"],
            capture_output=True,
            text=True,
        ).stdout
        assert got.strip() == "FIXED", f"the drafted branch does not carry the fix: {got!r}"

    def test_a_merely_staged_fix_would_not_reach_it(self, tmp_path) -> None:
        """Pins WHY a commit is required — this is the bug the first attempt shipped."""
        import subprocess

        up, work = self._repo(tmp_path)
        (work / "a.txt").write_text("FIXED\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)  # staged, NOT committed
        subprocess.run(
            ["git", "-C", str(work), "push", "-q", str(up), "HEAD:refs/heads/staged-probe"],
            capture_output=True,
        )
        got = subprocess.run(
            ["git", "-C", str(up), "show", "staged-probe:a.txt"], capture_output=True, text=True
        ).stdout
        assert got.strip() == "original", "staging alone must not be mistaken for a commit"
