"""Redact-before-bound on ``_clone_github``'s stderr (dashboard themes).

``_clone_github`` reaches git with a user-supplied URL, and on an auth failure
git echoes that URL — userinfo and all — to stderr. Bounding BEFORE redaction
can cut the credential mid-match, leaving a fragment that no longer matches any
credential regex, so it escapes redaction and lands in the returned error text.
The fix (issue #7374, same class as PR #7316 / PR #7350) is
``security.redact_and_truncate``: scrub the FULL text first, bound after.

The behavioral test pins the site with the straddle layout: the secret is
placed so the 200-char bound falls INSIDE it, so a raw slice AND a
slice-then-redact reorder both go red. The structural test then pins the whole
module: any bounded char slice of a stderr/stdout expression must be applied to
the RESULT of a redact call, never before it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import themes as th

# The bound applied at the clone-failure site in _clone_github.
_CLONE_BOUND = 200

_MODULE_PATH = Path(th.__file__)


class TestCloneFailureStderrIsRedactedBeforeTheBound:
    """The clone failure path must never return a raw credential fragment."""

    @pytest.fixture(autouse=True)
    def _no_real_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `sandboxed_spawn_argv` raises SandboxUnavailableError on hosts
        # without an OS sandbox backend (every CI runner), so stub it to a
        # passthrough — only the error-mapping branch is under test.
        monkeypatch.setattr(
            th, "sandboxed_spawn_argv", lambda argv, *a, **k: (list(argv), {}, None)
        )

    def _clone_with_stderr(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stderr: str
    ) -> str | None:
        class _Proc:
            returncode = 128
            stdout = ""

        _Proc.stderr = stderr

        # Patch the module's own run_limited seam: narrower than stubbing the
        # stdlib subprocess.run underneath it, and it states the seam under
        # test explicitly.
        monkeypatch.setattr(th, "run_limited", lambda argv, **kw: _Proc())
        return th._clone_github("https://github.com/o/r", tmp_path / "clone")

    def test_a_clone_failure_does_not_leak_a_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        token = "s3cr3t-remote-token"  # a fake secret, only asserted absent
        stderr = f"fatal: could not read from 'https://ci-bot:{token}@github.example/r.git'\n"
        err = self._clone_with_stderr(monkeypatch, tmp_path, stderr)
        assert err is not None and err.startswith("git clone failed:")
        assert token not in err
        assert "[REDACTED" in err

    def test_the_credential_is_redacted_before_it_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Straddle layout: the 200-char bound falls INSIDE the token.

        A raw slice keeps the token's left half verbatim. A slice-then-redact
        reorder keeps it too, because the cut drops the ``@host`` tail the
        userinfo regex needs to match. Only redact-then-bound scrubs it, so
        this test goes red if the two steps are ever reordered.
        """
        token = "TOKENedge9876543210"  # a fake secret, only asserted absent
        userinfo = "https://ci-bot:"
        # Lay the token out to START ten characters shy of the bound, so the
        # bound cuts into it and the '@host' tail lands beyond the bound.
        pad = "x" * (_CLONE_BOUND - 10 - len("fatal: ") - len(userinfo))
        line = f"fatal: {pad}{userinfo}{token}@github.example/r.git"
        # Premise guards: the layout must actually straddle, or this test
        # silently stops pinning the invariant.
        start = line.index(token)
        assert start < _CLONE_BOUND < start + len(token)
        assert line.index("@") > _CLONE_BOUND

        err = self._clone_with_stderr(monkeypatch, tmp_path, line + "\n")
        assert err is not None
        assert token not in err
        # The exact fragment a bound-before-redact implementation would leak —
        # everything of the token left of the bound — must be absent too.
        leaked_prefix = token[: _CLONE_BOUND - start]
        assert leaked_prefix and leaked_prefix not in err


class TestNoRawBoundedStderrSliceInTheModule:
    """Structural class pin: a bounded char slice of subprocess output text is
    sanctioned only when applied to the RESULT of a redact call (redaction ran
    over the full text; the cut can at worst split a redaction marker)."""

    def test_no_slice_before_redaction(self) -> None:
        assert _find_slice_before_redact_offenders(_MODULE_PATH) == []


def _find_slice_before_redact_offenders(path: Path) -> list[str]:
    """AST scan: bounded char slices over DIRECT stderr/stdout expressions.

    A ``Subscript`` slice whose value expression mentions stderr/stdout is an
    offender unless the sliced value IS a redact call. Deliberate limits: a
    slice of a renamed local assigned from such an expression, or of a
    string-keyed subscript (``out["stderr"]``), is beyond this scan — the
    behavioral straddle tests carry those shapes. Numeric bounds under 10
    are ignored to keep the legitimate line-list idiom (``splitlines()[-1:]``)
    out of scope: a whole-line cut severs no secret, it is the CHAR slice that
    does. Shared by the themes and dev_fleet redact-before-bound tests.
    """
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        bound = None
        for edge in (node.slice.lower, node.slice.upper):
            if isinstance(edge, ast.Constant) and isinstance(edge.value, int):
                bound = edge.value
            elif (
                isinstance(edge, ast.UnaryOp)
                and isinstance(edge.op, ast.USub)
                and isinstance(edge.operand, ast.Constant)
                and isinstance(edge.operand.value, int)
            ):
                bound = edge.operand.value
        if bound is None or bound < 10:
            continue
        mentioned = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        mentioned |= {a.attr for a in ast.walk(node.value) if isinstance(a, ast.Attribute)}
        if not any("stderr" in name or "stdout" in name for name in mentioned):
            continue
        value = node.value
        sanctioned = isinstance(value, ast.Call) and (
            (isinstance(value.func, ast.Name) and "redact" in value.func.id)
            or (isinstance(value.func, ast.Attribute) and "redact" in value.func.attr)
        )
        if not sanctioned:
            offenders.append(f"{path.name}:{node.lineno}")
    return offenders
