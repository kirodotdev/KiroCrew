"""The shared label guard: one refusal/prose check for every label path.

A guard that lives in one module and is called from one place leaves every
other label path (the Slack/Telegram namer, the nav link chips, the session
summary) storing a model refusal. The behavioural tests per path live beside
those paths; this file pins the two things only the module can promise: the
ceilings are parameters, and the dashboard title runs THIS code rather than a
private copy that could drift.
"""

from __future__ import annotations

from kiro_crew import label_guard
from kiro_crew.dashboard import chat_title
from kiro_crew.label_guard import is_verdict_reply, looks_like_prose


def test_dashboard_title_delegates_to_the_shared_guard():
    """Mutation: give chat_title back a private ``_looks_like_prose`` -- red."""
    assert chat_title._looks_like_prose is looks_like_prose
    assert chat_title._is_verdict_reply is is_verdict_reply


def test_word_ceiling_is_a_parameter():
    """A 15-word line is prose for a 3-6 word title but a legitimate 18-word
    summary. Mutation: ignore ``max_words`` -- red on one of the two."""
    fifteen = " ".join(["word"] * 15)
    assert looks_like_prose(fifteen)
    assert not looks_like_prose(fifteen, max_words=36)


def test_unspaced_ceiling_is_a_parameter():
    """Same for the unspaced-script ceiling (CJK is one ``str.split`` word)."""
    thirty = "修" * 30
    assert looks_like_prose(thirty)
    assert not looks_like_prose(thirty, max_unspaced_chars=72)


def test_openers_fire_regardless_of_ceilings():
    """A refusal is caught by shape even when the ceilings are generous."""
    assert looks_like_prose("I cannot access that link", max_words=1000, max_unspaced_chars=1000)
    assert looks_like_prose("\uc8c4\uc1a1\ud569\ub2c8\ub2e4", max_words=1000)


def test_openers_are_a_parameter():
    """A path whose legitimate output opens like narration passes its own
    tuple; the entries it keeps still fire."""
    kept = tuple(o for o in label_guard.PROSE_OPENERS if o != "the conversation")
    assert looks_like_prose("The conversation covers redis tuning")
    assert not looks_like_prose("The conversation covers redis tuning", openers=kept)
    assert looks_like_prose("I cannot summarize this", openers=kept)


def test_sentence_shape_signals_can_be_switched_off():
    """The terminator and Korean-conjugation signals tell a name from a
    sentence; a path whose label IS a sentence turns them off, and the opener
    and ceiling signals keep working."""
    two_clauses = "User tunes redis. Assistant adds retry"
    korean_polite = "\uc0ac\uc6a9\uc790\uac00 redis\ub97c \uc870\uc815\ud569\ub2c8\ub2e4"
    assert looks_like_prose(two_clauses)
    assert looks_like_prose(korean_polite)
    assert not looks_like_prose(two_clauses, sentence_shape=False)
    assert not looks_like_prose(korean_polite, sentence_shape=False)
    assert looks_like_prose("Sorry, I cannot help. Try again", sentence_shape=False)
    assert looks_like_prose(" ".join(["w"] * 13), sentence_shape=False)


def test_verdict_with_reason_versus_title_opening_with_the_word():
    assert is_verdict_reply("SKIP - too vague", ("SKIP",))
    assert is_verdict_reply("skip: greetings only", ("SKIP",))
    assert not is_verdict_reply("SKIP and KEEP handling", ("SKIP",))
    assert not is_verdict_reply("SKIP_TESTS env var flag", ("SKIP",))


def test_title_defaults_match_the_documented_contract():
    assert label_guard.TITLE_MAX_WORDS == 12
    assert label_guard.TITLE_MAX_UNSPACED_CHARS == 24
