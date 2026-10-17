"""Pack-carried audio cues: what a sound file may be, and how to tell.

The pack tier supplies its own reactions. A pack already ships the per-state ART
the roster draws (``GET /api/appearances/{id}/slot/{slot}``); a pack may also ship
the per-state SOUND that plays with it, named by an optional ``sounds`` section in
its manifest. The pack is where a pack crew's per-state answers live. The crew record
still accepts a preset ``sounds`` cue beside the id for as long as the shipped
renderer plays a crew-record cue on every tier; once the pack's own audio is what
plays, that key retires with the frontend change that stops reading it.

Only the vocabulary and the two pure predicates live here, so that
:mod:`~kiro_crew.appearance_packs.store` (which reads the library) and
:mod:`~kiro_crew.appearance_packs.transfer` (which accepts bundles from outside)
judge a cue by the same rule. A bundle importer that accepted audio the reader
would later drop would install a pack whose cues silently never play.

Two rules hold on every path here:

* **The bytes decide the type, never the filename.** A manifest is hand-editable
  and a bundle comes from outside, so a name is a claim, not evidence. The type a
  cue is served as comes from :func:`sniff_audio`, reached through
  :func:`read_sound` -- the ONE judgement every side shares, so a cue the
  importer accepts is a cue the reader plays.
* **Junk is dropped, never fatal.** A pack is third-party content, and one
  unreadable cue must not cost the pack its art.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

#: Agent lifecycle states a pack may carry a cue for -- the states a REACTION
#: fires on. ``idle`` is deliberately absent: it is the resting state a pack's
#: art loops in, and a cue that played on it would loop with it.
SOUND_STATES = ("working", "done", "error")

#: Ceiling for ONE decoded cue. Room for a short sound at ordinary bitrates and
#: far below anything that would stall a page or exhaust memory on read. The
#: bundle importer applies the same cap, so an oversized cue is named in the
#: import answer's ``warnings`` where the user can see it, rather than only
#: dropped by the reader later with a server log nobody reads.
MAX_SOUND_BYTES = 512 * 1024

#: Audio containers a pack may carry, and the type each is served as. The value
#: is chosen by SNIFFING the decoded bytes, never by trusting the filename.
SOUND_MIME = {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav"}

#: The suffixes above, as a tuple for ``str.endswith``.
SOUND_SUFFIXES = tuple(SOUND_MIME)


def sniff_audio(raw: bytes) -> str:
    """The container ``raw`` actually is, as a ``SOUND_MIME`` key, or ``""``.

    Magic bytes only, and only for the three containers every target browser
    plays in an ``<audio>`` element. An unrecognised header is refused rather
    than served as a guess, because the browser would decide what to do with
    unknown bytes and this is content a third party authored.
    """
    if raw.startswith(b"ID3"):
        return ".mp3"
    # A bare MPEG frame with no ID3 tag: 11 sync bits.
    if len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0:
        return ".mp3"
    if raw.startswith(b"OggS"):
        return ".ogg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return ".wav"
    return ""


def read_sound(text: Any) -> tuple[tuple[bytes, str] | None, str]:
    """THE predicate for "can this cue be played", plus WHY not.

    ``((bytes, mime), "")`` when the cue plays, ``(None, <reason>)`` when it does
    not. One function behind every side of the boundary: the store reports
    presence exactly when this answers, the sound route serves exactly what it
    returns, and the bundle importer WARNS on exactly what it rejects -- which is
    what stops the gate and the reader drifting into "imported fine, plays
    nothing" with no one told.

    The reason exists because the two callers need different things from the same
    judgement. A reader DROPS a bad cue and logs it, so it needs only yes or no.
    An importer installs the cue anyway (our own export must re-import) but has
    to tell the person who picked the file which mistake they made: "too long a
    sound" and "not a sound at all" have different fixes. Returning both from one function is what keeps the
    importer from re-deriving the decode it would then drift from -- the reason is
    a fragment, so the caller owns the sentence it lands in.
    """
    if not isinstance(text, str):
        return None, "is not base64-encoded audio"
    # An empty string decodes to empty bytes rather than raising, so it reaches
    # the emptiness check below and is named for what it is.
    try:
        raw = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        return None, "is not base64-encoded audio"
    if not raw:
        return None, "is empty"
    if len(raw) > MAX_SOUND_BYTES:
        return None, "is a longer sound than a pack may carry"
    sniffed = sniff_audio(raw)
    if not sniffed:
        return None, "is not an mp3, ogg or wav"
    return (raw, SOUND_MIME[sniffed]), ""


def sound_body(text: Any) -> tuple[bytes, str] | None:
    """One cue's bytes and the type to serve it as, or ``None``.

    The yes-or-no half of :func:`read_sound`, for the reader, which drops a bad
    cue rather than explaining it.
    """
    body, _reason = read_sound(text)
    return body
