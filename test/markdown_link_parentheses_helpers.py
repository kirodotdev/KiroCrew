"""Shared link fixtures and readers for the ``test_markdown_link_parentheses*`` modules.

The two parenthesised URLs, the AWS example key the split-key tests hide, Slack's rendering
and what a Slack or Discord reader sees, the regex unit's destination, the screen's
readings, and the nested-link shape the canonical-form tests build.
"""

from __future__ import annotations

import re

from kiro_crew.constants import md_link_destination
from kiro_crew.messaging import display_safety
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.slack.format import render_one_for_slack

#: Real URL shapes an answer cites: a Wikipedia disambiguation page and an MSDN
#: versioned page. Both carry one balanced pair, which is what CommonMark allows.
WIKI = "https://en.wikipedia.org/wiki/Python_(programming_language)"


def _slack(text: str) -> str:
    return render_one_for_slack(text).text


def _slack_reader_sees(wire: str) -> str:
    """Read native Slack links by splitting their body at the first pipe."""

    def visible_text(match: re.Match[str]) -> str:
        body = match.group(1)
        return body.split("|", 1)[1] if "|" in body else body

    return re.sub(r"<([^>\n]*)>", visible_text, wire)


#: Destinations with a backslash-escaped parenthesis. CommonMark reads the escape,
#: the regex unit does not, so the link is left as written rather than linked to
#: a URL cut at the escape.
_ESCAPED = [
    r"[a](https://example.com/a\)b)",
    r"[a](https://example.com/\(a)",
]


#: The regex unit built on the no-whitespace class the renderers use, as the
#: ``(...)`` a renderer requires around a destination, anchored at the start of
#: ``"(" + rest`` so its end offset is comparable with the walker's.
_UNIT_CHAR_CLASS = r"[^()\s]"
_UNIT_RE = re.compile(rf"\(({md_link_destination(_UNIT_CHAR_CLASS)}*)\)")


def _regex_destination(rest: str) -> tuple[str | None, int]:
    match = _UNIT_RE.match("(" + rest)
    if match is None:
        return None, 0
    return match.group(1), match.end() - 1


#: The AWS example access key, split by a link so that only a reader who sees the
#: link rendered away sees it whole.
_KEY = "AKIAIOSFODNN7EXAMPLE"


def _screen_readings(text: str) -> list[str]:
    return [
        canonicalize_display(text),
        *(reading(text) for reading in display_safety.FURTHER_READINGS),
    ]


_DISCORD_PAIR = re.compile(r"(~~|\|\||\*\*|__|\*|_)(.+?)\1")


def _discord_reader_sees(text: str) -> str:
    """Read message content as Discord shows it: every delimiter pair consumed,
    a ``[label](url)`` left as written."""
    return _DISCORD_PAIR.sub(r"\2", text)


#: The trigger that makes ``redact_for_display`` emit the canonical form: a link
#: whose url holds the key split by ``**``, whole on a plain-text sink only.
_URL_SPLIT_TRIGGER = f"[l](https://x/AKIA**{_KEY[4:]}**)"


def _nested_link(depth: int) -> str:
    """``[AKIA](u)`` wrapped in *depth* more ``[...](u)`` links, one level per pass."""
    link = "[AKIA](u)"
    for _ in range(depth):
        link = f"[{link}](u)"
    return link
