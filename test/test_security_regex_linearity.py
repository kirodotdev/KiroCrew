"""Differential + complexity guards for the two ``security.py`` linearity fixes.

Both fixes are performance-only and MUST be behaviour-preserving, so the tests
here are written as *differentials*: the expected values were captured from the
implementation as it stood immediately BEFORE each change (origin/main
``760d8f570``) and are pinned as literals. A verdict or byte that moves in either
direction fails.

Covered:

* ``redact_credentials`` pass 1 was ``for m in
  _CREDENTIAL_PATTERNS.finditer(result): result = result.replace(...)``, which
  rebuilt the whole string per match (O(n^2) on credential-dense text). Pass 1
  walks ``_CREDENTIAL_PATTERNS.finditer(text)`` exactly once and records each
  match as a span; the string is spliced a single time after every pass. The
  redacted text AND the ``warnings`` list (content *and* order) must be
  unchanged.
* The sensitive-path regex anchor rewrite. That regex is gone (the
  shell gate does not match paths in command text; the OS sandbox and
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
# redact_credentials pass 1 -- single sub() must be byte-identical
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
    # Key-anchored branches redact the VALUE group only: the key and separator
    # survive and the reported length is the value's, not the whole match's.
    (
        "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        f"SecretAccessKey={_TAG}",
        ["Redacted credential pattern (40 chars)"],
    ),
    (
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
        f"aws_secret_access_key = {_TAG}",
        ["Redacted credential pattern (21 chars)"],
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

    Differential for the pass-1 rewrite. ``expected_warnings`` is compared with ``==`` on
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
    """Complexity guard for the pass-1 rewrite.

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


def test_pass4_is_linear_on_dense_partly_covered_token_values() -> None:
    """Complexity guard for pass 4's coalescing of a partly-covered value.

    A key-anchored pair nested in every `?token=` value is the shape that makes
    each value PARTLY claimed (pass 1 takes the secret, the key prefix is the
    gap). A per-match rescan and rebuild of every earlier claim would make N
    such values O(N^2) on the event loop that runs ``redact_credentials``
    synchronously; the sweep is O(M log C + C) for M matches over C claims --
    a bisect per match into the sorted claims, and each claim visited once.
    12000 values (~740 KB): the
    per-match rebuild measured 8.9 s here (and 4x per doubling), the sweep is
    sub-second; the ceiling matches the pass-1 guard above.
    """
    dense = "?token=aws_secret_access_key=test-secret-not-a-credential-0123456789 " * 12000
    started = time.perf_counter()
    result, warnings = redact_credentials(dense)
    elapsed = time.perf_counter() - started
    assert result == "?token=[REDACTED: credential] " * 12000
    assert len(warnings) == 24000
    assert elapsed < 5.0, f"pass 4 took {elapsed:.2f}s -- per-match claim rebuild is back"


def test_pass1_quoted_value_scan_is_linear_on_dense_quoted_pairs() -> None:
    """Complexity guard for the quoted-value boundary scan of pass 1.

    Every quoted key-anchored value scans forward for its closing quote. The
    scan is bounded twice over: it ends at the next same-kind quote, which a
    later quoted pair on the line carries, so a scan that runs to the line's
    end is the LAST pair of its kind on that line; and a claim's reach skips the
    matches inside it. 12000 pairs on one line (each pair's opening quote is
    the previous pair's close) and 12000 unterminated pairs, one per line,
    are both linear; a scan that re-walked to the end of the text per match
    would be O(N^2) on the event loop that runs ``redact_credentials``
    synchronously. The ceiling matches the pass-1 guard above.
    """
    one_line = '"aws_secret_access_key": "test-secret-not-a-credential-0123 tail ' * 12000
    per_line = "\n".join(
        f'"SessionToken": "test-session-not-a-credential-0123 rest of line {i}'
        for i in range(12000)
    )
    for text in (one_line, per_line):
        started = time.perf_counter()
        result, warnings = redact_credentials(text)
        elapsed = time.perf_counter() - started
        assert len(warnings) == 12000, len(warnings)
        assert "not-a-credential" not in result
        assert (
            elapsed < 5.0
        ), f"quoted scan took {elapsed:.2f}s -- per-match walk to the end is back"


# ─────────────────────────────────────────────────────────────────────────────
# sensitive-path verdicts -- zero change (DENY surface)
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
    """Differential for the anchor rewrite on ``is_sensitive_path``."""
    assert bool(is_sensitive_path(path)) is expected


def inspect_source(func: object) -> str:
    """``inspect.getsource`` indirection kept local so the test module has one import."""
    import inspect

    return inspect.getsource(func)  # type: ignore[arg-type]


def test_credential_pattern_module_still_compiles_one_alternation() -> None:
    """Invariant: pass 1 uses the shared compiled pattern, gated by its pre-filter.

    Guards against a future refactor swapping in a locally compiled regex, which
    would silently drop the ``_might_contain_credential`` pre-filter pairing, and
    against the pre-filter and the scan being asked about different strings --
    the pre-filter is only a sound gate for the exact text the scan then walks.
    """
    from kiro_crew import security as security_mod

    assert isinstance(security_mod._CREDENTIAL_PATTERNS, re.Pattern)
    body = inspect_source(security_mod.redact_credentials)
    assert "_CREDENTIAL_PATTERNS.finditer(text)" in body
    assert "_might_contain_credential(text)" in body
    assert "re.compile(" not in body
