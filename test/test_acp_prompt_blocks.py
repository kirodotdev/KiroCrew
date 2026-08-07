"""ACP prompt-block construction and the image capability gate.

Regression coverage for the defect where EVERY channel shipped a filesystem
path as prose: ``AcpSessionHandle.prompt`` hardcoded a single text block, while
the only image encoder lived on ``AcpClient`` -- the path ``AcpProvider.start``
replaces. An image therefore never reached the model as vision input.
"""

from __future__ import annotations

import base64
import io
import os
import random

import pytest

from kiro_crew.acp import prompt_blocks
from kiro_crew.acp.prompt_blocks import (
    _POSIX_PATH_RE,
    IMAGE_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE_PX,
    build_prompt_blocks,
)

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


class TestBuildPromptBlocks:
    def test_always_returns_at_least_a_text_block(self):
        blocks = build_prompt_blocks("just words")
        assert blocks == [{"type": "text", "text": "just words"}]

    def test_image_path_becomes_an_image_block(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p} please")

        assert [b["type"] for b in blocks] == ["text", "image"]
        # Text block leads, so the caller can pass this straight to session/prompt.
        assert blocks[0]["text"] == f"look at [image: {p.name}] please"
        assert blocks[1]["mimeType"] == "image/png"
        # The wire carries the BYTES, not the path.
        assert base64.b64decode(blocks[1]["data"]) == _PNG
        assert str(p) not in blocks[1].get("data", "")

    def test_capability_gate_keeps_path_as_text(self, tmp_path):
        """No advertised image capability -> no image block, path left intact.

        Dropping the reference would lose the attachment entirely; leaving the
        path lets a tool-capable agent still open the file.
        """
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p}", allow_image=False)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_oversized_image_falls_back_to_path(self, tmp_path):
        """Size is checked BEFORE base64: encoding inflates 4/3 and the whole
        request is one newline-delimited JSON frame."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}", max_image_bytes=1)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_missing_and_unreadable_files_are_skipped(self, tmp_path):
        blocks = build_prompt_blocks("/definitely/not/here.png")
        assert [b["type"] for b in blocks] == ["text"]

    def test_directory_with_image_suffix_is_not_read(self, tmp_path):
        d = tmp_path / "weird.png"
        d.mkdir()
        blocks = build_prompt_blocks(f"see {d}")
        assert [b["type"] for b in blocks] == ["text"]

    def test_multiple_images_each_get_a_block(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        blocks = build_prompt_blocks(f"{a} and {b}")

        assert [x["type"] for x in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == "[image: a.png] and [image: b.png]"

    def test_same_path_twice_is_encoded_once(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"{p} then {p} again")
        # One image block, and both textual occurrences are rewritten.
        assert [x["type"] for x in blocks] == ["text", "image"]
        assert str(p) not in blocks[0]["text"]

    @pytest.mark.parametrize("suffix,mime", sorted(IMAGE_MEDIA_TYPES.items()))
    def test_every_supported_suffix_maps_to_its_mime(self, tmp_path, suffix, mime):
        p = tmp_path / f"img{suffix}"
        p.write_bytes(_PNG)
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == mime

    def test_svg_is_not_inlined(self, tmp_path):
        """SVG is scriptable XML, not a raster image. The direct client listed it
        in its media map while its regex omitted it, so the mapping was already
        unreachable -- keep it excluded deliberately."""
        p = tmp_path / "vector.svg"
        p.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        blocks = build_prompt_blocks(f"see {p}")

        assert [b["type"] for b in blocks] == ["text"]
        assert ".svg" not in IMAGE_MEDIA_TYPES

    def test_bare_filename_is_not_treated_as_a_path(self, tmp_path):
        """Only absolute paths are candidates, so prose mentioning a filename
        does not trigger a filesystem probe."""
        blocks = build_prompt_blocks("the file shot.png is attached")
        assert [b["type"] for b in blocks] == ["text"]
        assert blocks[0]["text"] == "the file shot.png is attached"

    def test_default_cap_is_ten_mib(self):
        assert MAX_IMAGE_BYTES == 10 * 1024 * 1024


class TestSensitivePathGate:
    """Image bytes must travel through the centralized sensitive-path gate.

    The gate itself (``hooks.safe_read_file_bytes``: realpath canonicalization,
    ``is_sensitive_path``, ``O_NOFOLLOW``) has its own tests. What matters here
    is that this builder ROUTES through it and honours a refusal -- paths
    reaching it are scraped from message text and so are user-influenced.
    """

    def test_refused_read_is_not_inlined(self, tmp_path, monkeypatch):
        p = _png(tmp_path)
        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", lambda raw: None)

        blocks = build_prompt_blocks(f"look at {p}")

        # No image block -- and the path STAYS in the text rather than being
        # silently deleted, so a tool-capable agent can still choose to open it.
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_gate_receives_the_path(self, tmp_path, monkeypatch):
        p = _png(tmp_path)
        seen: list[str] = []

        def _spy(raw: str) -> bytes:
            seen.append(raw)
            return _PNG

        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", _spy)
        build_prompt_blocks(f"look at {p}")
        assert seen == [str(p)]

    def test_encoded_bytes_come_from_the_gate(self, tmp_path, monkeypatch):
        """The wire payload is the gate's output, not a second unguarded read."""
        pil = pytest.importorskip("PIL.Image")
        p = _png(tmp_path)  # on-disk content is the 1x1 _PNG
        # The gate returns DIFFERENT (valid, within-cap) bytes; prove the wire
        # carries THOSE, not a re-read of the file. A non-image sentinel would be
        # dropped now: undecodable bytes fail closed (see TestImageDownscale).
        buf = io.BytesIO()
        pil.new("RGB", (2, 2), (1, 2, 3)).save(buf, format="PNG")
        gate_bytes = buf.getvalue()
        assert gate_bytes != p.read_bytes()
        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", lambda raw: gate_bytes)

        blocks = build_prompt_blocks(f"look at {p}")

        assert base64.b64decode(blocks[1]["data"]) == gate_bytes


class TestPlatformPathGrammar:
    """The path grammar is host-specific on purpose."""

    def test_posix_pattern_matches_posix_paths(self):
        assert prompt_blocks._POSIX_PATH_RE.search("/tmp/a.png") is not None

    def test_posix_pattern_ignores_windows_shapes(self):
        r"""Prose like ``C:\shots\logo.png`` must NOT be a candidate on POSIX.

        Backslash and ``:`` are legal POSIX filename characters, so one merged
        pattern would make a merely-MENTIONED Windows path matchable -- and a
        file with that literal name can exist in the CWD, which would inline
        something the user only talked about.
        """
        assert prompt_blocks._POSIX_PATH_RE.search(r"C:\shots\logo.png") is None
        assert prompt_blocks._POSIX_PATH_RE.search(r"\\host\share\logo.png") is None

    @pytest.mark.parametrize(
        "text",
        [
            r"C:\Users\alice\AppData\Local\Temp\tmpabc.png",
            r"C:/Users/alice/AppData/Local/Temp/tmpabc.png",
            r"\\fileserver\team\diagram.jpg",
        ],
    )
    def test_windows_pattern_matches_native_absolute_paths(self, text):
        """The shapes the gateway actually produces on Windows."""
        assert prompt_blocks._WINDOWS_PATH_RE.search(text) is not None

    def test_windows_pattern_requires_an_absolute_path(self):
        assert prompt_blocks._WINDOWS_PATH_RE.search(r"shots\logo.png") is None

    def test_active_pattern_follows_the_host(self):
        expected = (
            prompt_blocks._WINDOWS_PATH_RE if os.name == "nt" else prompt_blocks._POSIX_PATH_RE
        )
        assert prompt_blocks._PATH_RE is expected

    def test_natively_produced_path_is_inlined_on_this_host(self, tmp_path):
        """End-to-end guard against the gap Windows CI exposed.

        ``tmp_path`` yields backslash paths on Windows, which the POSIX-only
        grammar could not match -- so every image silently stayed prose on a
        supported, CI-tested platform.
        """
        p = _png(tmp_path, "native.png")
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]


class TestPathsAdjacentToUrls:
    r"""A URL in the message must not swallow the appended image path.

    ``slack/events.py`` emits ``"<user text>\n<image path>"``. With ``\s`` in the
    character class (which matches ``\n``) the leading URL chained across the
    newline into the path, so ``see https://x.com/d\n/tmp/a.png`` matched as the
    single nonexistent path ``//x.com/d\n/tmp/a.png`` -- meaning ANY Slack
    message containing a link silently lost its image, and the temp file was
    then deleted at end of turn.
    """

    def test_url_then_newline_then_path(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs\n{p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_url_then_space_then_path(self, tmp_path):
        """Same defect on one line -- the newline-only fix does not cover this."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_url_ending_in_an_image_suffix_is_not_a_path(self):
        """A remote URL is not a local file and must not even be a candidate."""
        assert _POSIX_PATH_RE.search("see https://example.com/logo.png") is None

    def test_multiple_urls_do_not_break_a_trailing_path(self, tmp_path):
        p = _png(tmp_path)
        text = f"a https://x.com/1 b http://y.com/2/z\n{p}"
        blocks = build_prompt_blocks(text)
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_two_images_after_a_url_both_survive(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        blocks = build_prompt_blocks(f"ref https://x.com/d\n{a}\n{b}")
        assert [x["type"] for x in blocks] == ["text", "image", "image"]

    def test_newline_is_not_part_of_a_path(self):
        r"""``\n`` must never be inside a captured path."""
        m = _POSIX_PATH_RE.search("/tmp/one\n/tmp/two.png")
        assert m is not None and "\n" not in m.group(1)

    def test_filename_with_spaces_still_matches(self, tmp_path):
        """Horizontal whitespace stays allowed -- this is why `\\s` was used."""
        p = _png(tmp_path, "my shot.png")
        blocks = build_prompt_blocks(f"look at {p}")
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_path_inside_markdown_image_syntax(self, tmp_path):
        """The dashboard emits `![image](<path>)`."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"![image]({p})")
        assert [b["type"] for b in blocks] == ["text", "image"]


def _sized_image(tmp_path, w, h, fmt="PNG", name=None):
    """Write a solid-colour raster of exactly ``w``x``h`` and return its path.

    Solid colour keeps PNG/JPEG/WEBP encodings tiny so they clear the byte gate
    and the downscale (not the byte cap) is what the test exercises.
    """
    pil = pytest.importorskip("PIL.Image")
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp", "GIF": "gif"}[fmt]
    p = tmp_path / (name or f"big.{ext}")
    pil.new("RGB", (w, h), (123, 200, 60)).save(p, format=fmt)
    return p


def _decoded_size(block):
    pil = pytest.importorskip("PIL.Image")
    with pil.open(io.BytesIO(base64.b64decode(block["data"]))) as im:
        return im.size


class TestImageDownscale:
    """The server-side dimension backstop: no image reaches kiro-cli over the
    Anthropic many-image cap, whatever channel (or skipped client resize) it
    came from."""

    def test_default_cap_is_2000(self):
        assert MAX_IMAGE_EDGE_PX == 2000

    def test_oversized_image_is_downscaled(self, tmp_path):
        p = _sized_image(tmp_path, 4000, 3000)
        blocks = build_prompt_blocks(f"see {p}")
        w, h = _decoded_size(blocks[1])
        # Longest edge capped, aspect preserved (4000x3000 -> 2000x1500).
        assert max(w, h) <= MAX_IMAGE_EDGE_PX
        assert (w, h) == (2000, 1500)

    def test_portrait_image_downscaled_on_its_long_edge(self, tmp_path):
        p = _sized_image(tmp_path, 1000, 4000)
        blocks = build_prompt_blocks(f"see {p}")
        assert _decoded_size(blocks[1]) == (500, 2000)

    def test_image_within_cap_is_byte_identical(self, tmp_path):
        """At/under the cap the original bytes ride through untouched -- no
        needless re-encode (which would recompress and drift quality)."""
        p = _sized_image(tmp_path, 1600, 1200)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}")
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_custom_edge_param_is_honoured(self, tmp_path):
        p = _sized_image(tmp_path, 40, 20)
        blocks = build_prompt_blocks(f"see {p}", max_image_edge=10)
        assert _decoded_size(blocks[1]) == (10, 5)

    def test_edge_zero_disables_downscale(self, tmp_path):
        """The escape hatch: a non-positive cap leaves bytes exactly as-is."""
        p = _sized_image(tmp_path, 4000, 10)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", max_image_edge=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_oversized_jpeg_keeps_jpeg_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_gif_becomes_png_still(self, tmp_path):
        """GIF re-encodes to a PNG first frame: the vision model reads frame 0
        only, and palette rescaling is lossy, so a lossless still is faithful."""
        p = _sized_image(tmp_path, 3000, 100, fmt="GIF")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/png"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_bmp_is_capped(self, tmp_path):
        # A thin BMP stays under the 10 MB byte gate yet over the edge cap, so
        # the downscale (not the byte gate) is what fires.
        p = _sized_image(tmp_path, 2400, 80, fmt="BMP")
        blocks = build_prompt_blocks(f"see {p}")
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_webp_keeps_webp_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="WEBP")
        blocks = build_prompt_blocks(f"see {p}")
        assert blocks[1]["mimeType"] == "image/webp"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_undecodable_oversized_image_is_not_inlined(self, tmp_path, monkeypatch):
        """Fail CLOSED: an oversized raster we cannot shrink (here a Pillow
        decompression-bomb rejection) must NOT reach the model as the original
        >2000px payload -- that is exactly what poisons the session. The path is
        left as text instead."""
        pil = pytest.importorskip("PIL.Image")
        p = _sized_image(tmp_path, 2001, 2001)  # over the edge cap
        # Force a decompression-bomb ERROR on decode/resize (pixels > 2 x limit).
        monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", 1_000_000)
        blocks = build_prompt_blocks(f"see {p}")
        assert [b["type"] for b in blocks] == ["text"]  # no image block
        assert str(p) in blocks[0]["text"]  # path preserved for a tool-capable agent

    def test_exif_orientation_is_baked_on_downscale(self, tmp_path):
        """A re-encode drops the EXIF orientation tag, so orientation must be
        baked into the pixels first or the model sees a rotated photo."""
        pil = pytest.importorskip("PIL.Image")
        p = tmp_path / "rot.jpg"
        img = pil.new("RGB", (3000, 1000), (10, 20, 30))
        exif = img.getexif()
        exif[0x0112] = 6  # Orientation = rotate 90 CW -> displayed as 1000x3000
        img.save(p, format="JPEG", exif=exif)
        blocks = build_prompt_blocks(f"see {p}")
        with pil.open(io.BytesIO(base64.b64decode(blocks[1]["data"]))) as out:
            w, h = out.size
            assert 0x0112 not in out.getexif()  # tag baked away, not carried
            assert h > w  # rotation applied to the pixels -> portrait
            assert max(w, h) <= MAX_IMAGE_EDGE_PX


def _noise_image(tmp_path, w, h, name="noise.png", fmt="PNG"):
    """An image that resists compression, so its encoded size tracks pixel count."""
    pil = pytest.importorskip("PIL.Image")
    rnd = random.Random(1234)
    img = pil.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    p = tmp_path / name
    img.save(p, format=fmt)
    return p


class TestImageEncodedBudget:
    """The per-image ENCODED byte ceiling.

    The dimension cap alone is not enough: Bedrock rejects a single image over
    5 MiB base64, and a raster can sit well inside 2000px while encoding past
    that. A rejected image is replayed from history every later turn, so letting
    one through wedges the whole session.
    """

    def test_default_cap_is_5_mib(self):
        assert prompt_blocks.MAX_IMAGE_B64_BYTES == 5 * 1024 * 1024

    def test_b64_len_matches_real_encoding(self):
        for n in (0, 1, 2, 3, 4, 100, 1023, 4096):
            assert prompt_blocks._b64_len(n) == len(base64.b64encode(b"x" * n))

    def test_image_inside_dimension_cap_but_over_budget_is_shrunk(self, tmp_path):
        """The exact production defect: dimensions are already legal, so the
        dimension pass is a no-op, yet the payload still exceeds the wire limit.
        """
        p = _noise_image(tmp_path, 900, 900)
        budget = len(base64.b64encode(p.read_bytes())) // 3
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=budget)
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert len(blocks[1]["data"]) <= budget
        # Shrunk, not passed through: the bug was inlining the original here.
        assert base64.b64decode(blocks[1]["data"]) != p.read_bytes()
        assert max(_decoded_size(blocks[1])) < 900

    def test_image_within_budget_is_byte_identical(self, tmp_path):
        p = _sized_image(tmp_path, 100, 80)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}")
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_unshrinkable_image_falls_back_to_a_path(self, tmp_path):
        """Fail CLOSED: a budget no rendition can meet must leave the path as
        text rather than inline a payload the backend will reject forever."""
        p = _noise_image(tmp_path, 400, 400)
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=8)
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_zero_budget_disables_the_check(self, tmp_path):
        p = _noise_image(tmp_path, 120, 120)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_budget_applies_after_the_dimension_cap(self, tmp_path):
        """Both caps hold at once -- shrinking for bytes must not reintroduce an
        over-dimension rendition, and vice versa."""
        p = _noise_image(tmp_path, 2400, 2400, name="big.jpg", fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=400_000)
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX
        assert len(blocks[1]["data"]) <= 400_000

    def test_shrink_floor_is_respected(self, tmp_path):
        """The loop never grinds an image below the usable-accuracy floor; it
        gives up and hands back a path instead."""
        p = _noise_image(tmp_path, 1000, 1000)
        blocks = build_prompt_blocks(f"see {p}", max_image_b64_bytes=64)
        assert [b["type"] for b in blocks] == ["text"]
