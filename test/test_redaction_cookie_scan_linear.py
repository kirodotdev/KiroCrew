"""A text that repeats a cookie anchor costs the header scan linear time, with the same output.

``detect_headers`` finds every ``cookie`` / ``set-cookie`` in the text and reads the
``name=value`` pairs after each. A line that repeats the anchor was walked once per anchor
(the pair rescan), and each anchor's value check (``is_placeholder``) read the whole value;
both are quadratic, and the dashboard redacts pages of up to 3 MiB. The cookie reader now
shares one walk between a line's anchors and settles a long, placeholder-free value without
reading it whole. These tests pin that the output is unchanged and that the work is linear.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from redaction_structured_cases import assert_linear_work

from kiro_crew.security.redaction_structured import redact
from kiro_crew.security.redaction_structured.charsets import ascii_lower
from kiro_crew.security.redaction_structured.checks import (
    MASK_CHARS,
    PLACEHOLDER_TRIGGER_WORDS,
    _ValueScan,
    cookie_value_is_placeholder,
    is_placeholder,
    session_name,
)

_TAG = "[REDACTED: credential]"

# (input, redacted text), captured from the scan before the walk was shared and the value
# check bounded. A session/token cookie's VALUE is redacted; the name, attributes and a
# CSRF token stay; a placeholder value (an example) is left as written.
SHAPES: dict[str, tuple[str, str]] = {
    "session with theme": (
        "Cookie: sessionid=abcdefgh12345678; theme=dark",
        f"Cookie: sessionid={_TAG}; theme=dark",
    ),
    "set-cookie session": (
        "Set-Cookie: session=dGhpc2lzYXNlY3JldHNlc3Npb252YWx1ZTEyMzQ1Ng; "
        "Path=/; HttpOnly; Secure; SameSite=Lax",
        f"Set-Cookie: session={_TAG}; Path=/; HttpOnly; Secure; SameSite=Lax",
    ),
    "csrf beside sid": (
        "Cookie: csrftoken=abc123def456ghi; sid=secretvalue1234567",
        f"Cookie: csrftoken=abc123def456ghi; sid={_TAG}",
    ),
    "merged name quirk": (
        "cookie=authxx; theme=VALUE1234",
        f"cookie=authxx; theme={_TAG}",
    ),
    "placeholder angle": ("Cookie: sid=<token>", "Cookie: sid=<token>"),
    "placeholder your-here": (
        "Cookie: sid=your-secret-here",
        "Cookie: sid=your-secret-here",
    ),
    # A long value that holds a mask run is an example (not a credential): the shortcut
    # must fall back to the full check and keep it.
    "mask run value": ("Cookie: sid=" + "x" * 100, "Cookie: sid=" + "x" * 100),
    # A long value with no mask character and no placeholder word is a credential: the
    # shortcut settles it as not-placeholder and it is redacted, as before.
    "long real value": ("Cookie: sid=" + "A" * 100, f"Cookie: sid={_TAG}"),
    "repeated set-cookie sid": (
        "set-cookie=sid=AAAAAAAAAAAAAAAAAAAA" * 3,
        f"set-cookie=sid={_TAG}",
    ),
    "repeated cookie token": (
        "cookie=token=BBBBBBBBBBBBBBBBBBBB" * 3,
        f"cookie=token={_TAG}",
    ),
    "multi session names": (
        "Cookie: sessionid=abcdefghijklmnopq; csrf=xyzxyzxyz; token=secrettoken1234567",
        f"Cookie: sessionid={_TAG}; csrf=xyzxyzxyz; token={_TAG}",
    ),
    "benign awsalb": (
        "Set-Cookie: AWSALB=" + "Z" * 120 + "; Expires=Mon; Path=/",
        "Set-Cookie: AWSALB=" + "Z" * 120 + "; Expires=Mon; Path=/",
    ),
}


@pytest.mark.parametrize("name", SHAPES)
def test_every_cookie_shape_redacts_as_before(name: str) -> None:
    text, redacted = SHAPES[name]
    assert redact(text)[0] == redacted


def _fill(unit: str, lead: str = "") -> Callable[[int], str]:
    return lambda n: lead + (unit * (n // len(unit) + 1))[:n]


# Whole-text shapes whose value has no pair separator, so one value runs the length of the
# text. These were quadratic in the pair walk and in the value check; the streamed form
# holds the growing value (sibling R3, a separate item) and is not covered here.
_WHOLE_SHAPES: dict[str, Callable[[int], str]] = {
    "set_cookie_sid": _fill("set-cookie=sid="),
    "cookie_token": _fill("cookie=token="),
    "cookie_sid": _fill("cookie=sid="),
    "cookie_eq": _fill("cookie="),
    "cookie_header_pairs": _fill("a=AAAAAAAA; cookie=B; ", lead="Cookie: "),
    # Masked / trigger / form values: each anchor's value holds a feature, so the cheap
    # proof cannot settle it and the shared classifier must keep the work linear anyway.
    "set_cookie_sid_mask": _fill("set-cookie=sid=x="),
    "cookie_sid_mask": _fill("cookie=sid=x="),
    "cookie_sid_dot": _fill("cookie=sid=.a="),
    "cookie_sid_your": _fill("cookie=sid=your="),
    "cookie_sid_eightrun": _fill("cookie=sid=xxxxxxxx="),
    "cookie_sid_dollar": _fill("cookie=sid=$a="),
    "cookie_sid_ws": _fill("cookie=sid=\u00a0a="),
    # Far ``=``: every ``cookie:`` anchor's NAME reaches the one ``=``, and the value is an
    # ``$ENV`` run — both were read per anchor (the name slice and the charset scan).
    "name_far_env": lambda n: "cookie: " * (n // 16) + "session=$" + "A" * (n // 2),
    "name_far_token": lambda n: "cookie: " * (n // 16) + "sesstoken=" + "x" * (n // 2),
    # A ``name:`` followed by a long blank run, repeated: the per-anchor blank skip.
    "blank_run": lambda n: ("cookie:" + " " * 64) * (n // 71),
}


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _WHOLE_SHAPES.items()])
def test_repeated_cookie_anchor_costs_linear_work_whole(build: Callable[[int], str]) -> None:
    assert_linear_work(build, redact)


# Values that exercise every branch the bounded cookie check can take: short, boundary
# whitespace, form-keying first character, example suffix, mask character, placeholder
# word, and long placeholder-free runs that the shortcut settles.
_VALUES = [
    "",
    "ab",
    "abcd",
    "secret",
    "password",
    "changeme",
    "placeholder",
    "redacted",
    "<token>",
    "${TOKEN}",
    "{{VALUE}}",
    "%ENV%",
    "$ENV_VAR",
    "your-token-here",
    "sk_test_YOUR_KEY_HERE",
    "ghp_" + "x" * 36,
    "x" * 8,
    "*" * 9,
    "." * 3,
    "AAAA" + "x" * 40,
    "A" * 100,
    "set-cookie=sid=" * 8,
    "token=token=token=" * 8,
    "a" * 40 + "here",
    "pa\u00dfword",
    "\u00a0" * 5 + "A" * 40,
    "A" * 40 + "EXAMPLE",
    "A" * 40 + "\u00a0",
    "Zx9_kq-3Lm.Pw~" * 6,
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
]


@pytest.mark.parametrize("value", _VALUES)
def test_bounded_check_matches_is_placeholder(value: str) -> None:
    """With the flags the caller computes (a mask character present, a trigger word
    present), the bounded cookie check equals the full ``is_placeholder`` for every value.
    Passing the fallback flag (``True``) only forces the shared classifier, which must agree
    too; the shared classifier (``_ValueScan``) mirrors ``is_placeholder`` for every value."""
    want = is_placeholder(value)
    text = f"pre={value}="
    vs, ve = 4, 4 + len(value)
    assert text[vs:ve] == value
    scan = _ValueScan(text)
    has_mask = any(ch in MASK_CHARS for ch in value)
    has_trigger = any(word in ascii_lower(value) for word in PLACEHOLDER_TRIGGER_WORDS)
    assert (
        cookie_value_is_placeholder(text, vs, ve, has_mask, has_trigger, scan.is_placeholder_at)
        == want
    )
    # Forcing the classifier (fallback flag) must give the same answer.
    assert cookie_value_is_placeholder(text, vs, ve, True, True, scan.is_placeholder_at) == want
    # And the classifier called directly mirrors is_placeholder for every value.
    assert scan.is_placeholder_at(vs, ve) == want


# Cookie names that exercise every branch of the bounded session-name check, including the
# unicode cases that matter: Kelvin K lowercases to ASCII ``k`` (so ``to<K>en`` is a token),
# while the sharp s does NOT (so ``se<ß>ion`` is not a session) — using casefold here would
# wrongly match ``sess`` via ß→ss, which is why the check lowers per character.
_NAMES = [
    "x" * 300 + "token",
    "x" * 300,
    "csrf" + "x" * 300 + "token",
    "x" * 300 + "to\u212aen",
    "se\u00dfion" + "x" * 300,
    "x" * 300 + "SESSION",
    "x" * 300 + "phpsessid",
    "x" * 300 + "xsrf" + "token",
    "\u0130" + "x" * 300 + "token",
    "sid",
    "csrftoken",
    "SeSsIoN",
    "x" * 300 + "\u00dfess",
    "\u00a0\u00a0" + "x" * 300 + "auth\u00a0",
]


@pytest.mark.parametrize("name", _NAMES)
def test_session_name_at_matches_session_name(name: str) -> None:
    """The bounded ``_ValueScan.session_name_at`` equals the full ``session_name`` for every
    name, so a far-``=`` cookie is classified the same as before."""
    text = "c=" + name + "=v"
    i, eq = 2, 2 + len(name)
    assert text[i:eq] == name
    assert _ValueScan(text).session_name_at(i, eq) == session_name(name.strip())


def test_shortcut_invariants_hold() -> None:
    """The shortcut is exact only while these hold: its mask set and trigger words are the
    ones ``is_placeholder`` keys on, and every short all-value form is shorter than the
    length at which the shortcut starts to skip them. Pin them so a later change to
    ``is_placeholder`` that broke the proof fails here instead of silently over-redacting."""
    from kiro_crew.security.redaction_structured import checks

    assert set(checks.MASK_CHARS) == set(checks._MASK_CHARS)
    assert set(checks.PLACEHOLDER_TRIGGER_WORDS) == set(checks.PLACEHOLDER_PARTS)
    assert max(len(word) for word in checks.ALLOW_WORDS) < checks._SHORTCUT_MIN


def test_long_clean_value_is_settled_without_reading_it_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long value with no mask character and no placeholder word never reaches the full
    ``is_placeholder``: the shortcut proves it is not a placeholder."""
    import kiro_crew.security.redaction_structured.checks as checks

    def boom(_value: str) -> bool:
        raise AssertionError("is_placeholder read the whole value")

    monkeypatch.setattr(checks, "is_placeholder", boom)

    def no_classify(vs: int, ve: int) -> bool:
        raise AssertionError("built the shared classifier for a clean value")

    text = "sid=" + "A" * 200 + "="
    assert checks.cookie_value_is_placeholder(text, 4, 204, False, False, no_classify) is False
