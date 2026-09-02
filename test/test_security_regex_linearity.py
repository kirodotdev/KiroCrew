"""Differential + complexity guards for the two ``security.py`` linearity fixes.

Both fixes are performance-only and MUST be behaviour-preserving, so the tests
here are written as *differentials*: the expected values were captured from the
implementation as it stood immediately BEFORE each change (origin/main
``760d8f570``) and are pinned as literals. A verdict or byte that moves in either
direction fails.

Covered:

* Mesh-3654 -- ``redact_credentials`` pass 1 was ``for m in
  _CREDENTIAL_PATTERNS.finditer(result): result = result.replace(...)``, which
  rebuilt the whole string per match (O(n^2) on credential-dense text). It is now
  a single ``_CREDENTIAL_PATTERNS.sub(...)``. The redacted text AND the
  ``warnings`` list (content *and* order) must be unchanged.
* Mesh-3693 -- eleven branches of the sensitive-path regex were anchored
  ``(?:^|.*[\\s'\\"=:,;])``. The leading ``.*`` is redundant under ``re.search``
  (which retries at every offset) and made matching quadratic in the longest
  line. The anchor is now ``(?:^|[\\s'\\"=:,;])``. This is a DENY surface, so the
  verdict tests below replay positives and negatives to make it obvious that
  nothing became more permissive.
"""

from __future__ import annotations

import re
import time

import pytest

from kiro_crew.security import (
    is_sensitive_bash_command,
    is_sensitive_path,
    redact_credentials,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mesh-3654: redact_credentials pass 1 -- single sub() must be byte-identical
# ─────────────────────────────────────────────────────────────────────────────

# (input, expected_redacted_text, expected_warnings) captured from the
# pre-change loop implementation. Secret-shaped fixtures are written as adjacent
# literals so no single source line is a complete provider token (matches the
# convention in test_security.py, which keeps secret scanners quiet).
_AKIA = "AKIAIOSFODNN7EXAMPLE"
_GHP = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
_ANT = "sk-ant-api03-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
_GLPAT = "glpat-" "xxxx1234xxxx5678xxxx"
_XOXB = "xoxb-" "1234567890-abcdefghij"
_TAG = "[REDACTED: credential]"

REDACTION_GOLDEN: list[tuple[str, str, list[str]]] = [
    (
        f"Found key {_AKIA} in output",
        f"Found key {_TAG} in output",
        ["Redacted credential pattern (20 chars)"],
    ),
    # Two occurrences of the SAME credential: both spans replaced, two warnings.
    # This is the case the old `str.replace(matched, tag, 1)` shape depended on
    # positional luck for -- sub() splices each matched span in place.
    (
        f"a {_AKIA} b {_AKIA} c",
        f"a {_TAG} b {_TAG} c",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (20 chars)",
        ],
    ),
    # Three DIFFERENT credentials -- pins warning ORDER (20, 38, 26 chars),
    # which is the ordering guarantee sub() has to preserve.
    (
        f"first {_AKIA} then {_GHP} and {_XOXB} tail",
        f"first {_TAG} then {_TAG} and {_TAG} tail",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (38 chars)",
            "Redacted credential pattern (26 chars)",
        ],
    ),
    (
        "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        _TAG,
        ["Redacted credential pattern (56 chars)"],
    ),
    (
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
        _TAG,
        ["Redacted credential pattern (45 chars)"],
    ),
    (
        f"Token is {_XOXB}",
        f"Token is {_TAG}",
        ["Redacted credential pattern (26 chars)"],
    ),
    (f"KEY={_GHP}", f"KEY={_TAG}", ["Redacted credential pattern (38 chars)"]),
    (f"KEY={_ANT}", f"KEY={_TAG}", ["Redacted credential pattern (55 chars)"]),
    (f"KEY={_GLPAT}", f"KEY={_TAG}", ["Redacted credential pattern (26 chars)"]),
    (
        "mongodb://user:supersecretpassword@cluster0.example.net/db",
        f"{_TAG}cluster0.example.net/db",
        ["Redacted credential pattern (35 chars)"],
    ),
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r",
        _TAG,
        ["Redacted credential pattern (48 chars)"],
    ),
    # Negatives: the cheap superset gate must still short-circuit to identity.
    (
        "See the PRIVATE KEY handling section of the runbook.",
        "See the PRIVATE KEY handling section of the runbook.",
        [],
    ),
    (
        "just some ordinary log line with no secrets at all",
        "just some ordinary log line with no secrets at all",
        [],
    ),
    ("", "", []),
]


@pytest.mark.parametrize(
    ("text", "expected_text", "expected_warnings"),
    REDACTION_GOLDEN,
    ids=[f"case-{i}" for i in range(len(REDACTION_GOLDEN))],
)
def test_pass1_single_sub_is_byte_identical_to_pre_change_loop(
    text: str, expected_text: str, expected_warnings: list[str]
) -> None:
    """Pass 1 as one ``sub()`` reproduces the old loop's bytes and warnings.

    Differential for Mesh-3654. ``expected_warnings`` is compared with ``==`` on
    the list, so both the CONTENT and the ORDER are pinned -- appending in the
    replacement callback has to keep the left-to-right match order the old
    ``finditer`` loop had.
    """
    result, warnings = redact_credentials(text)
    assert result == expected_text
    assert warnings == expected_warnings


def test_pass1_warning_order_tracks_match_order_not_length() -> None:
    """Warnings come out in match order, not sorted or grouped.

    A replacement callback that batched or reordered its appends would still
    produce identical TEXT, so this asserts the ordering separately.
    """
    text = f"{_ANT} {_AKIA} {_GHP}"
    _, warnings = redact_credentials(text)
    assert warnings == [
        f"Redacted credential pattern ({len(_ANT)} chars)",
        f"Redacted credential pattern ({len(_AKIA)} chars)",
        f"Redacted credential pattern ({len(_GHP)} chars)",
    ]


def test_pass1_warnings_still_carry_no_secret_bytes() -> None:
    """The replacement callback must not slice the match into the warning."""
    text = f"KEY={_ANT}"
    _, warnings = redact_credentials(text)
    joined = " ".join(warnings)
    assert _ANT not in joined
    assert _ANT[:20] not in joined
    assert "Redacted credential pattern" in joined


def test_pass1_is_linear_on_credential_dense_text() -> None:
    """Complexity guard for Mesh-3654.

    The old shape rebuilt the whole string per match, so redacting N credentials
    in an N-credential string was O(N^2). 4000 credentials (~84 KB) is
    sub-second as one ``sub()`` pass; the generous ceiling keeps this off slow
    CI's flake list while still failing hard if the per-match rebuild returns.
    """
    dense = f"{_AKIA} " * 4000
    started = time.perf_counter()
    result, warnings = redact_credentials(dense)
    elapsed = time.perf_counter() - started
    assert len(warnings) == 4000
    assert _AKIA not in result
    assert elapsed < 5.0, f"pass 1 took {elapsed:.2f}s -- per-match string rebuild is back"


# ─────────────────────────────────────────────────────────────────────────────
# Mesh-3693: sensitive-path anchor -- zero verdict change (DENY surface)
# ─────────────────────────────────────────────────────────────────────────────

# (command, expected_verdict) captured from the pre-change regex. Ordered so the
# separator-boundary cases the character class exists for are explicit: the path
# preceded by a space, a single quote, a double quote, `=`, `:`, `,`, `;`, and at
# string start -- plus mid-token cases that must stay NEGATIVE.
SENSITIVE_COMMAND_GOLDEN: list[tuple[str, bool]] = [
    # ── separator boundaries: each must stay a HIT ──
    ("cat ~/.aws/credentials", True),  # space
    ("cat '~/.aws/credentials'", True),  # single quote
    ('cat "~/.ssh/id_rsa"', True),  # double quote
    ("FOO=~/.aws/credentials", True),  # `=` (VAR=path)
    ("PATH=/x:~/.ssh/id_rsa", True),  # `:` (PATH-style list)
    ("cmd --a=1,~/.aws/credentials", True),  # `,`
    ("run;~/.aws/credentials", True),  # `;`
    ("~/.aws/credentials", True),  # start-of-string (`^`)
    ("echo x\n~/.aws/credentials", True),  # newline is in the class
    # ── the same hits further into the line: `.*` was never what found these,
    #    `re.search` retrying at every offset was ──
    ("a b c d e f g h ~/.aws/credentials", True),
    ("prefix text then FOO=bar:~/.aws/credentials suffix", True),
    ("deploy --flag ~/.ssh/id_rsa", True),
    ("a,~/.gnupg/secring.gpg", True),
    # ── other spellings that route through the rewritten branches ──
    ("cat $HOME/.aws/credentials", True),
    ("type %USERPROFILE%\\.aws\\credentials", True),
    ("type $env:USERPROFILE\\.ssh\\id_rsa", True),
    ("cp ~/.kiro/agents/x.json /tmp/y", True),
    # ── embedded MID-TOKEN: no separator immediately before the path, so the
    #    anchor must NOT fire. These are the cases that would flip to True if
    #    the character class were dropped along with the `.*`. ──
    ("xyz~/.aws/credentials", False),
    ("FOO=bar~/.aws/credentials", False),
    ("VAR=x~/.gnupg/secring.gpg", False),
    ("printf q~/.aws/credentials", False),
    # ── ordinary commands: must stay allowed ──
    ("ls -la", False),
    ("echo hello world", False),
    ("cat myfile.txt", False),
    ("notaws/credentials", False),
    ("python -c 'print(1)'", False),
    ("grep -r pattern src/", False),
    ("cat ./relative/notsensitive.json", False),
    ("git status", False),
    ("make build", False),
]


@pytest.mark.parametrize(("command", "expected"), SENSITIVE_COMMAND_GOLDEN)
def test_sensitive_bash_verdicts_unchanged_by_anchor_rewrite(command: str, expected: bool) -> None:
    """Differential for Mesh-3693 on ``is_sensitive_bash_command``.

    Every verdict is pinned to what the pre-change regex returned. Dropping the
    redundant ``.*`` cannot change any of them: the alternative is still ``^`` or
    a single separator character, and ``re.search`` already retried at every
    offset. A regression in EITHER direction fails here -- the negatives are what
    make it obvious the gate did not become more permissive.
    """
    assert bool(is_sensitive_bash_command(command)) is expected


SENSITIVE_PATH_GOLDEN: list[tuple[str, bool]] = [
    ("~/.aws/credentials", True),
    ("~/.ssh/id_rsa", True),
    ("~/.gnupg/secring.gpg", True),
    ("/tmp/harmless.txt", False),
    ("./README.md", False),
    ("src/kiro_crew/security.py", False),
    ("notes.md", False),
]


@pytest.mark.parametrize(("path", "expected"), SENSITIVE_PATH_GOLDEN)
def test_sensitive_path_verdicts_unchanged_by_anchor_rewrite(path: str, expected: bool) -> None:
    """Differential for Mesh-3693 on ``is_sensitive_path``."""
    assert bool(is_sensitive_path(path)) is expected


def test_sensitive_anchor_has_no_leading_wildcard() -> None:
    """Source guard: the redundant ``.*`` must not come back.

    ``_build_sensitive_regex`` is the only place these anchors are written. The
    check is on the source text rather than the compiled pattern because the
    compiled form interpolates the path alternations and is impractical to
    assert against.
    """
    from kiro_crew import security as security_mod

    source = inspect_source(security_mod._build_sensitive_regex)
    assert r"""(?:^|.*[\s'\"=:,;])""" not in source, (
        "a leading `.*` is back in the sensitive-path anchor -- it is redundant "
        "under re.search and makes matching quadratic in the longest line"
    )
    # And the fixed form is still there, on every branch it was applied to.
    # Count the BRANCH spelling (``rf"|`` prefix) so the explanatory comment in
    # `_build_sensitive_regex`, which quotes the anchor in prose, is not counted.
    branch_anchor = r"""rf"|(?:^|[\s'\"=:,;])"""
    assert source.count(branch_anchor) == 11


def inspect_source(func: object) -> str:
    """``inspect.getsource`` indirection kept local so the test module has one import."""
    import inspect

    return inspect.getsource(func)  # type: ignore[arg-type]


def test_long_nonshell_line_does_not_blow_up() -> None:
    """Complexity guard for Mesh-3693.

    A ~20 KB newline-free non-shell string is the worst case for the old anchor:
    eleven branches each retried a greedy ``.*`` from every offset. Measured on
    the dev box this took ~27 s before the rewrite and ~1.5 s after, so a 6 s
    ceiling clears the fixed path by ~4x while the quadratic form overshoots by
    ~4.5x. Deliberately generous -- this test exists to catch a complexity
    regression, not to benchmark CI.
    """
    blob = "abcdefgh " * 2500
    assert len(blob) > 20_000
    started = time.perf_counter()
    verdict = is_sensitive_bash_command(blob)
    elapsed = time.perf_counter() - started
    assert bool(verdict) is False
    assert elapsed < 6.0, (
        f"is_sensitive_bash_command took {elapsed:.2f}s on a 20 KB line -- "
        "a leading `.*` in the sensitive-path anchor is quadratic"
    )


def test_credential_pattern_module_still_compiles_one_alternation() -> None:
    """Invariant: the rewritten pass 1 still uses the shared compiled pattern.

    Guards against a future refactor swapping in a locally compiled regex, which
    would silently drop the ``_might_contain_credential`` pre-filter pairing.
    """
    from kiro_crew import security as security_mod

    assert isinstance(security_mod._CREDENTIAL_PATTERNS, re.Pattern)
    body = inspect_source(security_mod.redact_credentials)
    assert "_CREDENTIAL_PATTERNS.sub(" in body
    assert "_might_contain_credential(result)" in body
