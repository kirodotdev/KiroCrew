"""Triggers match Chinese, Japanese and Thai text written without spaces.

Those scripts put no space between words, so a sentence is one run of characters,
and a trigger word like ``周报`` has to be found inside that run for crew routing
and the skills loader to match an ordinary message.
"""

from __future__ import annotations

import pytest

from kiro_crew.trigger_match import rank_triggered, trigger_score, words_of


@pytest.mark.parametrize(
    ("text", "triggers"),
    [
        ("帮我总结一下这周的周报", "周报, 总结"),
        ("今週の週報をまとめてください", "週報"),
        ("ช่วยสรุปรายงานประจำสัปดาห์ให้หน่อย", "สรุปรายงาน"),
    ],
)
def test_a_trigger_word_matches_inside_unspaced_text(text: str, triggers: str) -> None:
    assert rank_triggered(text, [("bot", triggers)]) == [("bot", 1.0)]


def test_an_absent_unspaced_word_still_does_not_match() -> None:
    assert rank_triggered("帮我写一封邮件", [("bot", "周报, 总结")]) == []


def test_a_negative_vetoes_inside_unspaced_text() -> None:
    score, negated = trigger_score("周报, !总结", words_of("帮我总结一下这周的周报"))
    assert score == 1.0 and negated is True


def test_punctuation_inside_an_unspaced_trigger_still_separates_words() -> None:
    # ・ is punctuation, so コード・レビュー is the two words コード and レビュー.
    assert trigger_score("コード・レビュー", words_of("コード レビュー")) == (1.0, False)
    negative = trigger_score("review, !コード・レビュー", words_of("review コード レビュー"))
    assert negative == (1.0, True)


def test_a_latin_trigger_matches_inside_unspaced_text() -> None:
    # The run of one script ends where another starts, so a Latin word written
    # straight into Chinese text is a word of its own.
    assert rank_triggered("帮我review一下这个PR", [("bot", "review")]) == [("bot", 1.0)]


def test_a_word_joining_two_scripts_counts_once_toward_the_score() -> None:
    # Q3报告 is one word of the phrase, so the message lacking it misses one of
    # four words, not two of five.
    triggers = "deploy staging prod Q3报告"
    assert rank_triggered("deploy staging prod now", [("bot", triggers)]) == [("bot", 0.75)]
    exact = "=" + triggers
    assert rank_triggered("deploy staging prod now", [("bot", exact)]) == [("bot", 0.75)]
