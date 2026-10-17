"""Every renderer links a parenthesised URL whole, and the screen reads the same link.

Slack, Telegram, WhatsApp and iMessage keep the whole destination; an escaped parenthesis
leaves the link as written; the shared regex unit closes where the extraction walker closes;
and the display-safety screen collapses every link a renderer shows, under a grammar no
renderer widens.
"""

from __future__ import annotations

import re

import pytest
from markdown_link_parentheses_helpers import (
    _ESCAPED,
    _KEY,
    WIKI,
    _regex_destination,
    _screen_readings,
    _slack,
    _slack_reader_sees,
)

from conftest import assert_rejected_without_backtracking
from kiro_crew.constants import md_link_destination
from kiro_crew.imessage.plaintext import to_plaintext
from kiro_crew.messaging import display_safety
from kiro_crew.messaging.display_safety import canonicalize_display, redact_for_display
from kiro_crew.messaging.outbound_files import _walk_destination
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.slack import format as slack_format
from kiro_crew.telegram.renderer import _md_to_telegram_html, _strip_md
from kiro_crew.whatsapp.renderer import to_whatsapp_text

MSDN = "https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms123401(v=vs.85)"


class TestSlackLinksTheWholeUrl:
    @pytest.mark.parametrize("url", [WIKI, MSDN])
    def test_a_balanced_pair_stays_inside_the_link(self, url):
        assert _slack(f"See [the page]({url}) for details.") == (
            f"See <{url}|the page> for details."
        )

    def test_a_parenthesis_around_the_link_stays_text(self):
        """The closing ``)`` of a sentence's aside is not part of the URL."""
        assert _slack("(see [docs](https://example.com/a))") == (
            "(see <https://example.com/a|docs>)"
        )

    def test_two_links_on_one_line_stay_separate(self):
        text = f"[one]({WIKI}) and [two]({MSDN})"
        assert _slack(text) == f"<{WIKI}|one> and <{MSDN}|two>"

    def test_an_unbalanced_destination_is_not_a_link(self):
        """CommonMark does not link it, so no half of it becomes a link target."""
        assert _slack("[a](https://example.com/(b") == "[a](https://example.com/(b"

    def test_an_image_still_passes_through_raw(self):
        """The ``!`` lookbehind still holds image syntax out of the rewrite."""
        assert _slack(f"![chart]({WIKI})") == f"![chart]({WIKI})"

    def test_a_group_after_a_non_opener_label_fragment_stays_in_the_url(self):
        assert _slack("[l](https://x/a](b))") == "<https://x/a](b)|l>"

    def test_an_ipv6_destination_stays_linked(self):
        assert _slack("[l](http://[::1]:8080/)") == "<http://[::1]:8080/|l>"

    @pytest.mark.parametrize(
        "source",
        [
            "[docs](https://example.com/a|b)",
            "[docs](https://example.com/a>b)",
        ],
    )
    def test_a_native_slack_link_delimiter_in_a_destination_stays_as_markdown(self, source):
        assert _slack(source) == source

    @pytest.mark.parametrize(
        "payload",
        [
            "AKIA[l](https://x/a|IOSFODNN7EXAMPLE)",
            "[AKIA](https://x/|IOSFODNN7EXAMPLE)",
        ],
    )
    def test_a_pipe_destination_cannot_become_a_native_slack_link(self, payload):
        rendered = _slack(payload)
        assert rendered == payload
        assert _KEY not in _slack_reader_sees(rendered)

    def test_the_destination_class_excludes_native_slack_link_delimiters(self):
        assert slack_format._LINK_RE is display_safety.SLACK_MARKDOWN_LINK
        pattern = slack_format._LINK_RE.pattern
        # The class appears twice per nesting level: once as a destination
        # character and once as the escapee of a backslash escape, so an escaped
        # ``|`` or ``>`` is refused as firmly as a bare one.
        assert pattern.count("[^()|>]") == 4
        assert "[^()]" not in pattern


class TestTelegramLinksTheWholeUrl:
    @pytest.mark.parametrize("url", [WIKI, MSDN])
    def test_the_href_carries_the_whole_url(self, url):
        assert _md_to_telegram_html(f"See [the page]({url}) now") == (
            f'See <a href="{url}">the page</a> now'
        )

    def test_the_plain_fallback_names_the_whole_url(self):
        assert _strip_md(f"See [Python]({WIKI}) now") == f"See Python ({WIKI}) now"

    def test_a_parenthesis_around_the_link_stays_text(self):
        assert _md_to_telegram_html("(see [docs](https://example.com/a))") == (
            '(see <a href="https://example.com/a">docs</a>)'
        )


class TestWhatsAppAndIMessageNameTheWholeUrl:
    def test_whatsapp_label_and_url(self):
        assert to_whatsapp_text(f"See [Python]({WIKI}) now") == f"See Python ({WIKI}) now"

    def test_whatsapp_label_that_is_the_url_collapses_to_it(self):
        """The bare-URL form needs the WHOLE url to compare equal to the label."""
        assert to_whatsapp_text(f"[{WIKI}]({WIKI})") == WIKI

    def test_imessage_label_and_url(self):
        assert to_plaintext(f"See [Python]({WIKI}) now") == f"See Python ({WIKI}) now"

    def test_imessage_image_names_its_whole_url(self):
        assert to_plaintext(f"![chart]({WIKI})") == f"chart ({WIKI})"


class TestAnEscapedParenthesisLeavesTheLinkAsWritten:
    @pytest.mark.parametrize("source", _ESCAPED)
    def test_slack(self, source):
        assert _slack(f"see {source} now") == f"see {source} now"

    @pytest.mark.parametrize("source", _ESCAPED)
    def test_imessage(self, source):
        assert to_plaintext(f"see {source} now") == f"see {source} now"


#: Every distinct character class a caller passes to the unit, in order: Slack;
#: the session preview, voice reply and artifact snippet; Telegram, WhatsApp,
#: iMessage and the artifact-deploy page; the display-safety screen.
_CALLER_CLASSES = ["[^()|>]", "[^()]", r"[^()\s]", r"[^()\n]"]


class TestTheRegexUnitIsTheWalkerBoundedToOneLevel:
    """Wherever ``md_link_destination`` matches, it closes at the same ``)`` as
    the walker, so no renderer links a destination the extraction reader would
    end elsewhere. Only the text can differ: the walker drops the backslash of an
    escaped non-parenthesis character, and ``_finish_destination`` rewrites or
    rejects what it collected after the close is found."""

    @pytest.mark.parametrize(
        "rest",
        [
            f"{WIKI}) tail",
            f"{MSDN}) tail",
            "https://x/a) more",
            "https://x/(b)c) and (c)",
            "https://x/(1)(2)) x",
            "https://x/a)",
        ],
    )
    def test_depth_one_without_escapes_agrees_with_the_walker(self, rest):
        destination, consumed = _walk_destination(rest)
        assert destination is not None
        assert _regex_destination(rest) == (destination, consumed)

    @pytest.mark.parametrize(
        "rest",
        [
            "https://x/((b)))",
            r"https://x/a\)b)",
            r"https://x/\(a)",
        ],
    )
    def test_deeper_nesting_and_escapes_give_no_answer(self, rest):
        assert _walk_destination(rest)[0] is not None
        assert _regex_destination(rest) == (None, 0)

    @pytest.mark.parametrize(
        ("rest", "matches", "text_differs"),
        [
            ("https://x/a](b)) t", True, False),
            ("https://x/a [b](c) d) t", False, False),
            ("<https://x/a>) t", True, True),
            (r"https://x/a\\b) t", True, True),
            (r"https://x/\[b\]) t", True, True),
            # An escaped bracket is one token with its backslash, so the ``[`` is not
            # read as the opener of a nested ``[label](``.
            (r"https://x/\[b](c)) t", True, True),
            (r"https://x/\]b](c)) t", True, True),
            ('https://x/a "t") t', True, True),
            # An escaped backslash is one token, so the ``)`` after it closes.
            (r"https://x/a\\) t", True, True),
            (r"https://x/a\\\\) t", True, True),
            (r"https://x/a\\(b)) t", True, True),
            # An unescaped backslash before a parenthesis is refused.
            (r"https://x/a\)b) t", False, False),
            (r"https://x/\(a) t", False, False),
            (r"https://x/(a\)b)) t", False, False),
            (r"https://x/a\\\)b) t", False, False),
        ],
    )
    def test_every_callers_class_ends_where_the_walker_ends(self, rest, matches, text_differs):
        destination, consumed = _walk_destination(rest)
        assert destination is not None
        for char_class in _CALLER_CLASSES:
            unit_re = re.compile(rf"\(({md_link_destination(char_class)}*)\)")
            match = unit_re.match("(" + rest)
            # A class that admits no whitespace links no titled destination, so
            # only the classes that admit every character of the walk are held to
            # a match.
            admits = re.fullmatch(rf"(?:{char_class}|[()])*", rest[:consumed]) is not None
            assert (match is not None) is (matches and admits), char_class
            if match is None:
                continue
            assert match.end() - 1 == consumed, char_class
            assert (match.group(1) != destination) is text_differs, char_class


#: Link destinations the contract below runs every renderer over: balanced,
#: unbalanced, nested past the supported depth, and around-the-link parentheses.
_CORPUS = [
    f"[Python]({WIKI})",
    f"[MSDN]({MSDN})",
    "[a](https://example.com/(b)c)",
    "[a](https://example.com/(b)",
    "[a](https://example.com/((b)))",
    "([a](https://example.com/a))",
    "[a](https://example.com/a)",
    "[a](x [b](https://example.com/b) y)",
    *_ESCAPED,
]

_RENDERERS = {
    "slack": _slack,
    "telegram-html": _md_to_telegram_html,
    "telegram-plain": _strip_md,
    "whatsapp": to_whatsapp_text,
    "imessage": to_plaintext,
}


class TestTheScreenCollapsesEveryLinkARendererShows:
    def test_a_parenthesised_url_collapses_to_its_label(self):
        assert canonicalize_display(f"see [Python]({WIKI}) now") == "see Python now"

    @pytest.mark.parametrize("renderer", sorted(_RENDERERS))
    @pytest.mark.parametrize("source", _CORPUS)
    def test_a_rendered_link_is_a_collapsed_link(self, renderer, source):
        """If a renderer turned ``[a](...)`` into its platform form, the screen
        must have reduced the same source to the label too.

        The reverse is allowed: the screen may collapse more than a renderer
        links, because collapsing only widens what the credential scan sees.
        """
        label = source.split("[", 1)[1].split("]", 1)[0]
        raw = f"[{label}]("
        rendered = _RENDERERS[renderer](source)
        if raw not in rendered:
            assert raw not in canonicalize_display(source), (
                f"{renderer} renders {source!r} as a link, but the display screen "
                f"leaves it raw, so a credential split around it is never scanned whole"
            )


_MUST_CATCH_LINK_SHAPES = [
    "[AKIA](https://x/((a)))IOSFODNN7EXAMPLE",
    "[AKIA](https://x/(((a))))IOSFODNN7EXAMPLE",
    "[AKIA](https://x/((((a)))))IOSFODNN7EXAMPLE",
    "[AKIA](https://x/((((((((a)))))))))IOSFODNN7EXAMPLE",
    r"[AKIA](https://x/p\)q)IOSFODNN7EXAMPLE",
    "[AKIA](<https://x/)>)IOSFODNN7EXAMPLE",
    "[AKIA](https://x/((a)))IOSFODNN7EXAMPLE)",
    "[AKIA](https://x/[y](z))IOSFODNN7EXAMPLE",
]

_MUST_STAY_CAUGHT_LINK_SHAPES = [
    "[AKIA](https://x/(a))IOSFODNN7EXAMPLE",
    '[AKIA](https://x "t")IOSFODNN7EXAMPLE',
    '[AKIA](https://x/(a) "t")IOSFODNN7EXAMPLE',
    "[x](https://a[AKIA](https://b)IOSFODNN7EXAMPLE)",
    "[x](https://a [AKIA](https://b)IOSFODNN7EXAMPLE)",
]

_KEYLESS_LINK_SHAPES = [
    f"See [Python]({WIKI}) and [MSDN]({MSDN}).",
    "[AKIA](a)(b)IOSFODNN7EXAMPLE",
    "[a](http://x/(y)/(z))",
    "[a](b",
    "[a](<b)",
    "Ordinary prose with [one](https://example.com/one) and "
    "[two](https://example.com/two) links.",
]


class TestBalancedLinkReadings:
    @pytest.mark.parametrize("payload", _MUST_CATCH_LINK_SHAPES)
    def test_every_commonmark_link_shape_is_redacted(self, payload):
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert safe != payload
        assert all(_KEY not in reading for reading in _screen_readings(safe))

    @pytest.mark.parametrize("depth", [2, 3, 4, 8])
    def test_parenthesis_nesting_is_not_depth_bounded(self, depth):
        destination = "https://x/" + "(" * depth + "a" + ")" * depth
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert all(_KEY not in reading for reading in _screen_readings(safe))

    @pytest.mark.parametrize("payload", _MUST_STAY_CAUGHT_LINK_SHAPES)
    def test_existing_caught_shapes_stay_caught(self, payload):
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert all(_KEY not in reading for reading in _screen_readings(safe))

    @pytest.mark.parametrize("payload", _KEYLESS_LINK_SHAPES)
    def test_keyless_text_is_byte_identical(self, payload):
        assert redact_for_display(payload, _default_redactor) == (payload, False)

    @pytest.mark.parametrize(
        "build_pump",
        [
            pytest.param(lambda n: "[a](" * n, id="openers"),
            pytest.param(lambda n: "[a](" + "(" * n + ")" * n + ")", id="nested-parentheses"),
            pytest.param(lambda n: "[" * n, id="brackets"),
            pytest.param(lambda n: "[a](" * n + ")" * n, id="openers-then-closers"),
            pytest.param(lambda n: "](<" * n, id="angle-closers"),
        ],
    )
    def test_each_reading_stays_linear(self, build_pump):
        """Every screen reading handles a pump of openers in linear CPU time.

        Ramped on thread CPU by ``assert_rejected_without_backtracking``: a single
        wall-clock sample would charge this reading for the time the worker spends
        descheduled behind its siblings."""
        assert_rejected_without_backtracking(_screen_readings, build_pump, budget_seconds=1.0)


class TestRendererLinkGrammarDoesNotWiden:
    @pytest.mark.parametrize("renderer", sorted(_RENDERERS))
    @pytest.mark.parametrize(
        "source",
        ["[l](https://x/((a)))", r"[l](https://x/p\)q)"],
    )
    def test_two_level_or_escaped_parentheses_do_not_link_whole(self, renderer, source):
        assert "[l](" in _RENDERERS[renderer](source)

    @pytest.mark.parametrize("renderer", sorted(_RENDERERS))
    def test_one_balanced_pair_still_links_whole(self, renderer):
        assert "[l](" not in _RENDERERS[renderer]("[l](https://x/(a))")
