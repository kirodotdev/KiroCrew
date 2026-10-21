"""Credential regex spellings shared by the log-redaction leaf and the scrubber.

It is also the home of the rule tier table (:data:`RULE_TIERS`, at the foot).

``log_redaction`` installs at CLI bootstrap, before heavy modules load, so it
cannot import ``security``. The two AWS-key-ID and JWT spellings were therefore
written out twice -- once in each home -- and parity was held only by tests that
read ``security.py`` as SOURCE TEXT and grepped it for a literal. That guard
fires on a rename, not on a divergence in meaning, and it costs a test per
duplicated pattern.

This module is the single home for those spellings. Both consumers import from
here, so there is nothing left to keep in sync.

Why fragments rather than compiled patterns
-------------------------------------------
Everything below is pattern SOURCE. ``security.py`` embeds the AWS spelling in
five differently shaped patterns -- three plain alternation branches, one
``X-Amz-Credential=`` presigned-URL matcher and one fully anchored ``^...$``
parameter validator -- and each needs its own anchors, continuations and compile
flags. A compiled pattern could not be reused at four of those five sites.

Why this module imports NOTHING
-------------------------------
Not even ``re``, and not even ``from __future__ import annotations`` -- the one
annotation here needs none. ``log_redaction`` is a deliberate import leaf -- it is
installed before the heavy modules load -- so anything it imports is on the
bootstrap path. Exporting plain strings keeps this module's import cost at zero
and makes it impossible for a future edit here to drag a dependency into that
path. ``test_credential_patterns.py`` pins the import-free shape.

The two AWS spellings are DISJOINT
----------------------------------
``AWS_KEY_ID`` (``AKIA`` / ``ASIA``) is an Identifier: it names a key and cannot
sign a request, so no redactor removes it and no gate blocks on it. It stays here
as a structural anchor, because the presigned-URL validators recognise a valid
``X-Amz-Credential`` by it.

``AWS_KEY_ID_REDACTION`` is what the log floor still removes: the ``ABIA`` and
``ACCA`` ids, whose tier is not yet confirmed, so they keep the fail-closed
default. Both are built from the same body fragment.
"""

#: AWS access key-ID prefixes: long-term (``AKIA``) and temporary/STS (``ASIA``)
#: ids. Identifier tier: never redacted.
AWS_KEY_ID_PREFIXES = "AKIA|ASIA"

#: Prefixes the log floor still redacts -- ``ABIA`` (bearer tokens) and ``ACCA``
#: (context-specific credentials) -- until their tier is confirmed.
AWS_KEY_ID_REDACTION_ONLY_PREFIXES = "ABIA|ACCA"

#: The id body: exactly 16 uppercase alphanumerics after the 4-letter prefix.
#: Precise enough for near-zero false positives, and the id is the searchable
#: half of a leaked AWS credential pair.
AWS_KEY_ID_BODY = "[A-Z0-9]{16}"

#: AWS access key ID (``AKIA`` / ``ASIA``): a structural anchor, never a rule.
AWS_KEY_ID = f"(?:{AWS_KEY_ID_PREFIXES}){AWS_KEY_ID_BODY}"

#: The AWS key ids the log floor still redacts (``ABIA`` / ``ACCA``). Disjoint
#: from :data:`AWS_KEY_ID` by construction.
#:
#: No word-boundary assertions: an id rendered adjacent to word characters
#: (``aws_ABIA...``) has no ``\b`` between ``_`` and ``A`` and would slip past a
#: bounded pattern. Over-matching inside a longer token is the fail-closed
#: direction for a redaction floor.
AWS_KEY_ID_REDACTION = f"(?:{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"

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

#: The values that make a presigned or signed link work: a SigV4/SigV2 request
#: signature and the session token a temporary-credential link carries. Bearer
#: link tier -- the value is removed, the key ID beside it (Identifier tier) is
#: not. Keyed on the signing label case-insensitively (HTTP/2 lowercases header
#: names), tolerating a ``=``/``:`` separator or its ``%3D`` percent form, and a
#: value up to a URL or structural delimiter. The longer ``X-Amz-`` names come
#: first so the whole label is consumed rather than the bare ``Signature`` tail.
#: Used by the log floor, the deploy-scan floor and the frontend mirrors, which
#: never run the core redaction passes; the core pass keys the same values on its
#: own pass-4 names and a SigV4 header branch.
URL_SIGNING_VALUE = (
    r"(?:"
    # The AWS-specific signing parameters. Their hyphenated names do not occur in
    # ordinary prose, so a value after them is matched in any position (URL query,
    # JSON, header), tolerating a quote and whitespace around the separator.
    r"""(?i:X-Amz-Signature|X-Amz-Security-Token)["']?\s*[:=]\s*["']?[^\s"'&;#<>)\]}]+"""
    # A bare ``Signature`` is a common English word and a frequent CSS/JS/HTML
    # identifier (``.signature:hover``, ``const signature =``, ``<p>Signature:
    # Jane</p>``), so it is matched ONLY in a URL query position -- right after a
    # ``?`` / ``&`` or their HTML/percent encodings, with no spaces, as a real
    # query parameter is written. An Azure SAS ``sig`` and a GCS V4
    # ``X-Goog-Signature`` sign a link the same way, so they are read the same.
    r"""|(?:[?&]|&amp;|%26)(?i:Signature|sig|X-Goog-Signature)(?:=|%3[Dd])[^\s"'&;#<>)\]}]+"""
    # The SigV4 / SigV4A request signature as an ``Authorization`` header writes
    # it: a hex value right after ``Signature=``. SigV4 is exactly 64 hex; SigV4A
    # is a longer DER-encoded ECDSA signature, so the run is open-ended above the
    # 64-hex floor and bounded by the non-hex look-ahead. Prose ``signature =
    # sign(body)`` never produces a 64-hex run, so this cannot fire on it.
    r"""|(?i:Signature)(?:=|%3[Dd])[0-9a-fA-F]{64,}(?![0-9A-Za-z])"""
    r")"
)


# ── Labelled values ──
# A label names the field a secret sits in. The redactor replaces the VALUE and
# keeps the label, so ``api_key = [REDACTED: credential]`` still parses and the
# reader can see which field went. Labels are matched case-insensitively and
# accept ``_``, ``-``, ``.`` or a space between their words, because the same
# field is spelled ``aws_secret_access_key``, ``AWS_SECRET_ACCESS_KEY``,
# ``SecretAccessKey`` and ``secret-access-key`` by different tools.
#
# These are pattern SOURCE strings, shared by the core redactor and the log
# floor, so both read one spelling.

#: Between a label and its value: an optional closing quote or markdown emphasis,
#: ``=``, ``:``, ``=>``, ``:=``, an XML element's ``>`` or a fullwidth colon or
#: equals sign, with optional whitespace around it (a line break or a no-break
#: space too), and an optional emphasis or opening quote.
#: The blanks on either side of a separator: any run of spaces or tabs, since an
#: aligned INI file or a column layout pads it freely, across at most eight line
#: breaks. Line breaks delimit the runs, so the match stays linear.
SEPARATOR_SPACE = r"(?:[^\S\n]*\n){0,8}[^\S\n]*"

LABEL_SEPARATOR = rf"""["'*_]{{0,3}}{SEPARATOR_SPACE}(?:=>|:=|[:=>\uff1a\uff1d])[*_]{{0,3}}{SEPARATOR_SPACE}["']?"""

#: The AWS secret access key's field name, in any of its spellings.
AWS_SECRET_LABEL = r"(?i:secret[ _.-]?access[ _.-]?key)"

#: The AWS session token's field name, in any of its spellings, including the
#: ``X-Amz-Security-Token`` header and query parameter that carry it.
AWS_SESSION_LABEL = r"(?i:session[ _.-]?token|security[ _.-]?token)"

#: Field names that hold a secret of no particular vendor. A bare ``pass`` or
#: ``token`` is too common a word on its own, so it counts only as the last word
#: of a compound name (``db_pass``, ``SLACK_BOT_TOKEN``).
#: The words of every generic secret label, one tuple per label; the label
#: regex below and the stream's hold on a label still arriving are both built
#: from it, so a label added here is held too.
GENERIC_LABEL_WORDS: tuple[tuple[str, ...], ...] = (
    ("password",),
    ("passwd",),
    ("passphrase",),
    ("pwd",),
    ("client", "secret"),
    ("secret", "key"),
    ("api", "key"),
    ("apikey",),
    ("access", "token"),
    ("auth", "token"),
    ("refresh", "token"),
    ("bearer", "token"),
    ("private", "key"),
    ("signing", "key"),
    ("webhook", "secret"),
    ("secret",),
)

#: The words of the multi-word AWS labels (:data:`AWS_SECRET_LABEL`,
#: :data:`AWS_SESSION_LABEL`).
AWS_LABEL_WORDS: tuple[tuple[str, ...], ...] = (
    ("secret", "access", "key"),
    ("session", "token"),
    ("security", "token"),
)

GENERIC_SECRET_LABEL = (
    "(?i:"
    + "|".join("[ _.-]?".join(words) for words in GENERIC_LABEL_WORDS)
    + "|[_.-]pass|[_.-]token)"
)

#: A labelled value: runs to a blank, a quote or a structural delimiter, so it
#: never swallows the next JSON field or URL query parameter (``&``). ``[`` is
#: excluded, so a value that is already a redaction tag is never matched again.
LABEL_VALUE = r"""[^\s"'`,;&{}()<>\[\]\\]+"""

#: The labels whose value signs a request or a link (Bearer link tier, keyed on
#: the signing part): the value goes in every serialisation, a URL query, a JSON
#: or YAML field, a header.
SIGNING_LABEL = r"(?i:X-Amz-Signature|X-Amz-Security-Token)"

#: A bare ``Signature`` field: a signing label whose value is judged like any
#: labelled value, so prose (``Signature: Jane Doe``) and code stay.
BARE_SIGNATURE_LABEL = r"(?<![A-Za-z0-9-])(?i:signature)"

#: A whole value that is a reference or a mask, not a secret: ``$VAR``,
#: ``${VAR}``, ``$(cmd)``, ``%VAR%``, ``{{ x }}``, ``{name}``, ``<your key>``,
#: ``****``. Used with ``fullmatch``.
REFERENCE_VALUE = (
    r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\([^()]{0,256}\)|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\{\{[^{}]{0,256}\}\}|\{[A-Za-z_][\w.]{0,64}\}|<[^<>]{0,256}>|\*+"
)

#: :data:`LABEL_VALUE`, or nothing right after an opening quote, whatever the
#: quoted value starts with, the other quote too (``"password": ",x;y"``,
#: ``" lead"``, ``"[x"``, ``"'x"``): the
#: caller then reads the quoted value to its closing quote, and treats a value
#: that is already a redaction tag (``[REDACTED...``) as no value.
#: A YAML block-scalar header (``|``, ``>-``) at the line end: the value is the
#: indented body after it, which the readers take from here.
BLOCK_SCALAR_HEADER = r"[|>](?:[-+][0-9]?|[0-9][-+]?)?(?=[ \t]*(?:#[^\n]*)?\n)"

#: Nothing, before a value that starts with a delimiter :data:`LABEL_VALUE`
#: stops at (``DB_PASSWORD=;x``, ``password: [x``): a config line's reader
#: (:func:`credential_values.config_value_end`) extends it to the line end; on
#: any other line the empty value is no value.
DELIMITER_LED_VALUE = r"""(?=[;,&{}()<>\[\]\\`])"""

LABEL_VALUE_OR_QUOTED = (
    rf"""(?:{BLOCK_SCALAR_HEADER}|{LABEL_VALUE}|(?<=")(?=[^"\n])|(?<=')(?=[^'\n])"""
    rf"""|(?<=[A-Za-z]>)(?=[^<\s])|{DELIMITER_LED_VALUE})"""
)

#: An ``Authorization`` header and its scheme. The scheme and the blank after it
#: belong to the label; the credential after them is the value.
AUTHORIZATION_LABEL = (
    rf"""(?i:(?:proxy-)?authorization)["']?{SEPARATOR_SPACE}[:=]{SEPARATOR_SPACE}["']?"""
    r"(?i:bearer|basic|token)[^\S\n]+"
)

#: An ``Authorization`` credential: RFC 6750 ``b64token``, which also covers the
#: base64 of a Basic header. Any non-empty value after the explicit scheme,
#: except a short single-case word (``Bearer token`` in prose names the scheme).
AUTHORIZATION_VALUE = (
    r"(?:[A-Za-z0-9._~+/-]{8,}"
    r"|(?![a-z]{1,7}(?![A-Za-z0-9._~+/=-]))(?![A-Z]{1,7}(?![A-Za-z0-9._~+/=-]))"
    r"[A-Za-z0-9._~+/-]{1,7}(?![A-Za-z0-9._~+/-]))=*"
)


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


# ── Tiers ──
# Every redaction rule carries one tier: what a value it matches can do alone,
# or combined only with public data.
#
# * ``secret``      -- authenticates or signs (secret keys, session tokens,
#                      private keys, vendor API tokens, URI passwords, JWTs).
# * ``bearer_link`` -- a URL that grants access by itself until it expires
#                      (``?token=`` links, presigned and signed links keyed on
#                      their signature, suspicious outbound URLs).
# * ``identifier``  -- names or describes and cannot authenticate (key IDs,
#                      account IDs, ARNs, request IDs, digests).
#
# An identifier is never redacted and never blocks a request, so no redaction
# rule carries that tier: the AWS access key ID (``AKIA`` / ``ASIA``) has no rule.
# ``AWS_KEY_ID`` above stays as a structural anchor for the presigned-URL
# validators. ``ABIA`` / ``ACCA`` ids stay redacted in the log floor until their
# tier is confirmed.

TIER_SECRET = "secret"
TIER_BEARER_LINK = "bearer_link"
TIER_IDENTIFIER = "identifier"

#: The three tiers, in order of what a value can do.
TIERS = (TIER_SECRET, TIER_BEARER_LINK, TIER_IDENTIFIER)

#: Tier of every hand-written rule id a redactor in ``security`` can name, plus
#: the labels of :data:`VENDOR_TOKEN_PATTERNS`. A rule id missing here is a test
#: failure, so a new rule cannot ship without a tier. The generated vendor
#: catalog (``data/secret_catalog.generated.json``) carries a tier per rule in
#: the same three-tier vocabulary; ``security/secret_catalog.py`` reads it.
RULE_TIERS = {
    # Context rules (``security/redaction_labels.py``): labelled values and the
    # secret half of an AWS key pair.
    "aws_secret_access_key": TIER_SECRET,
    "aws_session_token": TIER_SECRET,
    "authorization_header": TIER_SECRET,
    "secret_label": TIER_SECRET,
    "aws_key_pair": TIER_SECRET,
    # Core redactor, pass 1 (fixed patterns).
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
    # Passes 2 and 4.
    "encoded_credential": TIER_SECRET,
    "token_parameter": TIER_BEARER_LINK,
    "request_signature": TIER_BEARER_LINK,
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
