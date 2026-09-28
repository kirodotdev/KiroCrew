"""Unit tests for WeCom outbound media upload protocol (`wecom/media_upload.py`).

Pure logic: no WS, no event loop needed for prepare_upload / body building. The
frames these produce are pinned field-by-field against the documented request
shapes, because a wrong field name fails only against the live platform.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from kiro_crew.wecom.media_upload import (
    CHUNK_SIZE_BYTES,
    CMD_CHUNK,
    CMD_FINISH,
    CMD_INIT,
    MAX_CHUNKS,
    MediaUpload,
    WeComUploadError,
    prepare_upload,
)

_MAX = 20 * 1024 * 1024


class TestPrepareUpload:
    def test_small_file_single_chunk(self) -> None:
        data = b"hello world"
        up = prepare_upload(data, "file", "test.txt", max_bytes=_MAX)
        assert up.media_type == "file"
        assert up.filename == "test.txt"
        assert up.total_size == len(data)
        assert up.total_chunks == 1
        assert (
            up.md5
            == hashlib.md5(  # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5
                data, usedforsecurity=False
            ).hexdigest()
        )

    def test_chunk_boundary_exact(self) -> None:
        # Exactly one chunk worth: still one chunk, not two.
        data = b"a" * CHUNK_SIZE_BYTES
        up = prepare_upload(data, "file", "f.bin", max_bytes=_MAX)
        assert up.total_chunks == 1

    def test_chunk_boundary_plus_one(self) -> None:
        data = b"a" * (CHUNK_SIZE_BYTES + 1)
        up = prepare_upload(data, "file", "f.bin", max_bytes=_MAX)
        assert up.total_chunks == 2
        # Chunks reassemble to the original, in order.
        assert b"".join(up.chunks) == data

    def test_multi_chunk_reassembles(self) -> None:
        data = bytes(range(256)) * 4096  # 1 MiB of varied bytes
        up = prepare_upload(data, "file", "f.bin", max_bytes=_MAX)
        assert up.total_chunks == 2
        assert b"".join(up.chunks) == data
        assert all(len(c) <= CHUNK_SIZE_BYTES for c in up.chunks)

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(WeComUploadError, match="unsupported media type"):
            prepare_upload(b"x", "sticker", "f", max_bytes=_MAX)

    def test_rejects_empty(self) -> None:
        with pytest.raises(WeComUploadError, match="empty"):
            prepare_upload(b"", "file", "f", max_bytes=_MAX)

    def test_rejects_over_max_bytes(self) -> None:
        with pytest.raises(WeComUploadError, match="over the"):
            prepare_upload(b"a" * 11, "file", "f", max_bytes=10)

    def test_chunk_guard_is_shadowed_by_the_size_cap(self) -> None:
        # With per-type ceilings (all ≤ 20_000_000 bytes) below
        # CHUNK_SIZE_BYTES * MAX_CHUNKS (50 MiB), a body large enough to need more
        # than MAX_CHUNKS chunks is now refused by the SIZE cap first, so the
        # chunk-count guard is defensive-only. Passing an even larger max_bytes
        # cannot re-expose it because the per-type cap clamps the effective ceiling.
        big = b"a" * (CHUNK_SIZE_BYTES * (MAX_CHUNKS + 1))  # ~50 MiB
        with pytest.raises(WeComUploadError, match="over the"):
            prepare_upload(big, "file", "f", max_bytes=CHUNK_SIZE_BYTES * (MAX_CHUNKS + 2))

    @pytest.mark.parametrize("mtype", ["file", "image", "voice", "video"])
    def test_accepts_all_media_types(self, mtype: str) -> None:
        up = prepare_upload(b"payload", mtype, "f", max_bytes=_MAX)
        assert up.media_type == mtype

    def test_rejects_at_or_under_five_byte_minimum(self) -> None:
        # WeCom requires strictly > 5 bytes; 5 and below are refused locally.
        for n in (1, 5):
            with pytest.raises(WeComUploadError, match="minimum"):
                prepare_upload(b"a" * n, "file", "f", max_bytes=_MAX)
        # 6 bytes is accepted.
        assert prepare_upload(b"a" * 6, "file", "f", max_bytes=_MAX).total_size == 6

    def test_image_over_two_mb_refused_even_when_caller_ceiling_is_higher(self) -> None:
        # An image just over the 2 MB image cap is refused, though the caller's
        # absolute ceiling (20 MiB) would admit it — the per-type cap is stricter.
        data = b"a" * (2 * 1_000_000 + 1)
        with pytest.raises(WeComUploadError, match="image is .* over the 2000000-byte limit"):
            prepare_upload(data, "image", "big.png", max_bytes=_MAX)

    def test_image_at_two_mb_accepted(self) -> None:
        data = b"a" * (2 * 1_000_000)
        up = prepare_upload(data, "image", "ok.png", max_bytes=_MAX)
        assert up.total_size == 2 * 1_000_000

    def test_voice_over_two_mb_refused(self) -> None:
        data = b"a" * (2 * 1_000_000 + 1)
        with pytest.raises(WeComUploadError, match="voice is .* over the 2000000-byte limit"):
            prepare_upload(data, "voice", "clip.amr", max_bytes=_MAX)

    def test_file_between_two_and_twenty_mb_accepted(self) -> None:
        # A 5 MB file is fine (over the image/voice cap, under the file cap) —
        # proving the cap is per-type, not a flat 2 MB.
        data = b"a" * (5 * 1_000_000)
        up = prepare_upload(data, "file", "report.pdf", max_bytes=_MAX)
        assert up.total_size == 5 * 1_000_000

    def test_video_uses_file_ceiling(self) -> None:
        # Video inherits the 20 MB file ceiling: 5 MB is accepted, 20 MB + 1 is not.
        assert prepare_upload(b"a" * (5 * 1_000_000), "video", "v.mp4", max_bytes=_MAX)
        with pytest.raises(WeComUploadError, match="video is .* over the 20000000-byte limit"):
            prepare_upload(b"a" * (20 * 1_000_000 + 1), "video", "v.mp4", max_bytes=_MAX)

    def test_caller_ceiling_still_bounds_when_smaller_than_type_cap(self) -> None:
        # If the caller passes a ceiling smaller than the type cap, the caller's
        # wins (effective = min of the two).
        with pytest.raises(WeComUploadError, match="over the 10-byte limit"):
            prepare_upload(b"a" * 11, "file", "f", max_bytes=10)


class TestFrameBodies:
    def _upload(self) -> MediaUpload:
        return prepare_upload(b"a" * (CHUNK_SIZE_BYTES + 100), "file", "test.pdf", max_bytes=_MAX)

    def test_init_body_fields(self) -> None:
        up = self._upload()
        body = up.init_body()
        assert body == {
            "type": "file",
            "filename": "test.pdf",
            "total_size": CHUNK_SIZE_BYTES + 100,
            "total_chunks": 2,
            "md5": up.md5,
        }

    def test_chunk_body_fields_and_base64(self) -> None:
        up = self._upload()
        body = up.chunk_body("UP123", 0)
        assert body["upload_id"] == "UP123"
        assert body["chunk_index"] == 0
        # base64_data decodes back to the raw chunk bytes.
        decoded = base64.b64decode(body["base64_data"])
        assert decoded == up.chunks[0]

    def test_chunk_index_is_zero_based_and_sequential(self) -> None:
        up = self._upload()
        assert up.chunk_body("U", 0)["chunk_index"] == 0
        assert up.chunk_body("U", 1)["chunk_index"] == 1

    def test_finish_body_fields(self) -> None:
        up = self._upload()
        assert up.finish_body("UP123") == {"upload_id": "UP123"}

    def test_command_constants(self) -> None:
        assert CMD_INIT == "aibot_upload_media_init"
        assert CMD_CHUNK == "aibot_upload_media_chunk"
        assert CMD_FINISH == "aibot_upload_media_finish"


class TestWeComMediaTypeMapping:
    """Round-trip the documented _WECOM_TYPE_BY_EXT allowlist and the default."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("photo.png", "image"),
            ("photo.JPG", "image"),  # case-insensitive
            ("clip.jpeg", "image"),
            # gif/bmp/webp are allowlisted but WeCom's image type is JPG/PNG only,
            # so they send as a generic file rather than a refused image.
            ("anim.gif", "file"),
            ("shot.bmp", "file"),
            ("pic.webp", "file"),
            # .amr voice is unreachable — audio/amr is not in BINARY_MIME_ALLOWLIST,
            # so the upload gate refuses it before the type ever matters; unmapped.
            ("hello.amr", "file"),
            ("movie.mp4", "video"),
            ("movie.webm", "video"),
            # .mov/.m4v are not allowlisted (video/quicktime, video/x-m4v), so they
            # never reach the type map either -> generic file.
            ("movie.mov", "file"),
            ("movie.m4v", "file"),
            ("report.pdf", "file"),  # unmapped -> generic file
            ("deck.pptx", "file"),
            ("noext", "file"),
            ("", "file"),
        ],
    )
    def test_media_type_by_extension(self, filename: str, expected: str) -> None:
        from kiro_crew.wecom.transport import wecom_media_type_for

        assert wecom_media_type_for(filename) == expected


class TestBasename:
    """_basename must strip both separators so a Windows path never leaks."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("C:\\Users\\alice\\report.pdf", "report.pdf"),  # Windows absolute
            ("/home/alice/docs/report.pdf", "report.pdf"),  # POSIX absolute
            ("report.pdf", "report.pdf"),  # bare name
            ("a/b\\c.png", "c.png"),  # mixed separators
            ("C:\\dir\\", ""),  # trailing sep -> empty leaf
        ],
    )
    def test_basename_strips_both_separators(self, path: str, expected: str) -> None:
        from kiro_crew.wecom.transport import _basename

        assert _basename(path) == expected
