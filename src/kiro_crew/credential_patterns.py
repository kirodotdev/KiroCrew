"""Credential regex spellings shared by the log-redaction leaf and the scrubber.

It is also the home of the rule tier table (:data:`RULE_TIERS`, at the foot).

``log_redaction`` installs at CLI bootstrap, before heavy modules load, so it
cannot import ``security``. The two AWS-key-ID and JWT spellings were therefore
written out twice -- once in each home -- and parity was held only by tests that
read ``security.py`` as SOURCE TEXT and grepped it for a literal. That guard
fires on a rename, not on a divergence in meaning, and it costs a test per
duplicated pattern.

This module is the single home for those spellings, and for the ``token`` URL
parameter's (:data:`TOKEN_PARAMETER`). Both consumers import from here, so there
is nothing left to keep in sync.

Why fragments rather than compiled patterns
-------------------------------------------
Everything below is pattern SOURCE. ``security.py`` embeds the AWS spelling in
five differently shaped patterns -- three plain alternation branches, one
``X-Amz-Credential=`` presigned-URL matcher and one fully anchored ``^...$``
parameter validator -- and each needs its own anchors, continuations and compile
flags. A compiled pattern could not be reused at four of those five sites.
:data:`TOKEN_PARAMETER` is the one whole pattern here: both of its sites compile
it as written.

Why this module imports NOTHING
-------------------------------
Not even ``re``, and not even ``from __future__ import annotations`` -- the
annotations here need none. ``log_redaction`` is a deliberate import leaf -- it is
installed before the heavy modules load -- so anything it imports is on the
bootstrap path. Exporting plain strings, plus the few pure string builders behind
:data:`TOKEN_PARAMETER`, keeps this module's import cost near zero and makes it
impossible for a future edit here to drag a dependency into that path.
``test_credential_patterns.py`` pins the import-free shape.

The two AWS spellings deliberately DIVERGE
------------------------------------------
The redaction floor matches two prefixes the scrubber does not. Over-matching is
the fail-closed direction for redaction: a false positive costs one masked log
line, while a miss writes a live key to disk. The scrubber's narrower spelling is
load-bearing in the other direction -- it gates request-blocking decisions and a
cheap literal pre-filter whose anchors must stay a superset of every branch (see
``_CREDENTIAL_PATTERNS`` in ``security.py``), so widening it is a security
behaviour change and not a refactor.

That asymmetry is preserved and made explicit here: ``AWS_KEY_ID_REDACTION`` is
BUILT from the same prefix and body fragments as ``AWS_KEY_ID`` plus named extra
prefixes. The superset relation now holds by construction, so no test has to
compare two hand-written strings to discover whether it still does.
"""

#: AWS access key-ID prefixes both consumers match: long-term (``AKIA``) and
#: temporary/STS (``ASIA``) ids.
AWS_KEY_ID_PREFIXES = "AKIA|ASIA"

#: Prefixes matched by the REDACTION floor only -- ``ABIA`` (bearer tokens) and
#: ``ACCA`` (context-specific credentials). Kept out of ``AWS_KEY_ID`` because
#: adding them to the scrubber widens what a security surface blocks and needs
#: matching pre-filter anchors; see the module docstring.
AWS_KEY_ID_REDACTION_ONLY_PREFIXES = "ABIA|ACCA"

#: The id body: exactly 16 uppercase alphanumerics after the 4-letter prefix.
#: Precise enough for near-zero false positives, and the id is the searchable
#: half of a leaked AWS credential pair.
AWS_KEY_ID_BODY = "[A-Z0-9]{16}"

#: AWS access key ID as the credential scrubber matches it.
AWS_KEY_ID = f"(?:{AWS_KEY_ID_PREFIXES}){AWS_KEY_ID_BODY}"

#: AWS access key ID as the log-redaction floor matches it: a strict superset of
#: :data:`AWS_KEY_ID`, by construction.
#:
#: No word-boundary assertions here or in :data:`AWS_KEY_ID`: a key rendered
#: adjacent to word characters (``aws_AKIA...``, ``key=AKIA...x``) has no ``\b``
#: between ``_`` and ``A`` and would slip past a bounded pattern. Over-matching
#: inside a longer token is the fail-closed direction for a redaction floor.
AWS_KEY_ID_REDACTION = (
    f"(?:{AWS_KEY_ID_PREFIXES}|{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"
)

#: Multi-segment JWT: an ``eyJ`` header plus 2-4 further dot-separated base64url
#: segments, covering 3-segment JWS AND 4-5-segment JWE (``dir``/ECDH-ES).
#: Catches tokens carried OUTSIDE a ``Bearer `` scheme -- JSON-embedded, bare, or
#: assignment-style.
#:
#: The segment class cannot cross the literal ``.`` separators, so it does not
#: over-capture into surrounding text. ``security.py`` carries a SECOND,
#: deliberately different two-segment link-token pattern whose ``{96,}`` floor is
#: tuned against false positives such as ``eyJ2IjoxfQ.json``; that one is a
#: distinct pattern for a distinct job, not another spelling of this one, and it
#: stays where it is used.
JWT_MULTI_SEGMENT = r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}"


#: Vendor and forge API-token spellings for the standalone builder's own scan and
#: any subset that cannot import the scrubber. Each entry is ``(label, fragment)``;
#: the fragment is pattern SOURCE (no anchors, no flags) so a consumer wraps it in
#: whatever ``\b`` / word-boundary shape its site needs. Kept here so the builder's
#: subset has ONE home: a set restated at each call site drifts silently, because
#: the copies must be edited together to stay right and no test sees a one-sided
#: omission. The credential scrubber (``security/redaction_patterns.py``) carries its own
#: spelling of these vendor forms; only ``AWS_KEY_ID`` and ``JWT_MULTI_SEGMENT``
#: above are imported by both. The bounds here are chosen to MATCH the scrubber's
#: for the same format (see the note below), so the builder never ships a short
#: token the scrubber would redact.
#:
#: The ``sk-proj-`` / ``sk-ant-`` bodies carry the hyphen INSIDE the class: a plain
#: ``sk-[A-Za-z0-9]{20,}`` stops at the first hyphen after ``sk-`` and never reaches
#: its length floor, so the project/vendor-scoped forms need their own spelling. The
#: fine-grained GitHub PAT is length-flexible (``{40,}``) rather than pinned to one
#: id/secret split, because a single exact length lets any other-length PAT past.
#:
#: Every lower bound here matches the scrubber's own spelling for the same format:
#: the bound is the one chosen against real tokens, and a tighter one here would ship
#: a short token the scrubber redacts. So these are copied FROM the scrubber, not
#: re-guessed -- a GitLab body is ``{16,}`` and an npm body ``{24,}`` for that reason.
SK_PROJECT_TOKEN = r"sk-proj-[A-Za-z0-9_-]{16,}"
SK_ANT_TOKEN = r"sk-ant-[A-Za-z0-9_-]{16,}"
SK_VENDOR_TOKEN = r"sk-[A-Za-z0-9]{20,}"
GITHUB_FINE_GRAINED_PAT = r"github_pat_[A-Za-z0-9_]{40,}"
GITLAB_PAT = r"glpat-[A-Za-z0-9_-]{16,}"
NPM_TOKEN = r"npm_[A-Za-z0-9]{24,}"
PYPI_TOKEN = r"pypi-[A-Za-z0-9_-]{16,}"

#: Iterable single source for the vendor/token formats above, as
#: ``(label, fragment)`` pairs. ``sk-proj-`` and ``sk-ant-`` precede the plain
#: ``sk-`` form so the more specific spelling is offered first; a consumer that
#: matches greedily still masks either way, but the order keeps the label truthful.
VENDOR_TOKEN_PATTERNS = (
    ("openai-project-key", SK_PROJECT_TOKEN),
    ("anthropic-key", SK_ANT_TOKEN),
    ("vendor-key", SK_VENDOR_TOKEN),
    ("github-fine-grained-pat", GITHUB_FINE_GRAINED_PAT),
    ("gitlab-pat", GITLAB_PAT),
    ("npm-token", NPM_TOKEN),
    ("pypi-token", PYPI_TOKEN),
)


# ── Token URL parameter ──
# The scrubber's pass 4 and the log-redaction floor compile the same
# :data:`TOKEN_PARAMETER`, so both read a token parameter the same way; what each
# then skips or exempts is its own. The fragments below are its parts; the
# scrubber also builds its streaming partial from them.
#
# Group 1 is the VALUE, and only the value is replaced: `token=` stays visible
# so a redacted URL still reads as a token URL. The precedent is
# `instances/token_mint._TOKEN_RE` (`[?&]token=([^\s&]+)`), which one caller
# kept privately because the scrubber lacked the pass; the value class here is
# WIDER-terminated per the issue's requirement -- it also stops at quotes and
# `#` so a match cannot run past the parameter into a quoted string or a URL
# fragment -- and additionally excludes the RFC 3986-forbidden bytes
# (`<>{}|\^` and backtick): no legal URL query can carry them, while SOURCE
# and DOC text quoting a token URL does (`?token={token}` in an f-string,
# `` ?token=` `` in markdown, `?token=<your-token>` in prose). Without the
# exclusion, pass 4 matches the template placeholder and the chip-diff path
# (`chat_runner.py`) redacts a snapshot of `dashboard/urls.py` IN PLACE with
# no recovery -- the exact non-cosmetic false-positive surface ``redaction_patterns``
# cites as its reason for refusing to relax the JWT floor. A template whose
# value starts with an excluded byte now yields an empty value and no match.
#
# The parameter NAME folds ASCII case (`(?ai:token)`) -- unlike `eyJ`, a
# parameter name is not a fixed encoding prefix, and `?Token=` / `?TOKEN=`
# from a third-party provider carries the same bearer value. ASCII scope keeps
# Unicode lookalikes such as the Kelvin sign from spoofing the parser-visible
# name; the value bytes are still matched exactly as written.
#
# ACCEPTED RESIDUAL: a template value made of LEGAL query bytes
# (`?token=$TOKEN`, `?token=%s`) still matches and is redacted -- the class
# excludes only bytes no legal query can carry, and a shape test on the value
# would reintroduce the false-negative lever this pass exists to avoid.
_TOKEN_PARAM_VALUE_CLASS = r"[^\s&\"'#<>{}|\\^`]"

_HTML_REF_AMP = (
    r"&(?:amp;|AMP;|amp(?![0-9A-Za-z])|AMP(?![0-9A-Za-z])"
    r"|#0{0,8}38(?:;|(?![0-9;]))|#[Xx]0{0,8}26(?:;|(?![0-9A-Fa-f;])))"
)
_HTML_REF_QUEST = r"&(?:quest;|#0{0,8}63(?:;|(?![0-9;]))|#[Xx]0{0,8}3[Ff](?:;|(?![0-9A-Fa-f;])))"
_HTML_REF_EQUALS = r"&(?:equals;|#0{0,8}61(?:;|(?![0-9;]))|#[Xx]0{0,8}3[Dd](?:;|(?![0-9A-Fa-f;])))"
_TOKEN_PARAM_SEP_ENTITY_RE = rf"(?:{_HTML_REF_AMP}|{_HTML_REF_QUEST})"
_TOKEN_PARAM_SEP_RE = rf"(?:[?&]|{_TOKEN_PARAM_SEP_ENTITY_RE})"
_TOKEN_PARAM_EQ_RE = rf"(?:=|{_HTML_REF_EQUALS})"


# Apply one standard decode per pipeline stage. An HTML parser decodes these
# references into structure BEFORE handing an attribute value to a query parser,
# while that query parser splits on raw separators BEFORE percent-decoding the
# parameter name. HTML references are therefore structure here, while encoded
# `%26` / `%3D` remain later-stage data rather than separators.
#
# Deliberate declines follow the HTML5 parser's actual table and attribute state:
# `&amptoken=` is unchanged because semicolon-less `&amp`/`&AMP` is decoded
# only before a NON-alphanumeric per the WHATWG flush rule; `&Amp;`,
# `&quest`, and `&equals` are absent from the table; `&amp;amp;token=` decodes only once to a non-token parameter name;
# and `%26token=` / `?token%3D` are data when the query parser performs its split.
#
# Numeric references stop at eight leading zeros. An unbounded `0*` would make
# the streaming WEAK holdback unbounded, so this is a DoS bound rather than a
# claim that longer spellings differ in the HTML specification.
# Each letter composes bounded HTML references over its literal byte and all
# three bytes of its percent escape.
def _html_numeric_refs(cp: int) -> list[str]:
    """The two bounded HTML numeric spellings of one code point.

    Mirrors `_HTML_REF_AMP`'s shape byte for byte, including the <=8-leading-zero
    DoS bound and the WHATWG flush rule (a semicolon-less numeric reference is
    decoded when the next byte cannot extend the number).

    The `;` is excluded from the zero-width alternative so a PRESENT semicolon
    MUST be consumed by the reference. Without it the engine can backtrack the
    reference to its semicolon-less branch and hand the `;` to a FOLLOWING pattern
    that accepts it: on `?token&#61;` with an empty value, `_TOKEN_PARAM_RE`'s EQ
    gave up its `;` and the value class captured it, so pass 4 spliced the
    credential tag over the semicolon in text `chat_runner.py` redacts IN PLACE.
    A real parser never leaves the terminator behind (`&#61;` decodes to `=`,
    `&#61;;` to `=;`), so the zero-width branch with `;` next models a decode no
    parser performs. Today only `_HTML_REF_EQUALS` is reachable -- `;` matches no
    name-letter, nibble, or separator alternative -- but the exclusion is uniform
    in this generator so the invariant is structural rather than per-site. The
    named `amp`/`AMP` lookaheads are deliberately NOT changed: `amp;` is ordered
    first and wins on every match, and the name position rejects `;`.
    """
    return [
        rf"&#0{{0,8}}{cp}(?:;|(?![0-9;]))",
        rf"&#[Xx]0{{0,8}}{cp:x}(?:;|(?![0-9A-Fa-f;]))",
    ]


def _html_or_literal(chars: str) -> str:
    """One anchor byte: its literal spellings, or an HTML reference to any of them.

    The literal class is left for the surrounding scoped `(?ai:...)` to fold, as
    the percent ladder already relies on. A numeric reference carries DIGITS,
    which no case fold reaches, so a letter byte emits references for BOTH cases
    explicitly -- without that, `%6&#102;` matched while `%6&#70;` did not.
    """
    literals = sorted(set(chars))
    alternatives = ["[" + "".join(literals) + "]" if len(literals) > 1 else literals[0]]
    for char in literals:
        alternatives += _html_numeric_refs(ord(char))
        if char.isalpha():
            alternatives += _html_numeric_refs(ord(char.swapcase()))
    return "(?:" + "|".join(alternatives) + ")"


#: `%` at the HTML stage. `&percnt;` REQUIRES its semicolon: unlike `amp`, it is
#: absent from the 106-entry semicolon-less legacy set, so `&percnt74` is data.
#: Named references are case-sensitive, so disable the surrounding name ladder's
#: ASCII case fold for this literal while numeric references keep folding `X`.
_PERCENT_SIGN_RE = "(?:" + "|".join(["%", *_html_numeric_refs(0x25), "(?-i:&percnt;)"]) + ")"


def _token_name_letter(letter: str) -> tuple[str, str]:
    """(complete, partial) spellings of one `token` letter.

    COMPLETE is every spelling that decodes to the letter: the literal
    (ASCII-case folded by the caller's `(?ai:...)`), an HTML reference to either
    case, and a percent escape whose three bytes are EACH spellable at the HTML
    stage -- the composition the two modelled stages admit (`&#37;74`,
    `%&#55;&#52;`). ASCII case differs in the HIGH nibble only, so the low nibble
    is case-invariant and the high nibble is a two-digit class.

    PARTIAL adds every end-of-chunk prefix whose last byte is NOT in
    `_CRED_CLASS` -- i.e. one ending at a `;` -- because `natural_cut` already
    holds every other prefix. The bare-`%` forms are kept from the round-4
    ladder so its committed behaviour is unchanged.
    """
    lower, upper = format(ord(letter), "x"), format(ord(letter.upper()), "x")
    assert lower[1] == upper[1], letter
    high = _html_or_literal(lower[0] + upper[0])
    low = _html_or_literal(lower[1])
    complete = (
        "(?:"
        + "|".join(
            [
                letter,
                *_html_numeric_refs(ord(letter)),
                *_html_numeric_refs(ord(letter.upper())),
                _PERCENT_SIGN_RE + high + low,
            ]
        )
        + ")"
    )
    partial = (
        "(?:"
        + "|".join(
            [
                complete,
                _PERCENT_SIGN_RE + high,
                _PERCENT_SIGN_RE,
                rf"%[{lower[0]}{upper[0]}]?",
            ]
        )
        + ")"
    )
    return complete, partial


_TOKEN_PARAM_NAME_SPELLINGS = tuple(_token_name_letter(c) for c in "token")
_TOKEN_PARAM_NAME_RE = "".join(c for c, _ in _TOKEN_PARAM_NAME_SPELLINGS)

#: A ``token`` URL parameter; group 1 is its value. A ``${`` right after
#: ``token=`` is a template literal, not a value.
TOKEN_PARAMETER = (
    rf"{_TOKEN_PARAM_SEP_RE}(?ai:{_TOKEN_PARAM_NAME_RE}){_TOKEN_PARAM_EQ_RE}"
    rf"(?!\$\{{)({_TOKEN_PARAM_VALUE_CLASS}+)"
)


# ── Tiers ──
# Every redaction rule carries one tier: what a value it matches can do alone,
# or combined only with public data.
#
# * ``secret``      -- authenticates or signs (secret keys, session tokens,
#                      private keys, vendor API tokens, URI passwords, JWTs).
# * ``bearer_link`` -- a URL that grants access by itself until it expires
#                      (``?token=`` links, suspicious outbound URLs).
# * ``identifier``  -- names or describes and cannot authenticate (key IDs,
#                      account IDs, ARNs, request IDs, digests).
#
# The tier is METADATA today: no redactor reads it, so what gets redacted is
# unchanged. An ``identifier`` rule is still redacted until its behaviour is
# changed deliberately, in its own reviewed change.

TIER_SECRET = "secret"
TIER_BEARER_LINK = "bearer_link"
TIER_IDENTIFIER = "identifier"

#: The three tiers, in order of what a value can do.
TIERS = (TIER_SECRET, TIER_BEARER_LINK, TIER_IDENTIFIER)

#: Tier of every rule id a redactor in ``security`` can name, plus the labels
#: of :data:`VENDOR_TOKEN_PATTERNS`. A rule id missing here is a test failure,
#: so a new rule cannot ship without a tier.
RULE_TIERS = {
    # Core redactor, pass 1 (fixed patterns) and its labelled AWS forms.
    "aws_access_key_id": TIER_IDENTIFIER,
    "aws_secret_access_key": TIER_SECRET,
    "aws_session_token": TIER_SECRET,
    "private_key": TIER_SECRET,
    "slack_token": TIER_SECRET,
    "telegram_bot_token": TIER_SECRET,
    "discord_bot_token": TIER_SECRET,
    "github_token": TIER_SECRET,
    "gitlab_token": TIER_SECRET,
    "stripe_key": TIER_SECRET,
    "sendgrid_key": TIER_SECRET,
    "openai_key": TIER_SECRET,
    "anthropic_key": TIER_SECRET,
    "npm_token": TIER_SECRET,
    "pypi_token": TIER_SECRET,
    "digitalocean_token": TIER_SECRET,
    "google_oauth_secret": TIER_SECRET,
    "url_userinfo": TIER_SECRET,
    "jwt": TIER_SECRET,
    # Pass-1 match no named rule claims (e.g. an ``Authorization: Bearer`` header).
    "credential_pattern": TIER_SECRET,
    # Passes 2-4.
    "encoded_credential": TIER_SECRET,
    "bare_aws_secret": TIER_SECRET,
    "token_parameter": TIER_BEARER_LINK,
    # Outbound-URL pass: each rule removes or blocks a whole URL.
    "exfil_decode_saturated": TIER_BEARER_LINK,
    "exfil_encoded_credential": TIER_BEARER_LINK,
    "exfil_fixed_credential": TIER_BEARER_LINK,
    "exfil_hard_credential": TIER_BEARER_LINK,
    "exfil_percent_encoding": TIER_BEARER_LINK,
    "exfil_query_length": TIER_BEARER_LINK,
    "exfil_query_pattern": TIER_BEARER_LINK,
    "exfil_unknown": TIER_BEARER_LINK,
    # Labels of VENDOR_TOKEN_PATTERNS.
    "openai-project-key": TIER_SECRET,
    "anthropic-key": TIER_SECRET,
    "vendor-key": TIER_SECRET,
    "github-fine-grained-pat": TIER_SECRET,
    "gitlab-pat": TIER_SECRET,
    "npm-token": TIER_SECRET,
    "pypi-token": TIER_SECRET,
}


def rule_tier(rule_id: str) -> str:
    """The tier of *rule_id*; ``KeyError`` for a rule with no tier."""
    return RULE_TIERS[rule_id]
