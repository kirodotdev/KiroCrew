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
import math
import re
from collections import Counter

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
    # The floor stays at 2: at 2 the two-segment dashboard token did not
    # match here at all and fell through to the bare-secret entropy pass, whose run
    # class `[A-Za-z0-9+/]` is STANDARD base64 and excludes base64url's `-`/`_`.
    # That made redaction depend on which characters a random HMAC signature
    # happened to contain. That rate is derivable, so it is stated as a closed form
    # rather than as a sample. HMAC-SHA256 is 256 bits and base64url-unpadded gives
    # 43 chars. The first 42 each carry a full 6 bits, so each is uniform over the
    # 64-char alphabet, of which exactly 2 are `-`/`_`. The 43rd carries only the
    # leftover 4 bits (256 - 42*6), and they land in the HIGH bits of its 6-bit
    # group with the low 2 bits zero, so it spans exactly the 16 alphabet indices
    # divisible by 4 (`048AEIMQUYcgkosw`) and can never be `-`/`_`, which sit at
    # 62/63. Hence P(no `-`/`_`) = (62/64)^42 = 26.4%, verified by encoding all
    # 256 possible final digest bytes.
    # So roughly a quarter of tokens had only the signature replaced (leaving the
    # payload claims verbatim in a URL that still looked complete but no longer
    # authenticated), and the other ~74% streamed out entirely unredacted. Matching the whole token here makes
    # the outcome deterministic and replaces it as one unit. The 2-segment token gets
    # its OWN alternative rather than relaxing the segment floor to `{1,4}`. Relaxing
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
# NO LONGER CONSULTED BY `redact_credentials`. Pass 3 derives its runs from
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
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

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
    if len(run) > _SECRET_KEY_LEN:
        if not _has_all_three_char_classes(run):
            return False
        if _HEX_ONLY_RE.match(run):
            return False
    if _decodes_to_printable_text(run):
        return False
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if _looks_like_secret_key(run[start : start + _SECRET_KEY_LEN]):
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
#: the dashboard chat notice does exactly that (issues #6189 and #8132).
#:
#: This tuple exists because the enumeration used to live at the call site, where
#: it silently missed the encoded tag and under-reported redactions on the
#: dashboard chat notice. Co-locating it means a NEW tag is added next to the list
#: that must name it; ``test_every_redaction_tag_constant_is_registered`` fails if
#: one is added without registering it, so the drift cannot recur silently.
#:
#: Invariant relied on by callers that SUM per-tag counts: no tag is a substring
#: of another, so one substitution cannot be counted twice.
CREDENTIAL_REDACTION_TAGS = (_REDACTED_CREDENTIAL_TAG, _REDACTED_ENCODED_CREDENTIAL_TAG)


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings: list[str] = []
    result = text

    # 1. Redact plaintext credential patterns
    #
    # Gated on the cheap superset pre-filter: when no branch of
    # `_CREDENTIAL_PATTERNS` can possibly match, `finditer` would yield nothing
    # and the loop body would not run, so skipping it cannot change the output.
    # This is the hot path — the alternation is 23 branches retried at nearly
    # every position, and real text almost never contains a credential.
    if _might_contain_credential(result):

        def _redact_one(m: re.Match[str]) -> str:
            # Emit ONLY non-sensitive metadata (length). Do NOT slice any part of
            # the match into the warning: `_CREDENTIAL_PATTERNS` matches the raw
            # secret value itself (e.g. `ghp_…`, `sk-ant-…`), so even a short prefix
            # is genuine plaintext key material — a fixed-length token prefix leaves
            # ~12-16 secret chars in a 20-char slice. The warnings list is a
            # redaction-subsystem output expected to be safe to log/surface, so it
            # must carry no secret bytes. Mirrors the base64 / bare-secret branches
            # below, which already log length only.
            warnings.append(f"Redacted credential pattern ({len(m.group())} chars)")
            return _REDACTED_CREDENTIAL_TAG

        # ONE pass. `sub` walks the matches left-to-right exactly as `finditer`
        # did and calls the replacer in that same order, so `warnings` is
        # appended in an identical order with identical contents. The previous
        # shape rebuilt the entire string per match via
        # `result.replace(matched, tag, 1)` — O(n) per match, O(n²) overall on
        # credential-dense text — and replaced the FIRST occurrence of the
        # matched text rather than the span that actually matched. `sub` splices
        # each matched span in place, which is both linear and positionally
        # exact.
        result = _CREDENTIAL_PATTERNS.sub(_redact_one, result)

    # Passes 2 and 3 both scan the ORIGINAL `text` for runs of the base64
    # alphabet, and they select the SAME spans: `[A-Za-z0-9+/]{40,}` is greedy and
    # leftmost, so it yields exactly the maximal runs of length >= 40 — which is
    # also precisely what `_BARE_SECRET_RUN_RE`'s `(?<![A-Za-z0-9+/])` /
    # `(?![A-Za-z0-9+/])` boundaries select. The only difference is the trailing
    # `={0,2}` padding that `_B64_CHUNK_RE` additionally consumes, and `=` is not
    # in the run's character class, so `rstrip("=")` recovers the bare run
    # exactly. So one scan feeds both passes instead of two.
    #
    # The two loops stay SEPARATE and in their original order. Fusing them into a
    # single per-run loop would interleave the passes, which changes both the
    # order of `warnings` and — because each pass mutates `result` via
    # `str.replace(…, 1)` — which occurrence each replacement lands on, and
    # whether pass 3's `run not in result` guard sees pass 2's edits. Sharing the
    # scan while keeping the loops ordered is what makes this byte-identical.
    b64_chunks = [m.group() for m in _B64_CHUNK_RE.finditer(text)]

    # 2. Detect and redact base64-encoded credentials
    for chunk in b64_chunks:
        decoded = _decode_b64_chunk(chunk)
        if decoded:
            result = result.replace(chunk, _REDACTED_ENCODED_CREDENTIAL_TAG, 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    # 3. Detect and redact BARE 40-char AWS secret keys with no label/prefix
    # These carry no distinctive marker for _CREDENTIAL_PATTERNS
    # to anchor on, so an entropy + structural heuristic is the only way to catch
    # a standalone secret value. Scan the ORIGINAL text (not the already-mutated
    # result) so match offsets are stable; skip any run whose text has already
    # been redacted away by an earlier pass.
    for chunk in b64_chunks:
        run = chunk.rstrip("=")
        # Slide a 40-char window across the run rather than gating the whole run
        # on len == 40: a real secret glued to an adjacent base64 char (no
        # delimiter) yields a 41+ char run that the exact-40 shape check would
        # miss, leaking the key verbatim. Redact the whole run if ANY window is a
        # secret.
        if not _contains_bare_secret(run):
            continue
        if run not in result:
            # Already redacted by pass 1/2 (e.g. it was a labelled value or an
            # encoded-credential chunk) — nothing left to replace.
            continue
        result = result.replace(run, _REDACTED_CREDENTIAL_TAG, 1)
        warnings.append(f"Redacted bare secret key ({len(run)} chars)")

    return result, warnings


# Absolute filesystem paths, POSIX and Windows. Deliberately narrow: anchored to
# real filesystem roots rather than "any slash-separated token", and both branches
# refuse to start mid-token so a URL is never mistaken for a path -- without the
# lookbehinds, ``https://api.github.com/repos/x`` matches twice (``s:/`` as a drive
# letter, ``/repos`` as a root) and the URL is destroyed.
_LOCAL_PATH_RE = re.compile(
    r"(?:"
    r"(?<![\w:/])/(?:home|Users|root|tmp|var|opt|usr|etc|private|mnt|srv|workspace|workplace)"
    r"|(?<![A-Za-z])[A-Za-z]:\\"
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
