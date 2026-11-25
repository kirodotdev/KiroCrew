"""These built-in skills fire on their own topic and stay quiet on the prose pinned below.

A trigger phrase fires when 70% of its words appear anywhere in the message, so a
phrase made only of common words fires on prose that merely uses them: "record" on
"Record a table of the results", "session expired" on "session" in one sentence and
"expired" in another. A phrase of four or more words fires with any one word
missing, so "look at the change" fires on "look at the" alone. With trigger
matching on (``skills.max_triggered`` above 0) each such match injects the whole
skill body into an unrelated turn. These cases pin the topic phrases that must
still fire and the prose that must not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.skills import SkillsLoader
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

BUILTIN = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "builtin_skills"


def _score(skill: str, text: str) -> tuple[float, bool]:
    # The loader's own parser, not a reimplementation of it.
    triggers = SkillsLoader._parse_frontmatter(BUILTIN / skill / "SKILL.md")["triggers"]
    return trigger_score(triggers, words_of(text))


@pytest.mark.parametrize(
    ("skill", "text"),
    [
        ("browser-recording", "record a video of the settings flow"),
        ("browser-recording", "make a gif of the wizard"),
        ("browser-recording", "I need a screen recording of the modal opening"),
        ("browser-auth", "the page is behind a login wall"),
        ("browser-auth", "it stops at a captcha"),
        ("browser-auth", "it asks for a 2fa code"),
        ("frontend-design-workflow", "show me mockups before you build it"),
        ("frontend-design-workflow", "redesign the settings panel"),
        ("frontend-design-workflow", "add a new component to the sidebar"),
        ("widgets", "make a chart of CI duration"),
        ("widgets", "render the results as a styled table widget"),
        ("artifacts", "iterate on the CR queue widget"),
        ("artifacts", "revert the widget to the previous version"),
        ("session-summaries", "summarize this session"),
        ("web-verify", "add screenshots to the pr"),
        ("web-verify", "verify the ui before you push"),
        ("kirocrew-dev/writing-tests", "fix the flaky test in the cron suite"),
        ("web-browse", "Open https://example.com in the browser and show me the page."),
        ("web-browse", "show me this site"),
        ("crystallize", "save this as a skill"),
        ("crystallize", "turn this into a skill"),
        ("crystallize", "create an active skill from this session"),
        ("feature-request", "I have a feature request"),
        ("debug-gateway", "my fix did not land in the gateway"),
        ("debug-gateway", "did my fix land in the gateway?"),
    ],
)
def test_topic_phrases_fire(skill: str, text: str) -> None:
    score, negated = _score(skill, text)
    assert not negated
    assert score >= MIN_TRIGGER_OVERLAP, f"{skill}: {text!r} scored {score}"


@pytest.mark.parametrize(
    ("skill", "text"),
    [
        ("browser-recording", "Record a table of the results and the fault for each test."),
        ("browser-recording", "Rebase after recording main's commit and the check name."),
        ("browser-recording", "record the flow of the review in the ledger"),
        ("browser-auth", "The session lease expired before the worker reported back."),
        ("browser-auth", "Please sign in to the portal and take over the review."),
        (
            "frontend-design-workflow",
            "Add a new section to the design doc and change the page title.",
        ),
        ("frontend-design-workflow", "The UI test failed after the last change."),
        ("frontend-design-workflow", "No visual diff; the change only touches the parser."),
        ("widgets", "Record a table of the results and the fault for each test."),
        ("artifacts", "Which Python version does the gateway run?"),
        ("session-summaries", "What is this test about?"),
        ("web-verify", "Take a look at the logs, the test passes."),
        ("web-verify", "Open a PR for the fix."),
        ("kirocrew-dev/writing-tests", "Fix a typo in the test name."),
        ("web-browse", "Show me the diff for the last commit."),
        ("web-browse", "Open the file in the editor."),
        ("crystallize", "Turn this into a list of steps."),
        ("crystallize", "Save this as a draft for later."),
        ("crystallize", "Create a branch from this commit."),
        ("crystallize", "Create an issue for the active incident."),
        ("crystallize", "Which skill is active in this session?"),
        ("feature-request", "I have an hour before the meeting."),
        ("debug-gateway", "Fix the import; it did not compile."),
        ("artifacts", "What have we got left to do?"),
    ],
)
def test_ordinary_prose_does_not_fire(skill: str, text: str) -> None:
    score, _negated = _score(skill, text)
    assert score < MIN_TRIGGER_OVERLAP, f"{skill}: {text!r} scored {score}"
