"""The PEM credential scan stays linear on repeated ``BEGIN PRIVATE KEY`` headers.

The canonical combined credential pattern carries a PEM branch whose complete
form lazily scans ``[\\s\\S]*?`` forward to a matching ``END`` header. A regex
engine restarts that scan at every ``BEGIN`` header, so iterating that pattern
over content quoting many ``BEGIN PRIVATE KEY`` headers with no matching ``END``
is O(n^2) -- for the uncapped design_tweak file preview that is seconds of
blocking on a few thousand headers.

The two content scanners run the non-PEM branches through the combined pattern
with the PEM branch removed, and answer the PEM case with a single linear pass
(:func:`kiro_crew.security.pem_fast.contains_pem_private_key`) whose accept /
reject decision is identical to the canonical branch's.

These tests pin both halves:

* **behaviour** -- the linear PEM check agrees with the canonical PEM branch's
  own body-backed rule across a randomized corpus, and both real callers refuse
  a genuine (complete OR truncated) key while allowing a quoted marker with no
  key body;
* **complexity** -- doubling the number of repeated headers does not quadruple
  the scan time for either caller.

Key-shaped fixtures are assembled from adjacent fragments so no single source
line is a complete private-key block.
"""

from __future__ import annotations

import re

import pytest

import kiro_crew.security as sec
from kiro_crew.apps.builtins.design_tweak.backend import preview_files, server
from kiro_crew.deploy.scan import scan_content
from kiro_crew.security.pem_fast import (
    CREDENTIAL_PATTERNS_SANS_PEM,
    contains_pem_private_key,
)

# A >=16-char base64 body run, assembled so this line is not itself a key block.
_BODY = "MIIBVgIBADANBgkqhkiG" + "9w0BAQEFAASCAUAwggEa"
# PEM marker literals are assembled from fragments so no single source line is a
# complete `BEGIN ... PRIVATE KEY` header -- that contiguous form is itself an
# internal-content-scan marker, even in test fixtures.
_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_END = "-----END " + "PRIVATE KEY-----"
_BEGIN_RSA = "-----BEGIN RSA " + "PRIVATE KEY-----"
_END_RSA = "-----END RSA " + "PRIVATE KEY-----"
_BEGIN_ENC = "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----"
_END_ENC = "-----END ENCRYPTED " + "PRIVATE KEY-----"


def _preview(text: str) -> bool:
    """The real design_tweak preview credential check on text bytes."""
    return preview_files.contains_credential(server, text.encode("utf-8"))


def _deploy_has_credential(text: str) -> bool:
    return any(f.severity == "credential" for f in scan_content(text))


# ─────────────────────────────────────────────────────────────────────────────
# Behaviour: the linear PEM check matches the canonical branch's body rule
# ─────────────────────────────────────────────────────────────────────────────

# The canonical body-backed rule, as the preview scanner applies it: a PEM hit
# counts only when armoured body survives marker stripping.
_PEM_MARKER_RE = re.compile(r"-{5}(?:BEGIN|END)[A-Z0-9 ]*PRIVATE KEY-{5}")
_PEM_BODY_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")


def _canonical_contains_credential(text: str) -> bool:
    for pattern in sec.get_credential_patterns():
        for match in pattern.finditer(text):
            hit = match.group()
            if "PRIVATE KEY" in hit and not _PEM_BODY_RE.search(_PEM_MARKER_RE.sub("", hit)):
                continue
            return True
    return False


def _new_contains_credential(text: str) -> bool:
    if CREDENTIAL_PATTERNS_SANS_PEM.search(text) is not None:
        return True
    return contains_pem_private_key(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(f"{_BEGIN}\n{_BODY}\n{_END}\n", True, id="complete-key"),
        pytest.param(f"{_BEGIN_RSA}\n{_BODY}\n{_END_RSA}\n", True, id="complete-rsa-key"),
        pytest.param(f"{_BEGIN}\n{_BODY}\n", True, id="truncated-key-no-end"),
        pytest.param(
            f"{_BEGIN_ENC}\nProc-Type: 4,ENCRYPTED\n"
            f"DEK-Info: AES-128-CBC,ABCDEF\n\n{_BODY}\n{_END_ENC}\n",
            True,
            id="encrypted-key-blank-line",
        ),
        pytest.param(
            f'paste "{_BEGIN}" then your key material here.\n', False, id="quoted-marker-no-body"
        ),
        pytest.param(
            f"see {_BEGIN} and {_END} markers in the docs\n", False, id="quoted-begin-end-pair"
        ),
        pytest.param(
            f"{_BEGIN}\nsome prose line, not base64\nmore prose\n", False, id="marker-then-prose"
        ),
        pytest.param(f"{_BEGIN}\n" * 50, False, id="many-bare-headers-no-body"),
        pytest.param(f"{_BEGIN}\n{_BEGIN}\n{_BODY}\n", True, id="later-header-has-body"),
        pytest.param("just ordinary text, no secrets at all\n", False, id="no-credential"),
        pytest.param("", False, id="empty"),
    ],
)
def test_linear_pem_check_matches_canonical_rule(text: str, expected: bool) -> None:
    # Every fixture here is PEM-only (no non-PEM credential), so the standalone
    # linear PEM check, the full new scanner, and the canonical rule must all agree.
    assert contains_pem_private_key(text) is expected
    assert _new_contains_credential(text) == _canonical_contains_credential(text) == expected


def test_differential_against_canonical_on_random_pem_text() -> None:
    import random

    rng = random.Random(1234)
    fragments = [
        _BEGIN,
        _END,
        _BEGIN_RSA,
        _END_RSA,
        _BODY,
        "ordinary prose line",
        "",
        "Proc-Type: 4,ENCRYPTED",
        "DEK-Info: AES-128-CBC,ABCDEF",
        '"a quoted marker"',
        "short",
    ]
    for _ in range(3000):
        text = "\n".join(rng.choice(fragments) for _ in range(rng.randint(0, 9)))
        assert _new_contains_credential(text) == _canonical_contains_credential(text), repr(text)


# ─────────────────────────────────────────────────────────────────────────────
# Fail-safe: a real credential hidden between quoted PEM markers is NOT leaked
# ─────────────────────────────────────────────────────────────────────────────


def test_credential_between_quoted_pem_markers_is_refused() -> None:
    # A credential-shaped string on an interior line between a quoted BEGIN and a
    # later END is a real finding and must be refused: a lazy BEGIN..END span
    # would otherwise swallow it into one body-less PEM span and let it through.
    # Both callers route the non-PEM branches around that span, so it is caught.
    # Uses AWS's own published example access-key id (a documented non-secret),
    # assembled from fragments so no single line is a complete key.
    example_key = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    text = f"{_BEGIN}\nkey={example_key}\n{_END}\n"
    assert _preview(text) is True
    assert _deploy_has_credential(text) is True


# ─────────────────────────────────────────────────────────────────────────────
# Both callers still refuse a real key and allow a bare quoted marker
# ─────────────────────────────────────────────────────────────────────────────


def test_both_callers_refuse_real_key_allow_quoted_marker() -> None:
    real = f"{_BEGIN}\n{_BODY}\n{_END}\n"
    truncated = f"{_BEGIN}\n{_BODY}\n"
    quoted = f'docs: paste "{_BEGIN}" here, then your own key\n'

    assert _preview(real) is True
    assert _preview(truncated) is True
    assert _preview(quoted) is False

    assert _deploy_has_credential(real) is True
    assert _deploy_has_credential(truncated) is True
    # Deploy's own _DEPLOY_EXTRA header regex flags even a bare quoted marker
    # (its long-standing behaviour); this test pins that it is unchanged.
    assert _deploy_has_credential(quoted) is True


# ─────────────────────────────────────────────────────────────────────────────
# Complexity: the work is linear in input size, asserted by COUNTING regex
# applications (deterministic -- no wall-clock measurement, so it cannot flake
# under runner load and cannot hang into a timeout on a regression).
# ─────────────────────────────────────────────────────────────────────────────


def _count_pem_regex_applications(text: str) -> int:
    """Run the linear PEM scan and return how many times its block regexes match.

    A linear scan applies the complete/truncated block regex a bounded number of
    times per header region (the monotonic cursor skips headers already
    consumed); a quadratic regression re-applies it from every nested header, so
    the count grows super-linearly with the header count. Counting applications
    is fully deterministic -- it does not measure elapsed time.
    """
    import unittest.mock as mock

    from kiro_crew.security import pem_fast

    calls = 0

    class _Counting:
        def __init__(self, real):
            self._real = real

        def match(self, *a, **k):
            nonlocal calls
            calls += 1
            return self._real.match(*a, **k)

    with (
        mock.patch.object(pem_fast, "_PEM_COMPLETE_RE", _Counting(pem_fast._PEM_COMPLETE_RE)),
        mock.patch.object(pem_fast, "_PEM_TRUNCATED_RE", _Counting(pem_fast._PEM_TRUNCATED_RE)),
    ):
        pem_fast.contains_pem_private_key(text)
    return calls


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda n: f"{_BEGIN}\n" * n, id="bare-headers-no-end"),
        pytest.param(lambda n: (f"{_BEGIN}\n" * n) + f"{_END}\n", id="many-headers-one-far-end"),
        pytest.param(
            lambda n: f"prose\n{_BEGIN}\n" + (f"Proc-Type: {_BEGIN}\n" * n),
            id="headers-nested-in-metadata-lines",
        ),
        pytest.param(lambda n: f"{_BEGIN}{_END[5:]}\n" * n, id="self-overlapping-begin-end"),
    ],
)
def test_repeated_pem_headers_scan_is_linear_by_work_count(shape) -> None:
    # Doubling the header count must not more than roughly double the regex-match
    # work. A quadratic regression (re-matching from every header) grows the
    # count ~4x per doubling, so a 3x ceiling on the doubling ratio separates
    # linear from quadratic with headroom, and the measure is a deterministic
    # integer count rather than a wall-clock reading.
    base = _count_pem_regex_applications(shape(2000))
    doubled = _count_pem_regex_applications(shape(4000))
    assert doubled <= 3 * base, (
        f"regex-match work grew {doubled}/{base} = {doubled / max(base, 1):.1f}x "
        f"when the input doubled (expected ~2x for a linear scan)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Module guards: the sans-PEM pattern really dropped the PEM branch, and the
# in-sync self-check would fire loudly if the canonical branch text drifted.
# ─────────────────────────────────────────────────────────────────────────────


def test_sans_pem_pattern_dropped_exactly_the_pem_branch() -> None:
    from kiro_crew.security import pem_fast
    from kiro_crew.security.redaction import get_credential_patterns

    (combined,) = get_credential_patterns()
    assert combined.pattern.count(pem_fast._PEM_BRANCH) == 1
    assert pem_fast._PEM_BRANCH not in CREDENTIAL_PATTERNS_SANS_PEM.pattern
    # Stripping only the PEM branch leaves every other branch intact.
    assert len(CREDENTIAL_PATTERNS_SANS_PEM.pattern) == len(combined.pattern) - len(
        pem_fast._PEM_BRANCH
    )
    # A bare PEM header is not matched by the sans-PEM pattern...
    assert CREDENTIAL_PATTERNS_SANS_PEM.search(f"{_BEGIN}\n") is None
    # ...while a non-PEM credential is.
    assert CREDENTIAL_PATTERNS_SANS_PEM.search("AKIA" + "IOSFODNN7EXAMPLE") is not None


def test_self_check_rejects_a_drifted_pem_branch() -> None:
    from kiro_crew.security import pem_fast

    original = pem_fast._PEM_BRANCH
    try:
        pem_fast._PEM_BRANCH = "|-----BEGIN A DIFFERENT BRANCH-----"
        with pytest.raises(AssertionError, match="exactly once"):
            pem_fast._build_sans_pem()
    finally:
        pem_fast._PEM_BRANCH = original
