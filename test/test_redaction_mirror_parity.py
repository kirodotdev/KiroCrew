"""Parity guard for the JWT credential patterns copied out of `security.py`.

`security.py` owns the two JWT alternatives. Two other files carry hand-written
copies because neither can import it: `pr_findings.py` is documented as portable
and stdlib-only, and `sanitize.ts` runs in the browser. That is the exact setup
that already failed once: both copies missed the two-segment dashboard link
token, so a bare token in CI-log prose and every token in chat rendered
verbatim while the backend redacted them.

Behavioural tests alone do not close that failure mode. Each copy is tested
against its own samples, so a backend-only change leaves every mirror green
while the mirrors no longer match the pattern they mirror. The planned
follow-up (decode segment one as a JOSE header, require `alg`/`enc`) lands in
`security.py` FIRST by design, which is precisely when that would happen.

The expectation is derived from `_CREDENTIAL_PATTERNS` at run time, never
restated here. `security_posture.py` records why: an earlier drift guard "could
not catch it because it hardcoded the same two registry names this function
did". A literal copy in this file would be a fourth copy, free to drift with
the thing it is supposed to pin.

One mirror is deliberately NOT verbatim. `sanitize.ts` omits the two-segment
alternative's leading lookbehind because the browser cannot afford it. Vite
declares no `build.target`, so the default `'modules'` floor is `safari14`, and
at that target esbuild rewrites an unsupported lookbehind literal into a
`new RegExp(...)` call. That defers the error from parse time to run time, but
`CRED_PATTERNS` is a module-level constant in the eagerly loaded entry chunk, so
the throw lands during module evaluation and blanks the dashboard on Safari 16.3
and older. Measured rather than assumed: with `RegExp` patched to reject
lookbehind, the top-level shape throws on import, while a function-scoped one
imports cleanly and throws only when called. The one pre-existing lookbehind in
the eager graph is function-scoped for exactly that reason.

Dropping a LEFT boundary widens the pattern, so this divergence is
one-directional and in the safe direction: the frontend still redacts everything
the backend redacts here, plus a token a renderer glued onto a label, which the
backend misses. The cost is a false positive needing an identifier that contains
`eyJ` followed by 96 or more identifier characters, a dot, and exactly 43 more.

Not every JWT-shaped regex in the tree is a mirror. Two are deliberately their
own thing, and the last two tests assert that so the exemption stays visible:
a reader who finds only the pinning tests could reasonably assume every copy
should match the backend and "fix" one, changing behaviour that is correct.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.security import _CREDENTIAL_PATTERNS

ROOT = Path(__file__).resolve().parents[1]

BACKEND = ROOT / "src" / "kiro_crew" / "security.py"
PREPARE_PR = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_findings.py"
)
FRONTEND = ROOT / "website" / "src" / "utils" / "sanitize.ts"
TOKEN_MINT = ROOT / "src" / "kiro_crew" / "instances" / "token_mint.py"


def _jwt_alternatives() -> tuple[str, str]:
    """Return (three_segment, two_segment) as written in the enforcing regex.

    Split on `|` is safe only while neither alternative contains one; if a
    future edit adds an internal `|`, the count check below fails loudly rather
    than silently pinning a fragment.

    The two are told apart by the repetition group only the multi-segment form
    uses, NOT by the two-segment form's leading boundary. Keying on the boundary
    would misclassify the moment the backend dropped it, which is the one edit
    that should surface the actionable message in `_two_segment_split`.
    """
    alts = [alt for alt in _CREDENTIAL_PATTERNS.pattern.split("|") if "eyJ" in alt]
    assert len(alts) == 2, f"expected 2 JWT alternatives in _CREDENTIAL_PATTERNS, found {len(alts)}: {alts}"
    three = [a for a in alts if "(?:" in a]
    two = [a for a in alts if "(?:" not in a]
    assert len(two) == 1 and len(three) == 1, f"cannot tell the alternatives apart: {alts}"
    return three[0], two[0]


_LEADING_LOOKBEHIND_RE = re.compile(r"^\(\?<[=!][^)]*\)")
_ANY_LOOKBEHIND_RE = re.compile(r"\(\?<[=!]")


def _two_segment_split() -> tuple[str, str]:
    """Return (leading_lookbehind, body) of the backend two-segment alternative.

    Both halves are sliced off the enforcing regex rather than restated, so the
    frontend's body assertion tracks a backend edit to the bounds automatically
    and only the boundary itself is exempt.
    """
    _, two_segment = _jwt_alternatives()
    match = _LEADING_LOOKBEHIND_RE.match(two_segment)
    assert match, (
        "the backend two-segment alternative no longer starts with a lookbehind. "
        "If the backend dropped it too, the mirrors are back to plain parity and "
        f"the frontend exemption below should be deleted.\ngot: {two_segment}"
    )
    return match.group(0), two_segment[match.end() :]


class TestMirrorsMatchTheBackend:
    """Each hand-written copy must track the backend literal, exemptions aside."""

    def test_two_segment_alternative_is_verbatim_in_prepare_pr(self) -> None:
        _, two_segment = _jwt_alternatives()
        assert PREPARE_PR.is_file(), f"mirror moved or was renamed: {PREPARE_PR}"
        assert two_segment in PREPARE_PR.read_text(encoding="utf-8"), (
            "pr_findings.py no longer carries the two-segment link-token alternative "
            "from security.py verbatim. It runs under CPython, so it has no reason to "
            "diverge. Copy it across, or delete this assertion and say in "
            f"pr_findings.py why the mirror is allowed to differ.\nexpected: {two_segment}"
        )

    def test_two_segment_body_is_verbatim_in_the_frontend_mirror(self) -> None:
        """The frontend pins everything except the leading boundary."""
        _, body = _two_segment_split()
        assert FRONTEND.is_file(), f"mirror moved or was renamed: {FRONTEND}"
        assert body in FRONTEND.read_text(encoding="utf-8"), (
            "sanitize.ts no longer carries the body of the two-segment link-token "
            "alternative from security.py verbatim. The leading boundary is exempt "
            "(see the next test); the bounds are not.\n"
            f"expected: {body}"
        )

    def test_frontend_omits_every_lookbehind_on_purpose(self) -> None:
        """Re-adding a lookbehind here ships a blank dashboard on Safari <= 16.3.

        Asserted on the whole file, not just the one alternative, because the
        constraint is the platform's and applies to any pattern this module
        evaluates at import. See the module docstring for the measured mechanism.
        """
        found = _ANY_LOOKBEHIND_RE.findall(FRONTEND.read_text(encoding="utf-8"))
        assert not found, (
            f"sanitize.ts contains {len(found)} lookbehind construct(s). Safari 16.3 "
            "and older cannot compile one, and CRED_PATTERNS is evaluated at module "
            "import in the eager entry chunk, so this throws before the dashboard "
            "renders. If the support floor has genuinely moved past Safari 16.4, set "
            "build.target in website/vite.config.ts and delete this test."
        )

    def test_three_segment_alternative_is_verbatim_in_the_frontend_mirror(self) -> None:
        three_segment, _ = _jwt_alternatives()
        assert FRONTEND.is_file(), f"mirror moved or was renamed: {FRONTEND}"
        assert three_segment in FRONTEND.read_text(encoding="utf-8"), (
            f"sanitize.ts no longer carries the JWS/JWE alternative from security.py "
            f"verbatim.\nexpected: {three_segment}"
        )


class TestDeliberateNonMirrors:
    """Two JWT regexes are intentionally NOT pinned to the backend."""

    def test_prepare_pr_keeps_its_own_wider_three_segment_form(self) -> None:
        """`pr_findings.py` predates the backend form and uses `{8,}` per segment.

        It is wider on the first segment and narrower on segment count. Only its
        two-segment alternative was added as a mirror; rewriting this one to match
        the backend would change what the script redacts and is out of scope for a
        parity guard.
        """
        three_segment, _ = _jwt_alternatives()
        text = PREPARE_PR.read_text(encoding="utf-8")
        assert r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}" in text
        assert three_segment not in text, (
            "pr_findings.py now also carries the backend three-segment alternative. "
            "If that was deliberate, move the assertion into TestMirrorsMatchTheBackend."
        )

    def test_token_mint_keeps_its_own_narrower_form(self) -> None:
        """`token_mint.py` matches tokens it is about to mint, not arbitrary egress.

        `{4,}` with `{1,4}` segments is looser than the redactor on purpose: it
        recognises its own short-lived output. It is not an egress redactor and is
        not a mirror.
        """
        three_segment, two_segment = _jwt_alternatives()
        text = TOKEN_MINT.read_text(encoding="utf-8")
        assert r"eyJ[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+){1,4}" in text
        assert three_segment not in text and two_segment not in text, (
            "token_mint.py now carries a backend alternative verbatim. If it became a "
            "mirror, move the assertion into TestMirrorsMatchTheBackend."
        )
