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
_SLACK20 = "xoxb-" "IOSFODNN7EXAMPL"  # 20 chars
_GHP = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"
_ANT = "sk-ant-api03-" "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOP"
_GLPAT = "glpat-" "xxxx1234xxxx5678xxxx"
_XOXB = "xoxb-" "1234567890-abcdefghij"
_TAG = "[REDACTED: credential]"

REDACTION_GOLDEN: list[tuple[str, str, list[str]]] = [
    (
        f"Found key {_SLACK20} in output",
        f"Found key {_TAG} in output",
        ["Redacted credential pattern (20 chars)"],
    ),
    # Two occurrences of the SAME credential: both spans replaced, two warnings.
    # This is the case the old `str.replace(matched, tag, 1)` shape depended on
    # positional luck for -- sub() splices each matched span in place.
    (
        f"a {_SLACK20} b {_SLACK20} c",
        f"a {_TAG} b {_TAG} c",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (20 chars)",
        ],
    ),
    # Three DIFFERENT credentials -- pins warning ORDER (20, 38, 26 chars),
    # which is the ordering guarantee sub() has to preserve.
    (
        f"first {_SLACK20} then {_GHP} and {_XOXB} tail",
        f"first {_TAG} then {_TAG} and {_TAG} tail",
        [
            "Redacted credential pattern (20 chars)",
            "Redacted credential pattern (38 chars)",
            "Redacted credential pattern (26 chars)",
        ],
    ),
    (
        # A labelled value keeps its label; only the value is replaced.
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
    text = f"{_ANT} {_SLACK20} {_GHP}"
    _, warnings = redact_credentials(text)
    assert warnings == [
        f"Redacted credential pattern ({len(_ANT)} chars)",
        f"Redacted credential pattern ({len(_SLACK20)} chars)",
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
    dense = f"{_SLACK20} " * 4000
    started = time.perf_counter()
    result, warnings = redact_credentials(dense)
    elapsed = time.perf_counter() - started
    assert len(warnings) == 4000
    assert _SLACK20 not in result
    assert elapsed < 5.0, f"pass 1 took {elapsed:.2f}s -- per-match string rebuild is back"


def test_log_floor_presigned_scan_cannot_restart_inside_a_run() -> None:
    """Complexity guard for the log floor's signing-parameter scan.

    The quadratic shape is the engine restarting a match at every position in a
    long ``[A-Za-z0-9%._-]`` run and backtracking it to the end. Two structural
    properties of the name group forbid that, and both are asserted on the
    pattern's SHAPE rather than on wall-clock time -- a loaded CI runner can pause
    a worker for seconds between two clock reads, so an absolute time ceiling
    measures the scheduler, not the matcher: (1) a negative look-behind, so a
    match cannot START inside a run; (2) a bounded ``{1,N}`` repetition, so the
    per-position attempt is constant work. The behaviour is pinned alongside: a
    long credential-free run is returned whole.
    """
    from kiro_crew.log_redaction import _PRESIGNED_PARAM_RE, SecretRedactionFilter

    src = _PRESIGNED_PARAM_RE.pattern
    # The name starts outside a run, or right after an encoded ``?``/``&`` at
    # any depth (``%3F``, ``%253F``: the name class itself never holds one), and
    # repeats a bounded unit.
    m = re.match(
        r"\(\?:\(\?<!\[A-Za-z0-9%\._-\]\)\|\(\?<=%3\[Ff\]\)\|\(\?<=%26\)"
        r"\|\(\?<=253\[Ff\]\)\|\(\?<=2526\)\)"
        r"\(\(\?:\[A-Za-z0-9\._-\]\|%\(\?!\(\?:25\)\{0,8\}\(\?:3\[DdFf\]\|26\)\)"
        r"\[0-9A-Fa-f\]\{2\}\)\{1,(\d+)\}\)",
        src,
    )
    assert m, f"name group must be a left-anchored, bounded class; got {src!r}"
    assert int(m.group(1)) <= 256, "name bound keeps the per-position attempt constant"
    line = "X-Amz-Date " + "A" * 64000
    assert SecretRedactionFilter([]).redact(line) == line
    encoded = "X-Amz-Date " + "%3F" * 20000
    assert SecretRedactionFilter([]).redact(encoded) == encoded
    for run in ("%2526" * 20000, "2526" * 20000, "%25" * 20000):
        line = "X-Amz-Date " + run
        assert SecretRedactionFilter([]).redact(line) == line


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
    # `redact_credentials` is a wrapper over the one plan both redactors share;
    # the scan lives in that plan's passes (``_planned_spans``), which
    # ``_credential_redaction_plan`` runs inside the catalog keyword context.
    from kiro_crew.security import redaction as redaction_mod

    body = inspect_source(redaction_mod._planned_spans)
    assert "_credential_matches(text)" in body
    assert "_credential_search(text, pos, starts)" in inspect_source(
        redaction_mod._credential_matches
    )
    assert "_CREDENTIAL_PATTERNS.match(text, first)" in inspect_source(
        redaction_mod._credential_search
    )
    assert "_might_contain_credential(text)" in body
    assert "re.compile(" not in body
