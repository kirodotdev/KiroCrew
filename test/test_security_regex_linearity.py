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
* Mesh-3693 -- the sensitive-path regex anchor rewrite. That regex is gone (the
  shell gate no longer matches paths in command text; the OS sandbox and
  ``is_sensitive_path`` hold the fence), so what remains of the differential is
  the ``is_sensitive_path`` half, which pins that the path gate's verdicts did
  not move.
"""

from __future__ import annotations

import re
import time

import pytest

from kiro_crew.security import is_sensitive_path, redact_credentials

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
# Mesh-3693: sensitive-path verdicts -- zero change (DENY surface)
# ─────────────────────────────────────────────────────────────────────────────

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


def inspect_source(func: object) -> str:
    """``inspect.getsource`` indirection kept local so the test module has one import."""
    import inspect

    return inspect.getsource(func)  # type: ignore[arg-type]


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
