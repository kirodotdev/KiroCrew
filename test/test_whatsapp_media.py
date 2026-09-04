"""WhatsApp inbound media extraction: what arrived, read from the protobuf.

``neonize`` is the optional ``[whatsapp]`` extra and is not installed here (nor is
the protobuf runtime it generates against), so the messages under test are built
from :class:`_Fake`, a stand-in that reproduces the protobuf semantics this module
actually depends on:

* a singular submessage field is NEVER ``None``: reading an absent one yields a
  default instance, and reading it does not make it present;
* ``HasField`` is the only presence probe, and it RAISES ``ValueError`` for a
  field the schema does not define and for a repeated field;
* a field outside the schema is an ``AttributeError``, not an empty string.

Those four are verified first (:func:`test_fake_reproduces_protobuf_presence`),
because a fake that accepts what the real protobuf rejects would let every test
below pass against broken code. The schema itself is transcribed from
``neonize``'s ``waE2E.Message`` descriptor, so a carrier named here is a carrier
that exists.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.whatsapp.media import (
    CARRIER_FIELDS,
    INGESTIBLE_KINDS,
    KIND_AUDIO,
    KIND_CONTACTS,
    KIND_DOCUMENT,
    KIND_IMAGE,
    KIND_LOCATION,
    KIND_POLL,
    KIND_REACTION,
    KIND_STICKER,
    KIND_TEXT,
    KIND_UNSUPPORTED,
    KIND_VIDEO,
    KIND_VOICE,
    MAX_UNWRAP_DEPTH,
    NOTE_CONTACTS,
    NOTE_LOCATION,
    NOTE_POLL,
    NOTE_REACTION,
    NOTE_VIDEO,
    caption_text,
    describe,
    unsupported_note,
    unwrap_message,
)

# ── the fake schema ─────────────────────────────────────────────────────────

#: ``type -> {field: spec}``. A spec is a scalar name (``str`` / ``int`` /
#: ``bool`` / ``float``), a message type name, or ``[]Type`` for a repeated
#: field. Every ``Message`` field below is a real field of ``waE2E.Message``
#: except the two marked hypothetical, which stand in for a schema newer than
#: this build's.
_SCHEMA: dict[str, dict[str, str]] = {
    "Message": {
        # text
        "conversation": "str",
        "extendedTextMessage": "ExtendedTextMessage",
        # media
        "imageMessage": "ImageMessage",
        "stickerMessage": "StickerMessage",
        "audioMessage": "AudioMessage",
        "videoMessage": "VideoMessage",
        "ptvMessage": "VideoMessage",
        "documentMessage": "DocumentMessage",
        # content this channel cannot ingest
        "reactionMessage": "ReactionMessage",
        "locationMessage": "LocationMessage",
        "liveLocationMessage": "LiveLocationMessage",
        "contactMessage": "ContactMessage",
        "contactsArrayMessage": "ContactsArrayMessage",
        "pollCreationMessage": "PollCreationMessage",
        "pollCreationMessageV2": "PollCreationMessage",
        "pollCreationMessageV3": "PollCreationMessage",
        "pollCreationMessageV5": "PollCreationMessage",
        "pollCreationMessageV6": "PollCreationMessage",
        "pollUpdateMessage": "PollUpdateMessage",
        "pollAddOptionMessage": "PollAddOptionMessage",
        # every FutureProofMessage-typed field of the pinned schema
        "viewOnceMessage": "FutureProofMessage",
        "ephemeralMessage": "FutureProofMessage",
        "documentWithCaptionMessage": "FutureProofMessage",
        "viewOnceMessageV2": "FutureProofMessage",
        "editedMessage": "FutureProofMessage",
        "viewOnceMessageV2Extension": "FutureProofMessage",
        "groupMentionedMessage": "FutureProofMessage",
        "botInvokeMessage": "FutureProofMessage",
        "lottieStickerMessage": "FutureProofMessage",
        "eventCoverImage": "FutureProofMessage",
        "statusMentionMessage": "FutureProofMessage",
        "pollCreationOptionImageMessage": "FutureProofMessage",
        "associatedChildMessage": "FutureProofMessage",
        "groupStatusMentionMessage": "FutureProofMessage",
        "pollCreationMessageV4": "FutureProofMessage",
        "statusAddYours": "FutureProofMessage",
        "groupStatusMessage": "FutureProofMessage",
        "limitSharingMessage": "FutureProofMessage",
        "botTaskMessage": "FutureProofMessage",
        "questionMessage": "FutureProofMessage",
        "groupStatusMessageV2": "FutureProofMessage",
        "botForwardedMessage": "FutureProofMessage",
        "questionReplyMessage": "FutureProofMessage",
        "newsletterAdminProfileMessage": "FutureProofMessage",
        "newsletterAdminProfileMessageV2": "FutureProofMessage",
        "spoilerMessage": "FutureProofMessage",
        "newsletterAdminProfileStatusMessage": "FutureProofMessage",
        # carriers that are not FutureProofMessage-shaped
        "deviceSentMessage": "DeviceSentMessage",
        "commentMessage": "CommentMessage",
        "protocolMessage": "ProtocolMessage",
        "sendPaymentMessage": "SendPaymentMessage",
        "requestPaymentMessage": "RequestPaymentMessage",
        # ordinary content with no reader here
        "senderKeyDistributionMessage": "SenderKeyDistributionMessage",
        # hypothetical: a schema newer than this build's
        "hologramMessage": "FutureProofMessage",
        "albumMessage": "[]Message",
    },
    "FutureProofMessage": {"message": "Message"},
    "DeviceSentMessage": {"destinationJID": "str", "message": "Message", "phash": "str"},
    "CommentMessage": {"message": "Message", "targetMessageKey": "MessageKey"},
    "ProtocolMessage": {"key": "MessageKey", "type": "int", "editedMessage": "Message"},
    "SendPaymentMessage": {"noteMessage": "Message"},
    "RequestPaymentMessage": {"noteMessage": "Message", "amount1000": "int"},
    "ImageMessage": {
        "URL": "str",
        "mimetype": "str",
        "caption": "str",
        "fileLength": "int",
        "height": "int",
        "width": "int",
        "contextInfo": "ContextInfo",
    },
    "AudioMessage": {
        "URL": "str",
        "mimetype": "str",
        "fileLength": "int",
        "seconds": "int",
        "PTT": "bool",
    },
    "VideoMessage": {
        "URL": "str",
        "mimetype": "str",
        "caption": "str",
        "fileLength": "int",
        "gifPlayback": "bool",
    },
    "DocumentMessage": {
        "URL": "str",
        "mimetype": "str",
        "title": "str",
        "fileName": "str",
        "caption": "str",
        "fileLength": "int",
        "pageCount": "int",
    },
    "StickerMessage": {"mimetype": "str", "fileLength": "int", "isAnimated": "bool"},
    "ExtendedTextMessage": {"text": "str", "matchedText": "str", "contextInfo": "ContextInfo"},
    "ReactionMessage": {"key": "MessageKey", "text": "str"},
    "LocationMessage": {"degreesLatitude": "float", "degreesLongitude": "float", "name": "str"},
    "LiveLocationMessage": {"degreesLatitude": "float", "caption": "str"},
    "ContactMessage": {"displayName": "str", "vcard": "str"},
    "ContactsArrayMessage": {"displayName": "str", "contacts": "[]ContactMessage"},
    "PollCreationMessage": {"name": "str", "selectableOptionsCount": "int"},
    "PollUpdateMessage": {"senderTimestampMS": "int"},
    "PollAddOptionMessage": {"senderTimestampMS": "int"},
    "SenderKeyDistributionMessage": {"groupID": "str"},
    "MessageKey": {"ID": "str", "remoteJID": "str"},
    "ContextInfo": {"stanzaID": "str", "participant": "str"},
}

_SCALAR_DEFAULTS: dict[str, Any] = {"str": "", "int": 0, "bool": False, "float": 0.0}

#: Carrier fields as the FAKE schema declares them, derived independently of the
#: module under test: a field whose type carries a ``Message``. Used to drive the
#: per-carrier test and to catch a typo in the module's own floor list.
_SCHEMA_CARRIERS: dict[str, str] = {
    name: inner
    for name, spec in _SCHEMA["Message"].items()
    if not spec.startswith("[]")
    for inner, inner_spec in _SCHEMA.get(spec, {}).items()
    if inner_spec == "Message"
}


class _FieldDescriptor:
    def __init__(self, name: str, message_type: Any = None, repeated: bool = False) -> None:
        self.name = name
        self.message_type = message_type
        self.label = 3 if repeated else 1


class _Descriptor:
    def __init__(self, type_name: str) -> None:
        self.name = type_name
        self.full_name = f"WAWebProtobufsE2E.{type_name}"
        self.fields: list[_FieldDescriptor] = []


_DESCRIPTORS: dict[str, _Descriptor] = {name: _Descriptor(name) for name in _SCHEMA}
for _type_name, _fields in _SCHEMA.items():
    for _field_name, _spec in _fields.items():
        _repeated = _spec.startswith("[]")
        _DESCRIPTORS[_type_name].fields.append(
            _FieldDescriptor(
                _field_name,
                _DESCRIPTORS.get(_spec[2:] if _repeated else _spec),
                _repeated,
            )
        )


class _Fake:
    """A protobuf stand-in (see the module docstring for what it reproduces)."""

    def __init__(self, _type_name: str = "Message", **set_fields: Any) -> None:
        self._type_name = _type_name
        self._schema = _SCHEMA[_type_name]
        unknown = sorted(set(set_fields) - set(self._schema))
        if unknown:
            # Keeps the fake honest: the real protobuf constructor raises too, so
            # a test may not invent a field and then assert on the reading of it.
            raise AssertionError(f"{_type_name} has no field(s) {unknown}")
        self._set = dict(set_fields)
        self.DESCRIPTOR = _DESCRIPTORS[_type_name]

    def HasField(self, name: str) -> bool:
        spec = self._schema.get(name)
        if spec is None:
            raise ValueError(f'Protocol message {self._type_name} has no "{name}" field.')
        if spec.startswith("[]"):
            raise ValueError(f"Field {self._type_name}.{name} does not have presence.")
        return name in self._set

    def __getattr__(self, name: str) -> Any:
        # Reached only when normal lookup fails, so the attributes set in
        # __init__ never come through here.
        schema = self.__dict__["_schema"]
        spec = schema.get(name)
        if spec is None:
            raise AttributeError(name)
        values = self.__dict__["_set"]
        if name in values:
            return values[name]
        if spec.startswith("[]"):
            return []
        if spec in _SCHEMA:
            # A default instance, freshly built and NOT remembered: reading an
            # absent submessage must not make it present.
            return _Fake(spec)
        return _SCALAR_DEFAULTS[spec]


def _msg(**set_fields: Any) -> _Fake:
    return _Fake("Message", **set_fields)


def _image(**kwargs: Any) -> _Fake:
    fields: dict[str, Any] = {"mimetype": "image/jpeg", "fileLength": 4096}
    fields.update(kwargs)
    return _Fake("ImageMessage", **fields)


def _wrap(field: str, inner: _Fake) -> _Fake:
    """*inner* inside the carrier *field*, using that carrier's own inner name."""
    holder_type = _SCHEMA["Message"][field]
    return _msg(**{field: _Fake(holder_type, **{_SCHEMA_CARRIERS[field]: inner})})


# ── the fake itself ─────────────────────────────────────────────────────────


def test_fake_reproduces_protobuf_presence():
    empty = _msg()
    # Never None, and reading it does not make it present.
    assert empty.imageMessage is not None
    assert empty.HasField("imageMessage") is False
    with pytest.raises(ValueError):
        empty.HasField("nonesuchMessage")
    with pytest.raises(ValueError):
        # Repeated fields have no presence.
        _Fake("ContactsArrayMessage").HasField("contacts")
    with pytest.raises(AttributeError):
        empty.nonesuchMessage
    # A scalar has presence too (these are proto2 messages).
    assert _msg(conversation="").HasField("conversation") is True
    assert empty.HasField("conversation") is False


# ── unwrapping ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("carrier", sorted(_SCHEMA_CARRIERS))
def test_every_carrier_unwraps(carrier):
    """Each carrier the schema declares, including the hypothetical future one.

    The parametrization comes from the SCHEMA, not from the module's list, so a
    carrier the module forgot fails here instead of silently reading as empty.
    """
    wrapped = _wrap(carrier, _msg(imageMessage=_image(caption="hi")))
    inner = unwrap_message(wrapped)
    assert inner.HasField("imageMessage")
    desc = describe(wrapped)
    assert desc.kind == KIND_IMAGE
    assert desc.caption == "hi"
    assert desc.wrappers == (carrier,)


def test_carrier_floor_names_real_fields():
    """Every pair in the module's fallback list exists in the schema.

    A typo'd name there is invisible in production: ``HasField`` reports it
    absent, so the carrier simply never unwraps.
    """
    assert dict(CARRIER_FIELDS).items() <= _SCHEMA_CARRIERS.items()


def test_nested_carrier_chain_unwraps_to_the_innermost():
    photo = _msg(imageMessage=_image(caption="the roof"))
    chain = _wrap(
        "ephemeralMessage",
        _wrap("deviceSentMessage", _wrap("viewOnceMessageV2", photo)),
    )
    desc = describe(chain)
    assert desc.kind == KIND_IMAGE
    assert desc.caption == "the roof"
    assert desc.wrappers == ("ephemeralMessage", "deviceSentMessage", "viewOnceMessageV2")


def test_unwrap_depth_is_bounded():
    deepest = _msg(imageMessage=_image())
    nested = deepest
    for _ in range(MAX_UNWRAP_DEPTH + 4):
        nested = _wrap("ephemeralMessage", nested)

    desc = describe(nested)
    # The walk stops on the bound rather than reaching the photo, and what it
    # stopped on is still a carrier: that is the bound biting, not the chain
    # ending.
    assert unwrap_message(nested).HasField("ephemeralMessage")
    assert len(desc.wrappers) == MAX_UNWRAP_DEPTH
    assert desc.kind == KIND_UNSUPPORTED


def test_hollow_carrier_stops_the_walk():
    """A carrier whose payload never arrived reads as unsupported, not as text."""
    hollow = _msg(ephemeralMessage=_Fake("FutureProofMessage"))
    assert describe(hollow).kind == KIND_UNSUPPORTED
    assert describe(hollow).wrappers == ()
    assert caption_text(hollow) == ""


def test_unwrap_is_idempotent():
    photo = _msg(imageMessage=_image())
    once = unwrap_message(_wrap("viewOnceMessage", photo))
    assert unwrap_message(once) is once


def test_repeated_message_field_is_not_a_carrier():
    """A repeated ``Message`` field cannot be probed for presence, so it is skipped.

    ``HasField`` raises on it, and the fail-closed reading of that raise is what
    keeps a schema addition of this shape from crashing the inbound path.
    """
    album = _msg(albumMessage=[_msg(imageMessage=_image())])
    assert unwrap_message(album) is album
    assert describe(album).kind == KIND_UNSUPPORTED


def test_carriers_are_found_without_a_descriptor():
    """The floor list carries the known carriers when the schema cannot be read."""

    class _NoDescriptor(_Fake):
        DESCRIPTOR = None

        def __init__(self, **set_fields: Any) -> None:
            super().__init__("Message", **set_fields)
            del self.DESCRIPTOR

    wrapped = _NoDescriptor(
        ephemeralMessage=_Fake("FutureProofMessage", message=_msg(conversation="still here"))
    )
    assert caption_text(wrapped) == "still here"


@pytest.mark.parametrize(
    "descriptor",
    [
        # A descriptor that describes nothing, and one whose fields cannot be
        # walked at all: both stand for a protobuf runtime that has moved the
        # introspection API. Distinct names because derivation is cached per
        # schema name.
        SimpleNamespace(full_name="Unreadable.Empty", fields=[]),
        SimpleNamespace(full_name="Unreadable.Broken", fields=17),
    ],
)
def test_unreadable_descriptor_falls_back_to_the_floor(descriptor):
    """Derivation that yields nothing must not cost the known carriers."""

    class _OddDescriptor(_Fake):
        def __init__(self, **set_fields: Any) -> None:
            super().__init__("Message", **set_fields)
            self.DESCRIPTOR = descriptor

    wrapped = _OddDescriptor(
        viewOnceMessageV2=_Fake("FutureProofMessage", message=_msg(conversation="one look"))
    )
    assert caption_text(wrapped) == "one look"


# ── the HasField trap ───────────────────────────────────────────────────────


def test_absent_submessage_is_not_mistaken_for_present():
    """The channel-fatal reading, asserted as such.

    ``getattr(msg, "imageMessage", None) is not None`` is True for EVERY message,
    because an absent singular submessage materializes a default instance. A
    message carrying only text must therefore still describe as text.
    """
    typed = _msg(conversation="just words")
    assert getattr(typed, "imageMessage", None) is not None  # the trap
    assert typed.HasField("imageMessage") is False  # the correct probe
    desc = describe(typed)
    assert desc.kind == KIND_TEXT
    assert desc.mimetype == ""
    assert desc.file_length == 0


def test_default_image_instance_is_not_an_image():
    empty = _msg()
    assert describe(empty).kind == KIND_UNSUPPORTED
    assert describe(empty).carrier_field == ""


# ── kinds ───────────────────────────────────────────────────────────────────


def test_text_from_conversation_and_extended_text():
    assert describe(_msg(conversation="ping")).kind == KIND_TEXT
    assert caption_text(_msg(conversation="ping")) == "ping"
    linked = _msg(
        extendedTextMessage=_Fake("ExtendedTextMessage", text="see https://example.invalid")
    )
    assert describe(linked).kind == KIND_TEXT
    assert caption_text(linked) == "see https://example.invalid"


def test_caption_on_an_image_is_the_typed_text():
    photo = _msg(imageMessage=_image(caption="what is wrong with this chart?"))
    desc = describe(photo)
    assert desc.kind == KIND_IMAGE
    assert desc.mimetype == "image/jpeg"
    assert desc.file_length == 4096
    assert desc.has_media is True
    assert caption_text(photo) == "what is wrong with this chart?"


def test_image_without_a_caption_has_no_text():
    assert caption_text(_msg(imageMessage=_image())) == ""


def test_ptt_distinguishes_a_voice_note_from_an_audio_file():
    voice = _msg(
        audioMessage=_Fake(
            "AudioMessage", mimetype="audio/ogg; codecs=opus", PTT=True, fileLength=8_000
        )
    )
    song = _msg(audioMessage=_Fake("AudioMessage", mimetype="audio/mpeg", fileLength=4_000_000))

    assert describe(voice).kind == KIND_VOICE
    assert describe(voice).is_voice_note is True
    # The declared type keeps its parameters; the comparable form drops them,
    # because the shared classifier matches image types by exact string.
    assert describe(voice).mimetype == "audio/ogg; codecs=opus"
    assert describe(voice).base_mimetype == "audio/ogg"

    assert describe(song).kind == KIND_AUDIO
    assert describe(song).is_voice_note is False
    assert {describe(voice).kind, describe(song).kind} <= INGESTIBLE_KINDS


def test_audio_present_but_ptt_absent_is_an_audio_file():
    """``PTT`` absent is a scalar default, which is exactly "not a voice note"."""
    plain = _msg(audioMessage=_Fake("AudioMessage", mimetype="audio/mp4"))
    assert plain.HasField("audioMessage") is True
    assert describe(plain).is_voice_note is False


def test_document_reports_its_name_size_and_caption():
    doc = _msg(
        documentMessage=_Fake(
            "DocumentMessage",
            mimetype="application/pdf",
            fileName="q3-plan.pdf",
            caption="page 4 please",
            fileLength=120_000,
        )
    )
    desc = describe(doc)
    assert (desc.kind, desc.filename, desc.file_length) == (KIND_DOCUMENT, "q3-plan.pdf", 120_000)
    assert desc.caption == "page 4 please"
    assert desc.has_media is True


def test_document_falls_back_to_its_title():
    doc = _msg(documentMessage=_Fake("DocumentMessage", mimetype="text/plain", title="notes.txt"))
    assert describe(doc).filename == "notes.txt"


def test_document_with_caption_carrier_is_a_document():
    wrapped = _wrap(
        "documentWithCaptionMessage",
        _msg(
            documentMessage=_Fake(
                "DocumentMessage", mimetype="application/pdf", fileName="a.pdf", caption="here"
            )
        ),
    )
    desc = describe(wrapped)
    assert desc.kind == KIND_DOCUMENT
    assert desc.caption == "here"


def test_sticker_is_ingestible_as_an_image():
    sticker = _msg(stickerMessage=_Fake("StickerMessage", mimetype="image/webp", fileLength=9_000))
    desc = describe(sticker)
    assert desc.kind == KIND_STICKER
    assert desc.has_media is True
    assert unsupported_note(desc) == ""


def test_video_note_shares_the_video_kind():
    for field_name in ("videoMessage", "ptvMessage"):
        clip = _msg(**{field_name: _Fake("VideoMessage", mimetype="video/mp4", fileLength=10**7)})
        desc = describe(clip)
        assert desc.kind == KIND_VIDEO
        # Refused from the envelope: nothing this large is downloaded first.
        assert desc.has_media is False
        assert unsupported_note(desc) == NOTE_VIDEO


@pytest.mark.parametrize(
    ("field_name", "sub_type", "kind", "note"),
    [
        ("reactionMessage", "ReactionMessage", KIND_REACTION, NOTE_REACTION),
        ("locationMessage", "LocationMessage", KIND_LOCATION, NOTE_LOCATION),
        ("liveLocationMessage", "LiveLocationMessage", KIND_LOCATION, NOTE_LOCATION),
        ("contactMessage", "ContactMessage", KIND_CONTACTS, NOTE_CONTACTS),
        ("contactsArrayMessage", "ContactsArrayMessage", KIND_CONTACTS, NOTE_CONTACTS),
        ("pollCreationMessage", "PollCreationMessage", KIND_POLL, NOTE_POLL),
        ("pollCreationMessageV3", "PollCreationMessage", KIND_POLL, NOTE_POLL),
        ("pollUpdateMessage", "PollUpdateMessage", KIND_POLL, NOTE_POLL),
    ],
)
def test_uningestible_kinds_are_named_and_explained(field_name, sub_type, kind, note):
    desc = describe(_msg(**{field_name: _Fake(sub_type)}))
    assert desc.kind == kind
    assert desc.has_media is False
    assert unsupported_note(desc) == note


def test_ingestible_and_text_kinds_get_no_note():
    assert unsupported_note(describe(_msg(conversation="hi"))) == ""
    assert unsupported_note(describe(_msg(imageMessage=_image()))) == ""


# ── degrading, never raising ────────────────────────────────────────────────


def test_unknown_content_type_degrades_to_unsupported():
    """A field this build has no reader for is unsupported, and says nothing.

    Silence is deliberate for this kind: the bucket is dominated by frames no
    human sent, so a note here would fire on ordinary protocol traffic.
    """
    keys = _msg(senderKeyDistributionMessage=_Fake("SenderKeyDistributionMessage", groupID="g"))
    desc = describe(keys)
    assert desc.kind == KIND_UNSUPPORTED
    assert unsupported_note(desc) == ""
    assert caption_text(keys) == ""


def test_future_carrier_unwraps_by_shape():
    """A carrier absent from the module's list still unwraps.

    ``hologramMessage`` stands in for a wrapper a later WhatsApp adds: it is
    recognized because its type carries a ``Message``, not because it was named
    in advance.
    """
    assert "hologramMessage" not in dict(CARRIER_FIELDS)
    wrapped = _wrap("hologramMessage", _msg(conversation="from the future"))
    assert caption_text(wrapped) == "from the future"
    assert describe(wrapped).wrappers == ("hologramMessage",)


@pytest.mark.parametrize("thing", [None, object(), SimpleNamespace(imageMessage=object()), "", 0])
def test_non_protobuf_input_degrades(thing):
    """Nothing here may raise: this runs on the channel's only inbound path."""
    assert unwrap_message(thing) is thing
    assert describe(thing).kind == KIND_UNSUPPORTED
    assert caption_text(thing) == ""
