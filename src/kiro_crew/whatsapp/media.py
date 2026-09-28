"""What arrived in one inbound WhatsApp message, decided from the protobuf alone.

The Web protocol delivers every kind of content as one ``waE2E.Message``, and the
answer to "is there a picture here" is a **presence** question about a field of
that message, not a text question. This module owns that reading: it unwraps the
carriers WhatsApp nests content in, names the kind that arrived, and hands back
the caption the user typed. Nothing here downloads bytes and nothing here builds
an :class:`kiro_crew.messaging.attachments.Attachment`: fetching needs the live
client, and the ingest half is channel-neutral and already written.

**Presence, never truthiness.** A singular protobuf message field is NEVER
``None``: reading an absent one materializes a default instance, so
``getattr(msg, "imageMessage", None) is not None`` is True for every message ever
received, and ``if msg.imageMessage:`` is False for a real image whose every
field happens to be default. ``HasField`` is the only correct probe, and it
raises ``ValueError`` for a field this build's schema does not define. Every read
here goes through :func:`_has`, which turns that raise into "absent", so a
message minted by a newer WhatsApp degrades to :data:`KIND_UNSUPPORTED` instead
of taking the channel's inbound path down.

**Carriers are found by shape, not by a list.** WhatsApp wraps content in a
``FutureProofMessage`` (``ephemeralMessage``, ``viewOnceMessageV2``,
``documentWithCaptionMessage`` and two dozen more) and in a handful of one-off
holders (``deviceSentMessage``, ``commentMessage``). The rule that identifies all
of them is structural: a submessage that itself carries a ``Message``. That is
read off the schema at first use, so a carrier added by a later ``neonize``
unwraps with no code change; :data:`CARRIER_FIELDS` is the floor for when the
descriptor cannot be read. The walk is bounded by :data:`MAX_UNWRAP_DEPTH` so a
hand-nested message cannot make it spin.

**Video is refused before it is downloaded.** kiro-cli advertises
``promptCapabilities.image`` only, so the shared ingest layer rejects video after
fetching it. Here the declared kind is known from the envelope, so a video is
named unsupported for the cost of a field read rather than of a multi-megabyte
download on the gateway's single event loop.

**Imports: none.** ``whatsapp`` is reachable from ``slack/gateway.py`` ->
``channels.py`` at import time, so a module-top ``neonize`` import would load the
Go core on every operator's boot whether or not this channel is enabled. Types
are ``Any`` and the schema is read from the message's own descriptor, which costs
nothing at import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Kinds ───────────────────────────────────────────────────────────────────
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_VOICE = "voice"
KIND_VIDEO = "video"
KIND_DOCUMENT = "document"
KIND_STICKER = "sticker"
KIND_REACTION = "reaction"
KIND_LOCATION = "location"
KIND_CONTACTS = "contacts"
KIND_POLL = "poll"
KIND_UNSUPPORTED = "unsupported"

#: Kinds whose bytes are worth fetching, because
#: :func:`kiro_crew.messaging.attachments.ingest_attachments` can turn them into
#: something the model consumes. Video is deliberately absent (see the module
#: docstring): its bytes are the largest and its rejection is certain.
INGESTIBLE_KINDS = frozenset({KIND_IMAGE, KIND_STICKER, KIND_AUDIO, KIND_VOICE, KIND_DOCUMENT})

# ── Carriers ────────────────────────────────────────────────────────────────

#: Carrier layers one message may be wrapped in before the walk gives up.
#: Observed chains reach three (``ephemeralMessage`` > ``viewOnceMessageV2`` >
#: ``documentWithCaptionMessage``), so this leaves room for a layer WhatsApp has
#: not shipped yet while keeping the walk bounded: the input is a remote peer's
#: message, and an unbounded loop over attacker-chosen nesting runs on the single
#: gateway event loop.
MAX_UNWRAP_DEPTH = 8

#: The inner field name on every ``FutureProofMessage``-shaped holder.
_INNER_MESSAGE = "message"

#: ``(carrier field, inner field)`` pairs of ``waE2E.Message``, as the pinned
#: schema declares them. This is a FLOOR, not the authority:
#: :func:`_carrier_fields` derives the same set from the message's descriptor and
#: unions this in, so the two paths agree and a schema the descriptor cannot
#: describe still unwraps what is known today. A name that a later schema drops
#: costs nothing: ``HasField`` reports it absent.
CARRIER_FIELDS: tuple[tuple[str, str], ...] = (
    ("viewOnceMessage", _INNER_MESSAGE),
    ("ephemeralMessage", _INNER_MESSAGE),
    ("documentWithCaptionMessage", _INNER_MESSAGE),
    ("viewOnceMessageV2", _INNER_MESSAGE),
    ("editedMessage", _INNER_MESSAGE),
    ("viewOnceMessageV2Extension", _INNER_MESSAGE),
    ("groupMentionedMessage", _INNER_MESSAGE),
    ("botInvokeMessage", _INNER_MESSAGE),
    ("lottieStickerMessage", _INNER_MESSAGE),
    ("eventCoverImage", _INNER_MESSAGE),
    ("statusMentionMessage", _INNER_MESSAGE),
    ("pollCreationOptionImageMessage", _INNER_MESSAGE),
    ("associatedChildMessage", _INNER_MESSAGE),
    ("groupStatusMentionMessage", _INNER_MESSAGE),
    ("pollCreationMessageV4", _INNER_MESSAGE),
    ("statusAddYours", _INNER_MESSAGE),
    ("groupStatusMessage", _INNER_MESSAGE),
    ("limitSharingMessage", _INNER_MESSAGE),
    ("botTaskMessage", _INNER_MESSAGE),
    ("questionMessage", _INNER_MESSAGE),
    ("groupStatusMessageV2", _INNER_MESSAGE),
    ("botForwardedMessage", _INNER_MESSAGE),
    ("questionReplyMessage", _INNER_MESSAGE),
    ("newsletterAdminProfileMessage", _INNER_MESSAGE),
    ("newsletterAdminProfileMessageV2", _INNER_MESSAGE),
    ("spoilerMessage", _INNER_MESSAGE),
    ("newsletterAdminProfileStatusMessage", _INNER_MESSAGE),
    ("deviceSentMessage", _INNER_MESSAGE),
    ("commentMessage", _INNER_MESSAGE),
    # Not FutureProofMessage-shaped, and reached by the same structural rule: an
    # inbound edit is a protocol frame whose payload is the replacement message,
    # and a payment carries the note the user typed beside it.
    ("protocolMessage", "editedMessage"),
    ("sendPaymentMessage", "noteMessage"),
    ("requestPaymentMessage", "noteMessage"),
)

#: Derived carrier pairs per schema, keyed by the descriptor's full name. The
#: derivation walks every field of ``Message`` once; the cache keeps that off the
#: per-message path, and one process only ever sees one schema.
_DERIVED_CARRIERS: dict[str, tuple[tuple[str, str], ...]] = {}

# ── Content fields, in the order they are consulted ─────────────────────────

#: Media-bearing fields. ``audioMessage`` splits into audio or voice on ``PTT``;
#: ``ptvMessage`` is a "video note", carried as a ``VideoMessage`` in its own
#: field, so it answers to the same kind.
_MEDIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("imageMessage", KIND_IMAGE),
    ("stickerMessage", KIND_STICKER),
    ("audioMessage", KIND_AUDIO),
    ("videoMessage", KIND_VIDEO),
    ("ptvMessage", KIND_VIDEO),
    ("documentMessage", KIND_DOCUMENT),
)

#: Fields that carry content this channel cannot hand to the model. Consulted
#: after the media and text fields, so a caption never loses to a container.
_OTHER_FIELDS: tuple[tuple[str, str], ...] = (
    ("reactionMessage", KIND_REACTION),
    ("locationMessage", KIND_LOCATION),
    ("liveLocationMessage", KIND_LOCATION),
    ("contactMessage", KIND_CONTACTS),
    ("contactsArrayMessage", KIND_CONTACTS),
    ("pollCreationMessage", KIND_POLL),
    ("pollCreationMessageV2", KIND_POLL),
    ("pollCreationMessageV3", KIND_POLL),
    ("pollCreationMessageV5", KIND_POLL),
    ("pollCreationMessageV6", KIND_POLL),
    ("pollUpdateMessage", KIND_POLL),
    ("pollAddOptionMessage", KIND_POLL),
)

#: Plain text lives in one of two fields: ``conversation`` for a bare message,
#: ``extendedTextMessage.text`` when it carries a link preview, a mention or a
#: quote.
_CONVERSATION_FIELD = "conversation"
_EXTENDED_TEXT_FIELD = "extendedTextMessage"
_TEXT_FIELD = "text"

#: The caption field on a media submessage, where one exists.
_CAPTION_FIELD = "caption"
#: Document names, in preference order: ``fileName`` is what the sender's client
#: sent, ``title`` is what WhatsApp shows when there is no filename.
_NAME_FIELDS = ("fileName", "title")
_MIMETYPE_FIELD = "mimetype"
_FILE_LENGTH_FIELD = "fileLength"
#: Push-to-talk. Set on an ``audioMessage`` recorded in the app, absent on an
#: audio FILE the user attached, which is the whole difference between a voice
#: note to transcribe and a song.
_PTT_FIELD = "PTT"

# ── User-facing notes ───────────────────────────────────────────────────────

#: One line per kind that arrived and was skipped. Silence is the defect this
#: exists to fix: the operator sends a poll and the agent answers as though
#: nothing happened. Constants so the wording is edited in one place.
NOTE_VIDEO = "[A video arrived and was skipped: send a screenshot or a summary instead]"
NOTE_REACTION = ""
NOTE_LOCATION = "[A location arrived and was skipped: locations are not supported]"
NOTE_CONTACTS = "[A contact card arrived and was skipped: contact cards are not supported]"
NOTE_POLL = "[A poll arrived and was skipped: polls are not supported]"

#: Kind -> note. :data:`KIND_UNSUPPORTED` is deliberately absent: that bucket is
#: dominated by traffic no human sent (key distribution, receipts, revocations,
#: pin and keep-in-chat frames), so a note there would fire on ordinary protocol
#: noise and teach the operator to ignore every note. A kind the operator can
#: actually see themselves send is named above instead.
_NOTES: dict[str, str] = {
    KIND_VIDEO: NOTE_VIDEO,
    # Deliberately empty, so a reaction produces NO note and therefore no turn.
    # This channel draws its own phase reactions, and a reaction that reached the
    # turn path would close a loop: react -> from_me echo -> note -> turn ->
    # react. It is also not a request, so there is nothing for a turn to answer.
    KIND_REACTION: NOTE_REACTION,
    KIND_LOCATION: NOTE_LOCATION,
    KIND_CONTACTS: NOTE_CONTACTS,
    KIND_POLL: NOTE_POLL,
}


@dataclass(frozen=True)
class MediaDescription:
    """What one inbound message turned out to be.

    Everything here is read from the envelope, so every value is the SENDER's
    claim: :attr:`mimetype` and :attr:`file_length` are advisory, and the shared
    ingest layer re-checks both against the bytes it actually receives.
    """

    #: One of the ``KIND_*`` constants.
    kind: str
    #: The protobuf field the content was found in, for logs. Empty when nothing
    #: recognizable was set.
    carrier_field: str = ""
    #: Declared type, verbatim, so a caller can log what the sender claimed.
    #: Use :attr:`base_mimetype` for a comparison.
    mimetype: str = ""
    #: What the user typed: a caption on media, the message text on a text
    #: message. Empty when they typed nothing.
    caption: str = ""
    #: The sender's filename, for a document. Empty for everything else, since
    #: no other WhatsApp media carries a name.
    filename: str = ""
    #: Declared size in bytes, 0 when absent. Advisory: the sender chose it.
    file_length: int = 0
    #: A voice note recorded in the app, rather than an attached audio file.
    is_voice_note: bool = False
    #: Carrier fields the content was unwrapped out of, outermost first. Lets a
    #: caller see that a photo was view-once or that a text is an edit of an
    #: older message, neither of which survives into the fields above.
    wrappers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_media(self) -> bool:
        """Whether fetching this message's bytes is worth doing."""
        return self.kind in INGESTIBLE_KINDS

    @property
    def base_mimetype(self) -> str:
        """:attr:`mimetype` lowercased with any parameters stripped.

        WhatsApp ships voice notes as ``audio/ogg; codecs=opus``, and the shared
        classifier matches image types by EXACT string, so a parameterized
        ``image/jpeg; foo`` would classify as "unsupported type" and be rejected
        after it had already been downloaded. Compare on this.
        """
        return self.mimetype.split(";", 1)[0].strip().lower()


def _has(msg: Any, name: str) -> bool:
    """Whether *name* is actually present on *msg*.

    ``HasField`` is the only correct presence probe on a protobuf (see the module
    docstring), and it raises rather than answering for a field the schema does
    not define, for a repeated field, or when *msg* is not a protobuf at all.
    Every one of those means "this content is not here", so they answer False:
    the inbound path must degrade, never raise, on a message shape this build
    does not know.
    """
    try:
        return bool(msg.HasField(name))
    except (AttributeError, ValueError, TypeError):
        return False


def _carrier_fields(msg: Any) -> tuple[tuple[str, str], ...]:
    """Carrier pairs valid for *msg*'s schema, derived once then cached.

    Structural rule: a singular submessage field whose own type carries a field
    of the SAME message type is a carrier, and that inner field is where the
    content sits. Deriving it beats listing it, because the list is exactly the
    thing that goes stale when WhatsApp adds a wrapper: an unlisted wrapper is
    not a parse error, it is a message that silently reads as empty.

    :data:`CARRIER_FIELDS` is unioned in so a descriptor this code cannot read
    (a fake, a future protobuf runtime that renames the introspection API) still
    unwraps every carrier known at the pin.
    """
    desc = getattr(msg, "DESCRIPTOR", None)
    if desc is None:
        return CARRIER_FIELDS
    key = str(getattr(desc, "full_name", "") or getattr(desc, "name", ""))
    cached = _DERIVED_CARRIERS.get(key)
    if cached is None:
        cached = _derive_carriers(desc)
        _DERIVED_CARRIERS[key] = cached
    return cached


def _derive_carriers(desc: Any) -> tuple[tuple[str, str], ...]:
    """Every carrier pair *desc* declares, unioned with :data:`CARRIER_FIELDS`."""
    pairs: list[tuple[str, str]] = []
    try:
        own = str(getattr(desc, "full_name", "") or getattr(desc, "name", ""))
        for outer in desc.fields:
            sub = getattr(outer, "message_type", None)
            if sub is None:
                continue
            for inner in sub.fields:
                inner_type = getattr(inner, "message_type", None)
                if inner_type is None:
                    continue
                name = str(getattr(inner_type, "full_name", "") or getattr(inner_type, "name", ""))
                if name == own:
                    pairs.append((str(outer.name), str(inner.name)))
                    break
    except (AttributeError, TypeError):
        # A descriptor shaped differently than expected is not a reason to stop
        # reading messages: fall back to what the pinned schema declares.
        logger.debug("whatsapp: could not derive carrier fields from the schema", exc_info=True)
    seen = set(pairs)
    pairs.extend(pair for pair in CARRIER_FIELDS if pair not in seen)
    return tuple(pairs)


def _unwrap(msg: Any) -> tuple[Any, tuple[str, ...]]:
    """The innermost message plus the carrier fields walked to reach it."""
    walked: list[str] = []
    current = msg
    for _ in range(MAX_UNWRAP_DEPTH):
        for name, inner_name in _carrier_fields(current):
            if not _has(current, name):
                continue
            holder = getattr(current, name, None)
            if holder is None or not _has(holder, inner_name):
                # A carrier whose payload is absent: stop here rather than
                # descend into a default instance, so the caller sees the empty
                # carrier that actually arrived.
                return current, tuple(walked)
            walked.append(name)
            current = getattr(holder, inner_name)
            break
        else:
            return current, tuple(walked)
    logger.warning(
        "whatsapp: message wrapped past %d carriers, reading the innermost reached",
        MAX_UNWRAP_DEPTH,
    )
    return current, tuple(walked)


def unwrap_message(msg: Any) -> Any:
    """The innermost ``waE2E.Message`` inside *msg*.

    Idempotent, and safe on anything: a message with no carrier, an empty
    carrier, or an object that is not a protobuf comes back unchanged. Callers
    that need the bytes must keep THIS message: ``download_any`` picks the media
    submessage out of the message handed to it, and the outer carrier has none.
    """
    return _unwrap(msg)[0]


def describe(msg: Any) -> MediaDescription:
    """What arrived in *msg*, unwrapping carriers first.

    Consults media fields, then text, then the containers this channel cannot
    ingest, so an image WITH a caption is an image (the caption rides along in
    :attr:`MediaDescription.caption`) rather than a text message that happens to
    mention one. Anything left is :data:`KIND_UNSUPPORTED`.
    """
    inner, wrappers = _unwrap(msg)

    for name, kind in _MEDIA_FIELDS:
        if not _has(inner, name):
            continue
        sub = getattr(inner, name)
        # PTT is a scalar bool: an absent one reads False, which is exactly the
        # meaning wanted here, so no presence probe is needed (unlike the
        # submessage reads above, where a default instance is indistinguishable
        # from a real one).
        voice = kind == KIND_AUDIO and bool(getattr(sub, _PTT_FIELD, False))
        return MediaDescription(
            kind=KIND_VOICE if voice else kind,
            carrier_field=name,
            mimetype=_string_at(sub, _MIMETYPE_FIELD),
            caption=_string_at(sub, _CAPTION_FIELD),
            filename=_first_string(sub, _NAME_FIELDS),
            file_length=_int_at(sub, _FILE_LENGTH_FIELD),
            is_voice_note=voice,
            wrappers=wrappers,
        )

    if _has(inner, _CONVERSATION_FIELD):
        return MediaDescription(
            kind=KIND_TEXT,
            carrier_field=_CONVERSATION_FIELD,
            caption=_string_at(inner, _CONVERSATION_FIELD),
            wrappers=wrappers,
        )
    if _has(inner, _EXTENDED_TEXT_FIELD):
        return MediaDescription(
            kind=KIND_TEXT,
            carrier_field=_EXTENDED_TEXT_FIELD,
            caption=_string_at(getattr(inner, _EXTENDED_TEXT_FIELD), _TEXT_FIELD),
            wrappers=wrappers,
        )

    for name, kind in _OTHER_FIELDS:
        if _has(inner, name):
            return MediaDescription(kind=kind, carrier_field=name, wrappers=wrappers)

    return MediaDescription(kind=KIND_UNSUPPORTED, wrappers=wrappers)


def caption_text(msg: Any) -> str:
    """What the user typed in *msg*: message text, or a caption on media.

    The one place that answers that question for this channel, so a photo's
    caption cannot be an instruction the transport never read. Derived from
    :func:`describe`, so the two can never disagree about which field held the
    text; a caller that already has a :class:`MediaDescription` should read
    :attr:`MediaDescription.caption` instead of calling this again.
    """
    return describe(msg).caption


def unsupported_note(desc: MediaDescription) -> str:
    """A line telling the operator *desc* was seen and skipped, or ``""``.

    Empty for anything the channel can use, and for
    :data:`KIND_UNSUPPORTED` (see :data:`_NOTES`). Callers append what they get
    to the turn's text the way
    :func:`kiro_crew.messaging.attachments.append_attachment_context` appends a
    rejection, which is where the bracketed wording comes from.
    """
    return _NOTES.get(desc.kind, "")


def _string_at(sub: Any, name: str) -> str:
    """A string field of *sub*, or ``""`` when absent or not a string field.

    Scalars need no presence probe: an absent one reads as the default, and for
    a caption or a mimetype the default IS "there is none". A field the schema
    does not define raises ``AttributeError``, which means the same thing.
    """
    return str(getattr(sub, name, "") or "")


def _first_string(sub: Any, names: tuple[str, ...]) -> str:
    for name in names:
        value = _string_at(sub, name)
        if value:
            return value
    return ""


def _int_at(sub: Any, name: str) -> int:
    """An integer field of *sub*, or 0 when absent or unreadable."""
    try:
        return int(getattr(sub, name, 0) or 0)
    except (TypeError, ValueError):
        return 0
