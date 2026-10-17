"""A key split around a link reaches no reader whole.

An outer link cannot swallow an inner one; a key split by emphasis, a link an earlier pass
closes, a native link or a delimiter pair inside the URL is redacted; the canonical form the
screen emits renders as itself; only redactor tags stay whole; and the settling bounds hold.
"""

from __future__ import annotations

import html
import re

import pytest
from markdown_link_parentheses_helpers import (
    _KEY,
    _URL_SPLIT_TRIGGER,
    WIKI,
    _discord_reader_sees,
    _nested_link,
    _slack,
    _slack_reader_sees,
)

from kiro_crew.discord.renderer import _redact_transformed as discord_redact_transformed
from kiro_crew.imessage.plaintext import to_plaintext
from kiro_crew.messaging import display_safety
from kiro_crew.messaging.display_safety import (
    _REDACTION_TAG,
    DISPLAY_SETTLING_PASSES,
    canonicalize_display,
    redact_for_display,
)
from kiro_crew.messaging.renderer import _default_redactor, count_redaction_tags
from kiro_crew.messaging.split import _flattened_for_any_cut
from kiro_crew.security import CREDENTIAL_REDACTION_TAGS, EXFILTRATION_REDACTION_TAG_PREFIX
from kiro_crew.telegram.renderer import (
    _display_safe,
    _md_to_telegram_html,
    _strip_md,
)
from kiro_crew.whatsapp.renderer import to_whatsapp_text


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
