"""Redact-before-bound on subprocess stderr across the auto-improvement app.

Several error payloads and log lines quote git stderr bounded to a fixed
character count. ``commit.py`` reaches git with an AUTHENTICATED remote URL
(``materialize_queued_diff`` passes ``_prefer_authenticated_remote``'s result as
argv), and on an auth failure git echoes that URL — userinfo and all — to
stderr. Bounding BEFORE redaction can cut the credential mid-match, leaving a
prefix that no longer matches any credential regex, so the downstream serving
route's own redaction pass (``routes.py``'s ``_redact_for_display``) cannot
recognise it either. The fix is redact-then-bound through the companion-aware
context shims (``redact_via_context`` for payloads, ``redact_log_via_context``
for log lines; ``security.redact_and_truncate`` remains the baseline spelling
elsewhere in the tree), which scrub
the full text first and bounds after.

The behavioral tests here pin the highest-reachability site (the fetch failure
in ``materialize_queued_diff``) with the straddle layout: the secret is placed
so the bound falls INSIDE it, so a raw slice AND a slice-then-redact reorder
both go red. The structural sweep then pins the whole class across every
non-test module of the app — head slices, tail slices (a tail cut keeps the
credential's RIGHT half, which equally matches nothing), and the
``tail[0][:N]`` / ``err[0][:N]`` last-line alias forms — so re-introducing a
raw bounded stderr slice at any sibling site fails without needing a per-site
behavioral test.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod

_APP_ROOT = Path(__file__).resolve().parents[1]

# The bound applied at the fetch-failure site in materialize_queued_diff.
_FETCH_BOUND = 160


def _proc(rc: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=rc, stdout="", stderr=stderr)


class TestFetchFailureStderrIsRedactedBeforeTheBound:
    """The commit path's fetch failure must never serve a raw credential fragment."""

    def _materialize_with_fetch_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stderr: str
    ) -> dict[str, object]:
        # Route the flow down the remote-url branch without touching a network
        # or a real repository: the config resolves to a URL, the authenticated
        # form is a fixed fake, and the ONLY git call made is the failing fetch.
        monkeypatch.setattr(commit_mod, "resolve_origin_url", lambda cfg: "https://x.test/r.git")
        monkeypatch.setattr(
            commit_mod, "_prefer_authenticated_remote", lambda url: "https://u:t@x.test/r.git"
        )
        calls: list[tuple] = []

        def _git(clone, *args, **kw):
            calls.append(args)
            assert args[0] == "fetch", f"unexpected git call before the fetch failed: {args}"
            return _proc(1, stderr=stderr)

        monkeypatch.setattr(commit_mod, "_git", _git)
        out = commit_mod.materialize_queued_diff(
            clone=tmp_path, branch="main", config={}, diff_text="--- a\n+++ b\n"
        )
        assert calls, "the fetch was never attempted, so this test exercised nothing"
        return out

    def test_a_fetch_failure_does_not_leak_the_remote_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        token = "s3cr3t-remote-token"  # a fake secret, only asserted absent
        stderr = f"fatal: could not read from 'https://ci-bot:{token}@github.example/r.git'\n"
        out = self._materialize_with_fetch_stderr(monkeypatch, tmp_path, stderr)
        assert out["ok"] is False
        assert token not in str(out["error"])
        assert "[REDACTED" in str(out["error"])

    def test_the_credential_is_redacted_before_it_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Straddle layout: the 160-char bound falls INSIDE the token.

        A raw slice keeps the token's left half verbatim. A slice-then-redact
        reorder keeps it too, because the cut drops the ``@host`` tail the
        userinfo regex needs to match. Only redact-then-bound scrubs it, so
        this test goes red if the two steps are ever reordered.
        """
        token = "TOKENedge9876543210"  # a fake secret, only asserted absent
        userinfo = "https://ci-bot:"
        # Lay the token out to START ten characters shy of the bound, so the
        # bound cuts into it and the '@host' tail lands beyond the bound.
        pad = "x" * (_FETCH_BOUND - 10 - len("fatal: ") - len(userinfo))
        line = f"fatal: {pad}{userinfo}{token}@github.example/r.git"
        # Premise guards: the layout must actually straddle, or this test
        # silently stops pinning the invariant.
        start = line.index(token)
        assert start < _FETCH_BOUND < start + len(token)
        assert line.index("@") > _FETCH_BOUND

        out = self._materialize_with_fetch_stderr(monkeypatch, tmp_path, line + "\n")
        assert out["ok"] is False
        err = str(out["error"])
        assert token not in err
        # The exact fragment a bound-before-redact implementation would leak —
        # everything of the token left of the bound — must be absent too.
        leaked_prefix = token[: _FETCH_BOUND - start]
        assert leaked_prefix and leaked_prefix not in err


class TestNoRawBoundedStderrSliceAnywhereInTheApp:
    """Structural class pin: the whole app is swept, not just the fixed sites.

    The defect recurred file by file (commit.py, pr_watchers.py, gate.py,
    driver.py, pr_recipe.py all carried it), so a per-site behavioral test
    cannot keep the class closed. Any non-test module that slices stderr to a
    bound must redact-then-bound through a redactor shim instead.
    """

    # The shapes redaction can no longer see through once the slice has run:
    # - a stderr expression head-sliced to a bound, with or without an interposed
    #   `or ''` default or `.strip()`  -> stderr...[:N]
    # - a stderr expression (or a var named like one) TAIL-sliced to a bound
    #   -> stderr...[-400:]; the multi-digit floor keeps the legitimate
    #   `splitlines()[-1:]` last-LINE idiom out (a whole line cuts no secret;
    #   it is the CHAR slice applied to it afterwards that does)
    # - that idiom's conventional aliases char-sliced -> tail[0][:N] / err[0][:N]
    # Known limits, accepted: the scan is per-line (a wrapped site whose stderr
    # expression and slice land on different lines evades it), and an alias
    # renamed away from tail/err evades the third alternative. proposer.py's
    # unbounded `r.stderr.strip()` RuntimeError is deliberately out of scope:
    # with no slice, a credential in it keeps its full shape, which downstream
    # pattern-based redaction can still match.
    _RAW_SLICE = re.compile(
        r"stderr\b[^\n]*\[:\d+\]" r"|stderr\w*\b[^\n]*\[-\d{2,}:\]" r"|\b(?:tail|err)\[0\]\[:\d+\]"
    )
    # The sanctioned forms: redact the FULL text, then cut the redactor's RESULT.
    # A slice of already-redacted text can at worst split a redaction marker,
    # never a secret. Covers redact(x)[-N:], redact_and_truncate's callers, and
    # the companion-aware log spelling redact_log_via_context(x)[:N]. The
    # trailing (?!\)) keeps the mirror-image defect flagged: in
    # redactor((stderr)[:N]) the slice sits INSIDE the call, so a ')' follows
    # the bracket and the line stays an offender.
    _SANCTIONED_TAIL = re.compile(r"redact\w*\([^\n]*\)\s*\[(?:-\d+:|:\d+)\](?!\))")

    def test_no_module_slices_stderr_before_redaction(self) -> None:
        offenders: list[str] = []
        for path in sorted(_APP_ROOT.rglob("*.py")):
            rel = path.relative_to(_APP_ROOT).as_posix()
            if rel.startswith("tests/"):
                continue
            for lineno, text in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                if self._RAW_SLICE.search(text) and not self._SANCTIONED_TAIL.search(text):
                    offenders.append(f"{rel}:{lineno}: {text.strip()}")
        assert offenders == [], (
            "raw bounded stderr slice (redaction cannot match a cut credential); "
            "use redact_via_context(text)[:bound] (or the log spelling) instead: "
            + "; ".join(offenders)
        )
