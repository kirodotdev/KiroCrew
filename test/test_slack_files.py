"""Tests for Slack file attachment processing."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack.files import (
    _MAX_IMAGE_BYTES,
    _MAX_TEXT_BYTES,
    _MAX_TEXT_INJECT,
    _safe_suffix,
    process_slack_files,
)


def _make_orch(download_content: bytes = b"hello") -> MagicMock:
    orch = MagicMock()
    orch.slack = AsyncMock()

    async def _download(url: str, dest: str) -> None:
        with open(dest, "wb") as f:
            f.write(download_content)

    orch.slack.download_file = AsyncMock(side_effect=_download)
    return orch


class TestSafeSuffix:
    def test_normal(self):
        assert _safe_suffix("png") == ".png"

    def test_strips_special_chars(self):
        assert _safe_suffix("../../foo") == ".foo"

    def test_empty_uses_default(self):
        assert _safe_suffix("", "png") == ".png"

    def test_all_special_uses_default(self):
        assert _safe_suffix("///", "bin") == ".bin"


class TestProcessSlackFiles:
    @pytest.mark.asyncio
    async def test_image_downloaded_to_temp(self):
        orch = _make_orch(b"\x89PNG fake image data")
        files = [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/img.png",
                "filetype": "png",
                "name": "screenshot.png",
                "size": 1024,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(image_paths) == 1
        assert image_paths[0].endswith(".png")
        assert os.path.exists(image_paths[0])
        os.unlink(image_paths[0])
        assert text_blocks == []

    @pytest.mark.asyncio
    async def test_image_too_large_skipped(self):
        orch = _make_orch()
        files = [
            {
                "mimetype": "image/jpeg",
                "url_private_download": "https://files.slack.com/big.jpg",
                "filetype": "jpg",
                "name": "huge.jpg",
                "size": _MAX_IMAGE_BYTES + 1,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert text_blocks == []

    @pytest.mark.asyncio
    async def test_text_file_content_injected(self):
        content = "def hello():\n    print('world')\n"
        orch = _make_orch(content.encode())
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/code.py",
                "filetype": "py",
                "name": "code.py",
                "size": len(content),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "[File: code.py]" in text_blocks[0]
        assert "def hello():" in text_blocks[0]
        assert "[End of file]" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_json_file_accepted(self):
        content = '{"key": "value"}'
        orch = _make_orch(content.encode())
        files = [
            {
                "mimetype": "application/json",
                "url_private_download": "https://files.slack.com/data.json",
                "filetype": "json",
                "name": "data.json",
                "size": len(content),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(text_blocks) == 1
        assert "[File: data.json]" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_text_file_too_large_skipped(self):
        orch = _make_orch()
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/big.log",
                "filetype": "log",
                "name": "big.log",
                "size": _MAX_TEXT_BYTES + 1,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "too large" in text_blocks[0]
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_content_truncated(self):
        content = "x" * (_MAX_TEXT_INJECT + 1000)
        orch = _make_orch(content.encode())
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/long.txt",
                "filetype": "txt",
                "name": "long.txt",
                "size": len(content),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(text_blocks) == 1
        assert "truncated" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_unsupported_type_metadata_only(self):
        orch = _make_orch()
        files = [
            {
                "mimetype": "application/zip",
                "url_private_download": "https://files.slack.com/archive.zip",
                "filetype": "zip",
                "name": "archive.zip",
                "size": 5000,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "unsupported type" in text_blocks[0]
        assert "archive.zip" in text_blocks[0]
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_audio_files_skipped(self):
        orch = _make_orch()
        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://files.slack.com/voice.webm",
                "filetype": "webm",
                "name": "voice.webm",
                "size": 1000,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert text_blocks == []
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_url_skipped(self):
        orch = _make_orch()
        files = [{"mimetype": "image/png", "name": "no_url.png", "size": 100}]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert text_blocks == []

    @pytest.mark.asyncio
    async def test_url_private_fallback(self):
        """url_private used when url_private_download is absent."""
        orch = _make_orch(b"\x89PNG")
        files = [
            {
                "mimetype": "image/png",
                "url_private": "https://files.slack.com/img.png",
                "filetype": "png",
                "name": "fallback.png",
                "size": 100,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(image_paths) == 1
        os.unlink(image_paths[0])

    @pytest.mark.asyncio
    async def test_download_failure_cleans_temp(self):
        """Temp file from mkstemp is cleaned up on download failure."""
        orch = MagicMock()
        orch.slack = AsyncMock()
        orch.slack.download_file = AsyncMock(
            side_effect=Exception("network error"),
        )
        files = [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/f.png",
                "filetype": "png",
                "name": "fail.png",
                "size": 100,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert text_blocks == []

    @pytest.mark.asyncio
    async def test_text_temp_file_cleaned(self):
        """Text file temp is cleaned up after reading."""
        import tempfile
        from unittest.mock import patch

        created_paths: list[str] = []
        original_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(**kwargs):  # type: ignore[no-untyped-def]
            fd, path = original_mkstemp(**kwargs)
            created_paths.append(path)
            return fd, path

        orch = _make_orch(b"hello world")
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/t.txt",
                "filetype": "txt",
                "name": "test.txt",
                "size": 11,
            }
        ]
        with patch("kiro_crew.slack.files.tempfile.mkstemp", side_effect=tracking_mkstemp):
            await process_slack_files(orch, files)
        assert len(created_paths) == 1
        assert not os.path.exists(created_paths[0])

    @pytest.mark.asyncio
    async def test_empty_text_file(self):
        """Empty text file produces valid wrapper."""
        orch = _make_orch(b"")
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/e.txt",
                "filetype": "txt",
                "name": "empty.txt",
                "size": 0,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(text_blocks) == 1
        assert "[File: empty.txt]" in text_blocks[0]
        assert "[End of file]" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_orch_slack_none_returns_empty(self):
        """Gracefully returns empty when orch.slack is None."""
        orch = MagicMock()
        orch.slack = None
        files = [
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/x.png",
                "name": "x.png",
                "size": 100,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert text_blocks == []

    @pytest.mark.asyncio
    async def test_mixed_files(self):
        """Multiple file types in one message."""
        img_data = b"\x89PNG"
        call_count = 0

        async def _download(url: str, dest: str) -> None:
            nonlocal call_count
            call_count += 1
            content = img_data if "img" in url else b"log line 1\n"
            with open(dest, "wb") as f:
                f.write(content)

        orch = MagicMock()
        orch.slack = AsyncMock()
        orch.slack.download_file = AsyncMock(side_effect=_download)

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://files.slack.com/voice.webm",
                "filetype": "webm",
                "name": "voice.webm",
                "size": 500,
            },
            {
                "mimetype": "image/png",
                "url_private_download": "https://files.slack.com/img.png",
                "filetype": "png",
                "name": "screenshot.png",
                "size": 1024,
            },
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/log.txt",
                "filetype": "txt",
                "name": "output.log",
                "size": 30,
            },
            {
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/doc.pdf",
                "filetype": "pdf",
                "name": "doc.pdf",
                "size": 2000,
            },
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)

        assert len(image_paths) == 1
        assert len(text_blocks) == 2  # text + pdf (could not extract)
        assert any("[File: output.log]" in b for b in text_blocks)
        assert any("could not extract text" in b for b in text_blocks)
        assert call_count == 3  # image + text + pdf document

        for p in image_paths:
            os.unlink(p)

    @pytest.mark.asyncio
    async def test_text_file_credentials_redacted(self):
        content = "aws_secret_access_key =" " ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd"
        orch = _make_orch(content.encode())
        files = [
            {
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/c.txt",
                "filetype": "txt",
                "name": "creds.txt",
                "size": len(content),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert len(text_blocks) == 1
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd" not in text_blocks[0]
        assert "REDACTED" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_image_svg_treated_as_unsupported(self):
        """SVG is not in the ACP-compatible image set."""
        orch = _make_orch()
        files = [
            {
                "mimetype": "image/svg+xml",
                "url_private_download": "https://files.slack.com/i.svg",
                "filetype": "svg",
                "name": "icon.svg",
                "size": 500,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "unsupported type" in text_blocks[0]
        orch.slack.download_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_docx_file_parsed(self):
        """A .docx attachment is downloaded, parsed, and text injected."""
        import zipfile as _zf

        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Narrative text</w:t></w:r></w:p></w:body></w:document>"
        )
        import io

        buf = io.BytesIO()
        with _zf.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", xml)
        docx_bytes = buf.getvalue()

        orch = _make_orch(docx_bytes)
        files = [
            {
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private_download": "https://files.slack.com/doc.docx",
                "filetype": "docx",
                "name": "narrative.docx",
                "size": len(docx_bytes),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "[Document: narrative.docx]" in text_blocks[0]
        assert "Narrative text" in text_blocks[0]
        assert "[End of document]" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_pptx_file_parsed(self):
        """A .pptx attachment is downloaded, parsed, and text injected."""
        import io
        import zipfile as _zf

        slide_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            "<p:cSld><p:spTree>"
            "<p:sp><p:txBody><a:p><a:r><a:t>Deck Title</a:t></a:r></a:p></p:txBody></p:sp>"
            "</p:spTree></p:cSld></p:sld>"
        )
        buf = io.BytesIO()
        with _zf.ZipFile(buf, "w") as zf:
            zf.writestr("ppt/slides/slide1.xml", slide_xml)
        pptx_bytes = buf.getvalue()

        orch = _make_orch(pptx_bytes)
        files = [
            {
                "mimetype": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "url_private_download": "https://files.slack.com/deck.pptx",
                "filetype": "pptx",
                "name": "review.pptx",
                "size": len(pptx_bytes),
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "[Document: review.pptx]" in text_blocks[0]
        assert "Deck Title" in text_blocks[0]

    @pytest.mark.asyncio
    async def test_document_too_large_skipped(self):
        """Documents exceeding size limit are skipped."""
        from kiro_crew.slack.files import _MAX_DOC_BYTES

        orch = _make_orch()
        files = [
            {
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "url_private_download": "https://files.slack.com/huge.docx",
                "filetype": "docx",
                "name": "huge.docx",
                "size": _MAX_DOC_BYTES + 1,
            }
        ]
        image_paths, text_blocks = await process_slack_files(orch, files)
        assert image_paths == []
        assert len(text_blocks) == 1
        assert "too large" in text_blocks[0]
        orch.slack.download_file.assert_not_called()
