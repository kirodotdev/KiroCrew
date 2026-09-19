"""Credential redaction on every output path.

This is the OUTPUT side of the module, and the widest external surface in it:
the batch redactors run on every path that persists, displays or forwards text,
so a name here is called from most of the codebase rather than from one caller.

The alternation and its pre-filter are ONE unit. The pre-filter is a documented
strict superset of the alternation, and the batch redactor SKIPS the scan
entirely when the pre-filter returns False, so an input the alternation would
have matched but the pre-filter rejects is a silent leak rather than a missed
optimisation. They are declared adjacent, with the comment that records the
relation, and the superset property is asserted by test.

The entropy machinery behind them answers a different question from the
alternation: a bare high-entropy run carries no marker to anchor on, so it is
judged by shape -- length, character classes, entropy, decodability -- and every
gate is a separate predicate so a refusal can name which one fired.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import math
import posixpath
import re
import secrets
from collections import Counter
from collections.abc import Callable

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
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:"
    r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
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
    # ── Third-party developer credentials (AWS-345 / AWS-59) ──
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
    # User portion is `*` (not `+`): empty-user connection strings (e.g. MongoDB
    # Atlas IAM `mongodb+srv://:secret@…`) still redact the password (ported
    # from the upstream project).
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
    # its trailing period. The 2-segment alternative therefore carries a left boundary
    # (`(?<![A-Za-z0-9_.-])`, as `_BARE_SECRET_RUN_RE` already does, plus `.` so an
    # attribute access `obj.eyJ…` is excluded too) and per-segment lengths taken from
    # the generator, not from guesswork, because a length FLOOR alone is beatable by a
    # sufficiently verbose identifier: at `{40,}` the 40-char
    # `eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue` matched.
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
    r"|(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"  # 2-seg link token
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
_CREDENTIAL_PREFILTER_GH_RE = re.compile(r"gh[opsur]_")
_CREDENTIAL_PREFILTER_TELEGRAM_RE = re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{30}")
_CREDENTIAL_PREFILTER_DISCORD_RE = re.compile(r"[MNO][A-Za-z0-9_-]{22,30}\.")
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
_CREDENTIAL_PREFILTER_AUTHORIZATION_RE = re.compile(r"(?i:Authorization)")


def _might_contain_credential(text: str) -> bool:
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
    )


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


# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ── Label-independent bare-secret detection ──
# A 40-char AWS *secret access key* (the value paired with an AKIA/ASIA access
# key ID) is a bare run of the base64 alphabet with NO distinctive prefix and NO
# key= label, so none of the labelled/prefixed patterns in _CREDENTIAL_PATTERNS
# catch it when it appears standalone (e.g. echoed alone, in a log line, or in a
# JSON array element). We add a conservative, entropy-gated detector for this
# shape. This is the HIGHEST false-positive-risk redaction rule in the module, so
# it is deliberately over-gated: a token must clear EVERY gate below to be
# redacted. The gates are ordered cheapest-first.
#
# AWS secret access keys are exactly 40 base64 characters. We match ANY isolated
# run of >=40 base64-alphabet chars (word-boundary look-arounds keep surrounding
# prose intact and stop a longer high-entropy blob from being split and missed),
# then require the *specific 40-char secret shape* per token.
#
# NOT CONSULTED BY `redact_credentials`. Pass 3 derives its runs from
# `_B64_CHUNK_RE` instead (`run = chunk.rstrip("=")`), because that one scan feeds
# both pass 2 and pass 3 and the two patterns select identical spans. The only
# remaining consumer here is `_text_contains_bare_secret`. That split is a
# desync hazard: WIDENING THIS PATTERN ALONE (adding base64url `-_`, say) would
# change the URL scan and leave the redactor untouched, silently. Any edit to the
# character class or the `{40,}` floor must be mirrored in `_B64_CHUNK_RE` above.
# `test_the_two_base64_run_patterns_stay_structurally_coupled` pins both literals
# so such an edit fails loudly rather than drifting.
_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")

# Exactly-40 is the AWS secret-key length. Keeping the shape check length-exact
# (rather than ">=40") is what lets the structural gates below cleanly separate
# real keys from 64-char sha256 hex, base64 document blobs, etc.
_SECRET_KEY_LEN = 40

# Shannon-entropy floor (bits/char). A uniformly-random 40-char base64 string
# averages ~4.78 bits/char and empirically almost never drops below ~4.4;
# English-word identifiers, hex digests, and repeated/low-alphabet runs sit
# below this. 4.3 is a conservative floor that admits real keys (the canonical
# AWS example scores 4.66) while rejecting camelCase code identifiers and file
# paths, which cluster around 4.0-4.3.
_SECRET_ENTROPY_MIN = 4.3

# Even after the entropy floor, camelCase / PascalCase code identifiers and
# slash-delimited file paths (e.g. src/main/java/com/Example/FooBarBazClas1) can
# survive on entropy ALONE. Two structural signals separate a random secret from
# a word-based identifier or path: (a) a random key almost never contains a long
# unbroken lowercase run, whereas identifiers/paths are built from dictionary
# words that do; (b) a random key has a low vowel ratio, whereas English words
# do not. NOTE: unlike a naive design we deliberately do NOT treat the presence
# of '/' or '+' as a free pass to redact — 40-char mixed-case file paths contain
# '/' yet are benign, so a '/' token must still clear both structural gates.
# Neither gate can speak for a path LONGER than one window, though: its straddling
# sub-windows are built from fragments of several components and clear both, which
# is what `_SECRET_MAX_SLASHES` below is for.
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

# Ceiling on the separators a window CUT OUT OF A LONGER RUN may hold, applied by
# :func:`_contains_bare_secret`. A window straddling several path components is a
# token nobody wrote, and it clears both gates above; its separator density is
# what gives it away -- `/` is 1 base64 character in 64, so a real 40-char key
# averages 0.6 of them, while a window spanning components carries one per
# component. Measured on 200,000 uniformly random 40-char keys: this declines
# 0.36% on its own, against 9.29% for the lowercase-run gate and 6.60% for the
# vowel-ratio gate. Three is the knee: four leaves the reported paths redacted.
_SECRET_MAX_SLASHES = 3

# A token that base64-decodes to >=85% printable ASCII is encoded *text*, not a
# random key (random 40-char keys decode to mostly non-printable bytes). Such a
# token is left to the existing base64 decode-and-scan path in redact_credentials
# so we do not double-count or mis-classify it here.
_SECRET_PRINTABLE_DECODE_RATIO = 0.85

_VOWELS: frozenset[str] = frozenset("aeiouAEIOU")

# All-hex runs are git SHAs (40 hex), sha256 (64 hex), md5 (32 hex), etc. — never
# an AWS secret key (which uses the full base64 alphabet). Reject them outright.
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

# The Shannon term ``(c / _SECRET_KEY_LEN) * log2(c / _SECRET_KEY_LEN)``, indexed by
# the character count ``c``. Element 0 is a ``0.0`` placeholder that keeps ``c``
# usable as a direct index; it is never read, because a count of zero cannot appear
# in a :class:`~collections.Counter` built from an iterable, and ``log2(0)`` would
# raise.
#
# Built for ONE length rather than parameterised over lengths, because
# :func:`_looks_like_secret_key` reaches the entropy gate only through its
# exactly-``_SECRET_KEY_LEN`` check, so that is the only length any production call
# can ask about. A per-length table would need a size cap and an eviction policy to
# bound what an arbitrary caller could materialise -- machinery guarding a caller
# that does not exist. Any other length falls through to the inline formula, which
# is what this table was derived from, so the general path is exactly as it was
# before the table existed.
#
# The terms are computed with the same operations the inline expression used, which
# is what makes this a pure precomputation rather than a re-derivation.
_ENTROPY_TERMS_KEY_LEN: tuple[float, ...] = (0.0,) + tuple(
    (c / _SECRET_KEY_LEN) * math.log2(c / _SECRET_KEY_LEN) for c in range(1, _SECRET_KEY_LEN + 1)
)


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character.

    The result is compared against :data:`_SECRET_ENTROPY_MIN` by
    :func:`_looks_like_secret_key`, so this is a gate on a redaction verdict and
    NOT a statistic anybody displays. A one-ULP drift at the boundary flips that
    verdict, and a flip in the permissive direction leaks a credential. The
    optimisation below is therefore built to be BIT-IDENTICAL, not merely close,
    and is pinned that way by ``TestShannonEntropyIsBitIdentical``.

    Each addend is ``(c / length) * log2(c / length)``. The sole production caller
    reaches this only through the exactly-``_SECRET_KEY_LEN`` check in
    :func:`_looks_like_secret_key`, and reaches it over and over --
    :func:`_contains_bare_secret` slides a 40-char window byte by byte across each
    base64-alphabet run that clears its prefilters -- so at that one length every
    addend is drawn from the fixed set :data:`_ENTROPY_TERMS_KEY_LEN` holds. That
    retires TWO true divisions and one ``math.log2`` call per DISTINCT CHARACTER per
    call -- ``c / length`` appears twice in the expression and CPython evaluates it
    twice, and a 40-char base64 window holds ~30 distinct characters -- plus the
    generator frames, in favour of a C-level ``map`` over a tuple index.

    Any other length takes the inline formula, unchanged from before the table
    existed. That keeps the fast path to the single length that is actually asked
    for, so no size cap or cache-eviction policy is needed to bound what an
    arbitrary caller could make this allocate.

    Why this is bit-identical rather than approximately equal:

    * Each addend is produced by the same three IEEE-754 operations on the same
      operands as before -- divide, ``log2``, multiply -- so each addend carries
      the same bit pattern. Precomputation changes WHEN a term is computed, never
      HOW.
    * ``Counter(token).values()`` still supplies the addends, in the same
      first-occurrence order, and ``map`` is consumed in order, so ``sum``
      accumulates identical addends in an identical sequence. The equality
      therefore does not rest on float addition being associative, which it is
      not. An algebraic rearrangement such as
      ``log2(length) - sum(c * log2(c)) / length`` IS mathematically equal and is
      measurably NOT bit-equal, which is why it is not used here.
    """
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    if length != _SECRET_KEY_LEN:
        return -sum((c / length) * math.log2(c / length) for c in counts.values())
    return -sum(map(_ENTROPY_TERMS_KEY_LEN.__getitem__, counts.values()))


def _has_all_three_char_classes(text: str) -> bool:
    """Return True if *text* holds at least one lowercase, uppercase AND digit.

    One pass with early exit, rather than three ``any()`` scans. Semantically
    identical, but this is the hottest predicate in the redaction path:
    :func:`_contains_bare_secret` slides a 40-char window BYTE BY BYTE across a
    base64-alphabet run that clears its prefilters, so a 512-char run reaching that
    loop asks this question 473 times. Three ``any()`` scans build three generators
    per call and cost the SUM of their three first-match offsets; one loop breaks on
    completion and costs the MAX. Both forms short-circuit, so the saving is
    generator frames plus that sum-vs-max difference.

    Absence of a class is closed under substring, which is what lets
    :func:`_contains_bare_secret` ask this about a whole run and retire every
    window at once.
    """
    has_lower = has_upper = has_digit = False
    for ch in text:
        if not has_lower and ch.islower():
            has_lower = True
        elif not has_upper and ch.isupper():
            has_upper = True
        elif not has_digit and ch.isdigit():
            has_digit = True
        if has_lower and has_upper and has_digit:
            return True
    return False


# The byte set counted as "printable" by :func:`_decodes_to_printable_text`: tab,
# LF, CR and the printable ASCII range 0x20-0x7E. Held as ``bytes`` so the count
# can be delegated to ``bytes.translate``, which runs in C.
_PRINTABLE_BYTES: bytes = bytes(sorted({0x09, 0x0A, 0x0D} | set(range(0x20, 0x7F))))


def _decodes_to_printable_text(token: str) -> bool:
    """Return True if *token* base64-decodes to mostly-printable ASCII.

    Encoded human-readable text (a base64 document blob) decodes to printable
    bytes; a random 40-char secret key decodes to mostly non-printable bytes. We
    use this to exclude encoded-text blobs from the bare-secret heuristic (they
    are handled by the existing decode-and-scan pass instead).
    """
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    # Count the printable bytes by DELETING them in C and measuring what is left,
    # rather than testing every byte in a Python loop. ``translate(None, set)``
    # returns exactly the bytes NOT in *set*, so ``len(raw) - len(...)`` is the
    # member count -- an integer identity, so the ratio and the comparison below
    # are bit-identical to the previous per-byte sum (asserted against a verbatim
    # copy of that sum in ``test_printable_count_matches_the_per_byte_sum``,
    # including all 256 single-byte inputs exhaustively).
    #
    # This is the single most expensive operation in pass 3, because the helper
    # runs once per base64-alphabet run AND again per 40-char window as gate 7 of
    # `_looks_like_secret_key`, and the old loop cost scaled with the DECODED byte
    # count rather than with the 40-char window. Measured 14.6x at 48 bytes rising
    # to 69x at 1500; a 2 KB encoded blob fell from 86.3 us to 1.2 us, which is
    # 98% of what `_contains_bare_secret` spent on such a run.
    printable = len(raw) - len(raw.translate(None, _PRINTABLE_BYTES))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _lowercase_run_exceeds(token: str, cap: int) -> bool:
    """Return True if any run of consecutive lowercase letters is longer than *cap*.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.

    The only question the caller asks is whether the longest run EXCEEDS a
    threshold, so this stops at cap+1 rather than scanning the whole token to
    find the true maximum. On the tokens this gate exists to reject -- the ones
    with a long lowercase run -- it exits after a handful of characters instead
    of all 40, which measured 3.97 -> 1.65 us per window.
    """
    current = 0
    for ch in token:
        if ch.islower():
            current += 1
            if current > cap:
                return True
        else:
            current = 0
    return False


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels.

    Deliberately left in this two-pass comprehension form. A single-pass rewrite
    measured 1.18x -- about 0.4 us on a 2.89 us gate -- which does not justify
    replacing the clearest possible expression of "fraction of letters that are
    vowels", and would owe its own independent-oracle test. Its neighbour
    :func:`_lowercase_run_exceeds` WAS rewritten because that one measured 2.4x.
    Do not optimise this unmeasured.
    """
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret.
    Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output).

    Gates are ordered by MEASURED cost per rejection, cheapest-per-reject first.
    Every gate is a pure predicate whose failure returns False, so the order is
    verdict-neutral and can be chosen purely for cost. Measured on a corpus of
    1705 windows that clear gates 1-3 (cost per window, share of windows that
    gate rejects on its own):

        lowercase run   1.65 us   66.5%  ->  2.5 us per rejection
        vowel ratio     2.89 us   62.3%  ->  4.6 us per rejection
        entropy         8.48 us   54.5%  -> 15.5 us per rejection
        decode          3.01 us    0.0%  ->  rejected nothing in that corpus

    These numbers are a SNAPSHOT from one corpus on one machine: treat them as a
    relative ranking, not a budget, and do not turn them into assertions (this
    repo's CI enables coverage on 3.12 only, so absolute durations are not
    comparable across shards). The ordering is the durable claim, and it is
    guarded by a test that counts which gates get evaluated -- see
    ``TestSecretGateOrderIsCostOrdered``.

    Putting the two cheap structural gates ahead of the entropy computation, and
    the decode check last, halves the cost of gates 4-7 and measured -47% on
    ``redact_credentials`` end to end. Do not reorder these back into
    "structural last" without re-measuring: the structural gates are both
    cheaper AND higher-yield than entropy, which is the opposite of the
    intuition that entropy is the primary discriminator.

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. No lowercase run longer than _SECRET_MAX_LOWER_RUN.
    5. Vowel ratio <= _SECRET_MAX_VOWEL_RATIO. Gates 4 and 5 are the
       structural-randomness pair: they separate a random key from word-based
       identifiers and slash-delimited file paths that survive the entropy
       floor. Both apply to EVERY token (a '/' or '+' does not exempt a token,
       so 40-char mixed-case file paths stay intact).
    6. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    7. Does not base64-decode to printable text (rejects encoded-text blobs).
       Last because it is the lowest-yield gate, not because it is optional --
       it is what keeps legitimate OAuth ``code_challenge`` values in sign-in
       URLs from being redacted (guarded by the OAuth-URL corpus).

    BOUNDARY ASSUMPTION: this classifier deliberately evaluates an EXACTLY-40-char
    window (gate 1). It does NOT itself scan longer runs — a real key glued to an
    adjacent base64 char with no delimiter (e.g. ``X`` + key, key + ``A``,
    ``SECRET=`` + key + ``ABC``, key + ``X`` + key) forms a 41+ char run that would
    fail the exact-40 gate and leak verbatim. Callers that receive raw ``{40,}``
    runs MUST use :func:`_contains_bare_secret`, which slides a 40-char window
    across the run so a glued secret is still caught. Keep the exact-40 shape here:
    it is what lets the structural gates cleanly separate real keys from 64-char
    sha256 hex, base64 document blobs, etc.
    """
    if len(token) != _SECRET_KEY_LEN:
        return False
    if not _has_all_three_char_classes(token):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _lowercase_run_exceeds(token, _SECRET_MAX_LOWER_RUN):
        return False
    if _vowel_ratio(token) > _SECRET_MAX_VOWEL_RATIO:
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    return not _decodes_to_printable_text(token)


def _contains_bare_secret(run: str) -> bool:
    """Return True if any 40-char window of *run* looks like a bare secret key.

    :func:`_looks_like_secret_key` only accepts an EXACTLY-40-char token, but the
    ``_BARE_SECRET_RUN_RE`` boundary look-arounds capture the longest possible run
    of base64-alphabet chars. A genuine 40-char secret glued to an adjacent
    base64 char with no delimiter (``X`` + key, key + ``A``, ``SECRET=`` + key +
    ``ABC``, key + ``X`` + key) produces a 41+ char run that would fail the
    exact-40 gate and leak verbatim. We slide a 40-char window across the run and
    report a hit if ANY window clears every gate. This stays linear in the run
    length (the regex yields disjoint spans), so cost is bounded overall.

    ENCODED-TEXT-BLOB EXCLUSION: if the WHOLE run base64-decodes to printable
    text it is a cohesive encoded blob (e.g. an OAuth/PKCE ``code_challenge``,
    which is ``base64(sha256-hex)``), not a bare secret — those are handled by
    the decode-and-scan pass instead. We must skip it here because sliding a
    40-char window byte-by-byte across such a blob creates base64-*misaligned*
    sub-windows whose garbage decode looks high-entropy and would clear every
    per-window gate, wrongly redacting a legitimate sign-in URL (regression
    guarded by the OAuth-URL corpus). This is the same bias-toward-not-redacting
    that :func:`_looks_like_secret_key` already applies per-window (gate 7),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.

    SEPARATOR CEILING FOR A FRAGMENT. ``/`` is in the run alphabet, so a deep
    absolute path is one run whose straddling sub-windows clear every per-window
    gate. A key-shaped window carrying a path's separator density
    (``_SECRET_MAX_SLASHES``) is declined, on two conditions that keep this a gate
    rather than a hole: only when the run is LONGER than one whole key (a
    40-char run IS the token somebody wrote, so a standalone key is never subject
    to it), and only AFTER :func:`_looks_like_secret_key` has answered, so every
    offset is still classified and a glued key is still found at its own offset.
    """
    if len(run) < _SECRET_KEY_LEN:
        return False
    # RUN-LEVEL FAST PATH. Two of the per-window gates reject on a property that
    # is closed under substring, so asking about the whole run once can retire
    # every window without classifying any of them:
    #   gate 2 -- a character class absent from the run is absent from all of its
    #             substrings, so no window can hold all three;
    #   gate 3 -- every substring of an all-hex run is itself all-hex.
    # Both answers are False either way, so this only reorders WHICH check
    # returns False, never the verdict. Guarded on a run longer than one window,
    # because at exactly 40 chars the sole window pays the same two gates anyway
    # and the pre-check would be pure duplicate work. This is what keeps the
    # slide affordable on long non-secret runs (hex digests, lowercase blobs),
    # which are the common shape in tool output.
    is_fragment = len(run) > _SECRET_KEY_LEN
    if is_fragment:
        if not _has_all_three_char_classes(run):
            return False
        if _HEX_ONLY_RE.match(run):
            return False
    if _decodes_to_printable_text(run):
        return False
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        window = run[start : start + _SECRET_KEY_LEN]
        if not _looks_like_secret_key(window):
            continue
        if is_fragment and window.count("/") > _SECRET_MAX_SLASHES:
            # Key-shaped, but a fragment carrying a path's separator density.
            continue
        return True
    return False


def _decode_b64_chunk(chunk: str) -> str:
    """Decode ONE `_B64_CHUNK_RE` match; return decoded credential text or ''.

    Equivalent to `_decode_b64_safe(chunk)` when *chunk* is itself a
    `_B64_CHUNK_RE` match, but without re-scanning it. `_decode_b64_safe` exists
    to find chunks inside arbitrary text; re-running that scan over a string that
    IS already one chunk can only rediscover the same single span —
    `[A-Za-z0-9+/]{40,}` is greedy so it consumes the whole run, and `={0,2}`
    takes the padding — so the inner `finditer` was pure duplicate work on the
    hot path, once per base64-looking run in every redacted message.
    """
    # NO LENGTH SHORT-CIRCUIT HERE, deliberately. It is tempting to skip the decode
    # when `len(chunk) % 4` is non-zero, on the reasoning that `validate=True`
    # rejects a length that is not a multiple of 4. That reasoning is INTERPRETER
    # DEPENDENT and would be a redaction bypass: `binascii.a2b_base64`'s padding
    # leniency changed with `strict_mode`, so on Python 3.10 and 3.11 a chunk of 40
    # data characters plus one `=` (length 41) DECODES, while on 3.12 it raises.
    # Skipping it would leave a base64-encoded credential in that shape unredacted
    # on exactly the interpreters CI still builds. No version-invariant form of the
    # test exists either -- 43 data characters plus `==` decodes on 3.10 while
    # failing both a total-length and a stripped-length predicate. Pinned by
    # `test_a_decode_length_precondition_would_be_version_dependent`.
    try:
        decoded = base64.b64decode(chunk, validate=True).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Gate the alternation behind the cheap superset pre-filter, exactly as pass 1
    # does. `_might_contain_credential` may return True where the pattern would not
    # match but never False where it would, so the verdict cannot move -- only the
    # cost. Real decoded blobs almost never look like credentials: 0 of 18 in the
    # session corpus and 1 of 849 in a hash-heavy corpus reach the alternation.
    #
    # LENGTH-GATED, because here the pre-filter is NOT unconditionally cheaper. Its
    # ~540 ns floor is fixed while the alternation's cost scales with length, so
    # below `_PREFILTER_MIN_LEN` the alternation wins outright. A decoded blob is
    # exactly the size where that matters -- 48 raw bytes from a 64-char run, and
    # shorter once `errors="ignore"` drops invalid sequences, measured 12-31
    # characters -- so this straddles the crossover instead of sitting above it.
    if len(decoded) >= _PREFILTER_MIN_LEN and not _might_contain_credential(decoded):
        return ""
    return decoded if _CREDENTIAL_PATTERNS.search(decoded) else ""


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''.

    Deliberately left UNOPTIMISED. `_decode_b64_chunk` above is the hot-path
    single-chunk form, and this function is what pins it: the differential test
    asserts the two agree on every chunk in the corpus, and the pre-optimisation
    reference oracle calls this one. Applying the same gates here would make both
    sides of that comparison share the change and the check would stop detecting
    anything.
    """
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _CREDENTIAL_PATTERNS.search(decoded):
                return decoded
        except Exception:
            continue
    return ""


def _contains_fixed_credential(text: str) -> bool:
    """Return True for canonical literal or base64-encoded credentials.

    Deliberately excludes the bare 40-character entropy heuristic. OAuth
    front-channel state and PKCE values are high-entropy by design, while the
    canonical signatures and decoded credentials remain unambiguous.
    """
    return bool(_CREDENTIAL_PATTERNS.search(text) or _decode_b64_safe(text))


def _text_contains_bare_secret(text: str) -> bool:
    """Return True when *text* contains an isolated bare AWS-secret run."""
    return any(_contains_bare_secret(match.group()) for match in _BARE_SECRET_RUN_RE.finditer(text))


# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
_REDACTED_CREDENTIAL_TAG = "[REDACTED: credential]"

# Public alias for modules that must emit the SAME tag rather than duplicate the
# literal — e.g. the pptx-maker preview, which excises a credential-bearing bitmap
# itself because this module's redactor recognises a narrower token set than that
# scan matches.
REDACTED_CREDENTIAL_TAG = _REDACTED_CREDENTIAL_TAG

#: Replacement tag for pass 2 (a base64-encoded credential). DISTINCT from
#: ``_REDACTED_CREDENTIAL_TAG`` and deliberately not a superstring of it, so a
#: consumer counting one tag does not accidentally match the other. Kept PRIVATE:
#: consumers should ask ``CREDENTIAL_REDACTION_TAGS`` below rather than name
#: individual tags, which is the whole point of that registry.
_REDACTED_ENCODED_CREDENTIAL_TAG = "[REDACTED: encoded credential]"

#: EVERY tag :func:`redact_credentials` can substitute for a credential, owned
#: HERE beside the passes that emit them rather than enumerated by each caller.
#: A consumer that needs to answer "did the CREDENTIAL redactor replace something
#: in this text" must check all of them: pass 1 (plaintext patterns) and pass 3
#: (bare secret runs) write ``_REDACTED_CREDENTIAL_TAG``, pass 2 (base64-encoded)
#: writes ``_REDACTED_ENCODED_CREDENTIAL_TAG``.
#:
#: Scope is deliberately CREDENTIALS ONLY, and a consumer must not read it as "was
#: this text rewritten at all". :func:`redact_exfiltration_urls` is a separate
#: rewriter that substitutes ``[REDACTED: suspicious URL to <domain>]`` -- a
#: variable string, so it is prefix-matched rather than compared, which is why it
#: is not a member here. Its stable prefix is exported as
#: :data:`kiro_crew.security.exfil.EXFILTRATION_REDACTION_TAG_PREFIX` (beside
#: the rewriter itself), and a consumer that needs the full "was this text
#: rewritten" answer must check that constant by prefix ALONGSIDE this tuple --
#: the dashboard chat notice does exactly that.
#:
#: This tuple exists so the enumeration lives beside the tags instead of at the
#: call site, where it silently misses a tag and under-reports redactions on the
#: dashboard chat notice. Co-locating it means a NEW tag is added next to the list
#: that must name it; ``test_every_redaction_tag_constant_is_registered`` fails if
#: one is added without registering it, so the drift cannot happen silently.
#:
#: Invariant relied on by callers that SUM per-tag counts: no tag is a substring
#: of another, so one substitution cannot be counted twice.
CREDENTIAL_REDACTION_TAGS = (_REDACTED_CREDENTIAL_TAG, _REDACTED_ENCODED_CREDENTIAL_TAG)


# ── `?token=` / `&token=` URL parameter values (pass 4) ──
# Keyed on the parameter NAME, not the value's shape, so an OPAQUE bearer value
# -- one that looks nothing like a JWT -- is redacted where every shape-based
# pattern above sees ordinary text. A parameter name is a context: `?token=`
# cannot match a filename, an identifier or a sourcemap name, so this adds
# coverage without inheriting shape-based false positives (the five measured
# `eyJ…` lookalikes recorded on the two-segment link-token alternative).
#
# Group 1 is the VALUE, and only the value is replaced: `token=` stays visible
# so a redacted URL still reads as a token URL. The precedent is
# `instances/token_mint._TOKEN_RE` (`[?&]token=([^\s&]+)`), which one caller
# kept privately because this module lacked the pass; the value class here is
# WIDER-terminated per the issue's requirement -- it also stops at quotes and
# `#` so a match cannot run past the parameter into a quoted string or a URL
# fragment -- and additionally excludes the RFC 3986-forbidden bytes
# (`<>{}|\^` and backtick): no legal URL query can carry them, while SOURCE
# and DOC text quoting a token URL does (`?token={token}` in an f-string,
# `` ?token=` `` in markdown, `?token=<your-token>` in prose). Without the
# exclusion, pass 4 matches the template placeholder and the chip-diff path
# (`chat_runner.py`) redacts a snapshot of `dashboard/urls.py` IN PLACE with
# no recovery -- the exact non-cosmetic false-positive surface this module
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
#
# Deliberately NOT a `_CREDENTIAL_PATTERNS` branch, for two reasons. A branch
# replaces its WHOLE span, which would swallow the `token=` anchor this pass
# exists to keep visible. And `_contains_fixed_credential` -- which gates
# request-BLOCKING decisions in `exfil.py` -- searches `_CREDENTIAL_PATTERNS`,
# so a branch would turn every `?token=` URL into a blocked request: a
# behaviour change the issue explicitly excludes. This pass redacts output
# only; the blocking surface is unchanged. The other credential-bearing
# parameter names (`access_token`, `id_token`, `api_key`, `code`) are excluded
# on the issue's own scoping ground -- each name wants its own false-positive
# analysis (`code=` especially collides with OAuth authorization codes AND
# ordinary prose) -- not because adding them HERE would change the blocking
# surface; a pass-4 name never feeds `_contains_fixed_credential`.
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
_TOKEN_PARAM_NAME_PREFIX_RE = (
    "(?:"
    + "|".join(
        "".join(c for c, _ in _TOKEN_PARAM_NAME_SPELLINGS[:k]) + _TOKEN_PARAM_NAME_SPELLINGS[k][1]
        for k in range(len(_TOKEN_PARAM_NAME_SPELLINGS))
    )
    + ")"
)
_TOKEN_PARAM_RE = re.compile(
    rf"{_TOKEN_PARAM_SEP_RE}(?ai:{_TOKEN_PARAM_NAME_RE}){_TOKEN_PARAM_EQ_RE}({_TOKEN_PARAM_VALUE_CLASS}+)"
)

# The in-progress form of the same anchor, for `StreamRedactor.feed`'s
# credential-anchored holdback escalation (mirrors `_BEARER_ANCHOR_PARTIAL_RE`).
# `?` `&` `=` are all in `_CRED_CLASS`, so a token URL is one withheld run --
# but a run longer than the 512-char DoS floor with NO recognised credential
# anchor is BISECTED, and for a >=512-char opaque value the bisection point
# lands inside the value: the committed prefix carries the `token=` anchor
# (and is redacted), while the tail reaches `flush()` anchor-less and streams
# raw. Recognising the trailing partial escalates the tail to the 4096
# ceiling and the fail-closed drop past it, exactly like a Bearer token.
#
# Every alternative below is one possible end-of-chunk prefix. For a percent
# spelling, `%(?:[57]4?)?` (and its siblings) includes the bare `%`, the first
# hex nibble, and the complete escape. The surrounding scoped `(?ai:...)` folds
# both literal letters and hex letters without admitting Unicode lookalikes.
# The same generated entity composition supplies complete-or-partial spellings
# at every letter boundary.
# `*` (not `+`): a buffer ending at a complete separator/name/equals spelling is
# already an in-progress value match. A mid-entity tail needs no extra
# alternative: every byte of `&amp` / `&#x2` belongs to `_CRED_CLASS`, while the
# terminating `;` does not. The explicit entity-only alternative holds the
# completed spelling at buffer end before that semicolon can release it.
_TOKEN_PARAM_PARTIAL_RE = re.compile(
    rf"(?:{_TOKEN_PARAM_SEP_RE}(?:(?ai:{_TOKEN_PARAM_NAME_PREFIX_RE})"
    rf"|(?ai:{_TOKEN_PARAM_NAME_RE})(?P<eq>{_TOKEN_PARAM_EQ_RE}){_TOKEN_PARAM_VALUE_CLASS}*)"
    rf"|{_TOKEN_PARAM_SEP_ENTITY_RE})\Z"
)


#: One redaction the batch redactor has decided on, positioned against the
#: ORIGINAL text: ``(start, end, replacement)``. Every pass produces these and
#: nothing is written until every pass has spoken.
_RedactionSpan = tuple[int, int, str]


def _span_end(span: _RedactionSpan) -> int:
    return span[1]


def _uncovered(start: int, end: int, taken: list[_RedactionSpan]) -> list[tuple[int, int]]:
    """Return the parts of ``[start, end)`` that no span in *taken* covers.

    *taken* must be sorted and pairwise disjoint, which is how
    :func:`redact_credentials` builds it, so the spans are ordered by ``end`` as
    well as by ``start`` and one bisect on ``end`` lands on the first span that
    can still reach into ``[start, end)``. From there a forward walk over the
    spans that begin before ``end`` yields each gap between them.
    """
    gaps: list[tuple[int, int]] = []
    cursor = start
    i = bisect.bisect_right(taken, start, key=_span_end)
    while i < len(taken) and taken[i][0] < end:
        if taken[i][0] > cursor:
            gaps.append((cursor, taken[i][0]))
        cursor = max(cursor, taken[i][1])
        i += 1
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def _splice(text: str, spans: list[_RedactionSpan]) -> str:
    """Apply *spans* (sorted, disjoint) to *text* in one left-to-right pass."""
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in spans:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).

    Every pass positions its redactions as spans against the IMMUTABLE input,
    and the string is rewritten exactly once at the end. Redacting by matched
    VALUE (``result.replace(matched, tag, 1)``) rewrites the first textual
    occurrence of the value, which is not necessarily the span that matched:
    when a later match also occurs as a substring of an earlier, longer run
    that is NOT itself redacted, the tag lands inside the innocent host and the
    real standalone credential survives in plaintext. Splicing by span makes
    that unreachable in all three passes.

    Passes are ranked: pass 1 outranks pass 2 outranks pass 3 outranks pass 4.
    A later pass's span never rewrites text an earlier pass already claimed; it
    redacts only the part of its span still standing in plaintext, so no
    character is redacted twice and no character a pass flagged is left behind.
    """
    warnings: list[str] = []

    # 1. Plaintext credential patterns.
    #
    # Gated on the cheap superset pre-filter: when no branch of
    # `_CREDENTIAL_PATTERNS` can possibly match, `finditer` would yield nothing
    # and the loop body would not run, so skipping it cannot change the output.
    # This is the hot path — the alternation is 23 branches retried at nearly
    # every position, and real text almost never contains a credential.
    #
    # `taken` is every span an earlier pass has claimed, kept sorted and
    # disjoint; it is what the later passes subtract from.
    taken: list[_RedactionSpan] = []
    if _might_contain_credential(text):
        for m in _CREDENTIAL_PATTERNS.finditer(text):
            # Emit ONLY non-sensitive metadata (length). Do NOT slice any part of
            # the match into the warning: `_CREDENTIAL_PATTERNS` matches the raw
            # secret value itself (e.g. `ghp_…`, `sk-ant-…`), so even a short prefix
            # is genuine plaintext key material — a fixed-length token prefix leaves
            # ~12-16 secret chars in a 20-char slice. The warnings list is a
            # redaction-subsystem output expected to be safe to log/surface, so it
            # must carry no secret bytes. The base64 / bare-secret passes below
            # likewise log length only.
            warnings.append(f"Redacted credential pattern ({len(m.group())} chars)")
            taken.append((m.start(), m.end(), _REDACTED_CREDENTIAL_TAG))

    # Passes 2 and 3 both scan the ORIGINAL `text` for runs of the base64
    # alphabet, and they select the SAME spans: `[A-Za-z0-9+/]{40,}` is greedy and
    # leftmost, so it yields exactly the maximal runs of length >= 40 — which is
    # also precisely what `_BARE_SECRET_RUN_RE`'s `(?<![A-Za-z0-9+/])` /
    # `(?![A-Za-z0-9+/])` boundaries select. The only difference is the trailing
    # `={0,2}` padding that `_B64_CHUNK_RE` additionally consumes, and `=` is not
    # in the run's character class, so `rstrip("=")` recovers the bare run
    # exactly. So one scan feeds both passes instead of two.
    #
    # The two loops stay SEPARATE and in their original order: `warnings` is a
    # contract (all pass-2 warnings precede all pass-3 warnings), and pass 3
    # subtracts every pass-2 claim, so pass 2 must have finished first.
    b64_matches = list(_B64_CHUNK_RE.finditer(text))

    # 2. Base64-encoded credentials.
    #
    # The warning is emitted for every chunk that decodes to a credential, even
    # one that pass 1 already claimed in full: the encoded credential IS
    # redacted, and the warning counts credentials found, not splices made.
    pass2: list[_RedactionSpan] = []
    for m in b64_matches:
        chunk = m.group()
        if not _decode_b64_chunk(chunk):
            continue
        warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")
        for start, end in _uncovered(m.start(), m.end(), taken):
            pass2.append((start, end, _REDACTED_ENCODED_CREDENTIAL_TAG))
    # Pass-2 chunks are disjoint from each other and were cut around `taken`,
    # so the union is disjoint and a sort restores the order.
    taken = sorted(taken + pass2)

    # 3. BARE 40-char AWS secret keys with no label/prefix. These carry no
    # distinctive marker for _CREDENTIAL_PATTERNS to anchor on, so an entropy +
    # structural heuristic is the only way to catch a standalone secret value.
    #
    # A run an earlier pass has claimed in full (it was a labelled value, or an
    # encoded-credential chunk) is skipped WITHOUT a warning. The check is
    # positional: a second occurrence of the same run elsewhere in the text is
    # judged on its own span, never on whether the value still appears
    # somewhere. A run only PARTLY claimed — a glued key whose tail is the first
    # word of a `aws_secret_access_key=` label, say — has the part still in
    # plaintext redacted, because the run as a whole was judged to hold a key
    # and the earlier pass consumed only its label.
    pass3: list[_RedactionSpan] = []
    for m in b64_matches:
        run = m.group().rstrip("=")
        # Slide a 40-char window across the run rather than gating the whole run
        # on len == 40: a real secret glued to an adjacent base64 char (no
        # delimiter) yields a 41+ char run that the exact-40 shape check would
        # miss, leaking the key verbatim. Redact the whole run if ANY window is a
        # secret.
        if not _contains_bare_secret(run):
            continue
        gaps = _uncovered(m.start(), m.start() + len(run), taken)
        if not gaps:
            continue
        for start, end in gaps:
            pass3.append((start, end, _REDACTED_CREDENTIAL_TAG))
        warnings.append(f"Redacted bare secret key ({len(run)} chars)")
    taken = sorted(taken + pass3)

    # 4. `?token=` / `&token=` URL parameter VALUES, keyed on the parameter
    # name (see `_TOKEN_PARAM_RE`). Ranked LAST so a value an earlier pass
    # already caught -- an AKIA key, a JWT, a link token -- keeps that pass's
    # tag, warning and span byte-identically; this pass only claims the opaque
    # values nothing shape-based can see.
    #
    # UNGATED, deliberately: the name folds case (`(?i:token)`), and a
    # case-insensitive pattern is only safely anchored by the SAME regex
    # engine (see `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` for the
    # `str.lower()` bypass this rule exists to prevent) -- so the cheapest
    # valid gate is a same-engine search whose cost equals the scan it would
    # skip, which is no gate at all. This pass is one two-alternation-free
    # regex, not the 23-branch alternation pass 1's pre-filter exists for.
    #
    # A value that is already one of this module's fixed credential tags is
    # skipped, not re-redacted: several surfaces run the redactor twice (the
    # streaming path re-redacts the persisted copy; `redact_path_segments`
    # requires its candidate to be a fixed point), and the value class stops at
    # a tag's interior space. Matching a canonical tag again would mangle
    # `token=[REDACTED: credential]` into
    # `token=[REDACTED: credential] credential]` on the second run.
    #
    # Trust is BYTE-IDENTITY with a module-owned fixed literal, never a shape.
    # `_TOKEN_PARAM_VALUE_CLASS` admits `[`, `]` and `:`, so a prefix test lets
    # adversary-authored `?token=[REDACTED<secret>` bypass this terminal pass.
    # `CREDENTIAL_REDACTION_TAGS` is the key because it contains ONLY fixed
    # literals. The exfiltration prefix is excluded for exactly that reason:
    # skipping a domain-bounded exfil shape is the same bypass --
    # `?token=[REDACTED: suspicious URL to <secret>.co]` satisfies the domain
    # class while carrying attacker-controlled bytes.
    #
    # ACCEPTED RESIDUAL: a genuine exfil tag value is redacted at its 10-byte
    # `[REDACTED:` head, yielding
    # `?token=[REDACTED: credential] suspicious URL to <domain>]`. That text is
    # already redacted and contains no secret; it is stable on re-redaction
    # because the second pass sees the exact credential literal and skips, and
    # the bare domain tail cannot re-trigger the exfil pass (`_URL_RE` requires
    # a scheme). One notice count moves from exfil to credential.
    pass4: list[_RedactionSpan] = []
    for m in _TOKEN_PARAM_RE.finditer(text):
        value_start, value_end = m.start(1), m.end(1)
        if any(text.startswith(tag, value_start) for tag in CREDENTIAL_REDACTION_TAGS):
            continue
        gaps = _uncovered(value_start, value_end, taken)
        if not gaps:
            continue
        for start, end in gaps:
            pass4.append((start, end, _REDACTED_CREDENTIAL_TAG))
        warnings.append(f"Redacted token parameter value ({value_end - value_start} chars)")

    if not taken and not pass4:
        return text, warnings
    return _splice(text, sorted(taken + pass4)), warnings


# Absolute filesystem paths, POSIX and Windows. Deliberately narrow: anchored to
# real filesystem roots rather than "any slash-separated token", and both branches
# refuse to start mid-token so a URL is never mistaken for a path -- without the
# lookbehinds, ``https://api.github.com/repos/x`` matches twice (``s:/`` as a drive
# letter, ``/repos`` as a root) and the URL is destroyed.
#
# The drive-letter branch accepts BOTH separators: ``C:\`` and ``C:/`` name the
# same file on Windows, and tools that normalise separators (Git Bash, Python's
# pathlib/posixpath, Node, MSYS) routinely print the forward-slash spelling, so
# matching only ``C:\`` left ``C:/Users/<login>/...`` -- the login and host
# layout -- unredacted wherever this shared scrub runs. The forward-slash form
# carries a ``(?!/)`` guard so a one-letter URI scheme (``x://host``) is never
# mistaken for a drive; longer schemes (``https:``) are already refused by the
# ``(?<![A-Za-z])`` lookbehind on the drive letter itself.
_LOCAL_PATH_RE = re.compile(
    r"(?:"
    r"(?<![\w:/])/(?:local/home|home|Users|root|tmp|var|opt|usr|etc|private|mnt|srv|workspace|workplace)"
    r"|(?<![A-Za-z])[A-Za-z]:(?:\\|/(?!/))"
    r")"
    r"[^\s'\"<>|]*"
)
_LOCAL_PATH_PLACEHOLDER = "[redacted-path]"


def redact_local_paths(text: str) -> tuple[str, list[str]]:
    """Strip absolute host filesystem paths from *text*.

    Complements :func:`redact_credentials`, which matches credential *patterns*
    and leaves a bare path such as
    ``[Errno 2] No such file or directory: '/home/alice/.kiro/crew/vaults/v1'``
    untouched. That string is the common shape of an OS or subprocess error, and
    on an error surface that reaches a browser it discloses the account name and
    on-disk layout of the host (CWE-209).

    Returns the redacted text and a list of human-readable notes, matching the
    signature of the sibling passes so callers can chain them uniformly.
    """
    notes: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        notes.append(f"Redacted local path ({len(match.group(0))} chars)")
        return _LOCAL_PATH_PLACEHOLDER

    return _LOCAL_PATH_RE.sub(_sub, text), notes


#: A random key generated once per gateway process and held only in memory: it
#: is never persisted, logged or exposed. It keys the per-segment label below.
_PATH_LABEL_KEY = secrets.token_bytes(32)

#: Joins a redacted path segment to the label :func:`redact_path_segments`
#: appends. Outside the credential alphabet, so the label can never be glued
#: onto a neighbouring run and read as part of one; and not a path separator, so
#: the segment count is kept.
_PATH_SEGMENT_DISCRIMINATOR_SEP = "~"


def _path_segment_label(segment: str) -> str:
    """The opaque, process-stable label for a redacted path *segment*.

    ``HMAC-SHA256(_PATH_LABEL_KEY, segment)``, truncated to 12 hex digits (48
    bits). Equal segments carry equal labels for the life of this process, and
    that equality is the point: the dashboard joins the project-tree response
    with the git-status response by path, so the same original must label the
    same way in both. Distinct segments carry distinct labels except with
    negligible probability (48 bits over the handful of collisions one tree can
    hold). The digest is KEYED: without the key nothing about the segment can be
    checked against the label, so a low-entropy secret behind a redaction tag is
    not exposed to an offline dictionary guess the way an unkeyed hash prefix
    would be. The key is fresh per gateway process, so the label changes across
    a restart; both responses of one join come from one process, so the join
    holds.
    """
    return hmac.new(
        _PATH_LABEL_KEY, segment.encode("utf-8", "surrogatepass"), hashlib.sha256
    ).hexdigest()[:12]


def redact_path_segments(path: str, redactor: Callable[[str], str] | None = None) -> str:
    """Redact a ``/``-separated *path* segment by segment, labelling each
    redacted segment so distinct originals stay distinct.

    The whole-string redactors replace a matched token wherever it sits, so a
    path whose filename is credential-shaped (``AKIA…_model.txt``) keeps its
    directory prefix and its non-secret tail but not the token. Here each segment
    is redacted on its own, so a credential-shaped segment is replaced by the tag
    while every clean segment around it is kept verbatim. Every segment the
    redactor changes is then suffixed with :data:`_PATH_SEGMENT_DISCRIMINATOR_SEP`
    and :func:`_path_segment_label` of its ORIGINAL bytes -- not only on a
    collision, because a single call cannot know what else the listing holds.

    The label is the one shape that meets all five properties the listings need
    at once:

    1. Distinct inputs stay distinct: two different credential-shaped segments
       collapse to the same tag but carry different labels, so a de-duplicating
       listing keeps both.
    2. No byte of the secret is in the output: the tag replaces the token whole
       and the label is a digest, not a substring.
    3. No UNKEYED digest of the secret: the label is an HMAC under a per-process
       random key, so a reader cannot enumerate low-entropy candidates offline
       and match them against the label.
    4. No dependence on listing position or order: the label is a function of
       the segment alone, so a sorted listing does not correlate the label with
       the secret's lexicographic rank, and the same path labels the same way
       whatever else is listed with it.
    5. Stable across responses within one gateway process: the dashboard joins
       the tree response with the git-status response by path, and a
       per-response label breaks that join when only one of two colliding paths
       appears in the status response. A keyed label is the same in every
       response this process serves.

    The key is regenerated when the gateway restarts, so labels differ across
    restarts; both responses of one join come from the same process, so that
    is fine.

    *redactor* is the whole-string redactor to apply -- callers on an egress
    surface pass the context-aware ``redact`` shim so a loaded companion's extra
    patterns apply; the default is the credential pass alone. Whatever it is,
    this function never emits LESS redaction than it would: the segment-wise
    result is returned only when redacting each segment on its own removes
    EXACTLY the bytes the whole-string pass removes (their unlabelled joins are
    equal) and the labelled result is itself a fixed point of the redactor;
    otherwise the whole-string result is returned unchanged. Equality with the
    whole-string pass is the load-bearing check: a token that spans a separator
    (a ``key=value`` whose value carries a ``/``) is matched by the whole pass
    but only up to the separator by the segment pass, and the leftover tail is
    not a match on its own, so a fixed-point check alone would let it through.
    A path the redactor leaves alone is returned as is. Splits on
    :data:`posixpath.sep` only, on every host: the project listings this serves
    emit POSIX-relative paths, not native ones.
    """
    _redact: Callable[[str], str] = redactor or (lambda s: redact_credentials(s)[0])
    whole = _redact(path)
    if whole == path:
        return path
    segments = path.split(posixpath.sep)
    outs = [_redact(segment) for segment in segments]
    # Floor 1: segment-wise redaction must reproduce the whole-string result
    # byte for byte before any label is added. Anything the whole pass removed
    # that a single segment did not is a tail the caller must not see.
    if posixpath.sep.join(outs) != whole:
        return whole
    labelled = [
        (
            out
            if out == segment
            else f"{out}{_PATH_SEGMENT_DISCRIMINATOR_SEP}{_path_segment_label(segment)}"
        )
        for segment, out in zip(segments, outs)
    ]
    candidate = posixpath.sep.join(labelled)
    # Floor 2: the labelled result must itself be a fixed point of the redactor
    # (a shape that only matches in context, or one the labels complete).
    if _redact(candidate) != candidate:
        return whole
    return candidate
