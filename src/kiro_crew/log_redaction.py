"""Log redaction filter for secret values and Bearer tokens.

Intercepts log records at creation time via ``logging.setLogRecordFactory``,
ensuring ALL handlers (including those added after installation) see only
redacted output. This module performs ZERO vault I/O — the caller resolves
any literal secret values and passes them in via ``install_log_redaction``.

Currently the only caller (``cli._setup_cli_logging``) passes an empty list,
so the built-in patterns — labelled secrets (the secret access key, the session
token and generic secret fields, label kept), ``Authorization`` and Bearer
credentials, vendor tokens, unconfirmed AWS key ids (``ABIA`` / ``ACCA``),
standalone JWTs and signing values — are what is redacted in practice. Wiring resolved
vault secret values into the filter at gateway boot is a follow-up.

Records are mutated only when redaction changes the rendered message — for
``msg``/``args`` whose args are exact immutable scalars (str/int/float/bool/
None, no subclasses) with clean text: a scalar's entire exportable surface IS
the string the scan inspects. Any opaque object arg disqualifies preservation
— its attributes can carry credentials a structured handler would serialize
but no text scan can bound. ``exc_info`` is ALWAYS rendered, redacted into
``exc_text``, and cleared: a live traceback is an object graph whose frames
carry locals no text scan can bound.

The trade this makes explicit: redaction inspects the RENDERED
text, never the internals of the ``args`` objects themselves, so a structured
handler that serializes arg objects directly (attribute dumps, JSON) can emit
content the text scan never saw. That limitation is inherent — the scan does not
look inside objects — and the alternative is destroying such records wholesale as
collateral. Preservation also moves rendering from creation time
to emit time: an arg object whose ``str()`` reads live state, or one mutated
before a ``QueueHandler`` drains, can emit text the creation-time scan never
saw. Both halves of the trade are accepted deliberately in favor of keeping
structured fields intact on the overwhelmingly common secretless record.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from kiro_crew.credential_patterns import (
    AUTHORIZATION_LABEL,
    AUTHORIZATION_VALUE,
    AWS_KEY_ID_REDACTION,
    AWS_SECRET_LABEL,
    AWS_SESSION_LABEL,
    BARE_SIGNATURE_LABEL,
    GENERIC_SECRET_LABEL,
    JWT_MULTI_SEGMENT,
    LABEL_SEPARATOR,
    LABEL_VALUE_OR_QUOTED,
    SEPARATOR_SPACE,
    SIGNING_LABEL,
    URL_SIGNING_VALUE,
    VENDOR_TOKEN_PATTERNS,
)
from kiro_crew.credential_values import (
    block_scalar_end,
    closes_label_literal,
    config_value_end,
    in_closing_tag,
    in_url_query,
    is_tag,
    is_weak_label,
    labelled_value_is_secret,
    names_working_dir,
    value_extent,
    without_tags,
    xml_value_end,
)

logger = logging.getLogger(__name__)

# Bearer tokens: RFC 6750 token68 charset (case-insensitive to catch
# serializations that lowercase the scheme, e.g. "bearer <token>"). A token holds a
# digit, a token68 symbol, mixed case or more than 15 letters, so prose such as
# "Bearer authentication failed" is left alone.
_BEARER_RE = re.compile(
    r"(?i:Bearer) (?:(?=[A-Za-z]*[0-9._~+/=-])|(?=[a-z]*[A-Z])(?=[A-Z]*[a-z])|(?=[A-Za-z]{16}))"
    r"[A-Za-z0-9._~+/=-]{8,}"
)

# Secret tier, from the shared table: a value behind a secret label (the label
# stays), an ``Authorization`` credential, and the vendor token formats.
_AUTHORIZATION_RE = re.compile(rf"({AUTHORIZATION_LABEL}){AUTHORIZATION_VALUE}")
_AWS_LABELLED_RE = re.compile(
    rf"((?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}){LABEL_SEPARATOR})({LABEL_VALUE_OR_QUOTED})"
)
_GENERIC_LABELLED_RE = re.compile(
    rf"({GENERIC_SECRET_LABEL}{LABEL_SEPARATOR})({LABEL_VALUE_OR_QUOTED})"
)
_SIGNING_LABELLED_RE = re.compile(
    rf"((?:{SIGNING_LABEL}|{BARE_SIGNATURE_LABEL})"
    rf"""["']?{SEPARATOR_SPACE}[:=]{SEPARATOR_SPACE}["']?)"""
    rf"({LABEL_VALUE_OR_QUOTED})"
)
_VENDOR_TOKEN_RE = re.compile("|".join(fragment for _label, fragment in VENDOR_TOKEN_PATTERNS))


def _labelled_value_end(m: "re.Match[str]") -> int:
    """Where the value of labelled match *m* ends: the shared reader
    (:func:`credential_values.value_extent`) for a quoted value, never shorter
    than the unquoted run."""
    label, text = m.group(1), m.string
    xml_end = xml_value_end(text, m.start(), m.start(2))
    if xml_end is not None:
        return xml_end
    block_end = block_scalar_end(text, m.start(), m.start(2))
    if block_end is not None:
        return block_end
    if not label or label[-1] not in "\"'":
        config_end = config_value_end(text, m.start(), m.start(2))
        return max(m.end(2), config_end or 0)
    return max(m.end(2), value_extent(text, m.start(2), label[-1]))


def _sub_labelled(
    pattern: "re.Pattern[str]",
    text: str,
    keep: Callable[["re.Match[str]", str], bool],
) -> str:
    """Replace each labelled value *pattern* finds, label kept, unless ``keep(m, value)``."""
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() < pos:
            continue  # inside a quoted value already dropped
        if closes_label_literal(text, m.start(), m.start(2)):
            continue  # the quote closes a literal the label is in
        if in_closing_tag(text, m.start()):
            continue  # ``</password>``: a closing tag, no value after it
        end = _labelled_value_end(m)
        if keep(m, text[m.start(2) : end]):
            continue
        out.append(text[pos : m.start(2)])
        out.append("[REDACTED]")
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _in_arn(text: str, pos: int) -> bool:
    """A label word right after a ``:`` of an ARN is a resource type; after
    ``$env:`` or any other colon it is still a label."""
    if pos == 0 or text[pos - 1] != ":":
        return False
    token = text[max(0, pos - 256) : pos].split()
    return bool(token) and "arn:" in token[-1].lower()


def _quoted(m: "re.Match[str]") -> bool:
    label = m.group(1)
    return bool(label) and label[-1] in "\"'"


def _on_config_line(m: "re.Match[str]") -> bool:
    """Whether labelled match *m* is a literal: a config line's unquoted value
    or an XML element's content."""
    if xml_value_end(m.string, m.start(), m.start(2)) is not None:
        return True
    if not _quoted(m) and in_url_query(m.string, m.start()):
        return True
    return not _quoted(m) and config_value_end(m.string, m.start(), m.start(2)) is not None


def _keep_aws_value(m: "re.Match[str]", value: str) -> bool:
    """An AWS label's value is kept unless the shared predicate calls it a secret."""
    end = m.start(2) + len(value)
    next_char = m.string[end] if end < len(m.string) else ""
    return not labelled_value_is_secret(
        value, quoted=_quoted(m), next_char=next_char, aws=True, config=_on_config_line(m)
    )


def _keep_generic_value(m: "re.Match[str]", value: str) -> bool:
    """A generic label's value is kept unless the shared predicate calls it a
    secret, and inside an ARN (``...:secret:name``) the word is a resource type."""
    if _in_arn(m.string, m.start()) or names_working_dir(m.group(1)):
        return True
    end = m.start(2) + len(value)
    next_char = m.string[end] if end < len(m.string) else ""
    return not labelled_value_is_secret(
        value,
        quoted=_quoted(m),
        next_char=next_char,
        weak=is_weak_label(m.group(1)),
        config=_on_config_line(m),
    )


def _keep_signing_value(m: "re.Match[str]", value: str) -> bool:
    """A named signing value goes whatever its shape; a bare ``Signature`` is
    judged like any labelled value, as in the batch redactor."""
    if not value or is_tag(value):
        return True
    if "amz" in m.group(1).lower():
        return False
    return _keep_generic_value(m, value)


def _redact_unless_tagged(m: "re.Match[str]") -> str:
    """A signing match whose value is a whole redaction tag (its label kept
    above) stays. Only whole tags count: text that merely starts like one is
    the writer's own and may carry the signature."""
    if _URL_SIGNING_RE.search(without_tags(m.string, m.start(), m.end())) is None:
        return m.group(0)
    return "[REDACTED]"


# AWS ``ABIA`` / ``ACCA`` key ids, whose tier is not yet confirmed. An ``AKIA`` /
# ``ASIA`` access key ID is Identifier tier and stays visible, so a log line can
# still be joined to the key it names. The spelling comes from
# ``credential_patterns``, which imports nothing, so this module stays an import
# leaf (it installs at CLI bootstrap, before heavy modules load).
_AWS_KEY_ID_RE = re.compile(AWS_KEY_ID_REDACTION)

# Standalone JWTs: the shared ``eyJ`` header plus 2-4 further dot-separated
# base64url segments, covering 3-segment JWS AND 4-5-segment JWE (dir/ECDH-ES).
# Catches tokens logged OUTSIDE a ``Bearer `` scheme -- JSON-embedded, bare, or
# assignment-style. Same string object as the scrubber's JWS/JWE branch.
_JWT_RE = re.compile(JWT_MULTI_SEGMENT)

# A presigned or signed link's signing values. The key ID in the link is an
# Identifier and stays visible; removing the part that makes the link work -- the
# SigV4/SigV2 signature and the session token -- is what stops a signed link in a
# CLI log being replayed. Shared import-free spelling, so this module stays an
# install-time import leaf.
_URL_SIGNING_RE = re.compile(URL_SIGNING_VALUE)

# One ``name=value`` (or ``name%3Dvalue``) query/header parameter. The name may carry
# percent-encoded octets; the value runs to the next delimiter. The name is anchored
# with a negative look-behind AND bounded to 128 chars so a match can never START
# inside a long ``[A-Za-z0-9%._-]`` run (a base64url or hex blob in a logged frame):
# without both, the engine restarts at every position in such a run and backtracks it
# to the end, which is quadratic and can stall the thread that logs the line.
#: A name never runs through an encoded ``?``, ``&`` or ``=`` (``%3F``, ``%26``,
#: ``%3D``), and one may start right after an encoded ``?`` or ``&``, so a link
#: nested percent-encoded in another (``o%3FX-Amz-Security-Token%3D...``) is
#: read parameter by parameter: a value ends at an encoded ``&`` or ``?`` too, and
#: at a literal ``?``, so a link inside another value (``url=...?Signature=``) is
#: read as its own parameters.
#: The value runs to its own terminator at any length: a capped value would
#: leave a long session token's tail in the record. The name's bound is what
#: keeps the scan linear, and the value's alternatives never overlap.
#: The same holds at any depth of encoding: a link encoded twice inside a nested
#: redirect writes its ``=`` as ``%253D`` and its ``&`` as ``%2526``, so every
#: encoded separator allows up to eight ``25`` (an encoded ``%``) before its code.
_PRESIGNED_PARAM_RE = re.compile(
    r"(?:(?<![A-Za-z0-9%._-])|(?<=%3[Ff])|(?<=%26)|(?<=253[Ff])|(?<=2526))"
    r"((?:[A-Za-z0-9._-]|%(?!(?:25){0,8}(?:3[DdFf]|26))[0-9A-Fa-f]{2}){1,128})"
    r"(=|%(?:25){0,8}3[Dd])((?:[^\s\"'&;#<>)\]}%?]|%(?!(?:25){0,8}(?:26|3[Ff]))|%$)+)"
)
_PCT_OCTET_RE = re.compile(r"%([0-9A-Fa-f]{2})")
#: A signature's own shape: a hex digest, or a base64 run of letters and digits.
_SIGNATURE_SHAPE_RE = re.compile(
    r"[0-9A-Fa-f]{32,}"
    r"|(?=[A-Za-z0-9+/_%-]*[0-9])(?=[A-Za-z0-9+/_%-]*[A-Za-z])[A-Za-z0-9+/_%-]{20,}={0,2}"
)
#: An encoded ``?`` or ``&`` at any depth, right before a parameter name.
#: Written HTML-escaped too (``&amp;``, ``&#38;``, ``&#x26;``), as in a link
#: copied out of an HTML error page.
_QUERY_START_RE = re.compile(
    r"(?:[?&]|%(?:25){0,8}(?:3[Ff]|26)|&(?:amp|#0{0,4}38|#[xX]0{0,4}26);)\Z"
)
# Parameter names whose VALUE makes a signed link work. The key ID
# (``AWSAccessKeyId`` / ``X-Amz-Credential``) is deliberately absent: it is an
# Identifier and stays visible, so a logged link can still be joined to its key.
_SIGNING_PARAM_NAMES = frozenset(
    {"signature", "x-amz-signature", "x-amz-security-token", "x-goog-signature"}
)


def _decode_pct_ascii(name: str) -> str:
    """Resolve ``%XX`` octets in a parameter name so ``%53ignature`` reads as its
    real name. Import-free -- it reuses the already-imported ``re`` and never pulls
    in ``urllib.parse``, keeping this module an install-time import leaf.
    """
    # A name encoded more than once (``%2553ignature``) is decoded to its end.
    for _ in range(9):
        decoded = _PCT_OCTET_RE.sub(lambda m: chr(int(m.group(1), 16)), name)
        if decoded == name:
            break
        name = decoded
    return name


def _in_query(line: str, pos: int) -> bool:
    """Whether a parameter name starting at *pos* follows ``?``/``&`` or their codes."""
    return _QUERY_START_RE.search(line, max(0, pos - 24), pos) is not None


def _redact_signing_params(line: str) -> str:
    """Drop the value of every signing parameter in *text* while keeping the key
    ID and ordinary parameters. The name is matched after percent-decoding, so an
    obfuscated ``%53ignature`` is caught, and the value is removed whatever its
    shape (64-hex SigV4, DER-hex SigV4A, or base64 SigV2). Keying on the decoded
    NAME is what bounds this: only a signing parameter's value goes, so running it
    on every record -- not only lines carrying a literal marker, which a
    percent-encoded ``%41WSAccessKeyId`` would hide -- cannot touch prose, since a
    bare ``signature`` in prose is not followed by ``=``/``%3D`` and a value.
    """

    def repl(m: "re.Match[str]") -> str:
        name, sep = m.group(1), m.group(2)
        if not without_tags(line, m.start(3), m.end(3)):
            return m.group(0)  # already a whole tag
        decoded = _decode_pct_ascii(name).lower()
        # ``sig`` (an Azure SAS signature) is too short a word to judge outside
        # a URL query, so it counts only right after ``?``/``&`` or their codes.
        # A bare ``signature`` is a common parameter name in code
        # (``def verify(signature=None)``): outside a query it counts only when
        # its value has a signature's shape.
        if decoded == "signature":
            signing = _in_query(line, m.start()) or (
                _SIGNATURE_SHAPE_RE.fullmatch(m.group(3)) is not None
            )
        else:
            signing = decoded in _SIGNING_PARAM_NAMES or (
                decoded == "sig" and _in_query(line, m.start())
            )
        return f"{name}{sep}[REDACTED]" if signing else m.group(0)

    return _PRESIGNED_PARAM_RE.sub(repl, line)


class SecretRedactionFilter:
    """Redacts secret patterns and Bearer tokens from log records.

    Installed via ``logging.setLogRecordFactory`` so it intercepts EVERY
    record at creation — no handler can see plaintext regardless of when
    it was added. This class performs ZERO I/O at runtime — patterns are
    frozen at construction time.
    """

    def __init__(self, patterns: list[str]) -> None:
        """Initialize with literal secret values to redact.

        Parameters
        ----------
        patterns:
            List of literal strings (secret values) to redact. Each value
            is regex-escaped. Values shorter than 4 chars are skipped to
            avoid false positives.
        """
        escaped = [re.escape(p) for p in patterns if len(p) >= 4]
        self._secret_pattern: Optional[re.Pattern[str]] = (
            re.compile("|".join(escaped)) if escaped else None
        )
        # The canonical key-pair detector: a 40-character secret beside an AWS
        # key ID, unlabelled (a CSV row). Resolved here, at install time, not at
        # import and not on the logging path, so this module stays an import
        # leaf and a record never waits on an import lock.
        from kiro_crew.security.redaction_labels import paired_secret_spans

        self._pair_spans = paired_secret_spans

    def redact(self, text: str) -> str:
        """Replace secret values, labelled secrets (label kept), Authorization and
        Bearer credentials, vendor tokens, unconfirmed AWS key ids, JWTs, and
        presigned/signed-link signing values.

        Zero I/O — only applies pre-compiled patterns.
        """
        if self._secret_pattern is not None:
            text = self._secret_pattern.sub("[REDACTED]", text)
        text = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", text)
        text = _BEARER_RE.sub("Bearer [REDACTED]", text)
        text = _sub_labelled(_SIGNING_LABELLED_RE, text, _keep_signing_value)
        text = _sub_labelled(_AWS_LABELLED_RE, text, _keep_aws_value)
        text = _sub_labelled(_GENERIC_LABELLED_RE, text, _keep_generic_value)
        text = self._redact_key_pairs(text)
        text = _VENDOR_TOKEN_RE.sub("[REDACTED]", text)
        text = _AWS_KEY_ID_RE.sub("[REDACTED]", text)
        text = _JWT_RE.sub("[REDACTED]", text)
        text = _URL_SIGNING_RE.sub(_redact_unless_tagged, text)
        # Drop a signing parameter's value even under a percent-encoded name
        # (``%53ignature``) or beside a percent-encoded marker. Keyed on the
        # decoded NAME, so it runs on every record without a literal-marker
        # prerequisite that an encoded marker would slip past; the anchored,
        # bounded name keeps it linear.
        text = _redact_signing_params(text)
        return text

    def _redact_key_pairs(self, text: str) -> str:
        """Drop each secret key the canonical detector pairs with a key ID; the
        key ID stays (Identifier tier)."""
        spans = sorted(self._pair_spans(text), key=lambda s: s.start)
        if not spans:
            return text
        parts: list[str] = []
        cursor = 0
        for span in spans:
            parts.append(text[cursor : span.start])
            parts.append("[REDACTED]")
            cursor = max(cursor, span.end)
        parts.append(text[cursor:])
        return "".join(parts)


#: Marks an installed factory as a redacting wrapper, and carries the two things it
#: needs: ``base`` (the factory it displaced, to call and to give back) and ``filter``
#: (the live :class:`SecretRedactionFilter`, or ``None`` to pass records through).
#:
#: Both live ON THE INSTALLED FUNCTION rather than in a module global, and that is the
#: invariant the whole module rests on. A module global is resolved at CALL time and can
#: be rewritten — or zeroed by a reload — while the wrapper stays in ``logging``'s slot,
#: so the two disagree and the wrapper ends up calling something other than what it
#: displaced. Held on the function, each wrapper's base is fixed at install:
#:
#: * A CYCLE IS UNREPRESENTABLE. Wrapping our own wrapper makes it the inner link of a
#:   finite chain whose base is still the real original, so the worst case is redacting
#:   twice. Resolved through a global, the same wrap made the wrapper its own base and
#:   every later record recursed until ``RecursionError`` — on a PROCESS-GLOBAL slot, so
#:   no record was emitted again, redacted or not.
#: * OWNERSHIP IS READ OFF THE SLOT. ``install``/``uninstall`` ask the installed object
#:   what it is instead of consulting a sentinel that anything replacing the slot (a test
#:   floor, a reload, another copy of this module) silently invalidates.
_FACTORY_BASE = "_kirocrew_redaction_base"
_FACTORY_FILTER = "_kirocrew_redaction_filter"


#: Exact scalar types whose exportable content IS their text: no attributes,
#: no interiors, nothing a structured handler can serialize beyond the string
#: form the scan inspects. Exact ``type()`` match — a *subclass* of ``str`` or
#: ``int`` can carry attributes holding credentials, so subclasses do not
#: qualify.
_BOUNDED_SCALAR_TYPES = (str, int, float, bool, type(None))


def _args_are_scan_bounded(args: object, filt: "SecretRedactionFilter") -> bool:
    """True only when every arg's exportable content was fully scanned clean.

    Args qualify only as a plain ``tuple`` whose every element is an exact
    immutable scalar (:data:`_BOUNDED_SCALAR_TYPES`) with clean text. The
    scalar restriction is what bounds the export surface: an OPAQUE object can
    have a benign ``str()`` while its attributes carry a credential that a
    structured handler serializing the object (attribute dump, JSON) would
    export — text scanning cannot bound that. A MAPPING is unbounded for the
    same reason: the dict object itself is exportable, and its KEYS (which
    ``%(name)s`` formatting never renders in full) can carry a credential — so
    mappings are never preserved, not key/value scanned. The text check on top
    closes the lossy-format hole (``%.100s`` rendering a clean prefix of a
    secret-bearing scalar).
    """
    # Only genuinely-absent args qualify as trivially bounded: ``None`` or an
    # exact empty tuple. A truthiness check would trust ANY falsy object —
    # e.g. a custom instance whose ``__bool__`` returns False while its
    # attributes carry a credential.
    if args is None or (type(args) is tuple and len(args) == 0):
        return True
    if type(args) is not tuple:
        # dict args (``%(name)s`` style), subclasses, or the odd bare arg:
        # all carry exportable content beyond their rendered text.
        return False
    try:
        for value in args:
            if type(value) not in _BOUNDED_SCALAR_TYPES:
                return False
            text = str(value)
            if filt.redact(text) != text:
                return False
    except Exception:
        return False
    return True


def _redacting_factory(base: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    """Build a record factory that redacts everything *base* produces.

    The wrapper runs at record CREATION, before any handler sees it, which is what
    covers handlers added after installation.

    Redaction stays process-wide — EVERY record is inspected. ``msg``/``args``
    are preserved only when every arg is an exact immutable scalar whose text
    scans clean (see :func:`_args_are_scan_bounded`), so a downstream JSON or
    observability handler still sees the original ``%s`` template and its
    ``args`` tuple on the common secretless-scalar record, while a record
    carrying any opaque object — whose attributes no text scan can bound — is
    materialized and cleared. ``exc_info`` is always rendered, redacted into
    ``exc_text``, and cleared — traceback frames carry locals no text scan can
    bound, so a live triple is never handed to a handler that might export
    them.
    """

    def factory(*args, **kwargs) -> logging.LogRecord:  # type: ignore[no-untyped-def]
        record = base(*args, **kwargs)
        filt: Optional[SecretRedactionFilter] = getattr(factory, _FACTORY_FILTER, None)
        if filt is None:
            return record

        # Materialize the full formatted message and redact it. Preserve the
        # record ONLY when nothing a handler could export carries a secret: the
        # rendered text AND every arg's own string form must scan clean.
        # Scanning args individually closes the lossy-format hole — a spec like
        # ``%.100s`` can render a clean prefix while the full secret stays in
        # ``record.args`` for a structured handler to export.
        try:
            rendered = record.getMessage()
        except Exception:
            # Render failure: args were never inspected, so "unchanged text ⇒
            # secretless" does not hold on this branch. Fall back to the old
            # unconditional destruction — scan the bare template and drop args
            # wholesale. Fidelity is already lost (the record cannot format),
            # and CPython's Handler.handleError prints ``Arguments: %s`` to
            # stderr on a later format failure, which would leak a
            # secret-bearing arg this scan never saw.
            record.msg = filt.redact(str(record.msg) if record.msg else "")
            record.args = None
            rendered = None
        if rendered is not None:
            redacted = filt.redact(rendered)
            # Preserve only when EVERY exportable component is bounded by the
            # scanned text: ``msg`` must be an exact ``str`` (an opaque msg
            # OBJECT has attributes a structured handler could export that its
            # rendered text never showed), and args must be a plain tuple of
            # exact scalars with clean text (see _args_are_scan_bounded).
            if (
                redacted != rendered
                or type(record.msg) is not str
                or not _args_are_scan_bounded(record.args, filt)
            ):
                record.msg = redacted
                record.args = None  # Already materialized — prevent double-format

        # exc_text: an already-rendered traceback string. Redacting in place is
        # idempotent, so assign unconditionally — the smaller form.
        if record.exc_text:
            record.exc_text = filt.redact(record.exc_text)

        # exc_info: ALWAYS render, redact, pin into exc_text, and clear. Unlike
        # msg/args — whose exportable surface is exactly the string forms the
        # scan inspects — a live exc_info triple is an object graph whose
        # traceback FRAMES carry locals (e.g. a client object holding a token)
        # that no text scan can bound: a locals-capturing handler would export
        # credentials the rendered-text check never saw. The preserve-on-clean
        # rule therefore applies only to msg/args; exc_info is destroyed
        # unconditionally, exactly as before this module preserved anything.
        if record.exc_info:
            import traceback

            rendered_tb = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = filt.redact(rendered_tb)
            record.exc_info = None  # Prevent handler re-render / locals export
        return record

    setattr(factory, _FACTORY_BASE, base)
    setattr(factory, _FACTORY_FILTER, None)
    return factory


def _installed_factory() -> Optional[Callable[..., logging.LogRecord]]:
    """The redacting wrapper currently in ``logging``'s slot, or ``None``.

    Only the TOP of the chain is inspected. A third party that wrapped ours answers
    ``None``, which is the right answer for both callers: the slot is not theirs to hand
    back, and installing a second wrapper over it is safe because chains terminate.

    Marker-based rather than identity-based so it survives this module being reloaded or
    imported a second time — either of which produces a new function object for a
    wrapper that is already installed and working.
    """
    factory = logging.getLogRecordFactory()
    return factory if hasattr(factory, _FACTORY_BASE) else None


def install_log_redaction(patterns: list[str]) -> SecretRedactionFilter:
    """Install the redaction filter via ``logging.setLogRecordFactory``.

    This intercepts records at creation time — a record-level chokepoint
    that covers ALL handlers, including those added later. The module
    performs ZERO vault I/O — the caller resolves secret values and passes
    them as *patterns*.

    Idempotent: when a redacting wrapper is already installed, its filter is
    re-pointed at *patterns* and the slot is left alone. Re-pointing the INSTALLED
    wrapper, rather than a global only this copy of the module can see, is what keeps
    the new patterns in force after a reload or a second import.

    Parameters
    ----------
    patterns:
        Literal secret values to redact from log output. Values shorter
        than 4 chars are ignored (too likely to cause false positives).
    """
    filt = SecretRedactionFilter(patterns)

    installed = _installed_factory()
    if installed is None:
        installed = _redacting_factory(logging.getLogRecordFactory())
        logging.setLogRecordFactory(installed)
    setattr(installed, _FACTORY_FILTER, filt)

    return filt


def uninstall_log_redaction() -> None:
    """Stop redacting, and give the record factory back.

    ``install_log_redaction`` needs a counterpart because the record factory is
    process-global: a caller that installs one and cannot remove it has changed
    every ``LogRecord`` for the life of the process, including dropping
    ``exc_info``, which the wrapper does on purpose so a handler cannot re-render
    an unredacted traceback.

    Idempotent, and it reverts only the wrapper at the TOP of the slot. If a third
    party wrapped ours, theirs is what the slot holds and ours is unreachable behind
    their closure, so nothing is touched and redaction CONTINUES until they remove
    their own wrapper. That is the safe direction for a control whose job is to keep
    secrets out of the log: the alternative is reaching into a chain this module does
    not own and unlinking whatever they added.
    """
    installed = _installed_factory()
    if installed is None:
        return
    setattr(installed, _FACTORY_FILTER, None)
    logging.setLogRecordFactory(getattr(installed, _FACTORY_BASE))
