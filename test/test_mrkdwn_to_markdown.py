"""The bell renders a Slack-bound send legibly: mrkdwn converts to Markdown.

One ``send_message`` call reaches two surfaces with two grammars — Slack renders
mrkdwn, the bell renders Markdown. The bell's copy converts
(``slack.mrkdwn_markdown``); the Slack leg and every non-Slack channel leg
deliver the caller's bytes untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import messaging as mod
from kiro_crew.slack.mrkdwn_markdown import mrkdwn_to_markdown


class TestLinks:
    def test_a_labelled_slack_link_becomes_a_markdown_link(self):
        assert (
            mrkdwn_to_markdown("Review ready: <https://example.com/reviews/123|REVIEW-123>")
            == "Review ready: [REVIEW-123](https://example.com/reviews/123)"
        )

    def test_several_links_on_one_line_each_convert(self):
        text = "<https://a.example/1|one> and <https://a.example/2|two>"
        assert (
            mrkdwn_to_markdown(text) == "[one](https://a.example/1) and [two](https://a.example/2)"
        )

    def test_a_bare_bracketed_url_becomes_a_self_labelled_link(self):
        assert (
            mrkdwn_to_markdown("see <https://example.com/x>")
            == "see [https://example.com/x](https://example.com/x)"
        )

    def test_a_bare_non_web_scheme_stays_clickable(self):
        # Markdown autolinking covers web schemes only, so a bare editor link
        # must come out as a self-labelled link or it goes dead in the bell.
        assert (
            mrkdwn_to_markdown("open <vscode://file/project/app.py:42>")
            == "open [vscode://file/project/app.py:42](vscode://file/project/app.py:42)"
        )

    def test_a_destination_with_parentheses_is_markdown_bracketed(self):
        assert (
            mrkdwn_to_markdown("<https://e.example/a_(b)|doc>")
            == "[doc](<https://e.example/a_(b)>)"
        )

    def test_a_bang_before_a_converted_link_does_not_make_an_image(self):
        # On Slack the "!" is plain text; unescaped, "![...](...)" reads as a
        # Markdown IMAGE in the bell and the link vanishes into a broken img.
        assert (
            mrkdwn_to_markdown("Important!<https://example.com/review/123|Review>")
            == "Important\\![Review](https://example.com/review/123)"
        )

    def test_an_already_escaped_bang_before_a_link_is_not_double_escaped(self):
        # "\!" is already Markdown-escaped; adding another backslash would
        # yield "\\!", a literal backslash plus an image-forming bang.
        text = "\\!<https://example.com/review/123|Review>"
        assert mrkdwn_to_markdown(text) == "\\![Review](https://example.com/review/123)"

    def test_an_escaped_backslash_before_a_bang_still_gets_the_escape(self):
        # Two backslashes are a literal backslash, so the bang itself is
        # unescaped and must still be protected from pairing with the link.
        assert (
            mrkdwn_to_markdown("\\\\!<https://e.example/1|x>") == "\\\\\\![x](https://e.example/1)"
        )

    def test_a_self_labelled_url_escapes_formatting_characters_in_the_label(self):
        # The visible label must show the URL's literal characters; only the
        # destination keeps them raw. Unescaped, "_reports_" renders italic
        # while the link still points at the raw path — text disagreeing with
        # its own destination.
        assert (
            mrkdwn_to_markdown("<https://example.com/_reports_/123>")
            == "[https://example.com/\\_reports\\_/123](https://example.com/_reports_/123)"
        )

    def test_a_caller_label_with_formatting_characters_stays_literal(self):
        # Slack shows labels literally, so the bell must too.
        assert (
            mrkdwn_to_markdown("<https://e.example/1|v2~beta_hot*x`y>")
            == "[v2\\~beta\\_hot\\*x\\`y](https://e.example/1)"
        )

    def test_a_backslash_in_a_label_is_escaped_before_the_brackets(self):
        # A label's own trailing backslash must not fuse with the link's
        # closing bracket into an escape that un-links the whole span.
        assert (
            mrkdwn_to_markdown("<vscode://file/C:/work/|C:\\work\\>")
            == "[C:\\\\work\\\\](vscode://file/C:/work/)"
        )

    def test_square_brackets_in_a_label_are_escaped(self):
        assert (
            mrkdwn_to_markdown("<https://e.example/1|[P0] fix>")
            == "[\\[P0\\] fix](https://e.example/1)"
        )

    def test_mentions_and_broadcasts_stay_literal(self):
        for text in ("<@U0123ABC>", "<#C0123ABC|general>", "<!here>"):
            assert mrkdwn_to_markdown(text) == text

    def test_emphasis_characters_inside_a_destination_are_not_emphasis(self):
        assert (
            mrkdwn_to_markdown("<https://e.example/_private_|doc>")
            == "[doc](https://e.example/_private_)"
        )


class TestEmphasis:
    def test_slack_bold_becomes_markdown_bold(self):
        assert mrkdwn_to_markdown("a *bold* word") == "a **bold** word"

    def test_slack_italic_becomes_markdown_italic(self):
        assert mrkdwn_to_markdown("an _italic_ word") == "an *italic* word"

    def test_slack_strike_becomes_markdown_strike(self):
        assert mrkdwn_to_markdown("a ~gone~ word") == "a ~~gone~~ word"

    def test_a_snake_case_identifier_is_not_italic(self):
        assert mrkdwn_to_markdown("call send_message_now()") == "call send_message_now()"

    def test_already_markdown_bold_passes_through(self):
        assert mrkdwn_to_markdown("a **bold** word") == "a **bold** word"

    def test_an_existing_markdown_link_passes_through(self):
        text = "see [docs](https://example.com/docs)"
        assert mrkdwn_to_markdown(text) == text

    def test_a_markdown_destination_with_delimiters_is_not_emphasis(self):
        # URL-shaped spans are atoms: "_" and "*" inside a destination are
        # path, not markup, so an already-Markdown body round-trips unharmed.
        text = "[report](https://example.com/_reports_/123)"
        assert mrkdwn_to_markdown(text) == text

    def test_a_markdown_image_destination_is_not_emphasis(self):
        text = "![shot](https://example.com/~v2~/img_01_.png)"
        assert mrkdwn_to_markdown(text) == text

    def test_a_markdown_destination_with_balanced_parens_is_one_atom(self):
        text = "[wiki](https://en.example.org/wiki/Python_(programming_language))"
        assert mrkdwn_to_markdown(text) == text

    def test_a_bare_url_with_delimiters_is_not_emphasis(self):
        assert (
            mrkdwn_to_markdown("see https://example.com/_private_/a*b* now *ok*")
            == "see https://example.com/_private_/a*b* now **ok**"
        )

    def test_slack_and_markdown_links_coexist_on_one_line(self):
        assert (
            mrkdwn_to_markdown("<https://a.example/1|one> vs [two](https://b.example/_2_)")
            == "[one](https://a.example/1) vs [two](https://b.example/_2_)"
        )

    def test_html_entities_pass_through(self):
        assert mrkdwn_to_markdown("a &lt;tag&gt; &amp; more") == "a &lt;tag&gt; &amp; more"


class TestEmphasisAcrossTokens:
    def test_bold_enclosing_a_link_converts_as_one_pair(self):
        assert (
            mrkdwn_to_markdown("*Review <https://example.com/reviews/123|REVIEW-123> now*")
            == "**Review [REVIEW-123](https://example.com/reviews/123) now**"
        )

    def test_bold_enclosing_inline_code_converts_as_one_pair(self):
        assert mrkdwn_to_markdown("*run `make build` now*") == "**run `make build` now**"

    def test_a_backtick_inside_a_label_belongs_to_the_link(self):
        # The "<" opened before the backtick, so the span is a link whose
        # label carries a literal backtick — not code that splits the link.
        assert (
            mrkdwn_to_markdown("<https://example.com/file|View `main.py` file>")
            == "[View \\`main.py\\` file](https://example.com/file)"
        )

    def test_a_link_inside_a_double_backtick_span_stays_code(self):
        text = "``echo `pwd` && curl <https://example.com/api>``"
        assert mrkdwn_to_markdown(text) == text


class TestCodeSpans:
    def test_inline_code_is_kept_verbatim(self):
        assert (
            mrkdwn_to_markdown("run `cmd *args*` then *stop*") == "run `cmd *args*` then **stop**"
        )

    def test_fenced_code_is_kept_verbatim(self):
        text = "before *b*\n```\n<https://x.example|y> *raw*\n```\nafter *b*"
        assert (
            mrkdwn_to_markdown(text)
            == "before **b**\n```\n<https://x.example|y> *raw*\n```\nafter **b**"
        )

    def test_an_unterminated_fence_runs_to_the_end(self):
        text = "*b* then\n```\n*raw to the end*"
        assert mrkdwn_to_markdown(text) == "**b** then\n```\n*raw to the end*"


_MRKDWN = "Ready: <https://e.example/r/1|R-1> is *done*"
_MARKDOWN = "Ready: [R-1](https://e.example/r/1) is **done**"


async def _fallback_send(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> MagicMock:
    """Run the fallback delivery with every egress leg stubbed; return state."""
    state = MagicMock()
    state.crons.list_jobs.return_value = []
    state.get_slot.return_value = None

    async def _slack_post(*args: Any, **kw: Any) -> None:
        return None

    async def _own(_state: Any, _key: str, _text: str, **_kw: Any) -> tuple[bool, str, str]:
        return True, "", ""

    monkeypatch.setattr(mod, "_post_send_message_to_slack", _slack_post)
    monkeypatch.setattr(mod, "_deliver_to_channel", _own)
    monkeypatch.setattr(mod, "_vet_app_messaging", lambda app, session: "")
    # member_identity_for_session is NOT patched: the owner imports it, so the
    # composition contract forbids rebinding it on the facade — and the empty
    # attribution_session below makes it answer None without any file IO.
    defaults: dict[str, Any] = dict(
        text=_MRKDWN,
        title="t",
        blocks=None,
        options=[],
        target_channel="",
        target_user="",
        thread_ts=None,
        reply_broadcast=None,
        target_session="",
        job_name=None,
        channel_target="",
        channel_type="",
        caller_session="",
        declared_session="",
        attribution_session="",
        is_cron_caller=False,
        send_to_slack=False,
    )
    defaults.update(kwargs)
    await mod._deliver_send_message_fallback(state, {}, mod._SendMessageOutcome(), **defaults)
    return state


@pytest.mark.asyncio
async def test_a_slack_bound_send_reaches_the_bell_as_markdown(monkeypatch):
    state = await _fallback_send(monkeypatch, send_to_slack=True)
    (_kind, _title, body), _kw = state.notify.call_args
    assert body == _MARKDOWN


@pytest.mark.asyncio
async def test_a_channel_type_send_reaches_the_bell_verbatim(monkeypatch):
    # A non-Slack channel's text is that channel's own format, not mrkdwn, so
    # the bell must not reinterpret it.
    state = await _fallback_send(monkeypatch, channel_type="telegram")
    (_kind, _title, body), _kw = state.notify.call_args
    assert body == _MRKDWN


@pytest.mark.asyncio
async def test_a_slack_bound_send_rescans_the_converted_copy_for_secrets(monkeypatch):
    # A secret split by a sentinel character and emphasis sigils survives the
    # route's pre-conversion display scan; conversion reassembles it, so the
    # BELL's copy must be re-scanned in its own settled form before storage.
    state = await _fallback_send(
        monkeypatch,
        send_to_slack=True,
        text="key AKIA\x00*IOSFODNN7EXAMPLE* end",
    )
    (_kind, _title, body), _kw = state.notify.call_args
    assert "IOSFODNN7EXAMPLE" not in body
