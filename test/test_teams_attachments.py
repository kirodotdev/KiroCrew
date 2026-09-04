"""Tests for Teams' file POLICY layer (``kiro_crew.teams.attachments``).

Covers the inbound envelope mapping -- including the auth flag that differs per
attachment kind and is a credential leak if inverted -- and the outbound
inline-image policy: the narrower format allow-list, the byte budgets handed to
the neutral extractor, and the refusals that keep a path visible.

The wire halves (the bounded download, the seal that posts an activity) live in
``test_teams_files.py``.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.messaging.attachments import IngestResult
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.teams.attachments import (
    REASON_INLINE_UNDELIVERED,
    REASON_INLINE_UNSUPPORTED,
    TEAMS_FILE_DOWNLOAD_INFO,
    TEAMS_INLINE_IMAGE_MIMES,
    TEAMS_MAX_INLINE_IMAGE_BYTES,
    TEAMS_MAX_INLINE_IMAGES,
    TEAMS_UPLOAD_LIMITS,
    inline_image_attachment,
    inline_image_name,
    map_inbound_attachments,
    process_teams_attachments,
    quoted_reply_text,
    undeliverable_rejection,
    unsupported_inline_rejection,
)

# A one-pixel PNG: real leading bytes, so the neutral signature check accepts it.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _download_info_attachment(url: str = "https://contoso.sharepoint.com/dl") -> dict:
    return {
        "contentType": TEAMS_FILE_DOWNLOAD_INFO,
        "contentUrl": "https://contoso.sharepoint.com/personal/a/notes.txt",
        "name": "notes.txt",
        "content": {"downloadUrl": url, "uniqueId": "abc", "fileType": "txt"},
    }


def _inline_image_activity_attachment(url: str = "https://smba.trafficmanager.net/img/1") -> dict:
    return {"contentType": "image/png", "contentUrl": url, "name": "shot.png"}


class TestInboundMapping:
    def test_download_info_is_fetched_without_the_bot_token(self) -> None:
        """A personal-chat upload carries its own capability in the URL.

        The bot's Connector token is credential-equivalent and this host is not
        guaranteed to be one Microsoft operates, so the flag MUST be False.
        """
        mapped, unsupported = map_inbound_attachments([_download_info_attachment()])
        assert unsupported == []
        attachment, needs_token = mapped[0]
        assert needs_token is False
        assert attachment.url == "https://contoso.sharepoint.com/dl"
        assert attachment.name == "notes.txt"
        # ``content.fileType`` drives the suffix hint, not the display name.
        assert attachment.suffix_hint == "txt"

    def test_inline_image_is_marked_as_needing_the_token(self) -> None:
        mapped, unsupported = map_inbound_attachments([_inline_image_activity_attachment()])
        assert unsupported == []
        attachment, needs_token = mapped[0]
        assert needs_token is True
        assert attachment.mimetype == "image/png"

    def test_message_body_echo_is_not_a_file(self) -> None:
        """Teams echoes rich text as a ``text/html`` attachment on ordinary messages.

        Ingesting it would inject the same words twice into every prompt, so it is
        skipped -- and NOT reported, because a per-message note would be noise.
        """
        mapped, unsupported = map_inbound_attachments(
            [
                {"contentType": "text/html", "content": "<p>hi</p>"},
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": {}},
            ]
        )
        assert mapped == []
        assert unsupported == []

    def test_unrecognized_kind_is_reported_never_fetched(self) -> None:
        mapped, unsupported = map_inbound_attachments(
            [{"contentType": "application/x-mystery", "contentUrl": "https://x.example/y"}]
        )
        assert mapped == []
        assert unsupported == ["application/x-mystery"]

    def test_non_https_or_data_image_url_is_refused(self) -> None:
        """A credential must never travel over http, and a data URI is not a fetch."""
        mapped, unsupported = map_inbound_attachments(
            [
                _inline_image_activity_attachment("http://smba.trafficmanager.net/img/1"),
                _inline_image_activity_attachment("data:image/png;base64,AAAA"),
            ]
        )
        assert mapped == []
        assert unsupported == ["image/png", "image/png"]

    def test_download_info_without_a_url_is_reported(self) -> None:
        raw = _download_info_attachment()
        raw["content"] = {"uniqueId": "abc"}
        mapped, unsupported = map_inbound_attachments([raw])
        assert mapped == []
        assert unsupported == [TEAMS_FILE_DOWNLOAD_INFO]

    def test_non_dict_entries_are_ignored(self) -> None:
        mapped, unsupported = map_inbound_attachments(["nope", None, 3])
        assert (mapped, unsupported) == ([], [])


class _RecordingClient:
    """Records the auth flag each URL was fetched with, and writes PNG bytes."""

    def __init__(self) -> None:
        self.fetched: list[tuple[str, bool]] = []

    async def download_inbound_file(
        self, url: str, dest: str, *, authenticated: bool = False
    ) -> None:
        self.fetched.append((url, authenticated))
        with open(dest, "wb") as fh:
            fh.write(PNG_BYTES)


class TestInboundIngestion:
    @pytest.mark.asyncio
    async def test_auth_flag_follows_the_attachment_kind(self, tmp_path, monkeypatch) -> None:
        """The per-URL decision is frozen before any fetch and never re-derived."""
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        client = _RecordingClient()
        result = await process_teams_attachments(
            client,
            [
                _download_info_attachment("https://contoso.sharepoint.com/dl"),
                _inline_image_activity_attachment("https://smba.trafficmanager.net/img/1"),
            ],
        )
        assert client.fetched == [
            ("https://contoso.sharepoint.com/dl", False),
            ("https://smba.trafficmanager.net/img/1", True),
        ]
        # The neutral pipeline routes each by CLASS: the inline image becomes a
        # path the prompt inlines, while the ``.txt`` upload becomes inlined text.
        assert len(result.image_paths) == 1
        assert len(result.text_blocks) == 1
        for path in result.temp_paths:
            assert path.startswith(str(tmp_path))
        from kiro_crew.messaging.attachments import cleanup

        cleanup(result.temp_paths)

    @pytest.mark.asyncio
    async def test_unsupported_kinds_are_surfaced_with_no_fetch(self) -> None:
        client = _RecordingClient()
        result = await process_teams_attachments(
            client, [{"contentType": "application/x-mystery", "contentUrl": "https://x/y"}]
        )
        assert client.fetched == []
        assert result.rejections == [
            "[Attachment of type application/x-mystery — unsupported here]"
        ]

    @pytest.mark.asyncio
    async def test_no_attachments_returns_an_empty_result(self) -> None:
        result = await process_teams_attachments(_RecordingClient(), [])
        assert isinstance(result, IngestResult)
        assert (result.image_paths, result.text_blocks, result.rejections) == ([], [], [])


class TestOutboundPolicy:
    def test_budgets_are_the_teams_ceilings(self) -> None:
        """The per-file ceiling must reach extraction, so an oversize image is
        refused BY THE READ with its markdown intact rather than after the cut."""
        assert TEAMS_UPLOAD_LIMITS.max_file_bytes == TEAMS_MAX_INLINE_IMAGE_BYTES
        assert TEAMS_UPLOAD_LIMITS.max_files == TEAMS_MAX_INLINE_IMAGES
        assert TEAMS_UPLOAD_LIMITS.max_total_bytes == (
            TEAMS_MAX_INLINE_IMAGES * TEAMS_MAX_INLINE_IMAGE_BYTES
        )
        assert TEAMS_MAX_INLINE_IMAGE_BYTES == 1024 * 1024

    def test_gif_webp_and_bmp_are_not_inlinable(self) -> None:
        """Teams says animated GIF does not render and animation is not decidable
        from leading bytes; WebP and BMP are not in its documented set."""
        assert TEAMS_INLINE_IMAGE_MIMES == {"image/png", "image/jpeg"}

    def test_attachment_carries_the_validated_bytes_not_the_path(self) -> None:
        """A transport must upload ``OutboundFile.data``; re-opening ``path`` could
        name a different file by then."""
        file = OutboundFile(path="/tmp/chart.png", data=PNG_BYTES, alt="chart", mime="image/png")
        attachment = inline_image_attachment(file, "chart.png")
        assert attachment["contentType"] == "image/png"
        prefix = "data:image/png;base64,"
        assert attachment["contentUrl"].startswith(prefix)
        assert base64.b64decode(attachment["contentUrl"][len(prefix) :]) == PNG_BYTES
        assert "/tmp/chart.png" not in attachment["contentUrl"]

    def test_name_is_sanitized_and_retyped_from_the_sniffed_mime(self) -> None:
        file = OutboundFile(path="/tmp/x.png", data=PNG_BYTES, alt="", mime="image/jpeg")
        # Separators collapse and the leading dots go, so a caption can never read
        # as a path or a hidden file.
        assert inline_image_name("../../etc/passwd\nrevenue Q1", file) == (
            "etc_passwd_revenue_Q1.jpg"
        )
        # An empty caption falls back to the basename, and the suffix follows the
        # SNIFFED type rather than claiming both.
        assert inline_image_name("", file) == "x.jpg"
        assert inline_image_name("***", file) == "image.jpg"

    def test_name_is_length_bounded(self) -> None:
        file = OutboundFile(path="/tmp/x.png", data=PNG_BYTES, alt="", mime="image/png")
        name = inline_image_name("a" * 500, file)
        assert name == "a" * 64 + ".png"

    def test_refusals_name_the_path_and_a_closed_reason_code(self) -> None:
        """A refused image keeps its path visible; the code is machine-readable so a
        caller can branch without parsing English."""
        file = OutboundFile(path="/tmp/chart.webp", data=b"RIFF", alt="", mime="image/webp")
        unsupported = unsupported_inline_rejection(file)
        assert unsupported.reason == REASON_INLINE_UNSUPPORTED
        assert unsupported.dest == "/tmp/chart.webp"
        assert "/tmp/chart.webp" in str(unsupported)
        undelivered = undeliverable_rejection(file)
        assert undelivered.reason == REASON_INLINE_UNDELIVERED
        assert "/tmp/chart.webp" in str(undelivered)


class TestQuoteReply:
    """Right-click -> Reply in a 1:1 chat, the only scope this channel serves.

    Teams prepends the QUOTED message to ``activity.text`` and puts the user's own
    words in the ``text/html`` body attachment after a Reply blockquote. Reading
    ``activity.text`` alone means a quote-replied ``/stop`` no longer starts with
    "/" -- so it reaches the model as prose and the turn keeps running -- and a
    quote-replied question arrives with the previous message on the front.
    """

    _MARKER = 'itemtype="http://schema.skype.com/Reply"'

    def _body(self, html: str) -> list[dict]:
        return [{"contentType": "text/html", "content": html}]

    def test_the_users_own_words_are_recovered(self) -> None:
        html = (
            f"<div><blockquote {self._MARKER} itemid='1'><strong>Alice</strong>"
            "<p>what should I do about the deploy?</p></blockquote><p>/stop</p></div>"
        )
        assert quoted_reply_text(self._body(html)) == "/stop"

    def test_paragraphs_become_lines_rather_than_a_run_on(self) -> None:
        html = (
            f"<blockquote {self._MARKER}><p>old</p></blockquote>"
            "<p>first line</p><p>second &amp; last</p>"
        )
        assert quoted_reply_text(self._body(html)) == "first line\nsecond & last"

    def test_a_br_is_a_line_break_too(self) -> None:
        html = f"<blockquote {self._MARKER}><p>old</p></blockquote>a<br/>b"
        assert quoted_reply_text(self._body(html)) == "a\nb"

    def test_an_ordinary_rich_text_body_is_not_a_reply(self) -> None:
        assert quoted_reply_text(self._body("<p>just a message</p>")) == ""

    def test_a_reply_with_nothing_after_the_quote_falls_back(self) -> None:
        """The caller keeps ``activity.text``, which is better than an empty prompt."""
        html = f"<blockquote {self._MARKER}><p>old</p></blockquote>"
        assert quoted_reply_text(self._body(html)) == ""

    def test_an_unrecognised_shape_is_never_guessed_at(self) -> None:
        """The marker is present but the blockquote does not close: do not guess."""
        html = f"<blockquote {self._MARKER}><p>old"
        assert quoted_reply_text(self._body(html)) == ""

    def test_an_oversized_body_is_skipped_rather_than_parsed(self) -> None:
        from kiro_crew.teams.attachments import _MAX_REPLY_HTML_CHARS

        html = (
            f"<blockquote {self._MARKER}><p>{'x' * _MAX_REPLY_HTML_CHARS}</p>"
            "</blockquote><p>hi</p>"
        )
        assert len(html) > _MAX_REPLY_HTML_CHARS
        assert quoted_reply_text(self._body(html)) == ""

    @pytest.mark.parametrize("markup", ["a<br>b", "a<br/>b", "a<br />b", "<p>a</p><p>b</p>"])
    def test_every_break_form_teams_emits_becomes_a_newline(self, markup: str) -> None:
        html = f"<blockquote {self._MARKER}><p>old</p></blockquote>{markup}"
        assert quoted_reply_text(self._body(html)) == "a\nb"

    def test_the_break_pattern_is_linear_not_quadratic(self) -> None:
        """No unbounded whitespace before a required literal.

        ``<\\s*br`` is a polynomial-ReDoS shape: every ``<`` is a candidate start,
        ``\\s*`` eats a run of tabs, the literal fails, and it backtracks one
        character at a time. This parse runs INLINE on the inbound path, so a 64 KB
        body of ``<`` + tabs would stall the gateway's event loop.

        Bounds the doubling RATIO rather than an absolute duration: CI enables
        coverage on 3.12 only, and that multiplier makes a wall-clock assertion fail
        on one shard and pass on another at the same commit.
        """
        import time

        from kiro_crew.teams.attachments import _BREAK_RE

        def _elapsed(n: int) -> float:
            payload = "<br" + "\t" * n
            start = time.perf_counter()
            for _ in range(20):
                _BREAK_RE.sub("\n", payload)
            return time.perf_counter() - start

        small = _elapsed(4_000)
        large = _elapsed(16_000)
        # Linear would be ~4x for 4x the input; quadratic ~16x. A generous ceiling
        # still separates the two by a wide margin.
        assert large < small * 8 + 0.05, f"{small=} {large=} looks super-linear"

    def test_a_non_html_attachment_is_ignored(self) -> None:
        assert quoted_reply_text([{"contentType": "text/plain", "content": "x"}]) == ""
        assert quoted_reply_text([{"contentType": TEAMS_FILE_DOWNLOAD_INFO}]) == ""
        assert quoted_reply_text(["not a dict", None, 7]) == ""

    def test_the_body_attachment_is_still_never_ingested_as_a_file(self) -> None:
        """Recovering text from it must not make it a file: that duplicates the prompt."""
        html = f"<blockquote {self._MARKER}><p>old</p></blockquote><p>hi</p>"
        mapped, unsupported = map_inbound_attachments(self._body(html))
        assert mapped == []
        assert unsupported == []
