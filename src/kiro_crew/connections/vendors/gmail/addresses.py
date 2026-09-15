"""Recipient addressing and ``sendAs`` alias validation — pure logic.

Two concerns live here, both format-only and free of any network call:

* **Address list handling.** ``To`` / ``Cc`` / ``Bcc`` are parsed from and
  serialized to RFC 5322 header text, with display names carrying non-ASCII
  characters encoded as RFC 2047 encoded-words on the way out and decoded on
  the way in. ``Bcc`` is modelled here but the builder is what guarantees it
  never reaches the wire header block (see :mod:`.builder`).

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

from kiro_crew.connections.vendors.gmail.errors import InvalidAliasError

# A deliberately permissive-but-real address shape: one ``@``, a non-empty
# local part with no spaces or angle brackets, and a domain with at least one
# dot-separated label pair. Full RFC 5322 address grammar is famously larger
# than any single regex; this rejects the shapes that matter for a send path
# (empty, no ``@``, no domain, embedded whitespace) without pretending to be a
# complete grammar.
_ADDR_SPEC_RE = re.compile(
    r"^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$",
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
        if not _ADDR_SPEC_RE.match(addr):
            raise InvalidAliasError(f"invalid email address: {self.address!r}")
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


@dataclass
class RecipientSet:
    """The three recipient classes of one outgoing message.

    ``bcc`` is held here but MUST NOT be serialized into the message's own
    header block — see :mod:`.builder`, which reads ``bcc`` only to compute the
    envelope recipient list and never writes a ``Bcc:`` header into the sent
    body. That invariant is what stops a blind-copy from leaking to every
    visible recipient.
    """

    to: list[Mailbox] = field(default_factory=list)
    cc: list[Mailbox] = field(default_factory=list)
    bcc: list[Mailbox] = field(default_factory=list)

    def visible_header_pairs(self) -> list[tuple[str, str]]:
        """The (header-name, header-value) pairs safe to write into the body.

        ``To`` and ``Cc`` only. ``Bcc`` is deliberately excluded — this is the
        single choke point the leak-prevention invariant depends on.
        """
        pairs: list[tuple[str, str]] = []
        if self.to:
            pairs.append(("To", format_mailbox_list(self.to)))
        if self.cc:
            pairs.append(("Cc", format_mailbox_list(self.cc)))
        return pairs

    def envelope_recipients(self) -> list[str]:
        """Every addr_spec that should actually receive the message.

        Union of To + Cc + Bcc, de-duplicated, order-preserving. This is the
        envelope the transport delivers to; the Bcc addresses appear here (they
        must receive the mail) but never in :meth:`visible_header_pairs`.
        """
        seen: set[str] = set()
        out: list[str] = []
        for box in [*self.to, *self.cc, *self.bcc]:
            if box.address not in seen:
                seen.add(box.address)
                out.append(box.address)
        return out


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
    """Decode any RFC 2047 encoded-words in a header fragment to Unicode."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "ascii", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


@dataclass(frozen=True)
class SendAsAlias:
    """One entry in an account's ``sendAs`` alias set.

    Mirrors the fields of Gmail's ``sendAs`` resource that matter to a pure
    validation decision. ``is_verified`` is the account-side verification state
    (an unverified alias cannot be used as ``From``); ``treat_as_default`` is
    carried for completeness and does not affect validation.
    """

    send_as_email: str
    is_verified: bool = True
    treat_as_default: bool = False


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
            if not alias.is_verified:
                raise InvalidAliasError(f"sendAs alias {alias.send_as_email!r} is not verified")
            return alias.send_as_email.strip()

    raise InvalidAliasError(
        f"From {requested_from!r} is not a verified sendAs alias for this account"
    )
