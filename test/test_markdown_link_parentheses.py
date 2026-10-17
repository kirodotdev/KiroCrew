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

import pytest
from markdown_link_parentheses_helpers import (
    _ESCAPED,
    _KEY,
    _URL_SPLIT_TRIGGER,
    WIKI,
    _discord_reader_sees,
    _nested_link,
    _regex_destination,
    _screen_readings,
    _slack,
    _slack_reader_sees,
)

from conftest import assert_rejected_without_backtracking
from kiro_crew import preview_text, voice_reply
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
from kiro_crew.telegram import renderer as telegram_renderer
from kiro_crew.telegram.renderer import (
    _display_safe,
    _md_to_telegram_html,
    _strip_md,
)
from kiro_crew.whatsapp import renderer as whatsapp_renderer
from kiro_crew.whatsapp.renderer import to_whatsapp_text

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

        def reject(text: str) -> None:
            assert pattern.sub("", text) == text, renderer

        assert_rejected_without_backtracking(reject, lambda n: "[b" * n, budget_seconds=0.5)


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

        def reject(text: str) -> None:
            assert pattern.sub("", text) == text, (site, opener)

        assert_rejected_without_backtracking(reject, lambda n: opener * n, budget_seconds=0.5)

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

        def reject(text: str) -> None:
            assert display_safety._balanced_link_reading(text) == text

        assert_rejected_without_backtracking(reject, lambda n: "[l](<" * n)


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
