"""Redact-before-bound on subprocess stderr in the dev_fleet worktree ops.

Two sites bound git stderr to a fixed character count before returning it in an
error payload: the worktree-removal failure (head cut, ``[:300]``) and the
rebase-conflict tail (``[-200:]``). Bounding BEFORE redaction can cut a
credential mid-match, leaving a fragment no redaction regex recognises — a tail
cut keeps the credential's RIGHT half, which equally matches nothing. The fix
(issue #7374, same class as PR #7316 / PR #7350) feeds ``_redact`` the FULL
text and applies the bound to its result: a cut of already-redacted text can at
worst split a redaction marker, never a secret.

The behavioral test pins the reachable tail-cut site (``_rebase_locked``'s
conflict path) with the straddle layout: the secret is placed so the tail
window's left edge falls INSIDE it, so a raw slice AND a slice-then-redact
reorder both go red. The structural test then pins the whole module — the
head-cut removal site included — without driving the full locked removal flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_theme_clone_stderr_redact_before_bound import _find_slice_before_redact_offenders

import kiro_crew.apps.builtins.dev_fleet.repository as repository
import kiro_crew.apps.builtins.dev_fleet.runtime as runtime
import kiro_crew.apps.builtins.dev_fleet.worktree_ops as worktree_ops

# The bound applied at the rebase-conflict tail site in _rebase_locked.
_REBASE_TAIL_BOUND = 200

_MODULE_PATH = Path(worktree_ops.__file__)


class TestRebaseConflictTailIsRedactedBeforeTheBound:
    """The rebase-conflict error must never carry a raw credential fragment."""

    async def _rebase_with_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
    ) -> dict:
        # Route the flow to the conflict branch without a real repository:
        # status is clean, the fetch succeeds, the rebase fails with the
        # crafted output, and the abort succeeds. ``_rebase_locked`` reaches
        # these helpers as ``repository.X`` / ``runtime.X`` attribute lookups,
        # so patching the owning modules takes effect.
        rebase_calls: list[list[str]] = []

        async def _git(path: str, *args: str, **kw: object) -> str | None:
            return ""

        async def _run_cmd(cmd: list[str], **kw: object) -> tuple[int, str, str]:
            rebase_calls.append(cmd)
            assert "rebase" in cmd, f"unexpected _run_cmd before the rebase: {cmd}"
            return 1, stdout, ""

        monkeypatch.setattr(repository, "_git", _git)
        monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)

        async def _remote() -> str:
            return "origin"

        monkeypatch.setattr(repository, "_upstream_remote", _remote)
        out = await worktree_ops._rebase_locked({"path": str(tmp_path)})
        assert rebase_calls, "the rebase was never attempted, so this exercised nothing"
        return out

    @pytest.mark.asyncio
    async def test_a_rebase_conflict_does_not_leak_a_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        token = "s3cr3t-remote-token"  # a fake secret, only asserted absent
        stdout = f"fatal: could not read from 'https://ci-bot:{token}@github.example/r.git'\n"
        out = await self._rebase_with_output(monkeypatch, tmp_path, stdout)
        assert out["ok"] is False and out["conflict"] is True
        assert token not in str(out["error"])
        assert "[REDACTED" in str(out["error"])

    @pytest.mark.asyncio
    async def test_the_credential_is_redacted_before_the_tail_cut(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Straddle layout: the tail window's left edge falls INSIDE the token.

        A raw ``[-200:]`` slice keeps the token's RIGHT half verbatim, and the
        cut drops the ``https://user:`` head the userinfo regex needs to
        match, so a slice-then-redact reorder leaks it too. Only
        redact-then-bound scrubs it, so this test goes red if the two steps
        are ever reordered.
        """
        token = "TOKENedge9876543210"  # a fake secret, only asserted absent
        head = f"fatal: could not read from 'https://ci-bot:{token}"
        # Pad AFTER the credential so the tail window opens mid-token: the
        # window's left edge lands ten characters into the token.
        cut_in_token = 10
        tail_len = len("@github.example/r.git'") + (len(token) - cut_in_token)
        pad = "x" * (_REBASE_TAIL_BOUND - tail_len)
        line = f"{head}@github.example/r.git'{pad}"
        # Premise guards: the layout must actually straddle, or this test
        # silently stops pinning the invariant.
        start = line.index(token)
        cut = len(line) - _REBASE_TAIL_BOUND
        assert start < cut < start + len(token)
        assert line.index("https://") < cut

        out = await self._rebase_with_output(monkeypatch, tmp_path, line)
        err = str(out["error"])
        assert token not in err
        # The exact fragment a bound-before-redact implementation would leak —
        # everything of the token right of the cut — must be absent too.
        leaked_suffix = token[cut - start :]
        assert leaked_suffix and leaked_suffix not in err


class TestNoRawBoundedStderrSliceInTheModule:
    """Structural class pin covering BOTH fixed sites: a bounded char slice of
    a DIRECT stderr/stdout expression is sanctioned only when applied to the
    RESULT of a redact call. (A slice of a renamed local assigned from such an
    expression is beyond this scan — the behavioral tests carry that case.)"""

    def test_no_slice_before_redaction(self) -> None:
        assert _find_slice_before_redact_offenders(_MODULE_PATH) == []
