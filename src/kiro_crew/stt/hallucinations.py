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

# Hallucination filter — suppress Whisper-family transcription artefacts.
#
# Whisper models fed silence or low-energy audio produce two recognisable
# artefacts, and both are worse than an empty transcript in this app: the text
# goes to agents, so a hallucinated sign-off becomes a meeting note, and a phrase
# repeated forty times becomes forty note lines.
#
#  1. One phrase repeating ("Thank you. Thank you. Thank you. …")
#  2. Boilerplate unrelated to the audio — subtitle credits, sign-offs, stock
#     phrases memorised from the training set's video captions.
#
# Applied to every Whisper-family provider. The repetition collapse below is
# pure text logic and language-independent; the boilerplate list is English-only
# and matches nothing in a zh-CN or de-DE transcript, so a non-English recording
# gets the repetition half of this filter and none of the phrase half.
# AWS Transcribe uses a different decoder and does not produce these,
# so it is deliberately excluded rather than filtered "just in case" — running
# the filter there could only ever delete genuine speech.
# ---------------------------------------------------------------------------

# Boilerplate phrases Whisper hallucinates on silence. Compared case-insensitively.
# LIST DISCIPLINE: an entry must be a CAPTION ARTEFACT — text that exists because
# a transcript was produced, not because anyone spoke. "Implausible as dictated
# speech" was the earlier bar and it was not strict enough: it admitted sign-offs
# and subscribe CTAs ("thank you for watching", "don't forget to subscribe",
# "hit the bell", "see you in the next video"), which anyone recording a demo or
# dictating a video script says out loud. Because the match is whole-sentence and
# a transcript filtered down to nothing returns None, such an entry can delete the
# only sentence a recording had — the speaker's own words, unrecoverable. Those
# entries are gone, along with the ordinary-speech phrases dropped before them
# ("goodbye", "copyright", "thanks for listening", "thanks for joining", "see you
# next time", "all rights reserved").
#
# What remains is attribution text a caption track carries about itself. Nobody
# utters "Subtitles by the Amara.org community" into a voice memo, so no reading of
# these deletes speech.
#
# Residual, accepted deliberately: a single un-repeated hallucinated sign-off now
# survives into the transcript. That is the safe direction of the trade — one stray
# line a reader can see and ignore, versus silently destroying real speech — and the
# repetition collapse below still removes the far more common form of this artefact,
# where the model emits the same sign-off for the rest of the decode window.
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

# Boilerplate matches the WHOLE sentence only, never a substring and never a
# word-count neighbourhood. Anything looser deletes real speech: a bare
# substring rule drops "Thanks for joining today's standup, let's start", and
# even a one-word slack drops "Thanks for joining, everyone." — a normal
# meeting opener. This filter runs on every Whisper-family transcript, so a
# false positive is silent loss of genuine speech; a false negative is one
# stray boilerplate line, which the repetition collapse usually removes anyway.
# Known multi-word artefact shapes ("Subtitles by Amara.org") are covered by
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
