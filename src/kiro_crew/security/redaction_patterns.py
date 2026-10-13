"""The credential alternation and the pre-filter that gates it.

The two are ONE unit and live in one module on purpose. The batch redactor skips
the alternation entirely when the pre-filter returns False, so the pre-filter is
a strict superset of every branch: an input a branch accepts and the pre-filter
rejects is a silent leak, not a missed optimisation. The comment above
``_CREDENTIAL_PATTERNS`` records the two-site rule for editing a branch, and
``test_credential_prefilter.py`` asserts the superset property.

The shape-only JWT branch is searched apart from the others, so one scan stays
linear on text that repeats a lookalike. :func:`get_credential_patterns` hands
the alternation to callers outside the package.

:mod:`kiro_crew.security.redaction` is the import path of every name defined
here.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator

import kiro_crew.security.pem_scan as pem_scan
from kiro_crew.credential_patterns import AWS_KEY_ID, JWT_MULTI_SEGMENT

# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().
#
# ⚠ THIS PATTERN HAS A DEPENDENT PRE-FILTER. `_might_contain_credential` below
# gates the scan of this pattern on a cheap necessary condition, and
# `redact_credentials` SKIPS the scan entirely when that gate returns False. The
# gate is therefore part of the redaction boundary, not an optimisation detail:
# any input a branch here accepts but the gate rejects is a silent leak.
#
# So EDITING A BRANCH IS A TWO-SITE CHANGE:
#   * ADDING a branch     -> register a sample in `test_credential_prefilter.py`
#                            and an anchor in `_might_contain_credential`.
#                            `test_every_pattern_branch_has_a_prefilter_anchor`
#                            fails on the branch count until you do.
#   * WIDENING a branch   -> widen the corresponding anchor to match, because the
#                            anchor must stay a SUPERSET of the branch. A widened
#                            branch does NOT change the branch count, so the count
#                            assertion cannot see it. Two tests cover this:
#                            `test_a_widened_branch_cannot_outgrow_its_anchor`
#                            enumerates each branch's own alternatives, so a NEW
#                            alternative (a second token prefix) is caught; and
#                            `test_widening_a_branch_cannot_outgrow_its_anchor`
#                            perturbs each sample, so a case-fold or homoglyph
#                            relaxation is caught.
#   * Making a branch CASE-INSENSITIVE -> the anchor MUST use the same regex
#                            engine. A case-sensitive literal cannot gate a
#                            `(?i:…)` branch, and neither can `str.lower()` —
#                            see `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` for the
#                            bypass that cost.
_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    # key-value forms: tolerate an optional closing quote after the key name and an
    # optional opening quote before the value so JSON (`"aws_secret_access_key": "v"`)
    # is redacted, not just bare `key=v` / `key: v`. Without the `["']?` the closing
    # quote in JSON sits between the key and `:` and defeats the match → secret leaks.
    # The value class is [^\s"',}]+ (NOT \S+): \S+ is greedy and, in compact JSON
    # like {"aws_secret_access_key":"SECRET","region":"x"}, swallows everything
    # through the closing brace (`"`, `,`, `}` all match \S) — destroying adjacent
    # fields and consuming a following credential key so it's never matched/counted.
    # Stopping at JSON structural delimiters bounds the value while still matching
    # bare key=value forms.
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    # PEM private key: match the ENTIRE block (header + base64 body), not just
    # the header phrase. redact_credentials() replaces the matched SPAN, so a
    # header-only match (the original form) left the secret base64 body verbatim.
    # Two mutually exclusive tails after the header:
    #   1. Full block — ``[\s\S]*?`` (any char, incl. newlines) spans the body
    #      lazily to the first END marker. ``[\s\S]`` (not a base64 char class)
    #      is required so encrypted keys — whose ``Proc-Type:``/``DEK-Info:``
    #      headers carry ``:`` and ``,`` — are fully spanned rather than cut
    #      short at the first non-base64 char.
    #   2. Truncated block (no END) — consume only *subsequent* PEM body lines:
    #      each continuation must start with a newline and be a base64 line or a
    #      ``Proc-Type:``/``DEK-Info:`` metadata header. This deliberately does
    #      NOT use ``$``/``\Z``: without re.MULTILINE ``$`` means end-of-STRING,
    #      so a lazy ``[\s\S]*?`` with a ``|$`` fallback swallowed everything
    #      from a header mentioned inline in prose (LLM output, docs) to the end
    #      of the string — silently deleting all trailing lines. Requiring a
    #      leading newline per line means an inline header in prose (real key
    #      material always begins on the line *after* the header) matches only
    #      the header phrase, leaving trailing content intact, while a genuine
    #      truncated key still has its body lines redacted.
    #      The final ``(?=\r?\n[A-Za-z0-9+/=])`` lookahead alternative lets the
    #      run cross a SINGLE blank line when the *next* line begins with base64
    #      material. RFC 1421 ENCRYPTED PEMs put a MANDATORY blank line between
    #      the ``DEK-Info:`` header and the base64 body; without this lookahead
    #      the per-line "every continuation must contain a base64 char" rule
    #      stopped at that blank line and leaked the whole encrypted body (for
    #      both a truncated key AND a complete encrypted key whose body exceeds
    #      the full-block cap). Because the lookahead consumes nothing, TWO+
    #      consecutive blank lines still terminate the run — trailing prose is
    #      preserved (no over-redaction).
    f"|{pem_scan.HEADER}"
    r"(?:"
    f"{pem_scan.FULL_BLOCK}"
    r"|(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
    r")"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    # Telegram bot token: ``<bot_id>:<secret>`` — bot_id is 6+ digits, secret is
    # ~35 URL-safe base64 chars. The ``{30,}`` floor sits deliberately below the
    # real length so shortened/rotated test tokens are still caught. Analogue to
    # the Slack token above. Telegram tokens can live in ``config.json``
    # (agent-readable), so an echoed config would otherwise leak a full
    # bot-control credential unredacted. The value class ``[A-Za-z0-9_-]`` stops
    # at structural delimiters (space, quote, comma, brace), so it can't swallow
    # adjacent fields; over-redacting a rare ``digits:token`` lookalike is the
    # safe direction.
    r"|[0-9]{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    # Discord bot token: three base64url segments — ``base64(application_id)``,
    # a 6-char timestamp, and an HMAC. The first segment is base64 of a decimal
    # snowflake, so its leading character is fixed by the id's first digit
    # (``M``/``N``/``O`` for the 1-9 range every live snowflake starts with), and
    # the timestamp segment is always EXACTLY 6 characters. Both anchors matter:
    # the same rule written as three open-ended runs matches an ordinary dotted
    # identifier or a base64 blob with periods in it, and a redactor that eats
    # arbitrary text is a different bug. Length floors sit below the real ones so
    # a shortened/rotated test token is still caught. Same reasoning as Telegram
    # above — ``discord.bot_token`` can live in ``config.json``, which the agent
    # can read, so an echoed config would otherwise leak bot control verbatim.
    # The boundary guards keep the leading ``[MNO]`` from landing mid-run inside
    # a longer base64 blob and redacting an arbitrary tail of it, the same way
    # the link-token branch below guards its own ``eyJ`` anchor.
    r"|(?<![A-Za-z0-9_-])[MNO][A-Za-z0-9_-]{22,30}"
    r"\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])"  # Discord bot token
    # ── Third-party developer credentials ──
    # Distinctive, fixed-case prefixes → very low false-positive risk.  Minimum
    # lengths are kept slightly below the real token lengths so shortened test /
    # rotated variants are still redacted (over-redaction on a prefix match is the
    # safe direction).  Case-sensitive by design (these prefixes are issued in a
    # fixed case); do NOT fold — folding would broaden false positives.
    r"|gh[opsur]_[A-Za-z0-9]{30,255}"  # GitHub PAT (ghp_) + oauth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab PAT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # SendGrid API key
    r"|sk-proj-[A-Za-z0-9_-]{16,}"  # OpenAI project key
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic API key
    r"|npm_[A-Za-z0-9]{24,}"  # npm access token
    r"|pypi-[A-Za-z0-9_-]{16,}"  # PyPI API token
    r"|do[opr]_v1_[A-Za-z0-9]{40,}"  # DigitalOcean PAT/OAuth/refresh
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"  # Google OAuth client secret
    # Connection/fetch URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here). http(s)/ftp(s)
    # are included because URL userinfo is a credential wherever it appears
    # (e.g. a token-bearing artifact CDN base quoted by an update-failure
    # message); the user:pass@ shape cannot false-positive on a bare URL — a
    # port (``:8080``) is never followed by ``@`` within the authority.
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?"
    r"|https?|ftps?)"
    # User portion is `*` (not `+`): an empty-user string (`mongodb+srv://:secret@…`) still redacts.
    # Password segment allows ``@`` (``[^\s/]`` not ``[^\s/@]``): an unencoded
    # ``@`` inside a password is common, and stopping the match at the FIRST
    # ``@`` would redact only the head and leak the rest (``…ss@host``) to
    # logs. ``/`` still bounds the authority, so greedy ``+`` consumes through
    # the FINAL ``@`` — the real userinfo/host separator — and never past it.
    r"://[^\s:/@]*:[^\s/]+@"
    # ── JWT / JWE / OAuth Bearer tokens ──
    # `eyJ` is the base64url encoding of every JWT header's `{"` prefix; a signed
    # JWT (JWS) is three `.`-separated base64url segments (header.payload.sig), an
    # encrypted JWT (JWE, RFC 7516) is five (header.key.iv.ciphertext.tag), and our
    # OWN dashboard link token is two — `base64url(payload).base64url(hmac_sig)`,
    # see `dashboard.token_auth.generate_token`. The 3-and-5-segment shapes are
    # matched by the `{2,4}` quantifier below; the 2-segment link token has its
    # OWN separately bounded alternative.
    #
    # The floor stays at 2 because the two-segment dashboard token is what a higher
    # floor drops: it would not match here at all and would fall through to the
    # bare-secret entropy pass, whose run class `[A-Za-z0-9+/]` is STANDARD base64
    # and excludes base64url's `-`/`_`. That makes redaction depend on which
    # characters a random HMAC signature happens to contain. That rate is derivable,
    # so it is stated as a closed form rather than as a sample. HMAC-SHA256 is 256
    # bits and base64url-unpadded gives 43 chars. The first 42 each carry a full 6
    # bits, so each is uniform over the 64-char alphabet, of which exactly 2 are
    # `-`/`_`. The 43rd carries only the leftover 4 bits (256 - 42*6), and they
    # land in the HIGH bits of its 6-bit
    # group with the low 2 bits zero, so it spans exactly the 16 alphabet indices
    # divisible by 4 (`048AEIMQUYcgkosw`) and can never be `-`/`_`, which sit at
    # 62/63. Hence P(no `-`/`_`) = (62/64)^42 = 26.4%, verified by encoding all
    # 256 possible final digest bytes.
    # So roughly a quarter of tokens would have only the signature replaced (leaving
    # the payload claims verbatim in a URL that still looks complete but is not
    # authenticated), and the other ~74% would stream out entirely unredacted.
    # Matching the whole token here makes the outcome deterministic and replaces it
    # as one unit. The 2-segment token gets its OWN alternative rather than
    # relaxing the segment floor to `{1,4}`. Relaxing
    # the floor over-redacts ordinary code and prose, because the pattern has no left
    # boundary and post-header segments allow an EMPTY match: `keyJson.get(raw)` then
    # redacts to `k[REDACTED…](raw)`, and a JWT quoted at the end of a sentence loses
    # its trailing period. The 3-to-5-segment alternative (`credential_patterns.JWT_MULTI_SEGMENT`,
    # `*` on post-header segments so an empty JWE segment matches) consumes such a period too, an
    # accepted over-redaction cost. The 2-segment alternative therefore carries a left boundary
    # (`(?<![A-Za-z0-9_-])`, plus a guard after `.` that keeps `obj.eyJ…` code unless the token has
    # what no identifier chain has: a `-`, or a payload shaped as base64url of ASCII JSON, each 4th
    # char from the 5th in `A-Za-f`, as every `generate_token` payload is) and per-segment lengths
    # from the generator, not guesswork, because a length FLOOR alone is beatable by a verbose
    # identifier: at `{40,}` the 40-char `eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue` matched.
    #
    # `token_auth._sign` is HMAC-SHA256 base64url-unpadded, so the signature is
    # EXACTLY 43 chars for every token ever minted; that is a property of the digest,
    # not of the payload, so it is pinned as `{43}` rather than a floor. See
    # `test_link_token_signature_is_43_chars`, which fails loudly if `_sign` changes
    # digest, instead of letting redaction silently stop matching.
    #
    # `generate_token` always emits 6 claims (`sub`/`exp`/`session_exp`/`iat`/`nonce`/
    # `gen`), with a 16-hex-char nonce and float timestamps; `app`, `prompt` and
    # `extra` only ADD. Payload length is NOT fixed. It scales with `len(sub)`, and
    # `json.dumps` writes each float timestamp at its own repr width, which base64
    # then quantises into 4-char steps. So the floor is derived, not sampled: a
    # 1-char `sub` (the narrowest a caller passes: the app validator requires at
    # least one char and the other call sites supply a literal fallback), `gen=0`,
    # and all three timestamps at their shortest 12-char repr (an exactly-integral
    # `time.time()` in the current 10-digit epoch era) measures 145 chars past
    # `eyJ`, which leaves the `{96,}` floor 49 chars of headroom against a future
    # shorter claim set while still excluding `eyJ2IjoxfQ.json`. ONLY that derived
    # floor is pinned, by `test_link_token_payload_clears_the_96_char_floor`, which
    # reads the bound from the compiled pattern and the claim keys from a real mint
    # so a dropped claim fails loudly instead of silently disabling redaction. Live
    # payloads are much larger and are NOT pinned, because the exact spread moves
    # with float reprs and caller mix: measured 168-185 for the mandatory-only
    # callers and 192-223 for the two that also pass `app=` (`handlers/core.py`,
    # `token_auth.py`), which adds an `"app"` claim.
    #
    # Order matters: the 3-to-5-segment
    # alternative is tried first at each position, so a real JWS still redacts whole
    # instead of matching `header.payload` and leaving `.signature` exposed.
    # The 3-to-5-segment alternative keeps `*` (not `+`) on post-header segments so an
    # EMPTY segment still counts: a compact JWE with direct
    # (`alg:dir`) or key-agreement (`ECDH-ES`) key management has an empty Encrypted
    # Key (2nd) segment — shape `header..iv.ciphertext.tag` — which a `+` quantifier
    # would fail to match, leaking the ciphertext + tag.
    # The HTTP `Authorization: Bearer <token>` header carries opaque or JWT bearer
    # creds. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url
    # prefix). The header name + scheme are matched case-insensitively via scoped
    # `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230
    # §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is
    # case-insensitive (RFC 6750 §2.1) — so `authorization: bearer …` emitted by
    # requests / net/http / HTTP2 frame logs is redacted too. The separator is
    # JSON-aware: an optional quote may precede the
    # `:`/`=` and the token, so a serialized header `{"Authorization": "Bearer
    # <tok>"}` in a structured-log/JSON request dump is redacted as well. Both
    # alternatives are scoped tightly: the JWT segment class cannot cross the
    # literal `.` separators and the Bearer token class (`[A-Za-z0-9._~+/-]`, RFC
    # 6750 `b64token`) stops at whitespace/quotes, so neither over-captures. A
    # Bearer header carrying a JWT redacts as one match (the Bearer class subsumes
    # the JWT); a bare JWT is still caught independently (defense in depth).
    f"|{JWT_MULTI_SEGMENT}"  # JWS (3-seg) / JWE (5-seg incl. dir/ECDH-ES), shared spelling
    r"|(?<![A-Za-z0-9_-])(?!(?<=\.)(?!eyJ[A-Za-z0-9_]*-)(?!eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_]*-)(?!eyJ[A-Za-z0-9_-](?:[A-Za-f][A-Za-z0-9_-]{3})*(?:[A-Za-f][A-Za-z0-9_-]{1,2})?\.))eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"  # 2-seg link token
    r"|(?i:Authorization)[\"\']?\s*[:=]\s*[\"\']?(?i:Bearer)\s+[A-Za-z0-9._~+/-]+=*"  # HTTP/JSON bearer
    r")",
)


def get_credential_patterns() -> list[re.Pattern[str]]:
    """Public accessor for the canonical credential regexes.

    Lets other modules (e.g. deploy-web's pre-publish content scan) reuse the
    same patterns without coupling to the private ``_CREDENTIAL_PATTERNS`` name,
    so a future rename here can't silently turn a downstream scan into a no-op.
    Returns a list so callers can iterate uniformly; the fork keeps a single
    combined compiled regex, so the list has one element.
    """
    return [_CREDENTIAL_PATTERNS]


# The JWS/JWE branch is shape-only (it matches `honeyJar.example.com`), so its hits need a
# JSON-object header. Only it and the one-dot link token start with `eyJ`: two dots mark them.
_CREDENTIAL_PATTERNS_SANS_JWT = re.compile(
    _CREDENTIAL_PATTERNS.pattern.replace(f"|{JWT_MULTI_SEGMENT}", "", 1)
)

# Telegram bot token, as `_CREDENTIAL_PATTERNS` spells it.
_TELEGRAM_BRANCH = r"[0-9]{6,}:[A-Za-z0-9_-]{30,}"
_TELEGRAM_AT_RE = re.compile(_TELEGRAM_BRANCH)

# Leftmost start of a non-JWT branch, found in linear time. Unguarded, the Telegram
# branch re-walks a digit run from every digit in it. A later digit has fewer digits
# before the same ``:``, so only the run's first digit can win; `_sans_jwt_start`
# checks the one exception, a run that begins before the scan position.
#
# It also drops the PEM full-block tail. A header starts a match whether or not an
# END marker follows (the truncated tail beside it can be empty), so every start is
# unchanged, and the tail would send each header with no END after it to the end of
# the text: quadratic over a text that repeats the header. See `pem_scan`.
_CREDENTIAL_STARTS_SANS_JWT = re.compile(
    _CREDENTIAL_PATTERNS_SANS_JWT.pattern.replace(f"{pem_scan.FULL_BLOCK}|", "", 1).replace(
        f"|{_TELEGRAM_BRANCH}", f"|(?<![0-9]){_TELEGRAM_BRANCH}", 1
    )
)

# `_CREDENTIAL_PATTERNS` for a PEM header that no END marker follows: only the
# truncated tail is left to try there.
_CREDENTIAL_PATTERNS_NO_PEM_BLOCK = re.compile(
    _CREDENTIAL_PATTERNS.pattern.replace(f"{pem_scan.FULL_BLOCK}|", "", 1)
)


def _sans_jwt_start(text: str, pos: int) -> int:
    """Leftmost index >= *pos* where a non-JWT branch matches, else ``len(text) + 1``."""
    if "0" <= text[pos - 1 : pos] <= "9" and _TELEGRAM_AT_RE.match(text, pos):
        return pos
    found = _CREDENTIAL_STARTS_SANS_JWT.search(text, pos)
    return len(text) + 1 if found is None else found.start()


def _is_json_object_segment(segment: str) -> bool:
    """Whether *segment* base64url-decodes to a JSON object: JOSE, itsdangerous, Flask session."""
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, RecursionError):
        return False
    return isinstance(header, dict)


_JWT_SEGMENT_RUN_RE = re.compile(r"[A-Za-z0-9_-]*")


def _segment_run_end(text: str, pos: int) -> int:
    """End of the run of JWT segment characters starting at *pos*."""
    run = _JWT_SEGMENT_RUN_RE.match(text, pos)
    return run.end() if run is not None else pos


def _jwt_branch_matches_at(text: str, i: int, run_end: int) -> bool:
    """Whether ``JWT_MULTI_SEGMENT`` matches at the ``eyJ`` at *i*, its run ending at *run_end*.

    The branch needs one segment character after ``eyJ``, then two ``.`` separators with
    a possibly empty run between them. Segment characters exclude ``.``, so the first
    separator can only sit at *run_end*.
    """
    return (
        run_end > i + 3
        and text.startswith(".", run_end)
        and text.startswith(".", _segment_run_end(text, run_end + 1))
    )


def _next_jwt_branch_start(text: str, pos: int, matched_run_end: int) -> tuple[int, int]:
    """Leftmost index >= *pos* where the JWT branch matches, and that start's run end.

    Every ``eyJ`` in one segment run shares the run's end, and a later one leaves fewer
    characters before it, so one failure settles the whole run and the scan jumps past
    it. ``search`` instead re-walks the run from each ``eyJ``, which is quadratic on a
    long ``eyJ`` repeat. For the same reason one success settles every later ``eyJ`` with
    a character after it in that run, so *matched_run_end*, the end of the run the last
    start matched in, is reused rather than walked again when an overlapping match of
    another branch moved *pos* past that start. No start: ``(len(text) + 1, -1)``.
    """
    if pos < matched_run_end:
        i = text.find("eyJ", pos, matched_run_end - 1)
        if i != -1:
            return i, matched_run_end
        pos = matched_run_end
    while (i := text.find("eyJ", pos)) != -1:
        run_end = _segment_run_end(text, i)
        if _jwt_branch_matches_at(text, i, run_end):
            return i, run_end
        pos = run_end
    return len(text) + 1, -1


def _credential_search(text: str, pos: int, starts: list[int]) -> re.Match[str] | None:
    """``_CREDENTIAL_PATTERNS.search(text, pos)``, in time linear over a whole scan.

    The leftmost match starts at the earlier of the leftmost JWT-branch start and the
    leftmost start of every other branch, and ``match`` there applies the alternation's
    own branch order. *starts* caches both starts, and the JWT start's run end, across
    calls with a rising *pos*; its last slot is the start of the text's last PEM END
    marker, ``-2`` until first needed.

    At a PEM header the full-block tail needs an END marker starting at or after the
    header's end, so the last marker alone says whether one exists. When none does,
    the match is made without that tail: it would fail only after reading to the end
    of the text, once per header, and the result is the same.
    """
    if starts[0] < pos:
        starts[0] = _sans_jwt_start(text, pos)
    if starts[1] < pos:
        starts[1], starts[2] = _next_jwt_branch_start(text, pos, starts[2])
    first = min(starts[0], starts[1])
    if first > len(text):
        return None
    if text.startswith(pem_scan.HEADER_PREFIX, first):
        if starts[3] == -2:
            starts[3] = pem_scan.last_end_start(text)
        if pem_scan.has_no_end_after(text, first, starts[3]):
            return _CREDENTIAL_PATTERNS_NO_PEM_BLOCK.match(text, first)
    return _CREDENTIAL_PATTERNS.match(text, first)


def _credential_matches(text: str) -> Iterator[re.Match[str]]:
    """``_CREDENTIAL_PATTERNS.finditer(text)``, minus JWT-branch hits whose header is not JSON.

    A rejected hit retries the other branches at its start, then resumes one character on,
    so a credential nested inside the rejected span is still found. A header holding a
    second ``eyJ`` stays a credential: rejecting it would rescan that header once per ``eyJ``.
    """
    pos = 0
    starts = [-1, -1, -1, -2]
    while (m := _credential_search(text, pos, starts)) is not None:
        header = m.group().split(".", 1)[0]
        if (
            m.group().startswith("eyJ")
            and m.group().count(".") >= 2
            and header.find("eyJ", 1) == -1
            and not _is_json_object_segment(header)
        ):
            alt = _CREDENTIAL_PATTERNS_SANS_JWT.match(text, m.start())
            if alt is None:
                pos = m.start() + 1
                continue
            m = alt
        yield m
        pos = max(m.end(), m.start() + 1)


def _contains_credential_pattern(text: str) -> bool:
    """Validated ``_CREDENTIAL_PATTERNS.search``: see :func:`_credential_matches`."""
    return next(_credential_matches(text), None) is not None


# ── Cheap pre-filter for `_CREDENTIAL_PATTERNS` (performance only) ──
# `_CREDENTIAL_PATTERNS` is a 23-branch alternation, so `re` retries every branch
# at essentially every position: measured 117 ns/char, and it is the single
# hottest line in the gateway's event loop (38.2% of all py-spy samples, reached
# per message per dirty-slot flush). The scan cost is paid in full even though
# real text almost never contains a credential — measured 0 matches across 1,804
# live session-history messages (1.47 MB).
#
# So `_might_contain_credential` answers the cheap question "could a match exist
# at all?" and lets `redact_credentials` skip the expensive scan when the answer
# is no. It is a strict SUPERSET of `_CREDENTIAL_PATTERNS`, i.e. for every string
# the pattern matches, this returns True. That direction is the security
# property: a false POSITIVE only costs a scan we would have run anyway, while a
# false NEGATIVE would skip redaction and leak a credential into persisted chat
# history. Every condition below is therefore a NECESSARY condition of a branch,
# never a restatement of it — each is deliberately looser than the branch it
# stands in for.
#
# THE MAINTENANCE HAZARD this is built against: adding a 24th branch to
# `_CREDENTIAL_PATTERNS` without adding a matching anchor here would silently
# disable redaction for it. Nothing about the pattern edit would look wrong, and
# the failure is invisible in output — the branch simply stops firing. So
# `test_credential_prefilter.py` splits `_CREDENTIAL_PATTERNS.pattern` on its
# top-level `|`, asserts the branch count equals the number of registered sample
# credentials, and asserts the pre-filter fires for each. A new branch fails that
# count assertion loudly instead of quietly widening the leak.
#
# Literals are case-sensitive because the branches they stand for are (these
# prefixes are issued in a fixed case); the sole case-insensitive branch
# (`Authorization: Bearer`) is handled separately below.
_CREDENTIAL_PREFILTER_LITERALS: tuple[str, ...] = (
    "AKIA",  # AWS access key ID
    "ASIA",  # AWS access key ID (STS)
    "AccessKey",  # SecretAccessKey + AccessKeyId (shared substring)
    "aws_secret_access_key",
    "aws_session_token",
    "aws_access_key_id",
    "SessionToken",
    "PRIVATE KEY-----",  # PEM header AND footer both carry it
    "xox",  # Slack token
    "github_pat_",
    "glpat-",
    "k_live_",  # sk_live_ / rk_live_ (shared substring)
    "k_test_",  # sk_test_ / rk_test_ (shared substring)
    "SG.",  # SendGrid
    "sk-proj-",  # OpenAI
    "sk-ant-",  # Anthropic
    "npm_",
    "pypi-",
    "_v1_",  # do[opr]_v1_ DigitalOcean
    "GOCSPX-",  # Google OAuth client secret
    "eyJ",  # JWS / JWE / 2-segment link token
)

# Branches with no usable literal anchor. Each is the branch's own leading shape
# with its expensive tail dropped, so it stays a superset while keeping a narrow
# first-character set that `re` can skip on.
#   `gh[opsur]_`     — GitHub PAT family; a bare "gh" literal matches ordinary
#                      prose ("through", "might"), so the class is kept.
#   `[0-9]{6,}:…{30}` — Telegram bot token. The trailing 30-char run matters: a
#                      bare `[0-9]{6,}:` matches an epoch timestamp followed by a
#                      colon, which fired on 29 of 614 real messages.
#   `[MNO]…\.`        — Discord bot token (first segment is base64 of a snowflake).
#   `://…:…@`         — URI userinfo. The scheme alternation is dropped, which is
#                      what leaves a `://` literal prefix for `re` to search on;
#                      a bare `://` would match every ordinary URL.
#
# Each anchor is written to START WITH A LITERAL, because that is the one shape
# `re` searches at memchr speed; a pattern starting with a class or a
# case-insensitive letter is tried at every position (measured 6-12 ns/char).
# The literal is the rarest character every match must contain, and the rest of
# the shape is checked around it with fixed-width look-behinds, so each anchor
# accepts EXACTLY the strings its plain spelling does:
#   Telegram `[0-9]{6,}:X{30}`     == `:` preceded by 6 digits, then X{30}.
#   Discord  `[MNO]X{22,30}\.`     == `.` preceded by X{23} (cheap reject) and,
#                                     for some n in 22..30, by [MNO]X{n}.
# `test_rewritten_anchors_agree_with_their_plain_spelling` holds each one to
# its plain spelling.
_CREDENTIAL_PREFILTER_GH_RE = re.compile(r"gh[opsur]_")
_CREDENTIAL_PREFILTER_TELEGRAM_RE = re.compile(r":(?<=[0-9]{6}:)[A-Za-z0-9_-]{30}")
_CREDENTIAL_PREFILTER_DISCORD_RE = re.compile(
    r"\.(?<=[A-Za-z0-9_-]{23}\.)(?:"
    + "|".join(rf"(?<=[MNO][A-Za-z0-9_-]{{{n}}}\.)" for n in range(22, 31))
    + ")"
)
_CREDENTIAL_PREFILTER_URI_RE = re.compile(r"://[^\s:/@]*:[^\s/]+@")

# The `Authorization: Bearer` branch is the ONLY case-insensitive branch, and it is
# spelled `(?i:Authorization)`. This anchor reuses that exact sub-pattern, so it is
# a superset of the branch BY CONSTRUCTION — same engine, same folding rules.
#
# `"authorization" in text.lower()` is NOT a valid anchor for it, because
# `str.lower()` and `re.IGNORECASE` are two DIFFERENT case-folding
# implementations and they disagree. `re` folds via `sre_compile._equivalences`,
# which treats U+0131 (LATIN SMALL LETTER DOTLESS I) and U+0130 (LATIN CAPITAL
# LETTER I WITH DOT ABOVE) as equivalent to `i`/`I`; `str.lower()` leaves U+0131
# unchanged and expands U+0130 to two code points. So the branch MATCHES
# `Authorızation: Bearer <token>` while a `.lower()` anchor MISSES it, which skips
# pass 1 and leaves the bearer token verbatim in persisted chat history. The same
# disagreement holds for U+017F/`s` and U+212A/`k`, so it is a class of defect
# rather than one homoglyph: a case-insensitive branch is only safely anchored by
# the SAME regex engine, never by a hand-rolled fold.
# Pinned by `test_unicode_case_folding_cannot_bypass_the_prefilter`.
#
# For speed the anchor is split on its `z`, the one letter `re.IGNORECASE` folds
# onto nothing but `z` and `Z`: each half starts with that literal (searched at
# memchr speed) and leaves every other letter to the same `(?i:…)` sub-pattern,
# so the folding is still the engine's own. Together the two accept exactly what
# `(?i:Authorization)` does.
_CREDENTIAL_PREFILTER_AUTHORIZATION_RE = re.compile(r"z(?<=(?i:Authori)z)(?i:ation)")
_CREDENTIAL_PREFILTER_AUTHORIZATION_UPPER_RE = re.compile(r"Z(?<=(?i:Authori)Z)(?i:ation)")


def _might_contain_credential_py(text: str) -> bool:
    """Return True if *text* could contain a `_CREDENTIAL_PATTERNS` match.

    A strict superset of `_CREDENTIAL_PATTERNS.search(text) is not None`: it may
    return True where the pattern would not match, but it MUST NOT return False
    where the pattern would match. Callers use it only to skip a scan whose
    result is already known to be empty, so output is unchanged either way.
    """
    for literal in _CREDENTIAL_PREFILTER_LITERALS:
        if literal in text:
            return True
    return (
        _CREDENTIAL_PREFILTER_GH_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_TELEGRAM_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_DISCORD_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_URI_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_AUTHORIZATION_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_AUTHORIZATION_UPPER_RE.search(text) is not None
    )


def _native_gate_fingerprint() -> str:
    """Hash of the gate's definition; the native module is used only when it matches.

    Covers every literal and anchor above plus the Unicode version, because the
    native gate spells Python's ``\\s`` and case folding out by hand. Any edit
    here changes the hash and turns the native gate off until it is rebuilt.
    """
    import hashlib
    import unicodedata

    spec = repr(
        (
            _CREDENTIAL_PREFILTER_LITERALS,
            _CREDENTIAL_PREFILTER_GH_RE.pattern,
            _CREDENTIAL_PREFILTER_TELEGRAM_RE.pattern,
            _CREDENTIAL_PREFILTER_DISCORD_RE.pattern,
            _CREDENTIAL_PREFILTER_URI_RE.pattern,
            _CREDENTIAL_PREFILTER_AUTHORIZATION_RE.pattern,
            _CREDENTIAL_PREFILTER_AUTHORIZATION_UPPER_RE.pattern,
            unicodedata.unidata_version,
        )
    )
    return hashlib.sha256(spec.encode()).hexdigest()[:16]


def _load_native_gate():  # type: ignore[no-untyped-def]
    """The optional ``kc_native`` gate, or None (not built, disabled, or out of date)."""
    import os

    if os.environ.get("KIROCREW_NATIVE_REDACTION", "1") == "0":
        return None
    try:
        import kc_native  # type: ignore[import-not-found]
    except ImportError:
        return None
    if getattr(kc_native, "GATE_FINGERPRINT", None) != _native_gate_fingerprint():
        # Built for another gate definition or Unicode version (the fingerprint
        # covers unicodedata.unidata_version, so a build matches one CPython
        # minor's Unicode tables). Safe, but the fast path is off: say so once.
        import logging

        logging.getLogger(__name__).debug(
            "kc_native fingerprint does not match this interpreter's credential gate; "
            "using the Python gate"
        )
        return None
    return kc_native.might_contain_credential


_NATIVE_GATE = _load_native_gate()


def _might_contain_credential(text: str) -> bool:
    """Return True if *text* could contain a `_CREDENTIAL_PATTERNS` match.

    Same answer as :func:`_might_contain_credential_py` for every string; uses the
    native single-pass scan when it is available, else the Python one. A str the
    native side cannot encode (a lone surrogate) takes the Python path.
    """
    if _NATIVE_GATE is not None:
        try:
            return _NATIVE_GATE(text)
        except UnicodeEncodeError:
            pass
    return _might_contain_credential_py(text)


# Minimum string length at which `_might_contain_credential` is cheaper than the
# `_CREDENTIAL_PATTERNS` alternation it gates. The pre-filter has a fixed ~590 ns
# floor (21 substring searches plus 5 anchored regex calls) that does not shrink
# with the input, so on a very short string the alternation simply wins: measured
# 684 ns against 494 ns at 8 characters, crossing over at 12 and reaching 3.4x by
# 256. Callers scanning SHORT strings -- a decoded base64 blob is typically 16-30
# characters -- must gate on this rather than assume the pre-filter is
# unconditionally cheaper.
#
# Held at 16 rather than the measured crossover of 12, deliberately: the gate is
# verdict-neutral (the pre-filter is a proven superset, so either route reaches the
# same answer), which makes a conservative threshold cost at most one alternation
# scan on a 12-15 character blob and makes it robust to the crossover drifting as
# the pre-filter's own cost changes. It has already drifted once -- adding the
# case-insensitive Authorization anchor moved it from 16 to 12.
_PREFILTER_MIN_LEN = 16
