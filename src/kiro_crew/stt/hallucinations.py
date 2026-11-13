"""Suppression of Whisper-family transcription artefacts.

Lives in the ``stt`` package rather than beside one provider because every
whisper-family path needs it: the batch decoder, the live session's final
transcript, and any future caller. It is pure text logic with no audio, no model
and no I/O, which is also what makes it cheap to test exhaustively.

Applied to FINAL transcripts only. A live partial is a prefix of an utterance
still being spoken, so collapsing repetitions in one would rewrite text that is
about to be superseded anyway, and could drop a genuine repetition the speaker
had only started.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Whisper models fed silence or low-energy audio produce two artefacts, both worse
# than an empty transcript because the text goes to agents as notes:
#
#  1. One phrase repeating ("Thank you. Thank you. Thank you. …")
#  2. Boilerplate unrelated to the audio, such as subtitle credits.
#
# The repetition collapse is language-independent; the boilerplate list is
# English-only, so other languages get only the repetition half. AWS Transcribe
# does not produce these artefacts and is excluded: filtering it could only delete
# genuine speech.

# Boilerplate phrases Whisper hallucinates on silence. Compared case-insensitively.
# An entry must be a CAPTION ARTEFACT: text that exists because a transcript was
# produced, not because anyone spoke. Sign-offs and subscribe CTAs ("thank you for
# watching", "don't forget to subscribe") do not qualify: people say them out loud,
# and because the match is whole-sentence and an emptied transcript returns None,
# such an entry can delete the only sentence a recording had. The accepted cost is
# that a single un-repeated hallucinated sign-off survives; the repetition collapse
# still removes the common form, where the model repeats it for the whole window.
_WHISPER_BOILERPLATE: tuple[str, ...] = (
    "subtitles by",
    "subtitles by amara.org",
    "subtitles by the amara.org community",
    "subtitles created by",
    "subtitled by",
    "translated by",
    "transcribed by",
    "captioned by",
    "amara.org",
    "www.mooji.org",
)

# How many consecutive identical sentences count as a repetition artefact rather
# than emphasis. Humans genuinely say a sentence two, three, even five times
# ("No. No. No.", a counted beat, an insistent refusal), so a low threshold
# rewrites real speech. The Whisper failure mode this targets repeats a phrase
# for the remainder of the decode window — typically dozens of times — so six
# is still far below the artefact and comfortably above plausible emphasis.
_REPEAT_THRESHOLD = 6

# Boilerplate matches the WHOLE sentence only, never a substring or a word-count
# neighbourhood: anything looser deletes real speech ("Thanks for joining,
# everyone." is a normal meeting opener). Multi-word artefacts are covered by
# listing the full phrase in _WHISPER_BOILERPLATE, not by loosening the match.


def _is_boilerplate_line(line: str) -> bool:
    """Return True if *line* is exactly (case/punctuation aside) known boilerplate."""
    stripped = line.strip().rstrip(".!?,;:").strip().lower()
    if not stripped:
        return False
    return stripped in _WHISPER_BOILERPLATE


def _collapse_repeated_phrases(text: str) -> str:
    """Collapse runs of >= :data:`_REPEAT_THRESHOLD` identical sentences to one.

    Splits on sentence boundaries, keeping each sentence's trailing punctuation.
    Only CONSECUTIVE runs collapse: the same sentence recurring later in a
    meeting is ordinary speech, not an artefact.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        return text
    output: list[str] = []
    i = 0
    while i < len(sentences):
        current_norm = sentences[i].strip().lower()
        j = i + 1
        while j < len(sentences) and sentences[j].strip().lower() == current_norm:
            j += 1
        if j - i >= _REPEAT_THRESHOLD:
            output.append(sentences[i])
        else:
            output.extend(sentences[i:j])
        i = j
    return " ".join(output)


def filter_hallucinations(text: str) -> str:
    """Remove Whisper hallucination artefacts from a transcript.

    May return ``""`` when the whole transcript was hallucinated — which is the
    honest answer for a recording of silence, and is why callers treat an empty
    result as "no transcript" rather than passing it on.

    Every removal is logged, because this is the one step in the pipeline that
    can delete words the speaker actually said, and a silent deletion is
    indistinguishable from the model never having heard them. What each log line
    carries is deliberate: matched boilerplate is named verbatim, since it comes
    from the fixed :data:`_WHISPER_BOILERPLATE` vocabulary and so reveals nothing
    about the recording, whereas a collapsed repetition is reported only as a
    COUNT — that text is ordinary speech and belongs in the transcript, not in
    the log. Discarding the transcript outright is a warning rather than an info
    line, because the caller then reports "no transcript" and the recording is
    gone with no other trace.
    """
    if not text:
        return text
    before = len(re.split(r"(?<=[.!?])\s+", text))
    text = _collapse_repeated_phrases(text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    collapsed = before - len(sentences)

    kept: list[str] = []
    dropped: list[str] = []
    for sentence in sentences:
        (dropped if _is_boilerplate_line(sentence) else kept).append(sentence)

    if collapsed or dropped:
        logger.info(
            "stt hallucination filter: collapsed %d repeated sentence(s), "
            "dropped %d boilerplate line(s)%s",
            collapsed,
            len(dropped),
            (": " + "; ".join(sorted(set(dropped)))) if dropped else "",
        )

    result = " ".join(kept).strip()
    if not result:
        logger.warning(
            "stt hallucination filter: discarded the entire transcript as "
            "hallucinated (%d sentence(s) in, none kept); the caller will "
            "report no transcript for this recording",
            len(sentences),
        )
    return result
