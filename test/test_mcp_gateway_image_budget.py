"""Tests for the MCP gateway inline-image budget.

Covers the tool-result seam of the inline-image budget (the prompt path is
covered by ``test_acp_prompt_blocks``):

(a) an oversized tool-result image is downscaled in place
(b) an already-compliant image rides through byte-identical (whole line)
(c) an image whose compliance cannot be established is replaced by a text
    block (fail closed: undecodable bytes, invalid base64, Pillow bomb
    refusal, source over the decode-pixel ceiling, Pillow absent)
(d) frames that are not an image-bearing tool result pass through untouched
(e) the pre-filter is escape-aware: its negative answer is provable
(f) the public ``downscale_image_block`` entry point honours the budget
"""

from __future__ import annotations

import base64
import io
import json
import random

import pytest

from kiro_crew import imaging
from kiro_crew.imaging import (
    MAX_IMAGE_B64_BYTES,
    MAX_IMAGE_EDGE_PX,
    MIN_IMAGE_EDGE_PX,
    downscale_image_block,
)
from kiro_crew.mcp_gateway import image_budget as image_budget_mod
from kiro_crew.mcp_gateway.image_budget import (
    line_may_carry_image_block,
    parse_image_bearing_frame,
    rewrite_image_frame,
)


def enforce_image_budget(line: bytes, server_name: str) -> bytes:
    """Compose the two stages exactly like the pump does (parse-confirm,
    then rewrite only image-bearing frames), so every assertion below runs
    against the same path production runs."""
    msg = parse_image_bearing_frame(line)
    if msg is None:
        return line
    return rewrite_image_frame(msg, line, server_name)


def _png_bytes(w: int, h: int) -> bytes:
    """A solid-colour PNG of exactly ``w``x``h`` (tiny encoding, so the
    dimension cap -- not the byte ceiling -- is what a test exercises)."""
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (w, h), (123, 200, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _noise_png_bytes(w: int, h: int) -> bytes:
    """A PNG that resists compression, so encoded size tracks pixel count."""
    pil = pytest.importorskip("PIL.Image")
    rnd = random.Random(1234)
    img = pil.new("RGB", (w, h))
    img.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _image_block(raw: bytes, mime: str = "image/png") -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": mime}


def _result_line(content: list, msg_id: str = "gw-1") -> bytes:
    msg = {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"


def _decoded_size(block: dict) -> tuple[int, int]:
    pil = pytest.importorskip("PIL.Image")
    with pil.open(io.BytesIO(base64.b64decode(block["data"]))) as im:
        return im.size


def _content_of(line: bytes) -> list:
    return json.loads(line.decode("utf-8"))["result"]["content"]


# --- (a) oversized image is downscaled in place ---


class TestOversizedImageDownscaled:
    def test_dimension_cap_applied(self):
        line = _result_line([_image_block(_png_bytes(4000, 3000))])
        out = enforce_image_budget(line, "test-server")
        assert out != line
        (block,) = _content_of(out)
        assert block["type"] == "image"
        assert block["mimeType"] == "image/png"
        # Longest edge capped, aspect preserved (4000x3000 -> 2000x1500).
        assert _decoded_size(block) == (2000, 1500)

    def test_frame_stays_routable(self):
        """The rewritten frame keeps its id and newline terminator, so the
        pump's routing (and the stub's line framing) still work on it."""
        line = _result_line([_image_block(_png_bytes(4000, 1000))], msg_id="gw-42")
        out = enforce_image_budget(line, "test-server")
        assert out.endswith(b"\n")
        assert json.loads(out.decode("utf-8"))["id"] == "gw-42"

    def test_missing_mime_type_defaults_to_png(self):
        block = _image_block(_png_bytes(4000, 3000))
        del block["mimeType"]
        out = enforce_image_budget(_result_line([block]), "test-server")
        (rewritten,) = _content_of(out)
        assert rewritten["type"] == "image"
        assert rewritten["mimeType"] == "image/png"
        assert max(_decoded_size(rewritten)) <= MAX_IMAGE_EDGE_PX

    def test_only_oversized_blocks_rewritten_in_mixed_content(self):
        small = _image_block(_png_bytes(800, 600))
        text = {"type": "text", "text": "screenshot attached"}
        line = _result_line([text, small, _image_block(_png_bytes(4000, 3000))])
        out = enforce_image_budget(line, "test-server")
        blocks = _content_of(out)
        assert blocks[0] == text
        assert blocks[1] == small  # compliant image untouched
        assert max(_decoded_size(blocks[2])) <= MAX_IMAGE_EDGE_PX

    def test_escaped_type_field_is_still_rewritten(self):
        """A block whose ``type`` is written with a JSON \\u escape parses to
        "image" all the same; the rewrite must treat it identically."""
        payload = base64.b64encode(_png_bytes(4000, 3000)).decode("ascii")
        line = (
            '{"jsonrpc":"2.0","id":"gw-1","result":{"content":[{"type":"\\u0069mage",'
            '"data":"' + payload + '","mimeType":"image/png"}]}}'
        ).encode("utf-8") + b"\n"
        out = enforce_image_budget(line, "test-server")
        (block,) = _content_of(out)
        assert block["type"] == "image"
        assert max(_decoded_size(block)) <= MAX_IMAGE_EDGE_PX

    def test_whitespace_wrapped_base64_is_accepted(self):
        """base64.encodebytes-style producers wrap at 76 columns; those images
        relay fine today, so the budget must not regress them into omission."""
        raw = _png_bytes(4000, 3000)
        wrapped = base64.encodebytes(raw).decode("ascii")  # newline-wrapped
        assert "\n" in wrapped
        block = {"type": "image", "data": wrapped, "mimeType": "image/png"}
        out = enforce_image_budget(_result_line([block]), "test-server")
        (rewritten,) = _content_of(out)
        assert rewritten["type"] == "image"
        assert max(_decoded_size(rewritten)) <= MAX_IMAGE_EDGE_PX

    def test_unknown_source_mime_relabels_to_the_written_format(self):
        """An oversized image whose mime has no dedicated save format (TIFF)
        re-encodes as PNG -- the emitted mimeType must say so, not repeat the
        source mime on bytes that are no longer that format."""
        pil = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        pil.new("RGB", (4000, 3000), (10, 20, 30)).save(buf, format="TIFF")
        block = {
            "type": "image",
            "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            "mimeType": "image/tiff",
        }
        out = enforce_image_budget(_result_line([block]), "test-server")
        (rewritten,) = _content_of(out)
        assert rewritten["type"] == "image"
        assert rewritten["mimeType"] == "image/png"
        assert max(_decoded_size(rewritten)) <= MAX_IMAGE_EDGE_PX

    def test_lying_mime_label_is_corrected_on_passthrough(self):
        """A compliant image whose claimed mimeType disagrees with its bytes
        (PNG data labeled image/jpeg) keeps its bytes but gets the label the
        header actually detected -- same mislabel class as the re-encode."""
        raw = _png_bytes(800, 600)
        block = {
            "type": "image",
            "data": base64.b64encode(raw).decode("ascii"),
            "mimeType": "image/jpeg",
        }
        out = enforce_image_budget(_result_line([block]), "test-server")
        (rewritten,) = _content_of(out)
        assert rewritten["type"] == "image"
        assert rewritten["mimeType"] == "image/png"
        assert base64.b64decode(rewritten["data"]) == raw  # bytes untouched


# --- (b) compliant image rides through byte-identical ---


class TestCompliantPassthrough:
    def test_small_image_line_is_returned_unchanged(self):
        """No rewrite means the ORIGINAL line object comes back: no
        re-serialization that could reorder keys or change whitespace."""
        line = _result_line([_image_block(_png_bytes(800, 600))])
        assert enforce_image_budget(line, "test-server") is line

    def test_small_whitespace_wrapped_image_is_canonicalized(self):
        """A compliant image with wrapped base64 keeps its bytes but is
        re-emitted in canonical encoding: the byte ceiling is measured on the
        canonical form, so canonical is what must go on the wire."""
        raw = _png_bytes(800, 600)
        block = {
            "type": "image",
            "data": base64.encodebytes(raw).decode("ascii"),
            "mimeType": "image/png",
        }
        out = enforce_image_budget(_result_line([block]), "test-server")
        (rewritten,) = _content_of(out)
        assert rewritten["type"] == "image"
        assert rewritten["data"] == base64.b64encode(raw).decode("ascii")
        assert base64.b64decode(rewritten["data"]) == raw  # bytes untouched


# --- (c) fail closed: unverifiable image becomes a text block ---


class TestFailClosed:
    def test_undecodable_image_bytes(self):
        """Valid base64 of non-image bytes: the budget cannot be verified, so
        the block must NOT be forwarded."""
        block = {
            "type": "image",
            "data": base64.b64encode(b"not an image at all").decode("ascii"),
            "mimeType": "image/png",
        }
        out = enforce_image_budget(_result_line([block]), "test-server")
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_invalid_base64(self):
        block = {"type": "image", "data": "!!!not-base64!!!", "mimeType": "image/png"}
        out = enforce_image_budget(_result_line([block]), "test-server")
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_empty_data(self):
        block = {"type": "image", "data": "", "mimeType": "image/png"}
        out = enforce_image_budget(_result_line([block]), "test-server")
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"

    def test_truncated_image_with_readable_header(self):
        """A truncated file whose header still reads (dimensions available,
        data chopped) must fail closed: byte-identical pass-through of a
        corrupt block is the same permanent history wedge as an oversized
        one."""
        raw = _png_bytes(800, 600)
        truncated = raw[: len(raw) // 2]  # header intact, IDAT/IEND chopped
        block = {
            "type": "image",
            "data": base64.b64encode(truncated).decode("ascii"),
            "mimeType": "image/png",
        }
        out = enforce_image_budget(_result_line([block]), "test-server")
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_pillow_bomb_refusal(self, monkeypatch):
        """An oversized raster Pillow refuses to decode (decompression-bomb
        guard) has no compliant rendition -- text fallback, never the original
        >2000px payload."""
        pil = pytest.importorskip("PIL.Image")
        raw = _png_bytes(2001, 2001)  # over the edge cap
        # Force a decompression-bomb ERROR on decode (pixels > 2 x limit) while
        # keeping the header read (and the source-pixel ceiling) permissive.
        monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", 1_000_000)
        monkeypatch.setattr(imaging, "MAX_IMAGE_SOURCE_PIXELS", 10_000_000)
        out = enforce_image_budget(_result_line([_image_block(raw)]), "test-server")
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_source_pixel_ceiling_refused_before_decode(self, monkeypatch):
        """A source over the decode-pixel ceiling is refused on the header
        read alone -- the adversarially-compressed huge-area raster must never
        reach a full decode."""
        monkeypatch.setattr(imaging, "MAX_IMAGE_SOURCE_PIXELS", 1_000)
        out = enforce_image_budget(
            _result_line([_image_block(_png_bytes(100, 100))]), "test-server"
        )  # 10,000 px > patched 1,000 px ceiling
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_pillow_absent_fails_closed(self, monkeypatch):
        """Without Pillow the dimensions are unverifiable; the gateway seam
        must omit rather than forward a possibly-wedging image."""
        monkeypatch.setattr(image_budget_mod, "pil_available", lambda: False)
        out = enforce_image_budget(
            _result_line([_image_block(_png_bytes(10, 10))]), "test-server"
        )
        (replaced,) = _content_of(out)
        assert replaced["type"] == "text"
        assert "image omitted" in replaced["text"]

    def test_blocks_past_the_frame_pixel_budget_are_dropped_undecoded(self, monkeypatch):
        """A frame asking for more cumulative decode work than the budget
        must not monopolize the image pool: blocks past it become text
        WITHOUT ever reaching the decoder."""
        decodes = []

        def _counting_downscale(raw, mime, **kwargs):
            decodes.append(1)
            return raw, mime

        monkeypatch.setattr(image_budget_mod, "downscale_image_block", _counting_downscale)
        # Each 100x100 block charges 10,000 px; budget admits exactly two.
        monkeypatch.setattr(image_budget_mod, "MAX_FRAME_SOURCE_PIXELS", 25_000)
        line = _result_line([_image_block(_png_bytes(100, 100)) for _ in range(5)])
        out = enforce_image_budget(line, "test-server")
        blocks = _content_of(out)
        assert len(blocks) == 5
        assert len(decodes) == 2  # budget spent after two; excess never decoded
        assert all(b["type"] == "image" for b in blocks[:2])
        for extra in blocks[2:]:
            assert extra["type"] == "text"
            assert "more image processing" in extra["text"]

    def test_many_tiny_images_all_pass_within_the_budget(self):
        """The budget is pixels, not a block count: a legitimate many-image
        result of small renders passes through whole."""
        line = _result_line([_image_block(_png_bytes(20, 20)) for _ in range(12)])
        assert enforce_image_budget(line, "test-server") is line

    def test_rejected_oversize_images_do_not_starve_later_valid_ones(self, monkeypatch):
        """A block the per-image ceiling refuses charges nothing (it costs a
        header read, never a decode), so a run of rejected oversize images
        cannot exhaust the frame budget ahead of a legitimate image."""
        monkeypatch.setattr(imaging, "MAX_IMAGE_SOURCE_PIXELS", 5_000)
        monkeypatch.setattr(image_budget_mod, "MAX_FRAME_SOURCE_PIXELS", 20_000)
        oversize = _image_block(_png_bytes(100, 100))  # 10,000 px > per-image 5,000
        valid = _image_block(_png_bytes(60, 60))  # 3,600 px, within everything
        line = _result_line([oversize, oversize, oversize, valid])
        out = enforce_image_budget(line, "test-server")
        blocks = _content_of(out)
        # The three oversize blocks fail closed via the per-image ceiling...
        for b in blocks[:3]:
            assert b["type"] == "text"
            assert "image omitted" in b["text"]
        # ...and the trailing valid image still passes, un-starved.
        assert blocks[3]["type"] == "image"

    def test_mass_rejected_blocks_collapse_into_one_summary(self):
        """One-for-one omission notes amplify a many-block frame (each note
        is ~10x an empty image block), so notes past the ceiling collapse
        into a single counted summary: output stays bounded near input size."""
        from kiro_crew.mcp_gateway.image_budget import _MAX_OMISSION_NOTES

        n_blocks = 500
        line = _result_line(
            [{"type": "image", "data": "", "mimeType": "image/png"}] * n_blocks
        )
        out = enforce_image_budget(line, "test-server")
        assert len(out) < 2 * len(line)  # no amplification blowup
        blocks = _content_of(out)
        notes = [b for b in blocks if b["type"] == "text" and "image omitted" in b["text"]]
        summaries = [b for b in blocks if "additional image blocks" in b.get("text", "")]
        assert len(notes) == _MAX_OMISSION_NOTES
        assert len(summaries) == 1
        assert str(n_blocks - _MAX_OMISSION_NOTES) in summaries[0]["text"]


# --- (d) non-matching frames pass through untouched ---


class TestNonMatchingPassthrough:
    def test_notification_with_image_word(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/message",
               "params": {"data": 'the word "image" appears here'}}
        line = json.dumps(msg).encode("utf-8") + b"\n"
        assert enforce_image_budget(line, "test-server") is line

    def test_text_only_tool_result(self):
        line = _result_line([{"type": "text", "text": "an image was saved to /tmp/x.png"}])
        assert enforce_image_budget(line, "test-server") is line

    def test_result_without_content_list(self):
        msg = {"jsonrpc": "2.0", "id": "gw-1", "result": {"ok": True}}
        line = json.dumps(msg).encode("utf-8") + b"\n"
        assert enforce_image_budget(line, "test-server") is line

    def test_non_json_line(self):
        line = b'not json "image" at all\n'
        assert enforce_image_budget(line, "test-server") is line

    def test_non_dict_content_items_tolerated(self):
        line = _result_line(["bare-string", 42, {"type": "text", "text": "ok"}])
        assert enforce_image_budget(line, "test-server") is line


# --- (e) the pre-filter's negative answer is provable ---


class TestLineMayCarryImageBlock:
    @pytest.mark.parametrize("separators", [(",", ":"), (", ", ": ")])
    def test_plain_image_block_matches(self, separators):
        msg = {"result": {"content": [{"type": "image", "data": "AA==",
                                       "mimeType": "image/png"}]}}
        line = json.dumps(msg, separators=separators).encode("utf-8")
        assert line_may_carry_image_block(line)

    def test_escaped_type_matches(self):
        """Evading the literal probe REQUIRES a \\u escape (the only JSON
        escape that yields a letter), and the escape itself is what the second
        probe half matches -- so the filter cannot be evaded."""
        line = b'{"result":{"content":[{"type":"\\u0069mage","data":"AA=="}]}}'
        assert b'"image"' not in line  # the literal probe alone would miss it
        assert line_may_carry_image_block(line)

    def test_plain_ascii_text_without_image_skips(self):
        line = b'{"result":{"content":[{"type":"text","text":"all done"}]}}'
        assert not line_may_carry_image_block(line)


# --- (f) the public entry point ---


class TestDownscaleImageBlock:
    def test_within_budget_is_byte_identical(self):
        raw = _png_bytes(800, 600)
        assert downscale_image_block(raw, "image/png") == (raw, "image/png")

    def test_within_cap_gif_keeps_its_gif_label(self):
        """The GIF->PNG mime swap belongs to the RE-ENCODE only; a compliant
        GIF passes through byte-identical with its own label."""
        pil = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        pil.new("P", (40, 40)).save(buf, format="GIF")
        raw = buf.getvalue()
        assert downscale_image_block(raw, "image/gif") == (raw, "image/gif")

    def test_within_cap_unsupported_format_is_converted(self):
        """A WITHIN-CAP image in a format outside the known-good table (TIFF)
        must not pass through: the backend rejects the format on every
        replay, the same wedge as an oversized image. It converts to PNG at
        its original dimensions instead."""
        pil = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        pil.new("RGB", (300, 200), (9, 8, 7)).save(buf, format="TIFF")
        fitted = downscale_image_block(buf.getvalue(), "image/tiff")
        assert fitted is not None
        out_bytes, out_mime = fitted
        assert out_mime == "image/png"
        with pil.open(io.BytesIO(out_bytes)) as im:
            assert im.format == "PNG"
            assert im.size == (300, 200)  # conversion, not a resize

    def test_oversized_is_downscaled(self):
        pil = pytest.importorskip("PIL.Image")
        fitted = downscale_image_block(_png_bytes(4000, 3000), "image/png")
        assert fitted is not None
        out_bytes, out_mime = fitted
        assert out_mime == "image/png"
        with pil.open(io.BytesIO(out_bytes)) as im:
            assert im.size == (2000, 1500)

    def test_undecodable_returns_none(self):
        assert downscale_image_block(b"garbage bytes", "image/png") is None

    def test_truncated_image_returns_none(self):
        raw = _png_bytes(800, 600)
        assert downscale_image_block(raw[: len(raw) // 2], "image/png") is None

    def test_source_pixel_ceiling_returns_none(self, monkeypatch):
        monkeypatch.setattr(imaging, "MAX_IMAGE_SOURCE_PIXELS", 10_000)
        assert downscale_image_block(_png_bytes(200, 200), "image/png") is None
        # 40,000 px > 10,000 px ceiling

    def test_encoded_ceiling_enforced(self):
        """A dimension-compliant image over a (test-scale) byte ceiling is
        shrunk until its base64 encoding fits."""
        raw = _noise_png_bytes(600, 600)
        ceiling = (len(raw) * 4 // 3) // 2  # force roughly one shrink pass
        fitted = downscale_image_block(raw, "image/png", max_b64_bytes=ceiling)
        assert fitted is not None
        out_bytes, _ = fitted
        assert 4 * ((len(out_bytes) + 2) // 3) <= ceiling

    def test_budget_constants_are_the_prompt_path_ones(self):
        """The tool-result seam must enforce the SAME budget as the prompt
        path -- both feed the same per-image backend limits, and prompt_blocks
        re-exports these very objects."""
        from kiro_crew.acp import prompt_blocks

        assert MAX_IMAGE_EDGE_PX == 2000
        assert MAX_IMAGE_B64_BYTES == 5 * 1024 * 1024
        assert MIN_IMAGE_EDGE_PX == 256
        assert prompt_blocks.MAX_IMAGE_EDGE_PX is MAX_IMAGE_EDGE_PX
        assert prompt_blocks.MAX_IMAGE_B64_BYTES is MAX_IMAGE_B64_BYTES
