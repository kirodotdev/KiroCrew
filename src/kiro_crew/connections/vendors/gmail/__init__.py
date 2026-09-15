"""Gmail MIME parsing and construction engine (``G1``) — pure logic, zero auth.

This package is the Gmail-side wire-format engine for the connector campaign's
Google stream (``W03``). It parses and constructs RFC 5322 / MIME messages and
the base64url ``raw`` form Gmail ``users.messages.send`` uses, with NO
dependency on the shared control plane (``kiro_crew.connections``) that ``W01``
owns and NO live Gmail call anywhere. Every function here operates on bytes and
data the caller supplies; real authorization is wired against ``W01``'s named
interfaces in a later slice.

Public surface
--------------

Construction:
    :func:`build_message` → :class:`BuiltMessage` (serialized bytes + envelope
    recipients, with ``Bcc`` kept out of the header block).

Parsing:
    :func:`parse_message` → :class:`ParsedMessage`; :func:`require_headers` for
    the "valid MIME but missing a required header" check.

Addressing / aliases:
    :class:`Mailbox`, :class:`RecipientSet`, :func:`validate_send_as`,
    :class:`SendAsAlias`.

Attachments / inline images:
    :class:`Attachment`, :class:`InlineImage`, :func:`sha256_hex`.

Threading:
    :func:`reply_headers`, :func:`add_reply_prefix`,
    :func:`build_references_chain`, :class:`ThreadHeaders`.

Raw wire form:
    :func:`encode_raw`, :func:`decode_raw`.

Errors:
    :class:`MimeError` and its subclasses.
"""

from __future__ import annotations

from kiro_crew.connections.vendors.gmail.addresses import (
    Mailbox,
    RecipientSet,
    SendAsAlias,
    format_mailbox_list,
    parse_mailbox_list,
    validate_send_as,
)
from kiro_crew.connections.vendors.gmail.attachments import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    Attachment,
    InlineImage,
    sha256_hex,
)
from kiro_crew.connections.vendors.gmail.builder import BuiltMessage, build_message
from kiro_crew.connections.vendors.gmail.errors import (
    AttachmentTooLargeError,
    InvalidAliasError,
    MalformedMimeError,
    MimeError,
    MissingHeaderError,
)
from kiro_crew.connections.vendors.gmail.parser import (
    ParsedMessage,
    parse_message,
    require_headers,
)
from kiro_crew.connections.vendors.gmail.raw import decode_raw, encode_raw
from kiro_crew.connections.vendors.gmail.threading import (
    ThreadHeaders,
    add_reply_prefix,
    build_references_chain,
    reply_headers,
)

__all__ = [
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "Attachment",
    "AttachmentTooLargeError",
    "BuiltMessage",
    "InlineImage",
    "InvalidAliasError",
    "Mailbox",
    "MalformedMimeError",
    "MimeError",
    "MissingHeaderError",
    "ParsedMessage",
    "RecipientSet",
    "SendAsAlias",
    "ThreadHeaders",
    "add_reply_prefix",
    "build_message",
    "build_references_chain",
    "decode_raw",
    "encode_raw",
    "format_mailbox_list",
    "parse_mailbox_list",
    "parse_message",
    "reply_headers",
    "require_headers",
    "sha256_hex",
    "validate_send_as",
]
