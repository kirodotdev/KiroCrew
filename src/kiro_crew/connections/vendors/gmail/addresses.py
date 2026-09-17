"""Recipient addressing and ``sendAs`` alias validation — pure logic.

Two concerns live here, both format-only and free of any network call:

* **Address list handling.** ``To`` / ``Cc`` / ``Bcc`` are parsed from and
  serialized to RFC 5322 header text, with display names carrying non-ASCII
  characters encoded as RFC 2047 encoded-words on the way out and decoded on
  the way in. All three classes — ``Bcc`` included — are written into the
  built message by the builder, because Gmail's ``messages.send`` delivers
  blind copies from the ``Bcc`` header in ``raw`` (see :mod:`.builder`).

* **``sendAs`` alias validation.** Gmail lets an account send *as* one of a set
  of verified alias addresses. This module's :func:`validate_send_as` is the
  pure gate: given the requested ``From`` and the account's declared alias set,
  it either returns the matched, canonicalized alias or raises
  :class:`InvalidAliasError`. It performs NO real Gmail call — wiring a live
  ``sendAs.list`` lookup is a later, ``W01``-mediated concern; here the alias
  set is data the caller supplies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.header import Header, decode_header
from email.headerregistry import Address
from email.utils import getaddresses

from kiro_crew.connections.vendors.gmail.errors import (
    InvalidAliasError,
    MalformedMimeError,
)

# A deliberately permissive-but-real address shape: a local part, one ``@``,
# and a domain. The local part is EITHER a normal dot-atom (no spaces, angle
# brackets, quotes, or commas — a bare comma is an address-LIST separator that
# would split the header) OR an RFC 5322 quoted-string. The domain is EITHER a
# dotted name (at least one label pair), a SINGLE-LABEL domain (``localhost``,
# ``root@localhost`` — legal and routine on internal/intranet delivery),
# OR an RFC 5321 domain-literal ``[...]`` (e.g. ``user@[192.168.0.1]`` /
# ``user@[IPv6:2001:db8::1]``), which is legal and producible by any inbound
# message. This regex is only a cheap first gate; ``Mailbox.__post_init__``
# additionally constructs a stdlib ``Address`` from the addr_spec, so shapes
# the regex admits but the RFC grammar rejects (a consecutive-dot local part,
# CR/LF injection) are still refused. Full RFC 5322 grammar is larger than any
# single regex; this rejects the shapes that matter for a send path without
# pretending to be a complete grammar.
_ADDR_SPEC_RE = re.compile(
    r"^(?:\"(?:[^\"\\]|\\.)+\"|[^\s<>@\",]+)@(?:\[[^\[\]]+\]|[^\s<>@,]+)$",
)


@dataclass(frozen=True)
class Mailbox:
    """One addressee: an ``addr_spec`` plus an optional display name.

    ``display_name`` may hold non-ASCII text; :meth:`format` emits it as an
    RFC 2047 encoded-word so the serialized header stays 7-bit clean.
    """

    address: str
    display_name: str = ""

    def __post_init__(self) -> None:
        addr = self.address.strip()
        # Reject CR/LF anywhere in the address OR display name before any other
        # check: a newline is the classic header-injection vector, and even the
        # quoted-local-part form of _ADDR_SPEC_RE would otherwise admit one (a
        # negated character class matches CR/LF in Python re). A Mailbox that
        # passed validation with an embedded newline would crash later at
        # header serialization (Address raises an uncaught ValueError), so it is
        # refused here as an invalid address rather than deferred to a crash.
        if any(c in addr for c in "\r\n") or any(c in self.display_name for c in "\r\n"):
            raise InvalidAliasError(
                f"address or display name contains a line break: {self.address!r}"
            )
        if not _ADDR_SPEC_RE.match(addr):
            raise InvalidAliasError(f"invalid email address: {self.address!r}")
        # The regex is a cheap first gate but does not model the full RFC 5322
        # local-part grammar: it admits shapes like a consecutive-dot local
        # part ("a..b@example.com") that the stdlib's Address — which the
        # builder constructs from this addr_spec via as_address() — rejects with
        # an uncaught InvalidHeaderDefect at header serialization. Construct the
        # Address here so that failure surfaces as an InvalidAliasError at
        # Mailbox creation (fail-closed, translated) rather than crashing the
        # build later. Address is only VALIDATED here; the stored address stays
        # the caller's normalized addr_spec.
        try:
            Address(display_name=self.display_name.strip(), addr_spec=addr)
        except Exception as exc:  # HeaderParseError / InvalidHeaderDefect / ValueError
            raise InvalidAliasError(f"invalid email address: {self.address!r}: {exc}") from exc
        # Normalize the stored address (strip surrounding whitespace) without
        # mutating the caller's original object semantics — frozen dataclass, so
        # go through object.__setattr__.
        object.__setattr__(self, "address", addr)
        object.__setattr__(self, "display_name", self.display_name.strip())

    def format(self) -> str:
        """Serialize to header form, RFC 2047-encoding a non-ASCII name."""
        if not self.display_name:
            return self.address
        try:
            self.display_name.encode("ascii")
            # Pure-ASCII name: quote it if it contains specials.
            return str(Address(display_name=self.display_name, addr_spec=self.address))
        except UnicodeEncodeError:
            encoded = Header(self.display_name, "utf-8").encode()
            return f"{encoded} <{self.address}>"

    def as_address(self) -> Address:
        """Return an :class:`email.headerregistry.Address`.

        Assigning this to an ``EmailMessage`` header lets the modern email
        policy RFC 2047-encode a non-ASCII display name AND fold it correctly.
        Pre-encoding with ``Header(...).encode()`` and assigning the resulting
        string instead inserts hard newlines that the setter rejects with
        ``ValueError`` for an ordinary-length Unicode name, so the builder uses
        this Address form.
        """
        return Address(display_name=self.display_name, addr_spec=self.address)


@dataclass
class RecipientSet:
    """The three recipient classes of one outgoing message.

    ``To``, ``Cc`` AND ``Bcc`` are all written into the built message's header
    block. On Gmail's ``users.messages.send`` path this is not a leak but the
    delivery mechanism: that API accepts only a ``raw`` field and derives every
    recipient — blind copies included — from the message's ``To`` / ``Cc`` /
    ``Bcc`` headers. Gmail then strips the ``Bcc`` header from the copies it
    delivers, so the blind-copy privacy the SMTP world enforces with a separate
    envelope is enforced here by the provider, on a header that MUST be present
    in ``raw`` for those recipients to receive anything at all. Reference:
    https://developers.google.com/gmail/api/reference/rest/v1/users.messages/send
    ("Sends the specified message to the recipients in the ``To``, ``Cc``, and
    ``Bcc`` headers.")
    """

    to: list[Mailbox] = field(default_factory=list)
    cc: list[Mailbox] = field(default_factory=list)
    bcc: list[Mailbox] = field(default_factory=list)

    def header_addresses(self) -> list[tuple[str, list[Address]]]:
        """Every recipient header to write into the message, as ``Address`` lists.

        ``To``, ``Cc`` and ``Bcc`` — all three. They are handed to the builder
        as ``Address`` objects so ``EmailMessage``'s policy encodes and folds
        non-ASCII display names safely (a pre-encoded string crashes the setter
        on an ordinary Unicode name). ``Bcc`` is included because Gmail's
        ``messages.send`` delivers blind copies from the ``Bcc`` header in
        ``raw``; omitting it would silently drop those recipients.
        """
        headers: list[tuple[str, list[Address]]] = []
        if self.to:
            headers.append(("To", [b.as_address() for b in self.to]))
        if self.cc:
            headers.append(("Cc", [b.as_address() for b in self.cc]))
        if self.bcc:
            headers.append(("Bcc", [b.as_address() for b in self.bcc]))
        return headers

    def has_recipients(self) -> bool:
        """Whether the set names at least one recipient in any class.

        The builder rejects a message with no recipient at all. There is no
        separate delivery-address list to return: on the ``messages.send`` path
        the recipients ARE the To/Cc/Bcc headers now written into ``raw``, so a
        method that returned a synthetic union would be a fake API describing a
        transport channel that does not exist.
        """
        return bool(self.to or self.cc or self.bcc)


def format_mailbox_list(boxes: list[Mailbox]) -> str:
    """Serialize a list of mailboxes to a comma-separated header value."""
    return ", ".join(box.format() for box in boxes)


def parse_mailbox_list(header_value: str) -> list[Mailbox]:
    """Parse an address-list header value into :class:`Mailbox` objects.

    Decodes RFC 2047 encoded-word display names back to their Unicode text.
    Skips entries with no ``addr_spec`` (a bare group name, a stray comma)
    rather than raising, so a tolerant parse of a real-world header does not
    fail on cosmetic noise — a caller that needs strictness checks the returned
    list length against expectation.
    """
    out: list[Mailbox] = []
    for raw_name, addr in getaddresses([header_value]):
        if not addr or "@" not in addr:
            continue
        name = _decode_2047(raw_name)
        try:
            out.append(Mailbox(address=addr, display_name=name))
        except InvalidAliasError:
            # A header value can carry a malformed token; a tolerant list parse
            # drops it rather than failing the whole parse.
            continue
    return out


def _decode_2047(value: str) -> str:
    """Decode any RFC 2047 encoded-words in a header fragment to Unicode.

    Decodes STRICTLY: an encoded-word whose bytes are not valid in its declared
    charset is a :class:`MalformedMimeError`, never silently replaced with
    U+FFFD. This matches the strict single-value-header path — a malformed
    recipient display name from an external sender must surface as corruption,
    not be reported as a plausible-but-wrong name.
    """
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, (bytes, bytearray)):
            # An encoded-word with invalid base64/QP decodes to EMPTY bytes
            # without raising, silently erasing the display name. Treat that as
            # corruption, matching the strict single-value-header path.
            if charset is not None and not chunk and "=?" in value:
                raise MalformedMimeError(
                    "address display name carries an encoded-word that "
                    "decoded to empty (invalid encoding)"
                )
            try:
                decoded.append(bytes(chunk).decode(charset or "ascii", errors="strict"))
            except (LookupError, UnicodeDecodeError) as exc:
                raise MalformedMimeError(
                    f"address display name carries an undecodable " f"encoded-word: {exc}"
                ) from exc
        else:
            decoded.append(_recover_raw_utf8(chunk))
    return "".join(decoded)


def _recover_raw_utf8(text: str) -> str:
    """Recover a raw UTF-8 (SMTPUTF8) display name from a compat32 fragment.

    compat32 exposes non-encoded-word header bytes as a str decoded byte-for-
    byte (latin-1), so a raw UTF-8 display name (e.g. ``张三`` sent without RFC
    2047 encoding) arrives as mojibake. If the fragment is pure ASCII it is
    already correct; otherwise, when its latin-1 bytes are valid UTF-8, re-decode
    them so the real characters are restored. A fragment whose bytes are NOT
    valid UTF-8 is left as-is (it may be a legitimate latin-1 name), never
    forced through ``errors="replace"``.
    """
    try:
        text.encode("ascii")
        return text  # pure ASCII: nothing to recover
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("latin-1").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


@dataclass(frozen=True)
class SendAsAlias:
    """One entry in an account's ``sendAs`` alias set.

    Mirrors the fields of Gmail's ``sendAs`` resource that matter to a pure
    validation decision. ``is_verified`` is the account-side verification state
    (an unverified alias cannot be used as ``From``).

    ``is_verified`` defaults to ``False`` (fail-closed): the only safe default
    for a sender-identity gate is to treat an alias as unverified until the
    caller proves otherwise. A future wiring author who maps ``sendAs.list``
    and forgets to carry the verification flag then gets a REJECTED alias, not
    a silently-accepted unverified sender (the spoofing class this exists to
    stop).
    """

    send_as_email: str
    is_verified: bool = False


def validate_send_as(
    requested_from: str,
    aliases: list[SendAsAlias],
) -> str:
    """Validate a requested ``From`` against the account's ``sendAs`` set.

    Returns the canonical alias address on success. Raises
    :class:`InvalidAliasError` when the requested address is syntactically
    invalid, is not present in ``aliases`` at all, or is present but not
    verified. Comparison is case-insensitive on the whole addr_spec, matching
    how mail systems treat address equality for this purpose.

    This is validation ONLY. It issues no Gmail call; ``aliases`` is the set
    the caller obtained through whatever authorized path exists — a later slice
    supplies it from a real ``sendAs.list`` behind ``W01``.
    """
    candidate = requested_from.strip()
    if not _ADDR_SPEC_RE.match(candidate):
        raise InvalidAliasError(f"invalid From address: {requested_from!r}")

    wanted = candidate.lower()
    for alias in aliases:
        if alias.send_as_email.strip().lower() == wanted:
            # Require the boolean True identity, not merely a truthy value: a
            # non-bool truthy field (e.g. the string "false", or any non-empty
            # string a caller mis-populated from JSON) must NOT pass a
            # sender-identity gate — fail closed unless verification is
            # explicitly the boolean True.
            if alias.is_verified is not True:
                raise InvalidAliasError(f"sendAs alias {alias.send_as_email!r} is not verified")
            return alias.send_as_email.strip()

    raise InvalidAliasError(
        f"From {requested_from!r} is not a verified sendAs alias for this account"
    )
