"""Typed errors for the Gmail MIME engine.

Every failure mode a caller must distinguish gets its own subclass so callers
switch on the exception TYPE, never on a substring of its message. The message
text itself carries the specific detail (which header was missing, which alias
was rejected, by how much a size cap was exceeded) for logs and tests.

All engine-raised errors inherit :class:`MimeError`, so a caller that only
needs "the engine refused this input" catches one base class.
"""

from __future__ import annotations


class MimeError(Exception):
    """Base class for every error raised by the Gmail MIME engine."""


class MalformedMimeError(MimeError):
    """The bytes handed to the parser are not a MIME message it can decode.

    Raised for structurally broken input: a multipart part with no boundary, a
    truncated body, a header block that cannot be parsed, an unparseable
    Content-Transfer-Encoding, and similar defects. This is distinct from a
    message that parses cleanly but is missing a header the caller required —
    that is :class:`MissingHeaderError`.
    """


class MissingHeaderError(MimeError):
    """A header the caller declared required was absent from the message.

    Which header is named in the message. Kept separate from
    :class:`MalformedMimeError` because a message can be perfectly well-formed
    MIME and still lack, say, a ``From`` the sender contract requires.
    """


class InvalidAliasError(MimeError):
    """A ``sendAs`` ``From`` value failed alias validation.

    Raised by the pure validation entry point when the requested ``From`` is
    not an address on the account's verified ``sendAs`` alias set, when the
    alias exists but is not in a usable/verified state, or when the address
    itself is syntactically invalid. Carries no network semantics — validation
    is a local decision over an alias set the caller supplies.
    """


class AttachmentTooLargeError(MimeError):
    """An attachment (or the assembled message) exceeded a declared size cap.

    The message records the offending size and the cap so a caller can surface
    both. Per-attachment validation happens at construction time, before that
    part is base64-expanded; the assembled-message total is additionally
    checked after serialization (that check necessarily runs post-encode).
    """
