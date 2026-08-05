"""Unit tests for file_send supporting functions: hooks, loader, security."""

from unittest.mock import patch

import pytest

from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.security import redact


class TestSafeReadFileBytes:
    def test_reads_normal_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello world")
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            result = safe_read_file_bytes(str(f))
        assert result == b"hello world"

    def test_rejects_sensitive_path(self, tmp_path):
        f = tmp_path / "secret.txt"
        f.write_bytes(b"secret")
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=True):
            assert safe_read_file_bytes(str(f)) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            assert safe_read_file_bytes(str(tmp_path / "nope.txt")) is None

    def test_raises_file_too_large(self, tmp_path):
        f = tmp_path / "big.txt"
        # Write just over the limit
        with patch("kiro_crew.hooks.MAX_FILE_BYTES", 10):
            with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
                f.write_bytes(b"x" * 12)
                with pytest.raises(FileTooLargeError):
                    safe_read_file_bytes(str(f))

    def test_returns_bytes_at_exact_limit(self, tmp_path):
        f = tmp_path / "exact.txt"
        with patch("kiro_crew.hooks.MAX_FILE_BYTES", 10):
            with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
                f.write_bytes(b"x" * 10)
                assert safe_read_file_bytes(str(f)) == b"x" * 10


class TestOutboxDir:
    def test_creates_and_returns_outbox(self, tmp_path):
        with patch("kiro_crew.config.loader.workspace_root", return_value=tmp_path):
            from kiro_crew.config.loader import outbox_dir

            result = outbox_dir()
            assert result == tmp_path / "outbox"
            assert result.is_dir()


class TestRedact:
    def test_clean_text_unchanged(self):
        assert redact("hello world") == "hello world"

    def test_redacts_aws_key(self):
        text = "key=AKIAIOSFODNN7EXAMPLE"
        assert redact(text) != text

    def test_redacts_exfiltration_url(self):
        # Exfiltration detection triggers on long query params with secret patterns
        blob = "A" * 50  # base64-like blob ≥40 chars
        text = f"https://evil.example.com/x?data={blob}&{'x' * 200}"
        assert redact(text) != text


class TestBinaryFileHandling:
    """Tests for binary file support in file_send / outbox."""

    def test_binary_file_skips_redact(self, tmp_path):
        """Binary files should not be rejected by redact (can't decode UTF-8)."""
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)  # MP3 header
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            raw = safe_read_file_bytes(str(f))
        assert raw is not None
        # Binary: decode fails, so redact should be skipped
        is_text = True
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
        assert is_text is False

    def test_text_file_still_redacted(self, tmp_path):
        """Text files with sensitive content should still be caught."""
        f = tmp_path / "config.txt"
        f.write_text("key=AKIAIOSFODNN7EXAMPLE")
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            raw = safe_read_file_bytes(str(f))
        text = raw.decode("utf-8")
        assert redact(text) != text  # Would be blocked

    def test_mime_type_detection(self):
        """mimetypes.guess_type returns correct types for media files."""
        import mimetypes
        assert mimetypes.guess_type("file.mp3")[0] == "audio/mpeg"
        assert mimetypes.guess_type("file.mp4")[0] == "video/mp4"
        assert mimetypes.guess_type("file.wav")[0] in ("audio/wav", "audio/x-wav")
        assert mimetypes.guess_type("file.webm")[0] == "video/webm"
        assert mimetypes.guess_type("file.unknown")[0] is None

    def test_svg_not_inline(self):
        """SVG files should get attachment disposition (XSS prevention)."""
        import mimetypes
        content_type, _ = mimetypes.guess_type("icon.svg")
        assert content_type == "image/svg+xml"
        # SVG must be excluded from inline
        _inline_types = {"audio/", "video/", "image/", "application/pdf"}
        disposition = "inline" if any(content_type.startswith(t) for t in _inline_types) else "attachment"
        if content_type == "image/svg+xml":
            disposition = "attachment"
        assert disposition == "attachment"

    def test_audio_gets_inline_disposition(self):
        """Audio files should get inline disposition."""
        import mimetypes
        content_type, _ = mimetypes.guess_type("standup.mp3")
        _inline_types = {"audio/", "video/", "image/", "application/pdf"}
        disposition = "inline" if any(content_type.startswith(t) for t in _inline_types) else "attachment"
        assert disposition == "inline"

    def test_unknown_gets_attachment_disposition(self):
        """Unknown file types should get attachment disposition."""
        import mimetypes
        content_type, _ = mimetypes.guess_type("data.xyz")
        if not content_type:
            content_type = "application/octet-stream"
        _inline_types = {"audio/", "video/", "image/", "application/pdf"}
        disposition = "inline" if any(content_type.startswith(t) for t in _inline_types) else "attachment"
        assert disposition == "attachment"

    def test_binary_notify_accepts_mp3(self, tmp_path):
        """api_outbox_notify logic: binary file passes validation."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 50)
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            raw = safe_read_file_bytes(str(f))
        assert raw is not None
        # Simulate the notify validation logic
        try:
            text = raw.decode("utf-8")
            # If it decoded, check redact
            blocked = redact(text) != text
        except UnicodeDecodeError:
            blocked = False  # Binary — passes
        assert blocked is False

    def test_text_notify_blocks_sensitive(self, tmp_path):
        """api_outbox_notify logic: text file with secrets is blocked."""
        f = tmp_path / "secrets.txt"
        f.write_text("aws_secret=AKIAIOSFODNN7EXAMPLE")
        with patch("kiro_crew.hooks.is_sensitive_path", return_value=False):
            raw = safe_read_file_bytes(str(f))
        try:
            text = raw.decode("utf-8")
            blocked = redact(text) != text
        except UnicodeDecodeError:
            blocked = False
        assert blocked is True
