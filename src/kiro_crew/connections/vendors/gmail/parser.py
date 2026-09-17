"""Parse an RFC 5322 / MIME message into structured parts — pure logic.

:func:`parse_message` is the inverse of :func:`.builder.build_message`: it takes
message bytes (or a decoded :func:`.raw.decode_raw` result) and returns a
:class:`ParsedMessage` exposing decoded headers, the text and HTML bodies, and
every attachment and inline image as :class:`.attachments.Attachment` /
:class:`.attachments.InlineImage` objects — each carrying its raw bytes and a
recomputed checksum, so a caller can verify an extracted attachment against the
one that was constructed.

Decoding rules:

* RFC 2047 encoded-word headers (``Subject``, display names) are decoded back
  to Unicode.
* A body part's declared ``charset`` is honored, so a UTF-8 Chinese body comes
  back as the original text.
* An inline image is recognized by ``Content-Disposition: inline`` OR a
  ``Content-ID`` (Gmail-built related parts carry both); its ``cid`` token is
  exposed stripped of angle brackets, matching what an HTML ``cid:`` reference
  uses.

No silent data loss or corruption:

* A ``Bcc`` header present on the parsed bytes is surfaced on
  :attr:`ParsedMessage.bcc`, never dropped.
* An attached ``message/rfc822`` is captured whole as an attachment, not
  descended into (which would lose the attachment and could let its nested body
  masquerade as this message's own body).
* A body whose bytes are invalid for its declared charset is raised as
  malformed, never silently ``errors="replace"``-normalized into corruption
  carrying a checksum over that corruption.

Malformed input raises :class:`.errors.MalformedMimeError` rather than
returning a half-parsed object. A well-formed message that merely lacks a
header the caller required is NOT malformed — use :func:`require_headers` for
that check, which raises :class:`.errors.MissingHeaderError`.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import urllib.parse
from dataclasses import dataclass, field
from email.generator import BytesGenerator
from email.header import decode_header, make_header
from email.message import EmailMessage as _EmailMessage
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32 as compat32_policy
from email.policy import default as default_policy
from email.utils import getaddresses

from kiro_crew.connections.vendors.gmail.addresses import Mailbox
from kiro_crew.connections.vendors.gmail.attachments import Attachment, InlineImage
from kiro_crew.connections.vendors.gmail.errors import (
    AttachmentTooLargeError,
    InvalidAliasError,
    MalformedMimeError,
    MissingHeaderError,
)


@dataclass
class ParsedMessage:
    """The structured result of parsing a MIME message.

    ``headers`` holds decoded single-value headers by canonical name.
    ``text_body`` / ``html_body`` are ``None`` when absent. ``attachments`` and
    ``inline_images`` carry recomputed checksums for byte-level verification.
    """

    headers: dict[str, str] = field(default_factory=dict)
    to: list[Mailbox] = field(default_factory=list)
    cc: list[Mailbox] = field(default_factory=list)
    bcc: list[Mailbox] = field(default_factory=list)
    text_body: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    inline_images: list[InlineImage] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.headers.get("Subject", "")

    @property
    def from_(self) -> str:
        return self.headers.get("From", "")

    @property
    def message_id(self) -> str:
        return self.headers.get("Message-ID", "")

    @property
    def in_reply_to(self) -> str:
        return self.headers.get("In-Reply-To", "")

    @property
    def references(self) -> str:
        return self.headers.get("References", "")


# Headers whose decoded value we surface as a plain string. The email package's
# ``default`` policy already decodes RFC 2047 for us when we str() a header.
_SINGLE_HEADERS = (
    "From",
    "Subject",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Date",
)


def _recover_surrogate_utf8(text: str) -> str:
    """Recover a raw UTF-8 (SMTPUTF8) display name the default policy exposed as
    surrogateescape code points. Pure-ASCII text is returned unchanged; text
    carrying surrogateescape bytes that form valid UTF-8 is decoded; anything
    else is left as-is (never forced through ``errors="replace"``)."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _recipients_from(msg: _EmailMessage, name: str, raw_msg: Message) -> list[Mailbox]:
    """Build the recipient list for one header.

    The addr-specs and the name/address split come from the DEFAULT policy's
    structured ``.addresses`` (correct RFC 2047 + raw-UTF-8 decoding, reads
    every repeated occurrence). Display names, however, are decoded from the
    COMPAT32 raw via ``decode_header``/``make_header``, which joins adjacent
    encoded-words with NO separating whitespace per RFC 2047 §6.2 — the default
    policy inserts a stray space between a name folded across two encoded-words.
    Raw UTF-8 (SMTPUTF8) names the default policy surrogate-escapes are
    recovered. Group placeholders and entries with no addr-spec are skipped
    rather than raising, so cosmetic noise never fails the parse and no real
    recipient is dropped."""
    out: list[Mailbox] = []
    headers = msg.get_all(name)
    if not headers:
        return out
    # Pre-decode each raw occurrence's display name where it uses RFC 2047
    # encoded-words, so a name folded across encoded-words is joined with no
    # stray space (RFC 2047 §6.2). Keyed by POSITION (an ordered list), not by
    # addr-spec: the same address can appear more than once with DIFFERENT
    # display names, and an addr-spec key would give every later occurrence the
    # first name (silent name corruption). A raw UTF-8 (non-encoded) name is
    # left to the default-policy surrogateescape recovery below, because the
    # compat32 str() would mangle its bytes into replacement characters.
    raw_pairs: list[tuple[str, str | None]] = []
    for rv in raw_msg.get_all(name) or []:
        rv_text = str(rv)
        has_ew = "=?" in rv_text and "?=" in rv_text
        for rn, ad in getaddresses([rv_text]):
            if not ad or "@" not in ad:
                continue
            decoded: str | None = None
            if has_ew and rn and "=?" in rn:
                try:
                    decoded = str(make_header(decode_header(rn)))
                except Exception:  # pragma: no cover - defensive
                    decoded = None
            raw_pairs.append((ad, decoded))
    pos = 0
    for header in headers:
        for addr in getattr(header, "addresses", ()):  # AddressHeader.addresses
            spec = addr.addr_spec or ""
            if not spec or "@" not in spec:
                continue
            # Advance the positional cursor to the raw pair for THIS address
            # occurrence. The structured .addresses and the raw getaddresses
            # walk the same headers in the same order, so aligning by position
            # (skipping any raw pair whose addr-spec does not match, a defensive
            # guard against a cosmetic desync) gives each occurrence its own
            # decoded name.
            display: str | None = None
            while pos < len(raw_pairs) and raw_pairs[pos][0] != spec:
                pos += 1
            if pos < len(raw_pairs):
                display = raw_pairs[pos][1]
                pos += 1
            if display is None:
                display = _recover_surrogate_utf8(addr.display_name or "")
            try:
                out.append(Mailbox(address=spec, display_name=display))
            except InvalidAliasError:
                continue
    return out


def _wire_linesep(data: bytes) -> str:
    """Detect the line-ending style the message uses on the wire.

    RFC 5322 mandates CRLF, but locally-stored / Unix-authored ``.eml`` files
    are routinely LF-only. This linesep drives faithful CRLF-vs-LF serialization
    of non-``message/*`` embedded parts during extraction. A ``message/*``
    attachment's extracted bytes are subsequently CRLF-CANONICALIZED by
    :class:`~...attachments.Attachment` (so its stored bytes and checksum match
    the wire form regardless of source endings); the detection here does not
    override that canonicalization. A message with no CRLF but containing LF is
    treated as LF-only; otherwise CRLF (the wire default).
    """
    if b"\r\n" not in data and b"\n" in data:
        return "\n"
    return "\r\n"


def parse_message(data: bytes) -> ParsedMessage:
    """Parse message bytes into a :class:`ParsedMessage`.

    Raises :class:`MalformedMimeError` if the bytes cannot be parsed as a MIME
    message or if a declared multipart carries no usable parts.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("parse_message expects bytes")
    if not data.strip():
        raise MalformedMimeError("empty message")

    try:
        parser = BytesParser(_EmailMessage, policy=default_policy)
        msg = parser.parsebytes(bytes(data))
    except Exception as exc:  # the email parser is broad; normalize its failures
        raise MalformedMimeError(f"could not parse message: {exc}") from exc

    # A parallel compat32 parse preserves RFC 2047 encoded-words verbatim (the
    # default policy has already lossily decoded them), so the strict
    # encoded-word check in _strict_header_text can see the original bytes.
    try:
        raw_msg = BytesParser(policy=compat32_policy).parsebytes(bytes(data))
    except Exception as exc:
        raise MalformedMimeError(f"could not parse message: {exc}") from exc

    parsed = ParsedMessage()
    _reject_serious_defects(msg)
    for name in _SINGLE_HEADERS:
        # msg.get(name) under the DEFAULT (structured) policy parses the header
        # into a typed object; a malformed value can raise (or crash on str())
        # when the object is accessed/rendered. Translate any such failure to a
        # typed MalformedMimeError rather than letting a raw exception escape
        # parse_message uncaught.
        try:
            raw = msg.get(name)
            if raw is not None:
                parsed.headers[name] = _strict_header_text(name, str(raw), raw_msg.get(name))
        except MalformedMimeError:
            raise
        except Exception as exc:
            raise MalformedMimeError(f"header {name!r} is malformed: {exc}") from exc

    # A recipient header may legally appear more than once (RFC 5322 §3.6
    # bounds most headers to one occurrence, but real-world producers emit
    # repeated To/Cc/Bcc, and a lenient parser that reads only the first would
    # silently drop every later recipient). No recipient must disappear.
    #
    # Display names come from the DEFAULT policy's structured .addresses: it
    # decodes RFC 2047 encoded-words AND raw UTF-8 (SMTPUTF8) names, where the
    # compat32 str() would mangle raw UTF-8 into replacement characters. Raw
    # non-ASCII bytes the default policy could not map are exposed as
    # surrogateescape code points, recovered here as UTF-8. Strict encoded-word
    # validation (erasure / undecodable bytes) is preserved by running the
    # strict check over the compat32 raw of each recipient header in parallel,
    # so a corrupt encoded-word still raises regardless of which policy renders
    # the display name.
    for _rcpt in ("To", "Cc", "Bcc"):
        for _rv in raw_msg.get_all(_rcpt) or []:
            _strict_header_text(_rcpt, str(_rv), _rv)
    parsed.to = _recipients_from(msg, "To", raw_msg)
    parsed.cc = _recipients_from(msg, "Cc", raw_msg)
    # A well-formed delivered message is Bcc-stripped by the sending MTA, but a
    # message assembled locally (e.g. round-tripped before send, or captured
    # pre-transmission) can still carry a Bcc header. Surface it rather than
    # silently dropping blind-recipient data — a caller that must not leak it
    # decides that, not a silent parse.
    parsed.bcc = _recipients_from(msg, "Bcc", raw_msg)

    # A multipart declared with no boundary / no parts is malformed. The
    # email parser records this as a structural defect (see
    # _reject_serious_defects) rather than raising, and also collapses the
    # payload to a non-list; both are caught above and here.
    if msg.is_multipart():
        payload = msg.get_payload()
        if not isinstance(payload, list) or not payload:
            raise MalformedMimeError("multipart message has no parts")

    _walk_parts(msg, parsed, _wire_linesep(bytes(data)))
    return parsed


# Defect classes that mean the bytes are not the message they claim to be — a
# boundary was declared but the delimiter never appeared, a multipart
# Content-Type resolved to a non-multipart structure, or a part's declared
# Content-Transfer-Encoding could not decode the body it carries (invalid
# base64). These are structural lies about the message content, distinct from
# cosmetic defects (a header with a trailing space) the parser also records but
# which do not make the message unusable. The base64 defects close the
# attachment side of the "silent corruption" class the text path handles by
# strict decode. Referenced by class NAME so a stdlib version that reorders the
# module does not break the check.
_FATAL_DEFECT_NAMES = frozenset(
    {
        "StartBoundaryNotFoundDefect",
        "CloseBoundaryNotFoundDefect",
        "MultipartInvariantViolationDefect",
        "InvalidBase64CharactersDefect",
        "InvalidBase64PaddingDefect",
        "InvalidBase64LengthDefect",
    }
)


def _strict_header_text(name: str, decoded: str, raw_source: object) -> str:
    """Decode a single-value header strictly, rejecting undecodable bytes.

    The default email policy decodes RFC 2047 encoded-words but, when an
    encoded-word's bytes cannot be decoded in its declared charset, falls back
    to ``errors="replace"`` and yields U+FFFD (``�``) instead of raising. That
    silently corrupts the header — the same "silent corruption" class the body
    path closes by strict decode. ``raw_source`` is the UNDECODED header value
    (parsed under compat32, so encoded-words survive verbatim); re-decode every
    encoded-word in it STRICTLY, and if any chunk fails in its declared charset
    reject the message rather than return corrupted text. A header that
    legitimately carries a literal U+FFFD in unencoded (already-Unicode) text
    is unaffected, because only the bytes chunks (encoded-words) are checked.
    """
    if raw_source is None:
        return decoded
    raw_text = str(raw_source)
    try:
        chunks = decode_header(raw_text)
    except Exception:
        raise MalformedMimeError(f"header {name!r} could not be decoded") from None
    for chunk, charset in chunks:
        if isinstance(chunk, bytes) and charset is not None:
            try:
                chunk.decode(charset, errors="strict")
            except (LookupError, UnicodeDecodeError) as exc:
                raise MalformedMimeError(
                    f"header {name!r} carries an undecodable encoded-word: {exc}"
                ) from exc
    # An encoded-word with invalid base64/QP (e.g. `=?utf-8?b?!!!!?=`) does not
    # raise: decode_header returns empty bytes for that chunk, so its content is
    # silently ERASED while the strict-charset check above passes. This must be
    # caught even when the header MIXES the erased word with other valid text
    # (`Hello =?utf-8?b?!!!!?=` -> `Hello ` is non-empty, so a whole-string
    # emptiness test misses it). Detect any encoded-word chunk in the raw source
    # that yielded empty bytes — that is corruption, not a legitimately-empty
    # segment.
    if "=?" in raw_text and "?=" in raw_text:
        for chunk, charset in chunks:
            if isinstance(chunk, (bytes, bytearray)) and charset is not None and not chunk:
                raise MalformedMimeError(
                    f"header {name!r} encoded-word decoded to empty (invalid encoding)"
                )
    # A raw NON-encoded-word 8-bit byte (e.g. `Subject: \xff`) is decoded to
    # U+FFFD by the default policy and would otherwise be returned as plausible
    # text — the same silent-corruption class the encoded-word path above
    # closes. If the DECODED value carries a U+FFFD, reject unless the raw bytes
    # legitimately form that character. ``raw_source`` from compat32 preserves
    # the original bytes as surrogate-escape code points, so recover them and,
    # when they are NOT valid UTF-8 (and did not literally contain U+FFFD),
    # treat the U+FFFD as decode-introduced corruption.
    if "\ufffd" in decoded:
        raw_str = _surrogate_raw(raw_source)
        raw_bytes = raw_str.encode("utf-8", "surrogateescape")
        try:
            raw_bytes.decode("utf-8", errors="strict")
            raw_is_valid_utf8 = True
        except UnicodeDecodeError:
            raw_is_valid_utf8 = False
        if not raw_is_valid_utf8 and "\ufffd" not in raw_str:
            raise MalformedMimeError(
                f"header {name!r} carries raw bytes that are invalid in their "
                f"charset (decoded to U+FFFD)"
            )
    return decoded


def _surrogate_raw(raw_source: object) -> str:
    """Return a compat32 header's raw form preserving its original bytes.

    A compat32 ``Header`` stringifies 8-bit bytes to U+FFFD, but its chunks keep
    them as surrogate-escape code points. Prefer that byte-preserving form so a
    caller can recover the original bytes via ``encode("utf-8","surrogateescape")``;
    fall back to ``str()`` when no chunks are available.
    """
    chunks = getattr(raw_source, "_chunks", None)
    if chunks:
        try:
            return "".join(text for text, _charset in chunks)
        except Exception:  # pragma: no cover - defensive
            pass
    return str(raw_source)


def _strict_filename(part: Message) -> str:
    """Return a part's filename, rejecting silent U+FFFD corruption.

    ``Message.get_filename()`` decodes an RFC 2231 ``filename*=UTF-8''…`` (and
    RFC 2047 encoded-word) parameter with the stdlib's ``errors="replace"``, so
    malformed inbound filename bytes come back as U+FFFD while the content
    checksum is stamped over intact bytes — silent metadata corruption of the
    class this engine guards. If the decoded filename carries a U+FFFD that its
    raw parameter source did not literally contain, treat it as corruption.
    Returns "" when the part has no filename.
    """
    filename = part.get_filename()
    # Run the strict encoded/extended-parameter check for EVERY part, before the
    # None short-circuit: an RFC 2047 encoded-word filename that decodes to
    # EMPTY (an erased/invalid word) leaves get_filename() returning None or "",
    # carrying no U+FFFD to trip a presence-gated check — so gating the strict
    # check on U+FFFD would silently accept an erased filename. The helper is a
    # no-op (returns True) when the part has no encoded/extended filename
    # parameter, so calling it unconditionally is safe.
    if not _rfc2231_filename_decodes_cleanly(part):
        raise MalformedMimeError(
            "attachment filename carries undecodable bytes (invalid RFC 2231/2047 encoding)"
        )
    if filename is None:
        return ""
    # A NUL byte is never valid in a filename — reject outright.
    if "\x00" in filename:
        raise MalformedMimeError(
            "attachment filename carries a NUL byte (invalid RFC 2231/2047 encoding)"
        )
    # get_filename() decodes an RFC 2231 ``filename*=UTF-8''…`` (or RFC 2047
    # encoded-word) parameter with the stdlib's ``errors="replace"``, so
    # malformed inbound filename bytes come back as U+FFFD while the content
    # checksum is stamped over intact bytes — silent metadata corruption. But a
    # U+FFFD the sender LITERALLY put in the (validly-encoded) filename is a
    # legitimate character, not corruption. The strict check above already
    # rejected a decode-introduced U+FFFD against the ORIGINAL undecoded
    # parameter; a U+FFFD that survives here is one the raw bytes validly
    # encoded, so it is kept.
    #
    # …EXCEPT a raw NON-encoded 8-bit filename (a plain ``filename="…"`` whose
    # bytes are neither RFC 2231 nor RFC 2047, e.g. raw ``\xff``): the default
    # policy decodes those to U+FFFD too, but they carry no extended/encoded
    # parameter for the check above to inspect. Recover the raw bytes from the
    # compat32-preserved parameter (surrogate-escape) and reject when they are
    # not valid UTF-8 — decode-introduced corruption, not a literal U+FFFD.
    if "\ufffd" in filename:
        raw_fn = _raw_filename_param(part)
        if raw_fn is not None:
            raw_bytes = raw_fn.encode("utf-8", "surrogateescape")
            try:
                raw_bytes.decode("utf-8", errors="strict")
                raw_valid_utf8 = True
            except UnicodeDecodeError:
                raw_valid_utf8 = False
            if not raw_valid_utf8 and "\ufffd" not in raw_fn:
                raise MalformedMimeError(
                    "attachment filename carries raw bytes invalid in their "
                    "charset (decoded to U+FFFD)"
                )
    return filename


def _raw_filename_param(part: Message) -> str | None:
    """Return a part's raw (compat32) plain ``filename=``/``name=`` value.

    Uses the raw header tuples so a raw 8-bit byte survives as a surrogate-escape
    code point (recoverable via ``encode("utf-8","surrogateescape")``), rather
    than the default policy's lossy U+FFFD. Mirrors ``get_filename()``'s
    selection order — Content-Disposition ``filename`` first, then Content-Type
    ``name`` only when the disposition has none — so the strict check inspects
    the SAME parameter the decoded filename came from, not an unrelated one.
    Returns ``None`` when the selected value is an RFC 2231 extended
    (``filename*=``) or RFC 2047 encoded-word form (those are validated by
    :func:`_rfc2231_filename_decodes_cleanly`) or absent.
    """

    def _match(raw: str, key: str) -> str | None:
        m = re.search(rf'{key}=\s*"([^"]*)"', raw, re.IGNORECASE)
        if m is None:
            m = re.search(rf"{key}=\s*([^;\r\n]+)", raw, re.IGNORECASE)
        return m.group(1) if m else None

    headers = {
        hname.lower(): str(hval)
        for hname, hval in getattr(part, "_headers", ())
        if hname.lower() in ("content-disposition", "content-type")
    }
    # Precedence matches email.message.get_filename: CD filename, then CT name.
    for hkey, pkey in (("content-disposition", "filename"), ("content-type", "name")):
        raw = headers.get(hkey)
        if raw is None:
            continue
        value = _match(raw, pkey)
        if value is None:
            continue
        # An encoded/extended form is validated by the strict param check.
        if "=?" in value and "?=" in value:
            return None
        return value
    return None


def _rfc2231_filename_decodes_cleanly(part: Message) -> bool:
    """Whether a part's extended/encoded filename parameters decode strictly.

    Covers BOTH encodings a filename can use:

    * RFC 2231 ``filename*=charset'lang'pct`` (plus continuation segments
      ``filename*0*`` / ``filename*1*`` / …), and
    * RFC 2047 encoded-words ``filename="=?charset?b?…?="`` in a plain
      ``filename=``/``name=`` value.

    Returns ``True`` when nothing is present to prove corrupt, or when every
    such parameter's bytes decode without error in the declared charset.
    Returns ``False`` only when some parameter's raw bytes are genuinely
    undecodable — the decode-introduced-U+FFFD case. RFC 2231 splits a long
    filename across numbered segments, so validating only the initial section
    would let a corrupt continuation (``filename*1*=%ff``) through; an RFC 2047
    encoded-word filename carries no extended parameter at all, so it must be
    strict-decoded here too or a malformed encoded-word slips past as U+FFFD.
    """
    # Read the RAW header tuples (``part._headers``): the parsed
    # ``part.get('Content-Disposition')`` refolds ``filename*`` into a decoded
    # ``filename="…"`` (already lossy to U+FFFD), whereas the raw tuple preserves
    # the original ``filename*=charset'lang'<pct-bytes>`` form we must inspect.
    raw_headers = []
    for hname, hval in getattr(part, "_headers", ()):
        if hname.lower() in ("content-disposition", "content-type"):
            raw_headers.append(str(hval))
    if not raw_headers:
        return True
    blob = " ".join(raw_headers)
    # The INITIAL section carries the charset: name*=charset'lang'pct  OR
    # name*0*=charset'lang'pct. Continuation sections are name*<n>*=pct (n>=1),
    # in the SAME charset, and MUST also be strict-decoded.
    init_pat = re.compile(
        r"(?:filename|name)\*(?:0\*)?=\s*([\w-]+)'[^']*'([^;\r\n]+)",
        re.IGNORECASE,
    )
    cont_pat = re.compile(
        r"(?:filename|name)\*[1-9][0-9]*\*=\s*([^;\r\n]+)",
        re.IGNORECASE,
    )
    for charset, first_pct in init_pat.findall(blob):
        segments = [first_pct.strip()]
        segments.extend(p.strip() for p in cont_pat.findall(blob))
        try:
            raw_bytes = b"".join(urllib.parse.unquote_to_bytes(seg) for seg in segments)
            raw_bytes.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            return False
    # RFC 2047 encoded-word filename: a plain filename=/name= value may be one
    # or more =?charset?enc?data?= words, POSSIBLY mixed with literal text
    # (e.g. `report-=?utf-8?b?…?=.txt`). get_filename() decodes each encoded-word
    # with errors="replace", so a malformed word yields U+FFFD; extract the whole
    # filename/name value and strict-decode EVERY encoded-word within it — a
    # whole-value-only match would miss a bad word embedded among literal text.
    fn_pat = re.compile(
        r'(?:filename|name)=\s*(?:"([^"]*)"|([^;\r\n]+))',
        re.IGNORECASE,
    )
    ew_word = re.compile(r"=\?[^?]+\?[bBqQ]\?[^?]*\?=")
    for quoted, bare in fn_pat.findall(blob):
        value = quoted or bare
        for ew in ew_word.findall(value):
            try:
                for chunk, cs in decode_header(ew):
                    if isinstance(chunk, (bytes, bytearray)):
                        if cs is not None and not chunk:
                            return False  # encoded-word decoded to empty = erased
                        chunk.decode(cs or "ascii", errors="strict")
            except (LookupError, UnicodeDecodeError, ValueError):
                return False
    return True


def _reject_serious_defects(msg: Message) -> None:
    """Raise :class:`MalformedMimeError` on a structurally-lying multipart.

    The email parser does not raise on a multipart whose boundary never
    appears; it records a defect and collapses the payload. A connector send
    path must treat that as malformed rather than silently losing the body, so
    this promotes the fatal defect classes to an exception. Base64 CTE defects
    are only appended AFTER a part is decoded, so they are additionally checked
    per-part in :func:`_decoded_payload`.

    The scan does NOT descend into a ``message/rfc822`` part: a forwarded
    message is an opaque attachment captured whole (bytes + checksum), so a
    defect INSIDE the forwarded message is the attachment's own content, not a
    lie about THIS message's structure — descending would wrongly discard a
    valid outer message because its attached ``.eml`` is malformed.
    """
    _scan_defects(msg)


def _scan_defects(part: Message) -> None:
    for defect in getattr(part, "defects", []) or []:
        if type(defect).__name__ in _FATAL_DEFECT_NAMES:
            raise MalformedMimeError(f"malformed multipart: {type(defect).__name__}")
    if part.get_content_type() == "message/rfc822":
        # Opaque attachment — do not descend into the forwarded message.
        return
    if part.is_multipart():
        payload = part.get_payload()
        if isinstance(payload, list):
            for child in payload:
                if isinstance(child, Message):
                    _scan_defects(child)


def _walk_parts(msg: Message, parsed: ParsedMessage, linesep: str = "\r\n") -> None:
    """Recurse the MIME tree, filling bodies / attachments / inline images.

    Recursion is explicit rather than via ``Message.walk()`` because ``walk()``
    descends INTO a ``message/rfc822`` attachment's own sub-tree, which would
    both lose the attached message as an attachment and let its nested body be
    mistaken for this message's body. Here a ``message/rfc822`` (or any
    ``multipart`` explicitly dispositioned as an attachment) is captured whole
    and NOT descended into.
    """
    if msg.is_multipart():
        disposition = (msg.get_content_disposition() or "").lower()
        maintype = msg.get_content_maintype()
        # Any message/* container (message/rfc822, and the message/delivery-
        # status or message/global that is the STANDARD inline body part of a
        # multipart/report bounce/DSN) is an embedded message, never part of
        # THIS message's own body structure — capture it whole rather than
        # descending, which would scatter/discard its blocks. An attachment-
        # dispositioned multipart is likewise captured whole.
        if maintype == "message" or disposition == "attachment":
            _collect_message_attachment(msg, parsed, linesep)
            return
        payload = msg.get_payload()
        if isinstance(payload, list):
            for sub in payload:
                if isinstance(sub, Message):
                    _walk_parts(sub, parsed, linesep)
        return

    _collect_leaf(msg, parsed)


def _collect_leaf(part: Message, parsed: ParsedMessage) -> None:
    """Classify and collect one non-multipart leaf part."""
    ctype = part.get_content_type()
    disposition = (part.get_content_disposition() or "").lower()
    content_id = part.get("Content-ID")
    filename = part.get_filename()

    # An image is inline only when it is NOT explicitly attachment-dispositioned.
    # An image carrying both Content-Disposition: attachment AND a Content-ID
    # (common in forwarded mail) is a real attachment the sender chose to name;
    # routing it to inline handling would make it vanish from `attachments`.
    #
    # A part carrying a FILENAME (Content-Type name= or Content-Disposition
    # filename=) is a named payload the sender means as a file, so it is an
    # attachment UNLESS explicitly inline — even a `text/plain; name="notes.txt"`
    # with no Content-Disposition. EXCEPTION: an image carrying a Content-ID is a
    # cid: referent the HTML body points at; a name= on it (common in real mail)
    # does not make it an attachment unless it is explicitly dispositioned
    # attachment — otherwise it drops out of inline_images and the cid: reference
    # fails to resolve. Without this a named text part would fall to the body
    # branch below and, if not the first body leaf, be silently lost.
    cid_image = ctype.startswith("image/") and content_id is not None
    is_attachment = disposition == "attachment" or (
        filename is not None and disposition != "inline" and not cid_image
    )
    is_inline_image = (
        ctype.startswith("image/")
        and not is_attachment
        and (disposition == "inline" or content_id is not None)
    )

    if is_inline_image:
        _collect_inline_image(part, parsed, content_id)
    elif is_attachment:
        _collect_attachment(part, parsed)
    elif ctype == "text/plain":
        # Decode-validate EVERY text/plain leaf (not only the first): a later
        # leaf's invalid base64 CTE defect is appended only on decode, so a leaf
        # we skip for its body would still smuggle silent corruption past the
        # pre-decode _reject_serious_defects sweep. Decode it either way; keep
        # the FIRST as the body, and capture any SUBSEQUENT text/plain leaf as an
        # attachment rather than dropping its content (a second body leaf outside
        # a multipart/alternative is real content the sender sent).
        decoded = _decode_text(part)
        if parsed.text_body is None:
            parsed.text_body = decoded
        else:
            _collect_attachment(part, parsed)
    elif ctype == "text/html":
        decoded_html = _decode_text(part)
        if parsed.html_body is None:
            parsed.html_body = decoded_html
        else:
            _collect_attachment(part, parsed)
    elif ctype not in ("text/plain", "text/html"):
        # Any remaining non-text leaf — including one with a non-attachment
        # disposition such as Content-Disposition: inline on a non-image type
        # (e.g. an inline application/pdf), which matches none of the branches
        # above — is captured as an attachment so its bytes are never silently
        # dropped. (An inline IMAGE was already routed to _collect_inline_image,
        # and an explicit attachment disposition to _collect_attachment.)
        _collect_attachment(part, parsed)


def _embedded_message_bytes(part: Message, linesep: str = "\r\n") -> bytes:
    """Serialize the message EMBEDDED in a ``message/rfc822`` part, faithfully.

    ``Message.as_bytes()`` on the container is wrong for two reasons the review
    caught: it prepends the CONTAINER's own headers (``Content-Type:
    message/rfc822`` etc.) to the bytes, and it re-serializes through a policy
    that normalizes CRLF -> LF and can refold headers — so a checksum over it is
    a checksum over a rewritten form, not the forwarded message. Serialize the
    embedded message itself (``get_payload()[0]``) with a generator using the
    SOURCE message's own line ending (``linesep``, detected from the wire), so
    the attachment holds the forwarded ``.eml`` content as-is — an LF-only
    source stays LF-only, a CRLF source stays CRLF.
    """
    payload = part.get_payload()
    if (
        part.get_content_type() in ("message/rfc822", "message/global")
        and isinstance(payload, list)
        and payload
        and isinstance(payload[0], Message)
    ):
        inner = payload[0]
        # A message/rfc822 part may carry a base64 transfer-encoding that
        # encoded the ENTIRE embedded message. Python then wraps the still-
        # encoded body as an inner Message whose payload is the raw base64
        # STRING (with no inner headers), so flattening it would capture opaque
        # base64 text rather than the forwarded .eml — silent corruption. When
        # the CONTAINER declares base64, decode that string back to the real
        # inner message bytes, which are already the faithful wire form. The
        # decode is STRICT: out-of-alphabet or truncated base64 must raise
        # MalformedMimeError, never be silently dropped and stamped with a valid
        # checksum over the corrupted bytes (the class this engine guards).
        container_cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        inner_body = inner.get_payload()
        if container_cte == "base64" and isinstance(inner_body, str):
            try:
                data = inner_body.encode("ascii", errors="strict")
                # Standard MIME base64 is LINE-WRAPPED (CRLF every ~76 chars),
                # and CR/LF/space/tab are out-of-alphabet under validate=True.
                # Strip that permitted whitespace so line-wrapping is accepted,
                # while validate=True still rejects genuinely out-of-alphabet or
                # truncated content (the corruption class this guards).
                data = data.translate(None, b"\r\n\t ")
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
                raise MalformedMimeError(
                    f"embedded message/rfc822 has invalid base64 body: {exc}"
                ) from exc
            return decoded
        buf = io.BytesIO()
        # compat32 with the SOURCE line ending serializes without the header
        # refolding the default policy applies, preserving the wire form.
        gen = BytesGenerator(
            buf,
            mangle_from_=False,
            maxheaderlen=0,
            policy=compat32_policy.clone(linesep=linesep),
        )
        gen.flatten(inner)
        return buf.getvalue()
    # A non-rfc822 message/* attachment (e.g. message/delivery-status, common
    # in bounce/DSN mail): its real payload is the embedded status message(s),
    # NOT the container. Flattening the container would prepend its own
    # Content-Type/Content-Disposition wrapper headers into the captured bytes,
    # so the checksum would cover the wrapper, not the payload — silent
    # corruption. Serialize only the inner payload message(s), CRLF-faithfully.
    if part.get_content_maintype() == "message":
        # A base64-encoded message/* container (any subtype, e.g.
        # message/global) has a RAW base64 STRING payload, not a parsed inner
        # message; flattening it would capture opaque base64 text — the same
        # silent corruption the message/rfc822 branch above guards. Decode the
        # container's base64 body back to the embedded bytes, STRICTLY.
        container_cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        body = part.get_payload()
        if container_cte == "base64" and isinstance(body, str):
            try:
                data = body.encode("ascii", errors="strict").translate(None, b"\r\n\t ")
                return base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
                raise MalformedMimeError(
                    f"embedded {part.get_content_type()} has invalid base64 body: {exc}"
                ) from exc
        inner_payload = part.get_payload()
        if isinstance(inner_payload, list) and inner_payload:
            buf = io.BytesIO()
            gen = BytesGenerator(
                buf,
                mangle_from_=False,
                maxheaderlen=0,
                policy=compat32_policy.clone(linesep=linesep),
            )
            sep = linesep.encode("ascii")
            for i, sub in enumerate(inner_payload):
                if i:
                    buf.write(sep)
                gen.flatten(sub)
            return buf.getvalue()
    # Fallback: an attachment-dispositioned multipart that is NOT
    # message/rfc822 (e.g. multipart/appledouble, a forwarded raw multipart).
    # Serialize the WHOLE part — all children — CRLF-faithfully, so no child is
    # lost. (Gating the branch above on message/rfc822 is what makes this
    # reachable for multiparts; otherwise only the first child would survive.)
    buf = io.BytesIO()
    gen = BytesGenerator(
        buf,
        mangle_from_=False,
        maxheaderlen=0,
        policy=compat32_policy.clone(linesep=linesep),
    )
    gen.flatten(part)
    return buf.getvalue()


def _collect_message_attachment(
    part: Message, parsed: ParsedMessage, linesep: str = "\r\n"
) -> None:
    """Capture an attached ``message/rfc822`` (or attachment multipart) whole.

    The embedded message's own bytes become the attachment content (see
    :func:`_embedded_message_bytes` for why the container's ``as_bytes()`` is
    not used), so the forwarded message survives faithfully with a checksum, and
    its inner parts are never mistaken for this message's own body.
    """
    try:
        content = _embedded_message_bytes(part, linesep)
    except Exception as exc:
        raise MalformedMimeError(f"could not serialize embedded message attachment: {exc}") from exc
    filename = _strict_filename(part) or "attached-message.eml"
    try:
        parsed.attachments.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=part.get_content_type(),
            )
        )
    except (MalformedMimeError, AttachmentTooLargeError):
        raise
    except Exception as exc:
        raise MalformedMimeError(f"could not extract embedded message attachment: {exc}") from exc


def _decoded_payload(part: Message) -> bytes:
    """Decode a leaf's payload and reject post-decode corruption.

    ``get_payload(decode=True)`` applies the part's Content-Transfer-Encoding.
    For an invalid base64 body the stdlib does NOT raise: it silently returns
    the raw (undecoded) bytes and APPENDS a base64 defect to ``part.defects``
    only after this call. The parse-time :func:`_reject_serious_defects` sweep
    runs before any part is decoded, so it cannot see that defect — this
    re-checks the part's defects immediately after decoding it, reusing the
    same fatal-defect set, so silent CTE corruption becomes a
    :class:`MalformedMimeError` rather than a checksum over garbage.
    """
    payload = part.get_payload(decode=True)
    for defect in getattr(part, "defects", []) or []:
        if type(defect).__name__ in _FATAL_DEFECT_NAMES:
            raise MalformedMimeError(
                f"malformed content-transfer-encoding: {type(defect).__name__}"
            )
    if not isinstance(payload, (bytes, bytearray)):
        return b""
    return bytes(payload)


def _decode_text(part: Message) -> str:
    """Decode a text leaf using its declared charset — STRICTLY.

    Invalid bytes for the declared charset are NOT silently replaced: silent
    replacement returns corrupted content plus a checksum over that corruption,
    reported as success. A decode failure is raised as
    :class:`MalformedMimeError` so the caller sees the corruption instead of a
    plausible-but-wrong body. An unknown charset LABEL (as opposed to invalid
    bytes) falls back to utf-8 strict, since the label may simply be
    non-standard while the bytes are fine.
    """
    data = _decoded_payload(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset)
    except LookupError:
        # Unknown charset label — try utf-8 strictly rather than assuming.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedMimeError(
                f"body declares unknown charset {charset!r} and is not utf-8: {exc}"
            ) from exc
    except UnicodeDecodeError as exc:
        raise MalformedMimeError(f"body is not valid {charset}: {exc}") from exc


def _collect_attachment(part: Message, parsed: ParsedMessage) -> None:
    content = _decoded_payload(part)
    filename = _strict_filename(part)
    try:
        parsed.attachments.append(
            Attachment(
                filename=filename,
                content=content,
                content_type=part.get_content_type(),
            )
        )
    except (MalformedMimeError, AttachmentTooLargeError):
        # A too-large attachment is its own typed error; do not rewrite it as
        # malformed (a caller distinguishes size-cap from corruption).
        raise
    except Exception as exc:
        raise MalformedMimeError(f"could not extract attachment: {exc}") from exc


def _collect_inline_image(part: Message, parsed: ParsedMessage, content_id: str | None) -> None:
    content = _decoded_payload(part)
    cid = (content_id or "").strip().strip("<>").strip()
    if not cid:
        # An inline image with no usable Content-ID cannot be referenced from
        # the body; treat it as a regular attachment so its bytes survive.
        _collect_attachment(part, parsed)
        return
    try:
        parsed.inline_images.append(
            InlineImage(
                content_id=cid,
                content=content,
                content_type=part.get_content_type(),
                filename=_strict_filename(part),
            )
        )
    except (MalformedMimeError, AttachmentTooLargeError):
        raise
    except Exception as exc:
        raise MalformedMimeError(f"could not extract inline image: {exc}") from exc


def require_headers(parsed: ParsedMessage, names: list[str]) -> None:
    """Raise :class:`MissingHeaderError` if any named header is absent/empty.

    A separate step from parsing: a message can be valid MIME yet lack a header
    a particular caller's contract requires (e.g. a send path requiring
    ``From`` and a recipient). This distinguishes "not valid MIME" (parse
    raises) from "valid MIME, missing what I need" (this raises).
    """
    for name in names:
        if name.lower() == "to":
            if not parsed.to:
                raise MissingHeaderError("missing required header: To")
            continue
        if name.lower() == "cc":
            if not parsed.cc:
                raise MissingHeaderError("missing required header: Cc")
            continue
        if name.lower() == "bcc":
            # A parsed Bcc header is surfaced on parsed.bcc, a dedicated
            # recipient field, rather than in parsed.headers (which holds the
            # single-value headers), so check there.
            if not parsed.bcc:
                raise MissingHeaderError("missing required header: Bcc")
            continue
        if not parsed.headers.get(name):
            raise MissingHeaderError(f"missing required header: {name}")
