"""A Markdown link whose URL holds parentheses keeps its whole URL on every channel.

``[Python](https://en.wikipedia.org/wiki/Python_(programming_language))`` is valid
CommonMark: a link destination may contain parentheses when they balance. A
renderer that ends the URL at its first ``)`` links ``.../Python_(programming_language``
-- a page that does not exist -- and leaves a stray ``)`` after the label; one that
refuses parentheses outright leaves the markup raw.

The display-safety screen is tested beside the renderers because the two must
agree: the screen collapses a link to its label to see what the reader will see,
so a link a renderer shows but the screen does not collapse is a place a
credential split around the link reaches the reader whole.
"""

from __future__ import annotations

import html
import itertools
import random
import re
import time

import pytest

from kiro_crew import preview_text, voice_reply
from kiro_crew.constants import md_link_destination
from kiro_crew.dashboard.handlers import artifacts as artifact_handlers
from kiro_crew.deploy import render as deploy_render
from kiro_crew.discord.renderer import _redact_transformed as discord_redact_transformed
from kiro_crew.imessage import plaintext as imessage_plaintext
from kiro_crew.imessage.plaintext import to_plaintext
from kiro_crew.messaging import display_safety
from kiro_crew.messaging.display_safety import (
    _MD_LINK,
    _REDACTION_TAG,
    DISPLAY_SETTLING_PASSES,
    TELEGRAM_FALLBACK_FENCE,
    TELEGRAM_FALLBACK_LINK,
    TELEGRAM_FALLBACK_LINK_TEXT,
    TELEGRAM_FALLBACK_PASSES,
    canonicalize_display,
    redact_for_display,
)
from kiro_crew.messaging.outbound_files import _walk_destination
from kiro_crew.messaging.renderer import _default_redactor, count_redaction_tags
from kiro_crew.messaging.split import _flattened_for_any_cut
from kiro_crew.security import (
    CREDENTIAL_REDACTION_TAGS,
    EXFILTRATION_REDACTION_TAG_PREFIX,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.slack import format as slack_format
from kiro_crew.slack.format import render_one_for_slack
from kiro_crew.telegram import renderer as telegram_renderer
from kiro_crew.telegram.renderer import (
    _display_safe,
    _md_to_telegram_html,
    _strip_md,
)
from kiro_crew.whatsapp import renderer as whatsapp_renderer
from kiro_crew.whatsapp.renderer import to_whatsapp_text

#: Real URL shapes an answer cites: a Wikipedia disambiguation page and an MSDN
#: versioned page. Both carry one balanced pair, which is what CommonMark allows.
WIKI = "https://en.wikipedia.org/wiki/Python_(programming_language)"
MSDN = "https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms123401(v=vs.85)"


def _slack(text: str) -> str:
    return render_one_for_slack(text).text


def _slack_reader_sees(wire: str) -> str:
    """Read native Slack links by splitting their body at the first pipe."""

    def visible_text(match: re.Match[str]) -> str:
        body = match.group(1)
        return body.split("|", 1)[1] if "|" in body else body

    return re.sub(r"<([^>\n]*)>", visible_text, wire)


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


#: Destinations with a backslash-escaped parenthesis. CommonMark reads the escape,
#: the regex unit does not, so the link is left as written rather than linked to
#: a URL cut at the escape.
_ESCAPED = [
    r"[a](https://example.com/a\)b)",
    r"[a](https://example.com/\(a)",
]


class TestAnEscapedParenthesisLeavesTheLinkAsWritten:
    @pytest.mark.parametrize("source", _ESCAPED)
    def test_slack(self, source):
        assert _slack(f"see {source} now") == f"see {source} now"

    @pytest.mark.parametrize("source", _ESCAPED)
    def test_imessage(self, source):
        assert to_plaintext(f"see {source} now") == f"see {source} now"


#: The regex unit built on the no-whitespace class the renderers use, as the
#: ``(...)`` a renderer requires around a destination, anchored at the start of
#: ``"(" + rest`` so its end offset is comparable with the walker's.
_UNIT_CHAR_CLASS = r"[^()\s]"
_UNIT_RE = re.compile(rf"\(({md_link_destination(_UNIT_CHAR_CLASS)}*)\)")

#: Every distinct character class a caller passes to the unit, in order: Slack;
#: the session preview, voice reply and artifact snippet; Telegram, WhatsApp,
#: iMessage and the artifact-deploy page; the display-safety screen.
_CALLER_CLASSES = ["[^()|>]", "[^()]", r"[^()\s]", r"[^()\n]"]


def _regex_destination(rest: str) -> tuple[str | None, int]:
    match = _UNIT_RE.match("(" + rest)
    if match is None:
        return None, 0
    return match.group(1), match.end() - 1


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


#: The AWS example access key, split by a link so that only a reader who sees the
#: link rendered away sees it whole.
_KEY = "AKIAIOSFODNN7EXAMPLE"


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


def _screen_readings(text: str) -> list[str]:
    return [
        canonicalize_display(text),
        *(reading(text) for reading in display_safety.FURTHER_READINGS),
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
        "payload",
        [
            "[a](" * 5_000,
            "[a](" + "(" * 3_000 + ")" * 3_000 + ")",
            "[" * 5_000,
            "[a](" * 400 + ")" * 400,
            "](<" * 3_000,
        ],
    )
    def test_each_reading_stays_linear(self, payload):
        started = time.perf_counter()
        _screen_readings(payload)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, elapsed


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


class TestAnOuterLinkCannotSwallowAnInnerOne:
    """A destination may hold a balanced ``(...)``, but not the ``](...)`` of a link
    nested inside it.

    ``[x](a [AKIA](u)REST)`` is not one link to a CommonMark reader: an unbracketed
    destination admits no space, so the dashboard and Discord link ``[AKIA](u)`` and
    show ``AKIAREST`` joined. A screen that read the outer span as one link would
    collapse it to ``x``, scan no key, and pass the message through unredacted.
    The unit refuses a ``[`` that opens a nested ``[label](``, so the outer span
    does not match, the inner link collapses, and the scan sees the joined key.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            f"[x](a [AKIA](u){_KEY[4:]})",
            f"[x](a[AKIA](u){_KEY[4:]})",
        ],
    )
    def test_a_key_split_by_a_nested_link_is_redacted(self, payload):
        assert _KEY in canonicalize_display(payload)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)

    @pytest.mark.parametrize(
        "payload",
        [
            # A titled destination and an angle-bracketed one with a space, which
            # CommonMark links, and a space destination, which Slack links. A screen
            # class that excluded whitespace to refuse the nested shape would leave
            # all three raw, and each would reach its reader joined.
            f'[AKIA](https://x "t"){_KEY[4:]}',
            f"[AKIA](<a b>){_KEY[4:]}",
            f"[AKIA](a b){_KEY[4:]}",
        ],
    )
    def test_a_destination_with_a_space_is_still_collapsed(self, payload):
        assert canonicalize_display(payload) == _KEY
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe

    def test_a_group_after_a_non_opener_label_fragment_is_redacted(self):
        payload = f"[AKIA](https://x/a](b)){_KEY[4:]}"
        assert canonicalize_display(payload) == _KEY
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)

    def test_a_balanced_pair_still_links_beside_the_refusal(self):
        """The refusal is scoped to a nested ``[label](``; a URL group is untouched."""
        assert _slack(f"see [Python]({WIKI}) now") == f"see <{WIKI}|Python> now"
        assert canonicalize_display(f"[AKIA]({WIKI}){_KEY[4:]}") == _KEY


class TestAKeySplitByEmphasisInsideAUrlIsRedacted:
    """The canonical form drops a collapsed link's url; a plain-text sink prints it.

    ``[l](https://x/(a)/AKIA**REST**)`` collapses to ``l`` on the screen, so the
    url and the key split inside it leave the canonical scan, and the literal scan
    sees ``AKIA**`` and matches nothing. Telegram's ``_strip_md`` fallback (taken
    when an HTML edit or send is rejected) strips the ``**`` and prints
    ``l (https://x/(a)/AKIAIOSFODNN7EXAMPLE)``, the key whole. The screen scans
    that plain-text reading too and answers with the canonical form, which carries
    no url. The url without parentheses and the Slack form leak the same way and
    are closed by the same reading.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            f"[l](https://x/(a)/AKIA**{_KEY[4:]}**)",
            f"[l](https://x/AKIA**{_KEY[4:]}**)",
            f"<https://x/AKIA**{_KEY[4:]}**|l>",
        ],
    )
    def test_the_key_is_redacted_and_absent_from_the_plain_text_fallback(self, payload):
        assert _KEY not in canonicalize_display(payload)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in _strip_md(safe)
        assert _KEY not in _strip_md(_display_safe(payload))

    def test_emphasis_beside_a_parenthesised_link_without_a_key_is_untouched(self):
        payload = "**bold** see [docs](https://x/a_(b))"
        assert redact_for_display(payload, _default_redactor) == (payload, False)


class TestAKeyJoinedByALinkAnEarlierPassCloses:
    """``AKIA[IOSF...]**(https://x)**`` is no link to the screen's link pass, which
    runs first; the emphasis pass then drops the ``**`` and the canonical form
    ``AKIA[IOSF...](https://x)`` scans as split. Telegram's ``_strip_md`` removes
    the ``**`` and THEN flattens the link to ``label (url)``, showing
    ``AKIAIOSFODNN7EXAMPLE (https://x)``: the key whole. The plain reading has to
    apply the fallback's link pass after its delimiter passes, as the fallback
    does, so the screen sees that join and answers with the canonical form, which
    the settling passes then collapse and redact.
    """

    _JOINED = [
        ("double-star", f"AKIA[{_KEY[4:]}]**(https://x)**"),
        ("double-underscore", f"AKIA[{_KEY[4:]}]__(https://x)__"),
        ("inline-code", f"AKIA[{_KEY[4:]}]`(https://x)`"),
        ("head-before-a-longer-label", f"AKIAIOSF[{_KEY[8:]}]**(https://x)**"),
    ]
    _LEFT_ALONE = [
        ("rest-after-the-url", f"[AKIA]**(https://x)**{_KEY[4:]}"),
        ("split-label-rest-after-the-url", f"[{_KEY[:10]}]**(https://x)**{_KEY[10:]}"),
        ("a-space-before-the-url", f"AKIA[{_KEY[4:]}] **(https://x)**"),
        ("a-space-inside-the-emphasis", f"AKIA[{_KEY[4:]}]** (https://x)**"),
    ]

    @pytest.mark.parametrize(("case_name", "payload"), _JOINED, ids=[c[0] for c in _JOINED])
    def test_the_key_the_fallback_would_join_is_redacted(self, case_name, payload):
        assert _KEY not in canonicalize_display(payload)
        assert _KEY in _strip_md(payload), case_name
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)
        assert _KEY not in _strip_md(safe)
        assert _KEY not in _strip_md(_display_safe(payload))
        assert _KEY not in _md_to_telegram_html(_display_safe(payload))

    @pytest.mark.parametrize(("case_name", "payload"), _LEFT_ALONE, ids=[c[0] for c in _LEFT_ALONE])
    def test_a_shape_the_fallback_keeps_split_is_left_as_written(self, case_name, payload):
        assert _KEY not in _strip_md(payload), case_name
        assert redact_for_display(payload, _default_redactor) == (payload, False)


class TestAKeyJoinedByANativeLinkInsideARawMarkdownLink:
    """``[l](https://x/AK<u|IA>IOSF...)`` collapses to ``l`` on the screen, so the
    url and everything in it leave the canonical scan, and the literal scan sees
    ``AK<u|IA>IOSF...``, no key. Slack leaves the Markdown raw, because a ``|`` or
    ``>`` in the destination cannot go into a native link, and renders the embedded
    ``<u|IA>`` as a native link showing ``IA``: the reader sees the key whole. The
    screen scans that reading too, Markdown links raw and native links collapsed,
    and answers with the canonical form, which carries no url.
    """

    _JOINED = [
        ("token-in-the-middle", f"[l](https://x/AK<u|IA>{_KEY[4:]})"),
        ("token-at-the-start", f"[l](https://x/<u|AKIA>{_KEY[4:]}|)"),
        ("token-at-the-end", f"[l](https://x/AKIA<u|{_KEY[4:]}>)"),
        ("two-tokens", f"[l](https://x/AK<u|IA>IOSF<v|ODNN>{_KEY[12:]})"),
        ("angle-bracket-destination", f"[l](https://x/>AK<u|IA>{_KEY[4:]})"),
        ("token-in-the-label", f"[AK<u|IA>{_KEY[4:]}](https://x/a|b)"),
    ]

    @pytest.mark.parametrize(("case_name", "payload"), _JOINED, ids=[c[0] for c in _JOINED])
    def test_the_key_a_slack_reader_would_join_is_redacted(self, case_name, payload):
        assert _KEY not in payload
        assert _KEY in _slack_reader_sees(payload), case_name
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)
        assert _KEY not in _slack_reader_sees(_slack(payload))

    @pytest.mark.parametrize("head", range(1, len(_KEY) - 1, 3))
    @pytest.mark.parametrize("width", [1, 2, 5])
    def test_every_cut_of_the_key_is_redacted(self, head, width):
        tail = min(head + width, len(_KEY))
        payload = f"[l](https://x/{_KEY[:head]}<u|{_KEY[head:tail]}>{_KEY[tail:]})"
        assert _KEY not in payload
        assert _KEY in _slack_reader_sees(payload)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in _slack_reader_sees(_slack(payload))
        assert _KEY not in canonicalize_display(safe)

    @pytest.mark.parametrize("head", range(1, 19, 3))
    @pytest.mark.parametrize("width", [1, 2, 5])
    def test_a_cut_of_text_without_a_key_passes_through_byte_identical(self, head, width):
        text = "abcdefghijklmnopqrst"
        tail = min(head + width, len(text))
        payload = f"[l](https://x/{text[:head]}<u|{text[head:tail]}>{text[tail:]})"
        assert redact_for_display(payload, _default_redactor) == (payload, False)
        assert _slack(payload) == payload


#: Delimiter pairs a Markdown-rendering platform consumes that are in none of
#: Telegram's fallback passes, so only a link pass keeps or drops the url around them.
_PAIRS_OUTSIDE_THE_FALLBACK = [
    ("double-tilde", "~~"),
    ("spoiler", "||"),
    ("single-star", "*"),
    ("single-underscore", "_"),
]

_DISCORD_PAIR = re.compile(r"(~~|\|\||\*\*|__|\*|_)(.+?)\1")


def _discord_reader_sees(text: str) -> str:
    """Read message content as Discord shows it: every delimiter pair consumed,
    a ``[label](url)`` left as written."""
    return _DISCORD_PAIR.sub(r"\2", text)


class TestAKeySplitByAPairInsideAParenthesisedUrlIsRedacted:
    """``[l](https://x/(a)/AKIA~~IOSF...~~)`` collapses to ``l`` on the screen, so
    the url and the key split inside it leave the canonical scan; the literal scan
    sees ``AKIA~~`` and matches nothing, and the plain reading drops no ``~~``. A
    link grammar whose destination admits no parenthesis group leaves the link as
    written, its url text, where the emphasis pass joins the key: the screen scans
    that reading too, so a url the wider grammar links is never a url that leaves
    the scan. Discord renders the ``~~`` away and shows the joined key in message
    content, so the Discord path is checked as well.
    """

    @staticmethod
    def _payload(pair: str, tail: str = _KEY[4:]) -> str:
        return f"[l](https://x/(a)/AKIA{pair}{tail}{pair})"

    @pytest.mark.parametrize(("case_name", "pair"), _PAIRS_OUTSIDE_THE_FALLBACK)
    def test_the_key_is_redacted_on_every_reading(self, case_name, pair):
        payload = self._payload(pair)
        assert _KEY not in canonicalize_display(payload), case_name
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)
        assert _KEY not in _strip_md(safe)

    @pytest.mark.parametrize(("case_name", "pair"), _PAIRS_OUTSIDE_THE_FALLBACK)
    def test_discord_never_shows_the_joined_key(self, case_name, pair):
        payload = self._payload(pair)
        assert _KEY in _discord_reader_sees(payload), case_name
        sent = discord_redact_transformed(payload)
        assert _KEY not in _discord_reader_sees(sent), case_name
        assert _KEY not in canonicalize_display(sent), case_name

    @pytest.mark.parametrize(("case_name", "pair"), _PAIRS_OUTSIDE_THE_FALLBACK)
    def test_the_same_shape_without_a_key_passes_through_byte_identical(self, case_name, pair):
        payload = self._payload(pair, tail="rest")
        assert redact_for_display(payload, _default_redactor) == (payload, False), case_name
        assert discord_redact_transformed(payload) == payload, case_name

    def test_a_parenthesised_link_beside_emphasis_without_a_key_is_untouched(self):
        payload = f"~~gone~~ see [Python]({WIKI}) and ||spoiler||"
        assert redact_for_display(payload, _default_redactor) == (payload, False)


#: The trigger that makes ``redact_for_display`` emit the canonical form: a link
#: whose url holds the key split by ``**``, whole on a plain-text sink only.
_URL_SPLIT_TRIGGER = f"[l](https://x/AKIA**{_KEY[4:]}**)"


def _nested_link(depth: int) -> str:
    """``[AKIA](u)`` wrapped in *depth* more ``[...](u)`` links, one level per pass."""
    link = "[AKIA](u)"
    for _ in range(depth):
        link = f"[{link}](u)"
    return link


class TestTheEmittedCanonicalFormRendersAsItself:
    """A pass after the link pass can close a link the link pass went past.

    ``[AKIA]*(https://x)REST`` is text to the link pass, and the emphasis pass
    then drops the ``*`` and leaves ``[AKIA](https://x)REST``, a link on every
    renderer that joins the key. The canonical scan cleared the text before the
    star was gone, so a branch that emitted that canonical form handed Slack a
    link the screen never saw. The emitted text is rendered again until
    rendering it changes nothing, so what the reader sees is what was scanned.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            # A: the emphasis pass closes the link.
            f"{_URL_SPLIT_TRIGGER} [AKIA]*(https://x){_KEY[4:]}",
            # C: the same shape on the canonical branch, a key split by ``**``.
            f"AKIA**{_KEY[4:]}** [AKIA]*(https://x){_KEY[4:]}",
            # D: a format character closes the link.
            f"{_URL_SPLIT_TRIGGER} [AKIA]\u200b(https://x){_KEY[4:]}",
            # E: a collapsed Slack link closes the link.
            f"{_URL_SPLIT_TRIGGER} [AKIA]<u|>(https://x){_KEY[4:]}",
        ],
    )
    def test_a_link_closed_by_a_later_pass_is_redacted(self, payload):
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in canonicalize_display(safe)
        assert _KEY not in canonicalize_display(_slack(payload))
        assert _KEY not in _strip_md(safe)
        assert _KEY not in _strip_md(_display_safe(payload))

    def test_a_link_nested_one_level_deeper_settles(self):
        """Each pass peels one level, so the fix has to repeat until nothing moves."""
        payload = f"{_URL_SPLIT_TRIGGER} [[AKIA]*(https://x)]*(https://y){_KEY[4:]}"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert canonicalize_display(safe) == safe
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(_slack(payload))
        assert _KEY not in _strip_md(safe)

    @pytest.mark.parametrize("depth", [6, 9])
    def test_nesting_past_the_pass_bound_leaves_no_markup_to_render(self, depth):
        """The bound is reached before the key surfaces, so the fallback strips
        every delimiter: the urls stay between the halves as text."""
        payload = f"{_URL_SPLIT_TRIGGER} {_nested_link(depth)}{_KEY[4:]}"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)
        assert _KEY not in canonicalize_display(_slack(payload))
        assert "](" not in safe

    def test_the_construct_alone_is_left_as_written(self):
        """No reading of the text shows the key whole, so it goes out as written
        and the reader sees the brackets and the star."""
        payload = f"[AKIA]*(https://x){_KEY[4:]}"
        assert redact_for_display(payload, _default_redactor) == (payload, False)


def _nested_text(text: str, depth: int) -> str:
    """Wrap *text* in one Markdown link per settling pass."""
    for _ in range(depth):
        text = f"[{text}](u)"
    return text


def _telegram_html_reader_sees(wire: str) -> str:
    """Read Telegram HTML as visible text, without tag attributes."""
    return html.unescape(re.sub(r"<[^>]*>", "", wire))


def _imessage_reader_sees(text: str) -> str:
    """Apply the iMessage delivery path: flatten, then screen the result."""
    return redact_for_display(to_plaintext(text), _default_redactor)[0]


class TestOnlyRedactorTagsStayWhole:
    LOOKALIKE = "[REDACTED: SecretAccessKey #   : " "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY]"
    IPV6_URLS = [
        f"https://[fd00::1]/{_KEY}",
        f"https://[::ffff:1.2.3.4]/{_KEY}",
    ]

    @pytest.mark.parametrize(
        "tag",
        [
            *CREDENTIAL_REDACTION_TAGS,
            f"{EXFILTRATION_REDACTION_TAG_PREFIX}api.example.com]",
            *[_default_redactor(url) for url in IPV6_URLS],
        ],
    )
    def test_every_real_redactor_tag_matches(self, tag):
        assert _REDACTION_TAG.fullmatch(tag) is not None

    @pytest.mark.parametrize("url", IPV6_URLS)
    def test_an_ipv6_redactor_tag_survives_last_resort_paths(self, url):
        tag = _default_redactor(url)

        assert _REDACTION_TAG.fullmatch(tag) is not None
        for safe in [
            display_safety.redacted_without_markup(tag, _default_redactor),
            _flattened_for_any_cut(tag, _default_redactor),
        ]:
            assert safe == tag
            assert count_redaction_tags(safe) == (0, 1)

    @pytest.mark.parametrize("unsafe", ["#", " ", "*", "_", "`", "|", "<", ">", "~"])
    def test_a_suspicious_url_lookalike_with_markup_does_not_match(self, unsafe):
        lookalike = f"{EXFILTRATION_REDACTION_TAG_PREFIX}example{unsafe}com]"

        assert _REDACTION_TAG.fullmatch(lookalike) is None

    @pytest.mark.parametrize("unsafe", ["#", "*"])
    def test_a_bracketed_url_lookalike_with_markup_does_not_match(self, unsafe):
        lookalike = f"{EXFILTRATION_REDACTION_TAG_PREFIX}[fd00::{unsafe}1]]"

        assert _REDACTION_TAG.fullmatch(lookalike) is None

    def test_an_author_lookalike_loses_its_display_markup(self):
        safe = display_safety.redacted_without_markup(self.LOOKALIKE, _default_redactor)

        assert self.LOOKALIKE not in safe
        assert "#" not in safe


class TestATagShapedLabelPastTheSettlingBoundCannotBecomeALink:
    @staticmethod
    def _payload(label: str, *, separator: str = "", depth: int = 5) -> str:
        hidden_link = _nested_text(f"{label}(https://v)", depth)
        return f"{_URL_SPLIT_TRIGGER} {hidden_link}{separator}{_KEY[4:]}"

    @pytest.mark.parametrize(
        ("case_name", "label", "separator", "depth"),
        [
            ("tag-shaped-label-past-bound", "[REDACTED: AKIA]", "", 5),
            ("space-after-link", "[REDACTED: AKIA]", " ", 5),
            ("real-credential-tag-label", CREDENTIAL_REDACTION_TAGS[0], "", 5),
            ("tag-shaped-label-at-bound", "[REDACTED: AKIA]", "", 4),
        ],
    )
    def test_no_renderer_shows_the_joined_key(self, case_name, label, separator, depth):
        payload = self._payload(label, separator=separator, depth=depth)
        slack_wire = _slack(payload)
        visible_forms = {
            "slack": _slack_reader_sees(slack_wire),
            "telegram-plain": _strip_md(_display_safe(payload)),
            "telegram-html": _telegram_html_reader_sees(
                _md_to_telegram_html(_display_safe(payload))
            ),
            "whatsapp": to_whatsapp_text(payload),
            "imessage": _imessage_reader_sees(payload),
        }

        for renderer, visible in visible_forms.items():
            assert _KEY not in visible, (case_name, renderer, visible)


class TestTheSettlingBounds:
    _FALLBACK_MARKER = "<fallback-marker>"

    @pytest.mark.parametrize(
        ("passes_needed", "marker_survives"),
        [(4, True), (5, False)],
    )
    def test_the_literal_pass_bound_selects_settling_or_fallback(
        self, passes_needed, marker_survives
    ):
        payload = (
            f"{_URL_SPLIT_TRIGGER} {self._FALLBACK_MARKER}"
            f"{_nested_link(passes_needed - 1)}{_KEY[4:]}"
        )
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert (self._FALLBACK_MARKER in safe) is marker_survives
        assert _KEY not in canonicalize_display(safe)

    @pytest.mark.parametrize("delimiter", ["[", "]", "<", ">", "|", "*", "_", "~", "`"])
    def test_the_fallback_removes_every_literal_display_delimiter(self, monkeypatch, delimiter):
        def peel_one_layer(text):
            return text.removeprefix("layer:")

        redactor_inputs = []

        def recording_redactor(text):
            redactor_inputs.append(text)
            return _default_redactor(text)

        monkeypatch.setattr(display_safety, "canonicalize_display", peel_one_layer)
        payload = "layer:" * (DISPLAY_SETTLING_PASSES + 1) + f"AKIA{delimiter}{_KEY[4:]}"
        assert recording_redactor(payload) == payload

        safe = display_safety.settled_display_form(payload, recording_redactor)

        assert redactor_inputs[-1] == _KEY
        assert safe == CREDENTIAL_REDACTION_TAGS[0]


#: The link patterns every renderer composes from ``md_link_destination``. Each
#: label class must be no wider than the screen's ``_MD_LINK`` class: a label the
#: screen leaves raw but a renderer links is a split the screen never scanned.
_RENDERER_LINK_PATTERNS = {
    "slack": slack_format._LINK_RE,
    "telegram": TELEGRAM_FALLBACK_LINK,
    "whatsapp": whatsapp_renderer._LINK_RE,
    "imessage": imessage_plaintext._LINK_RE,
    "imessage-image": imessage_plaintext._IMAGE_RE,
}


def _label_class(link_pattern: re.Pattern[str]) -> str:
    """The character class of the label group, the first group after ``\\[``."""
    source = link_pattern.pattern
    start = source.index(r"\[(") + len(r"\[(")
    match = re.match(r"\[\^(?:\\.|[^\]])*\]", source[start:])
    assert match, source
    return match.group(0)


class TestRendererLabelsAreNoWiderThanTheScreens:
    """The screen's label admits no ``[``, ``]`` or line break. A renderer whose
    label class admitted them linked ``AKIA[IOSF...[z](u)`` from the first ``[``
    and showed ``AKIAIOSF...`` joined, while the screen saw only the inner link
    ``[z](u)`` collapse and scanned the key as split. Telegram's fallback did the
    same across a newline in the label.
    """

    _OPUS_SLACK_SHAPE = f"AKIA[{_KEY[4:]}[z](https://u)"
    _TELEGRAM_NEWLINE_SHAPE = f"AKIA[{_KEY[4:]}\nfoo](https://u)"

    @pytest.mark.parametrize("renderer", sorted(_RENDERER_LINK_PATTERNS))
    def test_the_label_class_is_the_screens(self, renderer):
        assert _label_class(_RENDERER_LINK_PATTERNS[renderer]) == _label_class(_MD_LINK)

    def test_slack_never_joins_a_key_around_a_bracket_in_the_label(self):
        payload = self._OPUS_SLACK_SHAPE
        assert _KEY not in canonicalize_display(payload)
        assert redact_for_display(payload, _default_redactor) == (payload, False)
        rendered = _slack(payload)
        assert rendered == f"AKIA[{_KEY[4:]}<https://u|z>"
        assert _KEY not in canonicalize_display(rendered)

    def test_telegram_never_joins_a_key_around_a_newline_in_the_label(self):
        payload = self._TELEGRAM_NEWLINE_SHAPE
        assert _KEY not in _strip_md(payload)
        assert _KEY not in _strip_md(_display_safe(payload))
        assert _KEY not in _md_to_telegram_html(_display_safe(payload))

    def test_whatsapp_never_joins_a_key_around_a_bracket_in_the_label(self):
        payload = self._OPUS_SLACK_SHAPE
        assert to_whatsapp_text(payload) == f"AKIA[{_KEY[4:]}z (https://u)"

    def test_imessage_never_joins_a_key_around_a_bracket_in_the_label(self):
        payload = self._OPUS_SLACK_SHAPE
        assert to_plaintext(payload) == f"AKIA[{_KEY[4:]}z (https://u)"

    def test_a_bracket_before_the_label_stays_text(self):
        """``[x[y](u)`` is ``[x`` followed by a link labelled ``y``."""
        assert _slack("[x[y](https://u)") == "[x<https://u|y>"
        assert _md_to_telegram_html("[x[y](https://u)") == '[x<a href="https://u">y</a>'
        assert _strip_md("[x[y](https://u)") == "[xy (https://u)"
        assert to_whatsapp_text("[x[y](https://u)") == "[xy (https://u)"
        assert to_plaintext("[x[y](https://u)") == "[xy (https://u)"

    def test_a_label_with_a_line_break_is_left_as_written(self):
        payload = "[a\nb](https://u)"
        assert _slack(payload) == payload
        assert _md_to_telegram_html(payload) == payload
        assert _strip_md(payload) == payload
        assert to_whatsapp_text(payload) == payload
        assert to_plaintext(payload) == payload

    @pytest.mark.parametrize("renderer", sorted(_RENDERER_LINK_PATTERNS))
    def test_a_run_of_openers_stays_fast(self, renderer):
        """A label class admitting ``[`` rescanned to the end of the text from
        every opener: quadratic in the length of attacker-supplied text."""
        pattern = _RENDERER_LINK_PATTERNS[renderer]

        def elapsed(repeats: int) -> float:
            text = "[b" * repeats
            started = time.perf_counter()
            assert pattern.sub("", text) == text
            return time.perf_counter() - started

        small, large = elapsed(5_000), elapsed(20_000)
        assert large < 0.5, (renderer, large)
        assert large <= max(8 * small, 0.05), (renderer, small, large)


#: The link patterns outside chat that read a destination with ``md_link_destination``:
#: the session preview, the voice reply, the artifact snippet and the deploy page.
#: Their label stops at the next ``[`` and, as CommonMark link text may, can span a
#: line break.
_OUTSIDE_CHAT_LINK_PATTERNS = {
    "preview": preview_text._LINK_RE,
    "preview-image": preview_text._IMAGE_RE,
    "voice": voice_reply._MARKDOWN_LINK_RE,
    "artifact-snippet": artifact_handlers._MD_LINK_RE,
    "deploy-page": deploy_render._LINK_RE,
}


class TestLinkPatternsOutsideChatStayLinear:
    """A label or destination that runs on past the next opener is rescanned to the
    end of the text from every ``[``, which is quadratic in the length of a message."""

    @pytest.mark.parametrize("opener", ["[b", "![b", "[b\n", "[b](x"])
    @pytest.mark.parametrize("site", sorted(_OUTSIDE_CHAT_LINK_PATTERNS))
    def test_a_run_of_openers_stays_fast(self, site, opener):
        pattern = _OUTSIDE_CHAT_LINK_PATTERNS[site]

        def elapsed(repeats: int) -> float:
            text = opener * repeats
            started = time.perf_counter()
            assert pattern.sub("", text) == text
            return time.perf_counter() - started

        small, large = elapsed(5_000), elapsed(20_000)
        assert large < 0.5, (site, opener, large)
        assert large <= max(8 * small, 0.05), (site, opener, small, large)

    @pytest.mark.parametrize("site", sorted(_OUTSIDE_CHAT_LINK_PATTERNS))
    def test_a_hard_wrapped_label_is_still_a_link(self, site):
        """CommonMark link text may span a line break, and these patterns read
        documents whose labels are hard-wrapped, so the label keeps the break."""
        text = "[two\nlines](https://u)"
        if site == "preview-image":
            text = "!" + text
        match = _OUTSIDE_CHAT_LINK_PATTERNS[site].search(text)
        assert match is not None and match.group(1) == "two\nlines", site


def _recording_redactor(warnings: list[str]):
    """``_default_redactor`` that also collects the credential redactor's warnings."""

    def redact(text: str) -> str:
        out, _ = redact_exfiltration_urls(text or "")
        out, credential_warnings = redact_credentials(out)
        warnings.extend(credential_warnings)
        return out

    return redact


class TestTheFallbackKeepsRedactionTagsWhole:
    """Past the settling bound every markup character is removed, but the ``[``
    and ``]`` of a tag the redactor wrote are not markup a renderer acts on: with
    them gone, ``?token=[REDACTED: credential]`` reads as a fresh opaque value,
    the final scan redacts its head again and hands the reader
    ``?token=[REDACTED: credential] credential`` plus a warning about a secret
    that was never there, and the text returned is not a redactor fixed point.
    """

    _OPAQUE_TOKEN_URL = "https://example.com/cb?token=q7Zt3mVx9pLw2Kd8"
    _PAST_THE_BOUND = f"{_URL_SPLIT_TRIGGER} {_nested_link(DISPLAY_SETTLING_PASSES + 2)}{_KEY[4:]}"

    def test_the_tag_pattern_matches_every_tag_the_redactor_writes(self):
        for tag in (
            *CREDENTIAL_REDACTION_TAGS,
            f"{EXFILTRATION_REDACTION_TAG_PREFIX}evil.example]",
        ):
            assert _REDACTION_TAG.fullmatch(tag), tag
        assert _REDACTION_TAG.search("[REDACTED: credential](https://u)").group(0) == (
            "[REDACTED: credential]"
        )

    def test_a_token_value_redacted_before_the_bound_keeps_its_tag(self):
        warnings: list[str] = []
        redactor = _recording_redactor(warnings)
        payload = f"{self._PAST_THE_BOUND} {self._OPAQUE_TOKEN_URL}"
        safe, redacted = redact_for_display(payload, redactor)
        assert redacted is True
        assert "?token=[REDACTED: credential]" in safe
        assert safe.count("[REDACTED: credential]") == 1
        assert "] credential" not in safe
        assert _KEY not in safe
        assert sum("token parameter" in warning for warning in warnings) == 1, warnings
        assert redactor(safe) == safe

    def test_a_suspicious_url_tag_keeps_its_brackets_past_the_bound(self):
        exfil_url = "https://evil.example/collect?d=" + "x" * 300
        safe = _default_redactor(exfil_url)
        assert safe.startswith(EXFILTRATION_REDACTION_TAG_PREFIX), safe
        payload = f"{self._PAST_THE_BOUND} {exfil_url}"
        out, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert safe in out
        assert _default_redactor(out) == out

    @pytest.mark.parametrize("depth", [6, 9])
    def test_the_existing_deep_nesting_cases_redact_to_themselves(self, depth):
        payload = f"{_URL_SPLIT_TRIGGER} {_nested_link(depth)}{_KEY[4:]}"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _default_redactor(safe) == safe

    def test_a_key_split_by_markup_on_the_fallback_path_is_still_redacted(self):
        payload = f"{self._PAST_THE_BOUND} AKIA**{_KEY[4:]}**"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)
        assert "**" not in safe


def _credentials_only(text: str) -> str:
    return redact_credentials(text)[0]


_TAG = CREDENTIAL_REDACTION_TAGS[0]
#: What follows a key or a tag: a parenthesised remark with a space, a plain
#: word, and a url. The last is the one the plain fallback also flattens.
_REMARKS = ["(rotated 2026-09-01)", "(see-runbook)", "(https://example.com/x)"]
#: Delimiter pairs around the remark, so an emphasis or spoiler pass removes them
#: and leaves a tag's ``]`` beside a ``(`` for a later link pass.
_REMARK_WRAPS = ["*{}*", "_{}_", "||{}||"]


def _tag_kept_whole(text: str, tags: int, after: str) -> None:
    """Every tag the redactor wrote is in *text* byte for byte, counted, and
    followed by *after*."""
    assert text.count(_TAG) == tags, text
    assert count_redaction_tags(text) == (tags, 0), text
    assert after in text, text
    assert _KEY not in text
    assert not any(_KEY in reading for reading in _every_reading(text)), text


class TestTheWholeTextFallbackKeepsRemovedRedactionTags:
    _URL = "https://evil.example/c?d=QUtJQUlPU0ZPRE5ON0VYQU1QTEVfc2VjcmV0X2RhdGFfaGVyZQ"

    def test_the_display_screen_keeps_a_suspicious_url_tag_hidden_by_a_link(self):
        payload = f"AKIA[]({self._URL}){_KEY[4:]}"
        suspicious_url_tag = _default_redactor(self._URL)

        safe, redacted = redact_for_display(payload, _default_redactor)

        assert redacted is True
        assert suspicious_url_tag in safe
        assert count_redaction_tags(safe) == (1, 1)
        assert _KEY not in safe
        assert not any(_KEY in reading for reading in _screen_readings(safe))

    def test_discord_keeps_a_suspicious_url_tag_hidden_by_a_link(self):
        payload = f"AKIA[]({self._URL}){_KEY[4:]}"
        suspicious_url_tag = _default_redactor(self._URL)

        delivered = discord_redact_transformed(payload)

        assert suspicious_url_tag in delivered
        assert count_redaction_tags(delivered) == (1, 1)
        assert _KEY not in delivered
        assert not any(_KEY in reading for reading in _screen_readings(delivered))

    def test_an_existing_credential_tag_hidden_by_a_link_is_delivered(self):
        payload = f"AKIA[]({_TAG}){_KEY[4:]}"

        safe, redacted = redact_for_display(payload, _default_redactor)

        assert redacted is True
        assert safe.count(_TAG) == 2
        assert count_redaction_tags(safe) == (2, 0)
        assert _KEY not in safe
        assert not any(_KEY in reading for reading in _screen_readings(safe))


class TestATagFollowedByAParenthesisIsNotALink:
    """A redactor-owned tag is an atom on every path that emits text.

    A key split by a spoiler or emphasis pair and followed by ``(...)`` is redacted
    to ``[REDACTED: credential](...)``. A settling pass that renders that whole text
    reads the tag's brackets as a link label, emits the bare label and drops the
    parenthesised text, so the delivered message carries no tag and
    :func:`count_redaction_tags` reports no redaction. The tag's ``]`` beside a
    ``(`` is the one way a rendering can use a tag's brackets; no emitting path
    may take it.
    """

    @pytest.mark.parametrize("remark", _REMARKS)
    @pytest.mark.parametrize("pair", ["||", "**"])
    def test_the_discord_path_keeps_the_tag_and_the_remark(self, pair, remark):
        payload = f"AKIA{pair}{_KEY[4:]}{pair}{remark}"
        delivered = discord_redact_transformed(payload)
        assert delivered == f"{_TAG}{remark}"
        _tag_kept_whole(delivered, 1, remark)

    @pytest.mark.parametrize("remark", _REMARKS)
    @pytest.mark.parametrize("pair", ["||", "**"])
    def test_the_display_screen_keeps_the_tag_and_the_remark(self, pair, remark):
        payload = f"AKIA{pair}{_KEY[4:]}{pair}{remark}"
        safe, redacted = redact_for_display(payload, _credentials_only)
        assert redacted is True
        assert safe == f"{_TAG}{remark}"
        _tag_kept_whole(safe, 1, remark)

    def test_a_tag_the_literal_scan_wrote_survives_the_canonical_branch(self):
        payload = f"{_KEY}(x) AKIA**{_KEY[4:]}**"
        safe, redacted = redact_for_display(payload, _credentials_only)
        assert redacted is True
        assert safe == f"{_TAG}(x) {_TAG}"
        _tag_kept_whole(safe, 2, "(x)")

    def test_a_tag_the_literal_scan_wrote_survives_a_further_reading(self):
        """Only the plain fallback joins ``AKIA[rest]**(https://u)**``; the tag
        written before it must not go with the answer to that reading."""
        payload = f"{_KEY}(https://x) AKIA[{_KEY[4:]}]**(https://u)**"
        assert _KEY not in canonicalize_display(_credentials_only(payload))
        safe, redacted = redact_for_display(payload, _credentials_only)
        assert redacted is True
        _tag_kept_whole(safe, 2, "(https://x)")

    @pytest.mark.parametrize(
        "path",
        [
            lambda text: display_safety.settled_display_form(text, _credentials_only),
            lambda text: display_safety.settled_display_form(
                text, _credentials_only, reading=slack_format._slack_client_display
            ),
            lambda text: display_safety.redacted_without_markup(text, _credentials_only),
            lambda text: display_safety.redacted_without_markup(
                text, _credentials_only, reading=slack_format._slack_client_display
            ),
            lambda text: _flattened_for_any_cut(text, _credentials_only),
        ],
        ids=["settled", "settled-slack", "past-the-bound", "past-the-bound-slack", "flattened"],
    )
    def test_a_tag_already_present_keeps_its_bytes_on_every_emitting_path(self, path):
        payload = f"{_TAG}(x) {_KEY}"
        delivered = path(payload)
        assert delivered.startswith(f"{_TAG}(x)"), delivered
        _tag_kept_whole(delivered, 2, "(x)")

    @pytest.mark.parametrize("wrap", _REMARK_WRAPS)
    def test_a_delimiter_pass_cannot_hand_the_link_pass_a_tag(self, wrap):
        payload = f"{_TAG}{wrap.format('(x)')}"
        for path in (
            lambda text: display_safety.settled_display_form(text, _credentials_only),
            lambda text: display_safety.settled_display_form(
                text, _credentials_only, reading=slack_format._slack_client_display
            ),
            lambda text: display_safety.redacted_without_markup(text, _credentials_only),
            discord_redact_transformed,
        ):
            delivered = path(payload)
            assert delivered.startswith(_TAG), delivered
            _tag_kept_whole(delivered, 1, "(x)")

    @pytest.mark.parametrize("wrap", _REMARK_WRAPS)
    def test_a_key_beside_a_wrapped_remark_keeps_its_tag_on_discord(self, wrap):
        payload = f"AKIA||{_KEY[4:]}||{wrap.format('(x)')}"
        delivered = discord_redact_transformed(payload)
        assert delivered.startswith(_TAG), delivered
        _tag_kept_whole(delivered, 1, "(x)")

    def test_a_key_joined_only_across_a_tag_is_still_redacted(self):
        """``AKIA[]([REDACTED: credential])REST`` shows the key on a reader that
        collapses the empty-labelled link, and the tag sat inside that link. The
        reading's redacted form is emitted, so the key is hidden and both input
        keys are counted."""
        payload = f"AKIA[]({_KEY}){_KEY[4:]}"
        literal = _credentials_only(payload)
        assert literal == f"AKIA[]({_TAG}){_KEY[4:]}"
        assert canonicalize_display(literal) == _KEY
        safe, redacted = redact_for_display(payload, _credentials_only)
        assert redacted is True
        assert count_redaction_tags(safe) == (2, 0), safe
        assert not any(_KEY in reading for reading in _every_reading(safe)), safe
        assert _credentials_only(safe) == safe


class TestAnEscapedBackslashClosesWhereTheWalkerCloses:
    """``\\\\`` before ``)`` is an escaped backslash, not an escaped parenthesis.

    ``[AKIA](https://x/a\\\\)REST`` is a link to a CommonMark reader, to the
    extraction walker and to Slack: the pair is one escape and the ``)`` after it
    closes the destination. A unit that refused every backslash before a
    parenthesis left it raw, so the screen collapsed nothing and the joined key
    reached the reader unredacted. The escape is one token, so the unit closes
    where the walker closes.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            f"[AKIA](https://x/a\\\\){_KEY[4:]}",
            f"[AKIA](https://x/a\\\\\\\\){_KEY[4:]}",
            f"[AKIA](https://x/a\\\\(b)){_KEY[4:]}",
        ],
    )
    def test_a_key_split_around_an_escaped_backslash_is_redacted(self, payload):
        assert canonicalize_display(payload) == _KEY
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert _KEY not in canonicalize_display(safe)

    def test_slack_links_a_destination_ending_in_an_escaped_backslash(self):
        assert _slack(r"see [l](https://x/a\\) now") == r"see <https://x/a\\|l> now"

    @pytest.mark.parametrize("source", _ESCAPED)
    def test_an_escaped_parenthesis_still_stays_as_written(self, source):
        """The token admits an escaped backslash, not an escaped parenthesis."""
        assert _slack(f"see {source} now") == f"see {source} now"
        assert "[a](" in canonicalize_display(f"see {source} now")


#: Destinations holding an escaped bracket before a ``label](`` that would open a
#: nested link if the bracket were unescaped, and the escaped-bracket shapes beside
#: them. Each is one link to a CommonMark reader, to the extraction walker and to
#: Discord, which reads a backslash and the character after it as one token.
_ESCAPED_BRACKET_DESTINATIONS = [
    r"https://x/\[b](c)",
    r"https://x/\]b](c)",
    r"https://example.com/[foo\](bar)",
    r"https://x/\[b\](c)",
    r"https://x/a\[b](c)d",
    r"https://x/\[b\]",
]


class TestAnEscapedBracketIsNotANestedOpener:
    """``[AKIA](https://x/\\[b](c))REST`` is one link wherever a reader honours the
    escape: ``\\[`` is an escaped bracket, so no ``[label](`` opens inside the url,
    and the balanced ``(c)`` sits inside it. A unit that consumed the backslash on
    its own re-read the ``[`` after it as a nested opener and refused the url, so the
    outer link stayed raw, the inner ``[b](c)`` collapsed instead, and the screen
    scanned a text that held no key while Discord showed ``AKIAREST`` joined. The
    escape and its escapee are one token, so the unit closes where the walker
    closes and the outer link collapses to its label.
    """

    @pytest.mark.parametrize("destination", _ESCAPED_BRACKET_DESTINATIONS)
    def test_a_key_split_around_an_escaped_bracket_is_redacted(self, destination):
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        assert canonicalize_display(payload) == _KEY
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert all(_KEY not in reading for reading in _every_reading(safe))

    @pytest.mark.parametrize("destination", _ESCAPED_BRACKET_DESTINATIONS)
    def test_the_discord_send_path_redacts_it(self, destination):
        """The literal text never holds the key; what Discord shows does, so the
        sent text is judged by its rendered form."""
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        sent = discord_redact_transformed(payload)
        assert sent != payload
        assert all(_KEY not in reading for reading in _every_reading(sent))

    @pytest.mark.parametrize("destination", _ESCAPED_BRACKET_DESTINATIONS)
    def test_the_same_shape_without_a_key_passes_through_byte_identical(self, destination):
        payload = f"see [l]({destination}) rest"
        assert redact_for_display(payload, _default_redactor) == (payload, False)
        assert discord_redact_transformed(payload) == payload

    @pytest.mark.parametrize("destination", _ESCAPED_BRACKET_DESTINATIONS)
    def test_every_renderer_links_the_whole_url(self, destination):
        """The renderers close the link where the screen does, at the final ``)``."""
        source = f"see [l]({destination}) rest"
        assert _slack(source) == f"see <{destination}|l> rest"
        assert _md_to_telegram_html(source) == f'see <a href="{destination}">l</a> rest'
        assert to_whatsapp_text(source) == f"see l ({destination}) rest"
        assert to_plaintext(source) == f"see l ({destination}) rest"
        assert _MD_LINK.fullmatch(f"[l]({destination})") is not None

    @pytest.mark.parametrize("destination", _ESCAPED_BRACKET_DESTINATIONS)
    def test_the_unit_closes_where_the_walker_closes(self, destination):
        rest = f"{destination}) tail"
        walked, consumed = _walk_destination(rest)
        assert walked is not None
        assert _regex_destination(rest)[1] == consumed

    @pytest.mark.parametrize(
        "payload",
        [
            f"[AKIA](https://x/a\\)b){_KEY[4:]}",
            f"[AKIA](https://x/\\(a){_KEY[4:]}",
        ],
    )
    def test_an_escaped_parenthesis_is_still_refused(self, payload):
        """A parenthesis is not a destination character, so ``\\)`` and ``\\(`` stay
        outside the token and the link stays as written."""
        assert canonicalize_display(payload) == payload
        assert slack_format.to_slack_mrkdwn(payload) == payload

    def test_an_unescaped_nested_opener_is_still_refused(self):
        """The refusal ``\\[`` bypasses still holds for a bare ``[label](``."""
        payload = f"[x](a[AKIA](u){_KEY[4:]})"
        assert _KEY in canonicalize_display(payload)
        assert redact_for_display(payload, _default_redactor)[1] is True

    def test_an_escaped_backslash_before_an_opener_leaves_the_opener_bare(self):
        """``\\\\[b](c)`` is an escaped backslash and then a bare ``[b](``, which the
        unit refuses as a nested opener, so the outer link stays as written."""
        payload = "[l](https://x/\\\\[b](c)) rest"
        assert canonicalize_display(payload) == "[l](https://x/\\\\b) rest"


#: Destinations the canonical link grammar refuses: an unescaped backslash before
#: a parenthesis, or a ``[label](`` opener inside the url. The grammar admitting no
#: parenthesis group reads the first ``)`` as the close, so it links some of them.
_REFUSED_DESTINATIONS = [
    r"https://x/p\)q",
    r"https://x/\)",
    r"https://x/p\(q\)r",
    r"https://x/a\)b\)c",
    r"https://x/(a)\)b",
    r"https://x/a\\\)b",
    r"https://x/[a](b",
]
_REFUSED_LINK_TRAILERS = ["", " **b**", " ~~s~~"]
_REFUSED_LINK_LAYOUTS = [
    ("label-holds-the-tail", "{head}[{tail}]({destination}){trailer}"),
    ("label-holds-the-head", "[{head}]({destination}){tail}{trailer}"),
]


def _refused_link_shapes(heads: tuple[str, ...], tail_for) -> list[tuple[str, str]]:
    shapes = []
    for (layout_name, layout), destination, head, trailer in itertools.product(
        _REFUSED_LINK_LAYOUTS, _REFUSED_DESTINATIONS, heads, _REFUSED_LINK_TRAILERS
    ):
        payload = layout.format(
            head=head, tail=tail_for(head), destination=destination, trailer=trailer
        )
        shapes.append((f"{layout_name}-{destination}-{head}{trailer}", payload))
    return shapes


_KEYED_REFUSED_LINK_SHAPES = _refused_link_shapes(
    (_KEY[:4], _KEY[:8], _KEY[:13]), lambda head: _KEY[len(head) :]
)
_KEYLESS_REFUSED_LINK_SHAPES = _refused_link_shapes((_KEY[:4],), lambda head: "rest")


def _every_reading(text: str) -> list[str]:
    return [
        canonicalize_display(text),
        *(reading(text) for reading in display_safety.FURTHER_READINGS),
    ]


#: Angle-bracketed destinations whose parentheses do not balance and that no
#: ``)`` closes early. The parenthesis grammar refuses each one, so only the angle
#: branch links it.
_UNBALANCED_ANGLE_DESTINATIONS = [
    "<https://x/(>",
    "<https://x/((>",
    "<https://x/(a>",
    "<https://x/a(b)c(>",
]


class TestAnAngleBracketedDestinationCollapsesToItsLabel:
    """``[AKIA](<https://x/(>)IOSF...``: Discord reads ``<...>`` as the whole
    destination, so the link shows ``AKIA`` and the reader sees the key joined
    to what follows. The parenthesis grammar refuses the unbalanced ``(`` inside
    the brackets and left the link raw, so the screen scanned the text as
    written and saw no key. The screen's link grammar has an angle-bracket
    branch: ``<``, any run without ``<``, ``>`` or a line break, ``>``.
    """

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_the_screen_links_it(self, destination):
        assert _MD_LINK.fullmatch(f"[l]({destination})") is None
        assert display_safety._balanced_link_reading(f"[AKIA]({destination}){_KEY[4:]}") == _KEY

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_a_key_split_around_it_is_redacted(self, destination):
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert all(_KEY not in reading for reading in _every_reading(safe))

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_the_discord_send_path_redacts_it(self, destination):
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        sent = discord_redact_transformed(payload)
        assert sent != payload
        assert _KEY not in sent
        assert all(_KEY not in reading for reading in _every_reading(sent))

    def test_the_finding_shape_verbatim(self):
        payload = "[AKIA](<https://x/(>)IOSFODNN7EXAMPLE"
        assert discord_redact_transformed(payload) == CREDENTIAL_REDACTION_TAGS[0]

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_a_key_split_by_a_pair_inside_it_is_still_scanned(self, destination):
        """The narrow grammar keeps no angle branch: a url only the angle branch
        links stays text under the no-parenthesis-group reading, where the emphasis
        pass joins a key a ``~~`` pair split inside it, as it did before the branch."""
        inner = destination[1:-1]
        payload = f"[l](<{inner}/AKIA~~{_KEY[4:]}~~>)"
        assert _KEY in display_safety._link_free_reading(payload)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe
        assert all(_KEY not in reading for reading in _every_reading(safe))

    def test_the_parenthesis_grammar_still_closes_first(self):
        """``[AKIA](<x)IOSF...>)`` is a link to ``<x`` and then the key, as a lazy
        reader closes it; an angle branch tried first would swallow the ``)`` and
        take the key out of the scan."""
        payload = f"[AKIA](<x){_KEY[4:]}>)"
        assert canonicalize_display(payload) == f"{_KEY}>)"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _KEY not in safe

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_no_renderer_shows_a_key_it_leaves_raw(self, destination):
        """Slack, Telegram, WhatsApp and iMessage link no angle-bracketed
        destination: each leaves the text as written, or shows the url beside the
        label, so no reader of theirs sees the key joined and the renderers keep
        the shared destination unit as it is."""
        payload = f"[AKIA]({destination}){_KEY[4:]}"
        assert _KEY not in _slack_reader_sees(_slack(payload))
        assert _KEY not in html.unescape(_md_to_telegram_html(payload))
        assert _KEY not in _strip_md(payload)
        assert _KEY not in to_whatsapp_text(payload)
        assert _KEY not in to_plaintext(payload)

    @pytest.mark.parametrize("destination", _UNBALANCED_ANGLE_DESTINATIONS)
    def test_the_same_shape_without_a_key_passes_through_byte_identical(self, destination):
        payload = f"see [l]({destination}) rest, and [Python](<{WIKI}>) too"
        assert redact_for_display(payload, _default_redactor) == (payload, False)
        assert discord_redact_transformed(payload) == payload

    @pytest.mark.parametrize(
        "destination",
        ["<a<b(>", "<a>b(>", "<a\nb(>"],
    )
    def test_the_branch_admits_no_bracket_or_line_break_inside(self, destination):
        """The run between the brackets excludes ``<``, ``>`` and the line break,
        so the closing ``>`` is consumable one way only and a doomed opener fails
        at the next bracket instead of rescanning to the end of the text."""
        payload = f"[l]({destination})"
        assert display_safety._balanced_link_reading(payload) == payload

    def test_a_run_of_angle_openers_stays_fast(self):
        """Every ``[l](<`` opener scans only to the next ``<``: linear on a text
        built from openers alone."""

        def elapsed(repeats: int) -> float:
            text = "[l](<" * repeats
            started = time.perf_counter()
            assert display_safety._balanced_link_reading(text) == text
            return time.perf_counter() - started

        small, large = elapsed(5_000), elapsed(20_000)
        assert large < 0.5, large
        assert large <= max(8 * small, 0.05), (small, large)


class TestAKeyJoinedByALinkTheCanonicalGrammarRefuses:
    """``AKIA[IOSF...](https://ex.com/p\\)q)`` is no link to the canonical grammar,
    which refuses an unescaped backslash before ``)``, so the canonical form is the
    text as written and scans as split. The grammar admitting no parenthesis group
    closes at that ``)`` and joins the key, as a CommonMark reader does when it
    shows ``AKIA`` beside the label. A reading that reveals a key has to be scanned
    whether or not the canonical form differs from the text, and the answer has to
    carry no key: the canonical redacted form is the text itself here, so the
    redacted reading is emitted instead.
    """

    _REPORTED = f"AKIA[{_KEY[4:]}](https://ex.com/p\\)q)"

    def test_the_reported_input_is_redacted_under_the_balanced_reading(self):
        assert redact_for_display(self._REPORTED, _default_redactor) == (
            "[REDACTED: credential]",
            True,
        )

    def test_unrelated_emphasis_does_not_hand_back_the_raw_link(self):
        """With ``**b**`` beside it the canonical form differs from the text, and
        the canonical redacted form is still the raw link, key inside."""
        payload = f"{self._REPORTED} **b**"
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert all(_KEY not in reading for reading in _every_reading(safe))

    def test_a_url_the_canonical_grammar_links_still_answers_with_the_canonical_form(self):
        payload = f"[l](https://x/(a)/AKIA~~{_KEY[4:]}~~)"
        assert redact_for_display(payload, _default_redactor) == ("l", True)

    def test_every_generated_key_shape_is_covered_by_a_reading(self):
        revealed = [
            any(_KEY in reading for reading in _every_reading(payload))
            for _, payload in _KEYED_REFUSED_LINK_SHAPES
        ]
        assert all(revealed)

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _KEYED_REFUSED_LINK_SHAPES,
        ids=[c[0] for c in _KEYED_REFUSED_LINK_SHAPES],
    )
    def test_a_key_any_reading_joins_is_redacted_and_no_other_shape_is_touched(
        self, case_name, payload
    ):
        if not any(_KEY in reading for reading in _every_reading(payload)):
            assert redact_for_display(payload, _default_redactor) == (payload, False), case_name
            return
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        assert _KEY not in safe, case_name
        assert all(_KEY not in reading for reading in _every_reading(safe)), case_name

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _KEYLESS_REFUSED_LINK_SHAPES,
        ids=[c[0] for c in _KEYLESS_REFUSED_LINK_SHAPES],
    )
    def test_the_same_shape_without_a_key_passes_through_byte_identical(self, case_name, payload):
        assert redact_for_display(payload, _default_redactor) == (payload, False), case_name


#: A second key, distinct from ``_KEY``, so one text can carry a key one reading
#: joins beside a key another reading joins.
_SECOND_KEY = "ASIA" + _KEY[4:]


def _shows_a_key(text: str) -> bool:
    return _KEY in text or _SECOND_KEY in text


def _keyed(text: str) -> str:
    return text.replace(_KEY[:4], _SECOND_KEY[:4])


#: One shape per way a single reading joins a key: a native link inside a raw
#: Markdown url or label, a link the canonical grammar refuses, an emphasis pair
#: closing a link, an emphasis pair on its own, and a pair inside a linked url.
_SHAPES_ONE_READING_JOINS = [
    ("native-link-in-a-raw-url", f"[l](https://x/<u|AKIA>{_KEY[4:]}|)"),
    ("native-link-in-a-label", f"[AK<u|IA>{_KEY[4:]}](https://x/a|b)"),
    ("slack-emphasis-in-a-raw-url", f"[l](https://x/AKIA_{_KEY[4:]}_|)"),
    ("refused-link-label-holds-the-head", f"[AKIA](https://x/\\){_KEY[4:]}"),
    ("emphasis-closes-a-link", f"AKIA[{_KEY[4:]}]**(https://x)**"),
    ("emphasis-pair", f"AKIA**{_KEY[4:]}**"),
    ("pair-inside-a-linked-url", f"[l](https://x/AKIA**{_KEY[4:]}**)"),
    ("pair-inside-a-parenthesised-url", f"[l](https://x/(a)/AKIA~~{_KEY[4:]}~~)"),
] + [
    (case_name, payload)
    for case_name, payload in _KEYED_REFUSED_LINK_SHAPES
    if case_name.startswith("label-holds-the-tail-")
    and case_name.endswith(f"-{_KEY[:4]}")
    and any(_KEY in reading for reading in _every_reading(payload))
]


_SLACK_RENDERED_DELIMITERS = [
    ("italic", "_"),
    ("bold", "*"),
    ("strike", "~"),
    ("inline-code", "`"),
]


class TestAKeySplitBySlackMarkupInsideARawMarkdownLink:
    @staticmethod
    def _payload(delimiter: str, tail: str = _KEY[4:]) -> str:
        return f"[l](https://x/AKIA{delimiter}{tail}{delimiter}|)"

    @pytest.mark.parametrize(
        ("case_name", "delimiter"),
        _SLACK_RENDERED_DELIMITERS,
        ids=[case[0] for case in _SLACK_RENDERED_DELIMITERS],
    )
    def test_slack_markup_cannot_hide_a_key_in_a_refused_link(self, case_name, delimiter):
        payload = self._payload(delimiter)
        assert slack_format.to_slack_mrkdwn(payload) == payload, case_name
        assert _KEY in display_safety._link_free_reading(payload), case_name
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        assert _KEY not in canonicalize_display(safe), case_name
        assert all(_KEY not in reading for reading in _every_reading(safe)), case_name

    @pytest.mark.parametrize(
        ("case_name", "delimiter"),
        _SLACK_RENDERED_DELIMITERS,
        ids=[case[0] for case in _SLACK_RENDERED_DELIMITERS],
    )
    def test_the_same_shapes_without_a_key_pass_through_byte_identical(self, case_name, delimiter):
        payload = self._payload(delimiter, tail="rest")
        assert redact_for_display(payload, _default_redactor) == (payload, False), case_name

    @pytest.mark.parametrize(
        ("case_name", "delimiter"),
        _SLACK_RENDERED_DELIMITERS,
        ids=[case[0] for case in _SLACK_RENDERED_DELIMITERS],
    )
    def test_an_unmatched_delimiter_does_not_invent_a_key(self, case_name, delimiter):
        payload = f"[l](https://x/AKIA{delimiter}rest|)"
        reading = display_safety._link_free_reading(payload)
        assert _default_redactor(reading) == reading, case_name
        assert redact_for_display(payload, _default_redactor) == (payload, False), case_name


_PAIRS_OF_SHAPES = [
    (f"{first_name}+{second_name}", f"{first} {_keyed(second)}")
    for (first_name, first), (second_name, second) in itertools.product(
        _SHAPES_ONE_READING_JOINS, repeat=2
    )
]


class TestEveryKeyAnyReadingJoinsIsRedactedTogether:
    """A text can carry a key one reading joins beside a key another reading joins:
    ``[l](https://x/<u|AKIA>IOSF...|)`` shows its key to a Slack reader, and
    ``ASIA[IOSF...](https://ex.com/p\\)q)`` beside it shows the second key to a
    CommonMark reader. An answer scanned only under the reading that fired removes
    the first key and hands the reader the second. The same hole sits on the
    canonical route: ``AKIA**IOSF...**`` beside the refused link is answered by the
    canonical redacted form, which still carries the link the narrow grammar
    joins. The emitted text has to show a key under no reading, and Discord's
    outbound path, which sends what the screen returns, is checked as well.
    """

    _TWO_VECTORS = (
        f"[l](https://x/<u|AKIA>{_KEY[4:]}|) {_keyed(f'AKIA[{_KEY[4:]}](https://ex.com/p\\)q)')}"
    )
    _REFUSED_LINK = f"AKIA[{_KEY[4:]}](https://ex.com/p\\)q)"
    _BESIDE_THE_REFUSED_LINK = [
        ("emphasis-pair-beside-the-refused-link", f"AKIA**{_KEY[4:]}** {_REFUSED_LINK}"),
        (
            "pair-inside-a-linked-url-beside-the-refused-link",
            f"[l](https://x/AKIA**{_KEY[4:]}**) {_REFUSED_LINK}",
        ),
    ]

    @staticmethod
    def _assert_clean_on_every_path(payload: str) -> None:
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert not any(_shows_a_key(reading) for reading in _every_reading(safe)), safe
        assert _default_redactor(safe) == safe
        sent = discord_redact_transformed(payload)
        assert not any(_shows_a_key(reading) for reading in _every_reading(sent)), sent
        assert not _shows_a_key(_slack_reader_sees(_slack(payload)))

    def test_two_keys_two_readings_are_both_redacted(self):
        assert _KEY in display_safety._link_free_reading(self._TWO_VECTORS)
        assert _SECOND_KEY in display_safety._balanced_link_reading(self._TWO_VECTORS)
        self._assert_clean_on_every_path(self._TWO_VECTORS)

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _BESIDE_THE_REFUSED_LINK,
        ids=[c[0] for c in _BESIDE_THE_REFUSED_LINK],
    )
    def test_a_key_beside_the_refused_link_takes_the_link_with_it(self, case_name, payload):
        assert _KEY in canonicalize_display(payload) or _KEY in _discord_reader_sees(payload)
        self._assert_clean_on_every_path(payload)

    def test_every_shape_is_joined_by_some_reading(self):
        assert len(_PAIRS_OF_SHAPES) < 300
        for case_name, payload in _SHAPES_ONE_READING_JOINS:
            assert any(_KEY in reading for reading in _every_reading(payload)), case_name

    @pytest.mark.parametrize(
        ("case_name", "payload"), _PAIRS_OF_SHAPES, ids=[c[0] for c in _PAIRS_OF_SHAPES]
    )
    def test_a_pair_of_shapes_is_clean_under_every_reading(self, case_name, payload):
        assert any(_shows_a_key(reading) for reading in _every_reading(payload)), case_name
        self._assert_clean_on_every_path(payload)

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _PAIRS_OF_SHAPES + [("tag-before-a-parenthesis", f"{_KEY}(https://x) {_REFUSED_LINK}")],
        ids=[c[0] for c in _PAIRS_OF_SHAPES] + ["tag-before-a-parenthesis"],
    )
    def test_the_form_past_the_bound_is_clean_under_every_reading(self, case_name, payload):
        past_the_bound = display_safety.redacted_without_markup(payload, _default_redactor)
        assert not any(_shows_a_key(r) for r in _every_reading(past_the_bound)), case_name
        assert _default_redactor(past_the_bound) == past_the_bound, case_name

    _HEADING_MARKER_BETWEEN_NAME_AND_COLON = (
        "[[x](u)](u)\n\nSecretAccessKey\n#   : wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
    )
    _FORMAT_CHARACTER_INSIDE_THE_KEY = f"{_KEY[:4]}\u200b{_KEY[4:]}"

    @pytest.mark.parametrize(
        "reading", [None, slack_format._slack_client_display], ids=["canonical", "slack"]
    )
    def test_a_heading_marker_is_no_seam_past_the_bound(self, reading):
        payload = self._HEADING_MARKER_BETWEEN_NAME_AND_COLON
        assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" in display_safety._plain_reading(payload)
        past_the_bound = display_safety.redacted_without_markup(
            payload, _default_redactor, reading=reading
        )
        assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" not in past_the_bound
        for shown in _every_reading(past_the_bound):
            assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" not in shown, shown
        assert _default_redactor(past_the_bound) == past_the_bound

    def test_a_format_character_is_no_seam_past_the_bound_under_a_sink_reading(self):
        past_the_bound = display_safety.redacted_without_markup(
            self._FORMAT_CHARACTER_INSIDE_THE_KEY,
            _default_redactor,
            reading=slack_format._slack_client_display,
        )
        assert not any(_shows_a_key(r) for r in _every_reading(past_the_bound)), past_the_bound
        assert _default_redactor(past_the_bound) == past_the_bound


_UNMATCHED_DELIMITER_URLS = [
    "[docs](https://example.com/AKIA_IOSFODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA*IOSFODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA~IOSFODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA`IOSFODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA_IOSF_ODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA_IOSFODNN7EXAMPLE) snake_case remains",
    "[docs](https://example.com/AKIA~~IOSF~~ODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA-*IOSF*-ODNN7EXAMPLE)",
    "[docs](https://example.com/AKIA-_IOSF_-ODNN7EXAMPLE)",
]


class TestLinkFreeReadingCoversUrlDelimiters:
    @pytest.mark.parametrize("payload", _UNMATCHED_DELIMITER_URLS)
    def test_only_a_key_the_link_free_reading_joins_is_redacted(self, payload):
        reading = display_safety._link_free_reading(payload)
        shows = _default_redactor(reading) != reading
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is shows
        if shows:
            assert all(_KEY not in shown for shown in _every_reading(safe))
        else:
            assert safe == payload

    def test_double_tilde_is_caught_without_changing_telegram_fallback(self):
        payload = "[l](https://x/AKIA~~IOSF~~ODNN7EXAMPLE)"
        assert _default_redactor(_strip_md(payload)) == _strip_md(payload)
        assert _default_redactor(display_safety._link_free_reading(payload)) != (
            display_safety._link_free_reading(payload)
        )
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert all(_KEY not in shown for shown in _every_reading(safe))


class _RecordingPattern:
    """Stand in for a module-level pattern and log each ``sub`` run against it."""

    def __init__(self, pattern: re.Pattern[str], log: list[re.Pattern[str]]) -> None:
        self._pattern = pattern
        self._log = log

    def sub(self, replacement, text: str) -> str:
        self._log.append(self._pattern)
        return self._pattern.sub(replacement, text)


def _patterns_applied_by(function, module, monkeypatch) -> list[re.Pattern[str]]:
    """The module-level pattern objects *function* runs ``sub`` on, in call order.

    Every pattern the module holds, alone or anywhere inside a tuple (a tuple of
    patterns, or of ``(pattern, replacement)`` pairs), is replaced by a recorder
    for the call, so the log names the object behind each pass rather than the
    name the function spells.
    """
    log: list[re.Pattern[str]] = []
    recorders = {
        id(value): _RecordingPattern(value, log)
        for value in vars(module).values()
        if isinstance(value, re.Pattern)
    }

    def recorded(value):
        if isinstance(value, re.Pattern):
            return recorders[id(value)]
        if isinstance(value, tuple):
            return tuple(recorded(member) for member in value)
        return value

    for name, value in list(vars(module).items()):
        with_recorders = recorded(value)
        if with_recorders != value:
            monkeypatch.setattr(module, name, with_recorders)
    function("```c```\n`b` **a** __d__ # e [f](https://g) - h")
    return log


def _same_objects(applied: list[re.Pattern[str]], expected: tuple[re.Pattern[str], ...]) -> bool:
    return len(applied) == len(expected) and all(
        pattern is wanted for pattern, wanted in zip(applied, expected)
    )


def _names_spelling(module, pattern: re.Pattern[str]) -> set[str]:
    """Every module-level name in *module* bound to a pattern that reads as *pattern*."""
    return {
        name
        for name, value in vars(module).items()
        if isinstance(value, re.Pattern)
        and (value.pattern, value.flags) == (pattern.pattern, pattern.flags)
    }


_SHARED_PASS_NAMES = (
    "TELEGRAM_FALLBACK_FENCE",
    "TELEGRAM_FALLBACK_INLINE_CODE",
    "TELEGRAM_FALLBACK_HEADING",
    "TELEGRAM_FALLBACK_BOLD_STAR",
    "TELEGRAM_FALLBACK_BOLD_USCORE",
    "TELEGRAM_FALLBACK_LINK",
)
_SHARED_PATTERNS = tuple(getattr(display_safety, name) for name in _SHARED_PASS_NAMES)


class TestPlainReadingSharesTelegramFallbacksPasses:
    """``_plain_reading`` and ``_strip_md`` apply ONE set of pattern objects, the
    ``TELEGRAM_FALLBACK_*`` patterns the screen defines and the renderer imports.
    A copy in either module, or a reordering, fails here. The passes are observed
    as the objects each function runs, not as the names its source spells, and
    each shared pattern may be spelled under exactly one name per module: ``re``
    caches compilations, so a byte-identical copy compiles to the SAME object and
    only its second name gives it away.
    """

    _REVIEW = (
        "Telegram's _strip_md gained, lost or reordered a pass: decide whether the pass "
        "can join two halves of a key and, if so, add it to TELEGRAM_FALLBACK_PASSES in "
        "kiro_crew.messaging.display_safety, which _plain_reading applies."
    )

    def test_the_shared_passes_are_the_named_patterns_in_order(self):
        assert tuple(pattern for pattern, _ in TELEGRAM_FALLBACK_PASSES) == _SHARED_PATTERNS

    @pytest.mark.parametrize("name", _SHARED_PASS_NAMES)
    def test_each_shared_pass_is_spelled_once_per_module(self, name):
        pattern = getattr(display_safety, name)
        assert getattr(telegram_renderer, name) is pattern
        assert _names_spelling(display_safety, pattern) == {name}
        assert _names_spelling(telegram_renderer, pattern) == {name}

    def test_strip_md_and_the_screen_apply_the_same_pattern_objects_in_order(self, monkeypatch):
        every_strip_md_pass = (*_SHARED_PATTERNS, telegram_renderer._BULLET_RE)
        applied_by_strip_md = _patterns_applied_by(_strip_md, telegram_renderer, monkeypatch)
        applied_by_screen = _patterns_applied_by(
            display_safety._plain_reading, display_safety, monkeypatch
        )
        shared_in_strip_md = [
            pattern
            for pattern in applied_by_strip_md
            if any(pattern is shared for shared in _SHARED_PATTERNS)
        ]
        assert _same_objects(shared_in_strip_md, _SHARED_PATTERNS), self._REVIEW
        assert _same_objects(applied_by_screen, _SHARED_PATTERNS), self._REVIEW
        assert _same_objects(applied_by_strip_md, every_strip_md_pass), self._REVIEW

    def test_the_link_pass_prints_what_the_fallback_prints(self):
        text = f"see [Python]({WIKI}) and [a](https://x/(b)c)"
        assert _strip_md(text) == TELEGRAM_FALLBACK_LINK.sub(TELEGRAM_FALLBACK_LINK_TEXT, text)
        assert _strip_md(text) == f"see Python ({WIKI}) and a (https://x/(b)c)"

    def test_the_unshared_bullet_pass_keeps_a_visible_separator(self):
        for payload in (
            "AKIA\n- IOSFODNN7EXAMPLE",
            "SecretAccessKey\n- : short-secret-value",
        ):
            assert _default_redactor(_strip_md(payload)) == _strip_md(payload), payload

    def test_a_url_opener_pairs_with_later_triple_emphasis(self):
        payload = "[l](https://x/AKIA**IOSFODNN7EXAMPLE) ***x***"
        rendered = _strip_md(payload)
        assert _default_redactor(rendered) != rendered
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True
        assert _default_redactor(_strip_md(safe)) == _strip_md(safe)


_FENCE_REGRESSION_CASES = [
    ("empty-fence-in-link", "[l](https://x/(a)/AKIA``````IOSFODNN7EXAMPLE)"),
    ("info-string-in-link", "[l](https://x/AKIA```junk```IOSFODNN7EXAMPLE)"),
    ("bare-info-string", "AKIA```y```IOSFODNN7EXAMPLE"),
]


#: The fence reading written the direct way: an info string, an optional newline,
#: then the content up to the closer. Its ``[^\n]*`` and ``.*?`` overlap, so an
#: unclosed opener backtracks quadratically in the text after it. The shared
#: pattern splits the newline and same-line cases into alternatives and is linear;
#: these tests hold it to the direct reading's result and match spans, which is
#: what every caller (the fallback, the HTML translation and the HR stash) reads.
_DIRECT_FENCE_READING = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)


class TestTheSharedFencePatternReadsLikeTheDirectOne:
    @staticmethod
    def _reading(pattern: re.Pattern[str], text: str) -> tuple[str, list[tuple[int, int]]]:
        return (
            pattern.sub(lambda match: match.group(1) or "", text),
            [match.span() for match in pattern.finditer(text)],
        )

    def _assert_same_reading(self, text: str) -> None:
        assert self._reading(TELEGRAM_FALLBACK_FENCE, text) == self._reading(
            _DIRECT_FENCE_READING, text
        ), repr(text)

    def test_every_short_string_reads_the_same(self):
        checked = 0
        for length in range(10):
            for characters in itertools.product("`a\n", repeat=length):
                self._assert_same_reading("".join(characters))
                checked += 1
        assert checked == 29_524

    def test_seeded_longer_strings_read_the_same(self):
        random_source = random.Random(14314)
        checked = 0
        for _ in range(20_000):
            text = "".join(
                random_source.choice("`ab x\n") for _ in range(random_source.randint(10, 80))
            )
            self._assert_same_reading(text)
            checked += 1
        assert checked == 20_000

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _FENCE_REGRESSION_CASES,
        ids=[case[0] for case in _FENCE_REGRESSION_CASES],
    )
    def test_a_key_joined_by_a_fence_is_removed_from_the_fallback(self, case_name, payload):
        rendered = _strip_md(payload)
        assert _default_redactor(rendered) != rendered, case_name
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is True, case_name
        safe_rendered = _strip_md(safe)
        assert _default_redactor(safe_rendered) == safe_rendered, (
            case_name,
            safe_rendered,
        )


_SPLIT_TOKENS = [
    ("star", "*"),
    ("double-star", "**"),
    ("triple-star", "***"),
    ("underscore", "_"),
    ("double-underscore", "__"),
    ("triple-underscore", "___"),
    ("tilde", "~"),
    ("double-tilde", "~~"),
    ("backtick", "`"),
    ("double-backtick", "``"),
    ("fence-opener", "```"),
    ("empty-fence", "``````"),
    ("same-line-fence", "```x```"),
]
_TRAILERS = [
    ("alone", ""),
    ("later-bold", " **text"),
    ("later-triple", " ***x***"),
    ("later-underscore", " __t__"),
    ("later-code", " `y`"),
]
_SPLIT_LAYOUTS = [
    ("one-split", (4,)),
    ("two-splits", (4, 8)),
]


def _split_key(token: str, positions: tuple[int, ...]) -> str:
    pieces: list[str] = []
    start = 0
    for position in positions:
        pieces.extend((_KEY[start:position], token))
        start = position
    pieces.append(_KEY[start:])
    return "".join(pieces)


_DIFFERENTIAL_CORPUS = [
    (
        f"{token_name}-{layout_name}-{trailer_name}",
        f"[l](https://x/{_split_key(token, positions)}){trailer}",
    )
    for token_name, token in _SPLIT_TOKENS
    for layout_name, positions in _SPLIT_LAYOUTS
    for trailer_name, trailer in _TRAILERS
]


class TestScreenIncludesTelegramDifferentially:
    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _DIFFERENTIAL_CORPUS,
        ids=[case[0] for case in _DIFFERENTIAL_CORPUS],
    )
    def test_screen_matches_what_the_fallback_shows(self, case_name, payload):
        readings = _every_reading(payload)
        shows = any(_default_redactor(reading) != reading for reading in readings)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is shows, (case_name, readings, safe)
        if shows:
            assert all(_default_redactor(reading) == reading for reading in _every_reading(safe)), (
                case_name,
                _every_reading(safe),
            )

    def test_corpus_has_both_outcomes(self):
        outcomes = {
            _default_redactor(_strip_md(payload)) != _strip_md(payload)
            for _, payload in _DIFFERENTIAL_CORPUS
        }
        assert outcomes == {False, True}


#: Where the label's ``[`` and ``]`` cut the key: before, inside or after it. The
#: label is never empty: no renderer links ``[]()``, while the screen collapses it.
_LABEL_CUTS = [(0, 4), (0, 10), (0, 20), (4, 10), (4, 20), (10, 20)]
_LINK_DELIMITERS = [
    ("none", ""),
    ("double-star", "**"),
    ("double-underscore", "__"),
    ("backtick", "`"),
]
#: Where a delimiter pair sits relative to the link's ``]`` and ``(url)``.
_LINK_WRAPS = [
    ("url", lambda d: f"]{d}(https://x){d}"),
    ("spaced-url", lambda d: f"] {d}(https://x){d}"),
    ("closer-and-url", lambda d: f"{d}](https://x){d}"),
    ("url-then-text", lambda d: f"]{d}(https://x){d}z"),
]
_LINK_OPENERS = [("bare", lambda d: "["), ("wrapped", lambda d: f"{d}[{d}")]


def _link_and_emphasis_shape(cut: tuple[int, int], delimiter: str, opener, wrap) -> str:
    before, label, after = _KEY[: cut[0]], _KEY[cut[0] : cut[1]], _KEY[cut[1] :]
    return f"{before}{opener(delimiter)}{label}{wrap(delimiter)}{after}"


_LINK_EMPHASIS_CORPUS = [
    (
        f"{cut[0]}-{cut[1]}-{delimiter_name}-{opener_name}-{wrap_name}",
        _link_and_emphasis_shape(cut, delimiter, opener, wrap),
    )
    for cut in _LABEL_CUTS
    for delimiter_name, delimiter in _LINK_DELIMITERS
    for opener_name, opener in _LINK_OPENERS
    for wrap_name, wrap in _LINK_WRAPS
]


def _telegram_reader_sees(html_text: str) -> str:
    """The text a Telegram reader sees of the HTML seal: tags gone, entities read."""
    return html.unescape(re.sub(r"<[^<>]+>", "", html_text))


class TestPlainReadingMatchesTelegramOnLinkAndEmphasisShapes:
    """Every way of placing ``[``, ``]``, a delimiter pair and ``(https://x)``
    around a split key. The screen redacts exactly the shapes where one of its
    readings shows the key joined: the rendered form (``canonicalize_display``,
    which the Telegram HTML seal never shows more than) or Telegram's ``_strip_md``
    fallback. Every other shape is left as written. The fallback reading is the
    one this corpus exists for: the shapes it alone joins are the ones the seal
    keeps split, and only the plain reading's own link pass sees them.
    """

    @staticmethod
    def _readings(payload: str) -> tuple[str, str, str]:
        return (
            canonicalize_display(payload),
            _telegram_reader_sees(_md_to_telegram_html(payload)),
            _strip_md(payload),
        )

    @classmethod
    def _shows_the_key(cls, payload: str) -> bool:
        return any(_default_redactor(reading) != reading for reading in cls._readings(payload))

    @pytest.mark.parametrize(
        ("case_name", "payload"),
        _LINK_EMPHASIS_CORPUS,
        ids=[case[0] for case in _LINK_EMPHASIS_CORPUS],
    )
    def test_screen_matches_what_a_reading_shows(self, case_name, payload):
        shows = self._shows_the_key(payload)
        safe, redacted = redact_for_display(payload, _default_redactor)
        assert redacted is shows, (case_name, self._readings(payload), safe)
        if not shows:
            assert safe == payload, case_name
            return
        assert _KEY not in safe, case_name
        assert not self._shows_the_key(safe), (case_name, self._readings(safe))

    def test_the_seal_shows_no_more_than_the_rendered_form(self):
        for case_name, payload in _LINK_EMPHASIS_CORPUS:
            canonical, sealed, _ = self._readings(payload)
            assert (_KEY in sealed) <= (_KEY in canonical), case_name

    def test_the_corpus_has_both_outcomes_under_every_delimiter(self):
        for delimiter_name, _ in _LINK_DELIMITERS:
            outcomes = {
                self._shows_the_key(payload)
                for case_name, payload in _LINK_EMPHASIS_CORPUS
                if f"-{delimiter_name}-" in case_name
            }
            assert outcomes == {False, True}, delimiter_name

    def test_the_fallback_alone_joins_the_key_in_some_shapes(self):
        fallback_only = [
            case_name
            for case_name, payload in _LINK_EMPHASIS_CORPUS
            if _KEY in _strip_md(payload) and _KEY not in canonicalize_display(payload)
        ]
        assert fallback_only


class TestTheLinkWalksVisitOnlyWhatTheyActOn:
    """The two per-line walks jump between escapes and brackets instead of
    stepping through every character. The expected strings were produced by the
    character-by-character walks they replaced; each shape is one the jumping
    scan could get wrong -- an escape hiding a bracket, a trailing backslash,
    an angle destination closed or left open, a ``<`` before the ``](``, an
    unclosed destination with a later link, and openers with no ``)`` at all."""

    @pytest.mark.parametrize(
        ("line", "balanced", "first_close"),
        [
            (
                "\\[a\\](https://x) [b](https://y\\)) [c](https://z)",
                "\\[a\\](https://x) b c",
                "\\[a\\](https://x) b) c",
            ),
            ("[a](https://x)\\ ", "a\\ ", "a\\ "),
            ("[a](https://x)\\\\", "a\\\\", "a\\\\"),
            ("[a](https://x\\ ", "[a](https://x\\ ", "[a](https://x\\ "),
            ("[a](<https://x/(y>)z", "az", "az"),
            ("[a](<https://x) [b](https://y)", "a b", "a b"),
            ("<[a](https://x)>", "<a>", "<a>"),
            ("a<b [c](https://x) d>e", "a<b c d>e", "a<b c d>e"),
            ("[a](<https://x\\>y>)", "a", "a"),
            ("[a](<https://x<y>)", "a", "a"),
            ("[a](https://x [b](https://y)", "[a](https://x b", "a"),
            (
                "[a](https://x [b](https://y [c](https://z",
                "[a](https://x [b](https://y [c](https://z",
                "[a](https://x [b](https://y [c](https://z",
            ),
            ("[a](https://x/((y))) z", "a z", "a)) z"),
            ("[a]([b](https://y))", "a", "a)"),
            ("[a](https://x)) [b](https://y)", "a) b", "a) b"),
            ("\\", "\\", "\\"),
            ("[a](", "[a](", "[a]("),
            ("[a]()", "a", "a"),
            ("](https://x) [a](https://y)", "](https://x) a", "](https://x) a"),
        ],
    )
    def test_each_walk_reads_the_shape_as_the_character_walk_did(self, line, balanced, first_close):
        assert display_safety._collapse_links_at_balanced_close(line) == balanced
        assert display_safety._collapse_links_at_first_close(line) == first_close
        two_lines = f"{line}\n{line}"
        assert display_safety._collapse_links_at_balanced_close(two_lines) == (
            f"{balanced}\n{balanced}"
        )
        assert display_safety._collapse_links_at_first_close(two_lines) == (
            f"{first_close}\n{first_close}"
        )
