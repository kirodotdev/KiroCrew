#!/usr/bin/env python3
"""check_harness_parity.py — keep the Kiro harness first-class on lines a change adds.

Kiro Crew drives one first-class agent harness, ``kiro-cli``
(``ACP_BACKEND_KIRO``, spelled ``""``), plus adapted ones: the
``ACP_BACKEND_CLAUDE`` seam, KAS, and whatever a bring-your-own adapter
registers next. An added harness may only adapt itself to the seams the Kiro
harness already runs through; it may not move, widen, or generalize them.

The defect class this gate catches is *silent capture*: a call site that spells
"this is Kiro" as the ABSENCE of another harness. It reads correctly with two
backends and then hands harness number three a capability, a sandbox waiver, or
a session label nobody granted it — and it fails toward the permissive answer,
so nothing goes red. Two such sites shipped before this gate existed
(``AcpProvider.is_session_sharing_eligible`` and ``AcpRuntime.spawn``'s
``is_kiro_cli``); both were one-line positive-test fixes.

The invariants, their ids, and what pins each one:
docs/system-specs/modules/harness-parity.md. Rules here enforce group B (H5-H8);
the structural invariants are pinned by ``test/test_harness_parity.py``.

## Why diff-scoped and not whole-tree

The tree carries eleven pre-existing negative identity tests, most of them in
the claude seam, and converting them all is a separate change with its
own review. A whole-tree gate would fail every PR until that lands and charge
the break to whoever pushed next. Added lines are complete for regression — a
line only reaches ``main`` through a diff that added it — and the whole-tree
count is still printed as a non-failing report so the backlog stays visible.

## Usage

    # enforce on what this branch adds (exit 1 on any violation)
    HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py

    # report the whole-tree backlog, enforce nothing (exit 0)
    python3 scripts/check_harness_parity.py

    # scan explicit files, ignoring git entirely
    python3 scripts/check_harness_parity.py src/kiro_crew/acp/runtime.py

    # self-test: plant one probe per rule, assert each verdict
    python3 scripts/check_harness_parity.py --test

## Escape hatch

A line the rules below model wrongly can opt out with a ``harness-ok`` marker in
a trailing comment, followed by the reason. It is unscoped and silences the
whole line, so a reviewer should ask why the positive form does not work.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only the backend package and its consumers can hold a harness identity test.
# Widening this to the whole tree buys nothing and costs a scan of 6,000 files.
SCAN_ROOTS = ("src/kiro_crew/",)
SCAN_SUFFIX = ".py"

# This gate and its test spell every forbidden form out literally, and the
# vocabulary module (VOCABULARY_PATH below) is the vocabulary's home rather than a
# consumer of it.
SKIP_PATHS = frozenset(
    {
        "scripts/check_harness_parity.py",
        "test/test_harness_parity.py",
    }
)

# The one module allowed to DEFINE harness identifiers and membership sets.
#
# Moved out of ``acp/types.py`` deliberately: importing anything under
# ``kiro_crew.acp`` executes that package's ``__init__`` (client + runtime), so the
# config loader and the dashboard could not read the vocabulary and each kept a
# literal copy of the selectable list instead — the drift this rule exists to
# prevent, reached by obeying it. ``acp_backends`` imports nothing from
# ``kiro_crew.acp``, so every consumer can name the constants instead of copying
# them. The invariant is unchanged: exactly ONE module defines them, and
# ``acp/types.py`` now re-exports from here (an import, not an assignment, so it
# does not match this rule).
VOCABULARY_PATH = "src/kiro_crew/acp_backends.py"

SUPPRESSION = re.compile(r"harness-ok")

# Harness identifiers as string literals. A bare literal is forbidden even where
# the comparison is positive, because the value of ACP_BACKEND_KIRO is the empty
# string and only the named constant makes that legible.
_HARNESS_LITERAL = r"(?:kiro|claude|kas|claude_code|kiro-cli)"


@dataclass(frozen=True)
class Rule:
    """One forbidden line shape.

    ``fix`` is the positive form to write instead. It is printed with every
    violation, because a gate that only says no teaches nothing.
    """

    rule_id: str
    invariant: str
    pattern: re.Pattern[str]
    message: str
    fix: str
    # Paths this rule does not apply to, matched by exact repo-relative path.
    exempt: frozenset[str] = frozenset()


RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="negative-identity",
        invariant="H5",
        pattern=re.compile(
            r"not\s+(?:[A-Za-z_][\w.]*\.)?(?:is_(?:kiro|claude|kas)_backend"
            r"|_is_(?:kiro|claude|kas))\b"
        ),
        message="harness identity expressed as the absence of another harness",
        fix="test the harness positively (`is_kiro_backend`, "
        "`backend == ACP_BACKEND_KIRO`) or by membership in a named set",
    ),
    Rule(
        rule_id="negative-constant",
        invariant="H5",
        pattern=re.compile(r"(?:!=\s*ACP_BACKEND_[A-Z_]+|ACP_BACKEND_[A-Z_]+\s*!=)"),
        message="harness identity tested by inequality against one backend",
        fix="use `== ACP_BACKEND_<THIS>` or `in ACP_BACKENDS_<CAPABILITY>` — an "
        "inequality silently captures every harness added later",
    ),
    Rule(
        rule_id="bare-literal",
        invariant="H8",
        pattern=re.compile(
            rf"""(?:backend|harness)\s*[=!]=\s*(?P<q>["']){_HARNESS_LITERAL}(?P=q)"""
        ),
        message="harness compared against a bare string literal",
        fix="compare against the named constant from acp/types.py "
        "(ACP_BACKEND_KIRO is the empty string; only the name is legible)",
    ),
    Rule(
        rule_id="sandbox-delegation",
        invariant="H7",
        # The flag makes wrap_argv SKIP Crew's own seatbelt, so it fails OPEN.
        # A bool literal is fine (the argv is known statically at that site);
        # anything derived from a negation is not.
        pattern=re.compile(r"is_kiro_cli\s*=\s*(?!True\b|False\b)(?=.*(?:\bnot\b|!=))"),
        message="sandbox delegation derived from a negative harness test "
        "(fails OPEN: Crew's seatbelt is skipped for a harness with no "
        "internal sandbox of its own)",
        fix="`is_kiro_cli=<backend> in ACP_BACKENDS_INTERNAL_SANDBOX`",
    ),
    Rule(
        rule_id="vocabulary-home",
        invariant="H8",
        pattern=re.compile(r"^\s*ACP_BACKEND(?:S)?_[A-Z_]+\s*(?::[^=]+)?=\s*\S"),
        message="harness identifier or membership set defined outside the " "vocabulary module",
        fix=f"define it in {VOCABULARY_PATH} and add every new identifier to "
        "ACP_BACKENDS_KNOWN, or provider construction will not reject a typo",
        exempt=frozenset({VOCABULARY_PATH}),
    ),
    Rule(
        rule_id="non-kiro-default",
        invariant="H1",
        pattern=re.compile(r"(?:default\s*=\s*|:\s*str\s*=\s*)ACP_BACKEND_(?!KIRO\b)[A-Z_]+"),
        message="a harness other than Kiro used as a default",
        fix="default to ACP_BACKEND_KIRO — an operator who configures nothing, "
        "and one whose configuration is unusable, both get the Kiro harness",
    ),
)


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    rule: Rule
    text: str

    def render(self) -> str:
        excerpt = self.text.strip()[:160]
        return (
            f"{self.path}:{self.line_no}: [{self.rule.rule_id} / "
            f"{self.rule.invariant}] {self.rule.message}\n"
            f"    {excerpt}\n"
            f"    fix: {self.rule.fix}"
        )


def in_scope(path: str) -> bool:
    if path in SKIP_PATHS:
        return False
    if not path.endswith(SCAN_SUFFIX):
        return False
    return any(path.startswith(root) for root in SCAN_ROOTS)


# Python 3 has no backtick syntax, so a backtick span is always prose — the
# docstring convention for naming a symbol (``not is_claude_backend``). Naming a
# forbidden form in order to forbid it must not be a violation of it.
_INLINE_CODE = re.compile(r"``[^`]*``|`[^`]*`")


def code_part(text: str) -> str:
    """``text`` up to an unquoted ``#``.

    Quote state is tracked rather than splitting on the first ``#``, because a
    string literal may contain one (``if sep == "#" and not self._is_claude:``)
    and truncating there would hide the real call site behind it. A gate that
    misses a line is worse than one that reports a comment.
    """
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return text[:i]
        i += 1
    return text


def scannable(text: str) -> str:
    """The part of a line that is executable code.

    Length-preserving substitution keeps column positions honest for any future
    caller that wants them.
    """
    return _INLINE_CODE.sub(lambda m: " " * len(m.group()), code_part(text))


def scan_line(path: str, line_no: int, text: str) -> Iterable[Violation]:
    # Suppression is read from the RAW line: the marker lives in a comment, which
    # `scannable` has already removed by the time the rules run.
    if SUPPRESSION.search(text):
        return
    candidate = scannable(text)
    if not candidate.strip():
        return
    for rule in RULES:
        if path in rule.exempt:
            continue
        if rule.pattern.search(candidate):
            yield Violation(path, line_no, rule, text)


def read_lines(path: str) -> list[str] | None:
    """File contents as lines, or None when it cannot be read as UTF-8 text."""
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8", newline="") as fh:
            return fh.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def scan_file(path: str) -> Iterable[Violation]:
    lines = read_lines(path)
    if lines is None:
        return
    for i, text in enumerate(lines, start=1):
        yield from scan_line(path, i, text)


def scan_lines(path: str, lines: list[str], only: set[int]) -> Iterable[Violation]:
    for line_no in sorted(only):
        if 1 <= line_no <= len(lines):
            yield from scan_line(path, line_no, lines[line_no - 1])


# ---------------------------------------------------------------------------
# git plumbing (same contract as scripts/check_brand_name.py)
# ---------------------------------------------------------------------------


def git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def tracked_files() -> list[str]:
    return [p for p in git(["ls-files"]).splitlines() if in_scope(p)]


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing (see ``scripts/ratchet_scope.py``).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` — and not at all
    when a test loads this gate by path. Shared so the env-base gates and the
    merge-ref ratchets agree on what a change added; a private copy per gate
    is how they would come to disagree. Loaded lazily so the explicit-file and
    ``--test`` modes, which never touch git, do not require it.
    """
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


def diff_base(base: str) -> str:
    """The commit to measure against (see ``ratchet_scope.resolve_base``)."""
    return _scope().resolve_base(base)


def changed_paths(frm: str) -> list[str]:
    """In-scope paths this change touches (``ratchet_scope.changed_paths_at``)."""
    try:
        paths = _scope().changed_paths_at(frm)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::harness gate: cannot diff against {frm} — the base commit "
            f"is not present. Fetch it before running, or unset HARNESS_BASE_REF "
            f"to report whole-tree counts without enforcing.\n{exc.stderr}"
        )
    return [p for p in paths if in_scope(p)]


def added_lines(frm: str, path: str) -> set[int]:
    """1-based line numbers this change adds to ``path``.

    Delegates to ``ratchet_scope.added_lines_at``: base-to-working-tree (a
    local run sees uncommitted edits), ``--text`` so a ``-diff`` gitattribute
    cannot hide a file, and a pure deletion contributes nothing.
    """
    return _scope().added_lines_at(frm, path)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (label, path, line, expected rule id or None)
PROBES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "negative-property",
        "src/kiro_crew/providers/acp.py",
        "        return not self.is_claude_backend",
        "negative-identity",
    ),
    (
        "negative-private",
        "src/kiro_crew/acp/client.py",
        "        if not self._is_claude:",
        "negative-identity",
    ),
    (
        "negative-module-helper",
        "src/kiro_crew/session.py",
        "    if not provider.is_kas_backend:",
        "negative-identity",
    ),
    (
        "negative-constant",
        "src/kiro_crew/acp/runtime.py",
        "        if self._acp_backend != ACP_BACKEND_KAS:",
        "negative-constant",
    ),
    (
        "negative-constant-reversed",
        "src/kiro_crew/acp/runtime.py",
        "        if ACP_BACKEND_KAS != self._acp_backend:",
        "negative-constant",
    ),
    (
        "bare-literal",
        "src/kiro_crew/session.py",
        '    return backend == "claude"',
        "bare-literal",
    ),
    (
        "sandbox-delegation",
        "src/kiro_crew/acp/runtime.py",
        "            is_kiro_cli=self._acp_backend != ACP_BACKEND_KAS,",
        "sandbox-delegation",
    ),
    (
        "sandbox-delegation-negation",
        "src/kiro_crew/acp/client.py",
        "            is_kiro_cli=not self._is_claude,",
        "negative-identity",
    ),
    (
        "sandbox-delegation-negation-also-flags-the-flag",
        "src/kiro_crew/acp/client.py",
        "            is_kiro_cli=not self._is_claude,",
        "sandbox-delegation",
    ),
    (
        "hash-inside-string-does-not-truncate",
        "src/kiro_crew/acp/client.py",
        '        if sep == "#" and not self._is_claude:',
        "negative-identity",
    ),
    (
        "vocabulary-elsewhere",
        "src/kiro_crew/providers/acp.py",
        'ACP_BACKEND_BYO = "byo"',
        "vocabulary-home",
    ),
    (
        "membership-set-elsewhere",
        "src/kiro_crew/subagent.py",
        "ACP_BACKENDS_FAST = frozenset({ACP_BACKEND_KIRO})",
        "vocabulary-home",
    ),
    (
        "non-kiro-default",
        "src/kiro_crew/acp/runtime.py",
        "        acp_backend: str = ACP_BACKEND_KAS,",
        "non-kiro-default",
    ),
    (
        "non-kiro-field-default",
        "src/kiro_crew/config/loader.py",
        "        default=ACP_BACKEND_CLAUDE,",
        "non-kiro-default",
    ),
    # ── allowed forms: each must produce NO hit ──
    (
        "positive-property",
        "src/kiro_crew/providers/acp.py",
        "        return self.is_kiro_backend",
        None,
    ),
    (
        "positive-membership",
        "src/kiro_crew/providers/acp.py",
        "        return self._client.backend in ACP_BACKENDS_SESSION_SHARING",
        None,
    ),
    (
        "positive-constant",
        "src/kiro_crew/acp/runtime.py",
        "        if self._acp_backend == ACP_BACKEND_KAS:",
        None,
    ),
    (
        "sandbox-membership",
        "src/kiro_crew/acp/runtime.py",
        "            is_kiro_cli=self._acp_backend in ACP_BACKENDS_INTERNAL_SANDBOX,",
        None,
    ),
    (
        "sandbox-literal",
        "src/kiro_crew/dashboard/handlers/agents.py",
        "    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)",
        None,
    ),
    (
        "vocabulary-at-home",
        VOCABULARY_PATH,
        'ACP_BACKEND_BYO = "byo"',
        None,
    ),
    (
        "kiro-default",
        "src/kiro_crew/acp/runtime.py",
        "        acp_backend: str = ACP_BACKEND_KIRO,",
        None,
    ),
    (
        "unrelated-backend-word",
        "src/kiro_crew/sandbox.py",
        '    if backend == "namespace":',
        None,
    ),
    (
        "comment-naming-the-form",
        "src/kiro_crew/acp/client.py",
        "        # never write `not self._is_claude` here",
        None,
    ),
    (
        "docstring-prose-naming-the-form",
        "src/kiro_crew/providers/acp.py",
        "        inferring it from ``not is_claude_backend`` — an inference that",
        None,
    ),
    (
        "trailing-comment-naming-the-form",
        "src/kiro_crew/acp/runtime.py",
        "        flag = True  # not self._is_claude, historically",
        None,
    ),
    (
        "suppressed",
        "src/kiro_crew/acp/client.py",
        "        x = not self._is_claude  # harness-ok: dormant seam, see H5",
        None,
    ),
)


def self_test() -> int:
    failures = 0
    for label, path, line, expected in PROBES:
        got = {v.rule.rule_id for v in scan_line(path, 1, line)}
        # A line can violate several rules at once (a negative constant used as
        # the sandbox flag violates two), so assert membership rather than
        # first-hit: asserting order would pin the RULES tuple's ordering, which
        # is not part of the contract.
        ok = (expected in got) if expected else not got
        if not ok:
            print(
                f"  FAIL {label}: expected {expected!r}, got {sorted(got) or 'no hit'} "
                f"— {line.strip()}"
            )
            failures += 1
        else:
            print(f"  ok   {label}")

    # Every rule must be exercised by at least one probe, or a typo that
    # disables one ships green.
    covered = {expected for _, _, _, expected in PROBES if expected}
    missing = sorted({r.rule_id for r in RULES} - covered)
    if missing:
        print(f"  FAIL rule-coverage: no probe exercises {missing}")
        failures += 1
    else:
        print(f"  ok   rule-coverage ({len(RULES)} rules)")

    print("self-test passed" if not failures else f"self-test FAILED ({failures})")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report(violations: Iterable[Violation], *, enforcing: bool, base: str | None) -> int:
    violations = list(violations)
    if not violations:
        scope = f"lines added since {base}" if enforcing else "whole tree"
        print(f"harness gate: Kiro identity tested positively in the {scope} ✓")
        return 0

    if enforcing:
        print(
            f"::error::harness gate: {len(violations)} line(s) added by this change "
            f"let a harness other than Kiro inherit something by default. The Kiro "
            f"harness is first-class: an added harness adapts to these seams, it "
            f"does not widen them. See docs/system-specs/modules/harness-parity.md."
        )
    else:
        print(
            f"::notice::harness gate report: {len(violations)} pre-existing line(s) "
            f"test harness identity negatively. Not enforced here; only lines a "
            f"change adds are gated."
        )
    shown = 200 if enforcing else 40
    for v in violations[:shown]:
        print(v.render())
    if len(violations) > shown:
        print(f"... and {len(violations) - shown} more")
    if enforcing:
        print(
            "\nIf a rule models your line wrongly, a trailing `harness-ok: <reason>` "
            "comment silences the whole line — a reviewer will ask why the positive "
            "form does not work."
        )
    return 1 if enforcing else 0


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change adds. Fails closed on anything unreadable."""
    frm = diff_base(base)
    found: list[Violation] = []
    unreadable: list[str] = []
    for path in changed_paths(frm):
        lines = added_lines(frm, path)
        if not lines:
            continue
        content = read_lines(path)
        if content is None:
            unreadable.append(path)
            continue
        found.extend(scan_lines(path, content, lines))

    if unreadable:
        # Reporting nothing for a file the change touched is how a gate quietly
        # stops gating, so refuse to pass instead of skipping.
        print(
            "::error::harness gate: cannot read these changed files as UTF-8 text, "
            "so the harness identity tests in them were never checked:"
        )
        for path in unreadable:
            print(f"  {path}")
        return 1

    return report(found, enforcing=True, base=base)


def force_utf8_output() -> None:
    """Print UTF-8 whatever the console's default encoding is.

    The clean verdict ends in a check mark, and a Windows cp1252 console raises
    ``UnicodeEncodeError`` on it — turning a PASS into a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    force_utf8_output()
    if "--test" in argv:
        return self_test()

    explicit = [a for a in argv if not a.startswith("-")]
    if explicit:
        unreadable = [p for p in explicit if read_lines(p) is None]
        if unreadable:
            print(
                "::error::harness gate: cannot read these paths as UTF-8 text, so "
                "the harness identity tests in them were never checked:"
            )
            for path in unreadable:
                print(f"  {path}")
            return 1
        found = [v for p in explicit for v in scan_file(p)]
        return report(found, enforcing=True, base=None)

    base = os.environ.get("HARNESS_BASE_REF", "").strip()
    if base:
        return enforce_diff(base)

    found = [v for path in tracked_files() for v in scan_file(path)]
    return report(found, enforcing=False, base=None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
