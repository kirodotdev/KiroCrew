"""ACP prompt-block construction and the image capability gate.

Regression coverage for the defect where EVERY channel shipped a filesystem
path as prose: ``AcpSessionHandle.prompt`` hardcoded a single text block, while
the only image encoder lived on ``AcpClient`` -- the path ``AcpProvider.start``
replaces. An image therefore never reached the model as vision input.

The builder takes the channel's STRUCTURED attachment list and builds every
image block from it; the text is never scanned for paths (that half of the
contract is pinned in ``test_prompt_attachments.py``). The tests here pass each
image both ways a real channel does -- named in the text and listed as an
attachment -- and check the bytes, the caps and the marker rewrite.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import re
from pathlib import Path

import pytest

from conftest import CountingText
from kiro_crew import hooks, image_refs, imaging, prompt_attachments
from kiro_crew.acp import prompt_blocks
from kiro_crew.acp.prompt_blocks import (
    IMAGE_MEDIA_TYPES,
    IMAGE_SIZE_NOTE,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_EDGE_PX,
    MAX_PROMPT_IMAGE_B64_BYTES,
    MAX_PROMPT_IMAGE_BLOCKS,
    PROMPT_LIMIT_NOTE,
    build_prompt_blocks,
    summarize_prompt_structure,
)
from kiro_crew.image_refs import _POSIX_PATH_RE
from kiro_crew.prompt_attachments import PromptAttachment, image_attachments

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def _distinct_png(tmp_path, name, width):
    """A real PNG whose bytes differ per *width*: two of these are two pictures,
    where two ``_png`` files are the same picture under two names."""
    p = tmp_path / name
    p.write_bytes(_image_bytes(size=(width, 1)))
    return p


def _att(*paths):
    """The structured list a channel hands the builder for *paths*.

    Every image block comes from this list; the text is never scanned, so the
    tests below pass the path BOTH ways a real channel does -- in the text (as
    Slack and the dashboard write it) and in the list -- and check the text
    rewrite the marker contract promises.
    """
    return image_attachments([str(p) for p in paths])


def _image_bytes(fmt="PNG", size=(2, 2)):
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", size, (127, 127, 127)).save(buf, format=fmt)
    return buf.getvalue()


class TestBuildPromptBlocks:
    def test_always_returns_at_least_a_text_block(self):
        blocks = build_prompt_blocks("just words")
        assert blocks == [{"type": "text", "text": "just words"}]

    def test_image_path_becomes_an_image_block(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"look at {p} please", attachments=_att(p))

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
        blocks = build_prompt_blocks(f"look at {p}", attachments=_att(p), allow_image=False)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_oversized_image_falls_back_to_path(self, tmp_path):
        """Size is checked BEFORE base64: encoding inflates 4/3 and the whole
        request is one newline-delimited JSON frame."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_bytes=1)

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_missing_and_unreadable_files_are_skipped(self, tmp_path):
        blocks = build_prompt_blocks("see", attachments=_att("/definitely/not/here.png"))
        assert [b["type"] for b in blocks] == ["text"]

    def test_directory_with_image_suffix_is_not_read(self, tmp_path):
        d = tmp_path / "weird.png"
        d.mkdir()
        blocks = build_prompt_blocks(f"see {d}", attachments=_att(d))
        assert [b["type"] for b in blocks] == ["text"]

    def test_multiple_images_each_get_a_block(self, tmp_path):
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)
        blocks = build_prompt_blocks(f"{a} and {b}", attachments=_att(a, b))

        assert [x["type"] for x in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == "[image: a.png] and [image: b.png]"

    def test_same_path_twice_is_encoded_once(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"{p} then {p} again", attachments=_att(p, p))
        # One image block, and both textual occurrences are rewritten.
        assert [x["type"] for x in blocks] == ["text", "image"]
        assert str(p) not in blocks[0]["text"]

    @pytest.mark.parametrize("suffix,mime", sorted(IMAGE_MEDIA_TYPES.items()))
    def test_every_supported_suffix_maps_to_its_mime(self, tmp_path, suffix, mime):
        # Fixtures are format-faithful (real bytes per format, not one PNG
        # renamed): the emitted mimeType tracks the header-DETECTED format,
        # because the backend validates the bytes, not the file extension.
        pil = pytest.importorskip("PIL.Image")
        fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
               ".gif": "GIF", ".webp": "WEBP", ".bmp": "BMP"}[suffix]
        p = tmp_path / f"img{suffix}"
        mode = "P" if fmt == "GIF" else "RGB"
        pil.new(mode, (2, 2)).save(p, format=fmt)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert blocks[1]["mimeType"] == mime

    def test_svg_is_not_inlined(self, tmp_path):
        """SVG is scriptable XML, not a raster image. The direct client listed it
        in its media map while its regex omitted it, so the mapping was already
        unreachable -- keep it excluded deliberately."""
        p = tmp_path / "vector.svg"
        p.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text"]
        assert ".svg" not in IMAGE_MEDIA_TYPES

    def test_bare_filename_is_not_treated_as_a_path(self, tmp_path):
        """Prose mentioning a filename triggers no filesystem probe: the text is
        never scanned, so only the attachment list names candidates."""
        blocks = build_prompt_blocks("the file shot.png is attached")
        assert [b["type"] for b in blocks] == ["text"]
        assert blocks[0]["text"] == "the file shot.png is attached"

    def test_default_cap_is_ten_mib(self):
        assert MAX_IMAGE_BYTES == 10 * 1024 * 1024

    def test_overlong_name_is_not_probed_and_does_not_raise(self, tmp_path):
        """A 400-char single-segment entry ending in .png names nothing: the
        probe would raise ENAMETOOLONG and fail the whole turn. It must stay
        plain text, while a real image in the same list still inlines."""
        p = _png(tmp_path)
        token = "/" + "a" * 400 + ".png"
        message = f"see {token} and {p}"

        blocks = build_prompt_blocks(message, attachments=_att(token, p))

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert token in blocks[0]["text"]
        assert f"[image: {p.name}]" in blocks[0]["text"]

    def test_probe_oserror_is_treated_as_not_a_file(self, tmp_path, monkeypatch):
        """Any OSError from the is_file() probe means "not a file", not a
        failed turn -- the reference stays in the text."""
        p = _png(tmp_path)
        real_is_file = Path.is_file

        def flaky_is_file(self):
            if self == p:
                raise OSError(5, "I/O error", str(self))
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", flaky_is_file)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]


class TestOnePromptImageRules:
    """What one prompt does with its own pictures, with no memory of earlier
    prompts: markers land only where the attachment's spelling stands delimited
    (and are appended when the text never names it), two files that share a
    name get two markers, the same bytes under two names are one block, and a
    prompt past its own count or byte cap keeps the rest as text that says so.
    Every picture here is handed over as the channel's list; the text alone
    never attaches anything."""

    def test_marker_substitution_touches_only_the_delimited_spelling(self, tmp_path):
        p = _png(tmp_path)
        # The same characters inside a URL are not the attachment standing on
        # its own; a whole-text replace would rewrite them too.
        blocks = build_prompt_blocks(
            f"see {p} and the mirror at https://example.com{p}", attachments=_att(p)
        )
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"see [image: shot.png] and the mirror at https://example.com{p}"

    @pytest.mark.parametrize("tail", [".backup", "x", "/other", "-v2", "_old", "~", "~1"])
    def test_a_longer_name_that_starts_with_an_attached_path_is_not_rewritten(self, tmp_path, tail):
        p = _png(tmp_path)
        text = f"restore {p}{tail} please"

        blocks = build_prompt_blocks(text, attachments=_att(p))

        # The longer token names a different file, so it is left as written;
        # the attached picture still rides, announced on its own line.
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"{text}\n[image: shot.png]"

    def test_a_sentence_glued_to_an_attached_path_leaves_the_text_as_written(self, tmp_path):
        # `.Then` glued to the path is not the attachment standing delimited:
        # the text stays as the user wrote it and the marker is appended, so
        # the picture is attached either way and no prose is rewritten.
        p = _png(tmp_path)
        text = f"see {p}.Then we moved on"

        blocks = build_prompt_blocks(text, attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"{text}\n[image: shot.png]"

    @pytest.mark.parametrize("suffix", ["版本", "-old", "📁", "Ａ"], ids=["cjk", "ascii", "emoji", "fullwidth"])
    def test_a_non_image_file_inside_a_directory_named_like_a_picture_attaches_nothing(
        self, tmp_path, suffix
    ):
        _png(tmp_path, "a.png")
        directory = tmp_path / f"a.png{suffix}"
        directory.mkdir()
        final = directory / "final.txt"
        final.write_text("not an image", encoding="utf-8")
        message = f"open {final}"

        # Named in the text only: nothing is read. Listed: the bytes are not a
        # raster, so it stays text, with no note (the notes speak about images).
        assert build_prompt_blocks(message) == [{"type": "text", "text": message}]
        assert build_prompt_blocks(message, attachments=_att(final)) == [
            {"type": "text", "text": message}
        ]

    @pytest.mark.parametrize(
        "glue",
        ["，", "和", "\U0001F4C1", "\\", ","],
        ids=["fullwidth-comma", "cjk", "emoji", "backslash", "comma"],
    )
    def test_two_attached_pictures_glued_in_the_text_are_both_attached(self, tmp_path, glue):
        # The glued spelling is one token that names neither picture, so the
        # text stays as written; the list still carries both, each announced
        # on its own line -- never the second picture alone with the first
        # dropped silently.
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)
        text = f"look {a}{glue}{b}"

        blocks = build_prompt_blocks(text, attachments=_att(a, b))

        assert [block["type"] for block in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == f"{text}\n[image: a.png]\n[image: b.png]"

    @pytest.mark.parametrize(
        ("template", "in_place"),
        [
            ("({a})({b})", True),
            ("'{a}' '{b}'", True),
            ("{a}\n{b}", True),
            ("{a}\u2014{b}", False),
            ("{a}\u2013{b}", False),
            ("{a}\u2192{b}", False),
            ("{a}\u2022{b}", False),
            ("{a}\u30fb{b}", False),
        ],
    )
    def test_two_pictures_separated_by_punctuation_are_two_pictures(self, tmp_path, template, in_place):
        # Both ride from the list whatever stands between them. The marker
        # replaces a path only where it stands delimited (a parenthesis, a
        # quote, whitespace); glued to a dash, an arrow or a bullet the path is
        # left as written and the marker is appended.
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)
        text = template.format(a=a, b=b)

        blocks = build_prompt_blocks(text, attachments=_att(a, b))

        assert [block["type"] for block in blocks] == ["text", "image", "image"]
        if in_place:
            assert blocks[0]["text"] == template.format(a="[image: a.png]", b="[image: b.png]")
        else:
            assert blocks[0]["text"] == f"{text}\n[image: a.png]\n[image: b.png]"

    def test_a_web_image_after_a_picture_is_left_as_a_url(self, tmp_path):
        a = _distinct_png(tmp_path, "a.png", 1)

        blocks = build_prompt_blocks(
            f"see {a} and the banner is at https://example.com/logo.png", attachments=_att(a)
        )

        assert [block["type"] for block in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "see [image: a.png] and the banner is at https://example.com/logo.png"

    def test_a_long_message_of_path_fragments_is_handled_in_bounded_work(self, tmp_path):
        # Nothing in the text is read as a path, and the one attachment's
        # spans are found with each character read a bounded number of times:
        # the work is counted, not timed.
        p = _png(tmp_path)
        text = (" /a" * 20000) + ".png~"

        alone = build_prompt_blocks(text)
        counted = CountingText(text)
        with_picture = build_prompt_blocks(counted, attachments=_att(p))

        assert alone == [{"type": "text", "text": text}]
        assert [b["type"] for b in with_picture] == ["text", "image"]
        assert with_picture[0]["text"] == f"{text}\n[image: shot.png]"
        assert counted.visits > 0, "the builder never read the text -- the counter is not wired"
        assert counted.visits <= 20 * len(text), (
            f"the builder read {counted.visits / len(text):.0f} characters per character of text"
        )

    def test_a_picture_inside_a_directory_named_like_a_picture_is_marked_whole(self, tmp_path):
        _png(tmp_path, "a.png")
        directory = tmp_path / "a.png版本"
        directory.mkdir()
        p = directory / "final.jpg"
        image = _image_bytes("JPEG")
        p.write_bytes(image)

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "see [image: final.jpg]"
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]) == image

    def test_a_double_suffix_name_is_one_picture(self, tmp_path):
        p = tmp_path / "a.png.jpg"
        image = _image_bytes("JPEG")
        p.write_bytes(image)

        blocks = build_prompt_blocks(str(p), attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "[image: a.png.jpg]"
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]) == image

    def test_a_backup_of_a_nested_picture_is_not_rewritten(self, tmp_path):
        directory = tmp_path / "a.png"
        directory.mkdir()
        p = _png(directory, "b.png")
        text = f"{p}~"

        blocks = build_prompt_blocks(text, attachments=_att(p))

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"{text}\n[image: b.png]"

    def test_a_longer_name_before_the_real_path_does_not_hide_it(self, tmp_path):
        p = _png(tmp_path)
        text = f"diff {p}.orig against {p}"
        blocks = build_prompt_blocks(text, attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"diff {p}.orig against [image: shot.png]"
        # The scrubber's grammar agrees on which token is the picture.
        assert [m.group(1) for m in image_refs._PATH_RE.finditer(text)] == [str(p)]

    @pytest.mark.parametrize(
        ("wrap", "in_place"),
        [
            ("({p})", True),
            ("'{p}'", True),
            ("{p}\n", True),
            ("{p}.", False),
            ("{p},", False),
            ("看 {p}这个图", False),
        ],
    )
    def test_punctuation_after_an_attached_path_marks_it_where_it_stands_delimited(
        self, tmp_path, wrap, in_place
    ):
        p = _png(tmp_path)
        text = wrap.format(p=p)
        blocks = build_prompt_blocks(text, attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]
        if in_place:
            assert blocks[0]["text"] == wrap.format(p="[image: shot.png]")
        else:
            assert blocks[0]["text"] == f"{text}\n[image: shot.png]"

    def test_distinct_files_sharing_a_basename_get_distinct_markers(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = tmp_path / "a" / "shot.png"
        second = tmp_path / "b" / "shot.png"
        first.write_bytes(_image_bytes(size=(1, 1)))
        second.write_bytes(_image_bytes(size=(2, 1)))
        blocks = build_prompt_blocks(f"{first} vs {second}", attachments=_att(first, second))
        assert [b["type"] for b in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == "[image: shot.png] vs [image: shot.png (2)]"

    def test_distinct_files_sharing_a_sender_name_get_distinct_markers(self, tmp_path):
        # The marker carries the SENDER'S name when the channel supplied one,
        # so two different pictures uploaded under one name are told apart.
        first = tmp_path / "x1.png"
        second = tmp_path / "x2.png"
        first.write_bytes(_image_bytes(size=(1, 1)))
        second.write_bytes(_image_bytes(size=(2, 1)))
        attachments = [
            PromptAttachment(path=str(first), name="photo.png"),
            PromptAttachment(path=str(second), name="photo.png"),
        ]
        blocks = build_prompt_blocks(f"{first} {second}", attachments=attachments)
        assert blocks[0]["text"] == "[image: photo.png] [image: photo.png (2)]"

    def test_identical_bytes_under_two_paths_are_one_block(self, tmp_path):
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")  # same bytes, another name
        blocks = build_prompt_blocks(f"{a} then {b}", attachments=_att(a, b))
        assert [x["type"] for x in blocks] == ["text", "image"]
        # Both places point at the one picture that was sent.
        assert blocks[0]["text"] == "[image: a.png] then [image: a.png]"

    @pytest.mark.parametrize("named", ["neither", "first", "second"])
    def test_identical_bytes_under_two_paths_are_announced_once(self, tmp_path, named):
        # One block, one marker: the model was handed one picture, so the text
        # says so once, whichever of the two paths the text names.
        a = _png(tmp_path, "a.png")
        b = _png(tmp_path, "b.png")
        text = {"neither": "caption", "first": f"see {a}", "second": f"see {b}"}[named]

        blocks = build_prompt_blocks(text, attachments=_att(a, b))

        assert [x["type"] for x in blocks] == ["text", "image"]
        assert blocks[0]["text"].count("[image: a.png]") == 1
        assert blocks[0]["text"] == {
            "neither": "caption\n[image: a.png]",
            "first": "see [image: a.png]",
            "second": "see [image: a.png]",
        }[named]

    def test_prompt_block_cap_leaves_the_rest_as_text_that_says_so(self, tmp_path, caplog):
        paths = []
        for i in range(3):
            p = tmp_path / f"p{i}.png"
            p.write_bytes(_image_bytes(size=(i + 1, 1)))
            paths.append(p)
        # Listed entries naming no file are skipped without a word, past the
        # cap or not: no note, no tally. Only a real picture that was dropped
        # is counted.
        nowhere = "C:\\nowhere" if os.name == "nt" else "/nowhere"
        missing = [f"{nowhere}{os.sep}n{i}.png" for i in range(50)]
        shaped = " ".join(missing)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.prompt_blocks"):
            blocks = build_prompt_blocks(
                " ".join(map(str, paths)) + " " + shaped,
                attachments=_att(*paths, *missing),
                max_prompt_image_blocks=2,
            )
        assert [b["type"] for b in blocks] == ["text", "image", "image"]
        # The third stays a usable path and says so, like a picture over the
        # per-image cap, so neither the user nor the model takes it as seen.
        assert blocks[0]["text"] == (
            f"[image: p0.png] [image: p1.png] {paths[2]} {PROMPT_LIMIT_NOTE} {shaped}"
        )
        over_cap = [r for r in caplog.records if "limit of 2 blocks" in r.getMessage()]
        assert len(over_cap) == 1
        assert "1 image" in over_cap[0].getMessage()

    def test_a_kept_out_picture_the_text_never_names_is_announced_on_its_own_line(self, tmp_path):
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)

        blocks = build_prompt_blocks(
            "two pictures", attachments=_att(a, b), max_prompt_image_blocks=1
        )

        assert [x["type"] for x in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"two pictures\n[image: a.png]\nb.png {PROMPT_LIMIT_NOTE}"

    def test_a_full_prompt_decodes_no_further_pictures(self, tmp_path, monkeypatch):
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)
        decoded: list[bytes] = []
        real = prompt_blocks.downscale_image_block
        monkeypatch.setattr(
            prompt_blocks,
            "downscale_image_block",
            lambda data, *args, **kwargs: (decoded.append(data), real(data, *args, **kwargs))[1],
        )
        build_prompt_blocks(f"{a} {b}", attachments=_att(a, b), max_prompt_image_blocks=1)
        assert decoded == [a.read_bytes()], "the second picture is past the cap before any decode"

    def test_a_duplicate_past_the_block_cap_maps_to_its_block(self, tmp_path):
        # The same bytes under a second name are the picture already attached,
        # cap or no cap: one block, both places marked, nothing dropped.
        a = _png(tmp_path, "a.png")
        copy = _png(tmp_path, "copy.png")

        blocks = build_prompt_blocks(f"{a} {copy}", attachments=_att(a, copy), max_prompt_image_blocks=1)

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "[image: a.png] [image: a.png]"

    def test_prompt_byte_cap_leaves_the_rest_as_text_that_says_so(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        a.write_bytes(_image_bytes(size=(1, 1)))
        b.write_bytes(_image_bytes(size=(2, 1)))
        one = len(build_prompt_blocks(str(a), attachments=_att(a))[1]["data"])
        blocks = build_prompt_blocks(f"{a} {b}", attachments=_att(a, b), max_prompt_image_b64_bytes=one)
        assert [x["type"] for x in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"[image: a.png] {b} {PROMPT_LIMIT_NOTE}"
        # The cap is inclusive: exactly at the cap still fits.
        both_blocks = build_prompt_blocks(f"{a} {b}", attachments=_att(a, b))
        both = len(both_blocks[1]["data"]) + len(both_blocks[2]["data"])
        at_cap = build_prompt_blocks(f"{a} {b}", attachments=_att(a, b), max_prompt_image_b64_bytes=both)
        assert [x["type"] for x in at_cap] == ["text", "image", "image"]

    def test_a_picture_over_the_per_image_cap_says_so(self, tmp_path):
        p = _png(tmp_path)

        blocks = build_prompt_blocks(f"see {p} now", attachments=_att(p), max_image_bytes=10)

        assert blocks == [{"type": "text", "text": f"see {p} {IMAGE_SIZE_NOTE} now"}]

    def test_the_note_lands_after_a_markdown_destination(self, tmp_path):
        # A note inside `![alt](...)` would corrupt the link; it follows the
        # reference instead, so the markdown still renders and still says so.
        p = _png(tmp_path)

        blocks = build_prompt_blocks(f"see ![shot]({p}) now", attachments=_att(p), max_image_bytes=10)

        assert blocks == [{"type": "text", "text": f"see ![shot]({p}) {IMAGE_SIZE_NOTE} now"}]

    def test_the_note_follows_the_other_markdown_destination_forms_too(self, tmp_path):
        # The composer wraps a destination holding a space in `<...>`; a title
        # may follow a bare one. Both are the picture's own spelling standing
        # in a destination, so the note lands after the closing parenthesis.
        spaced = _png(tmp_path, "my shot.png")
        titled = _png(tmp_path, "shot.png")
        wrapped = f"![shot]({prompt_attachments.markdown_image_dest(str(spaced))})"
        assert wrapped.startswith("![shot](<")
        with_title = f'![shot]({titled} "the shot")'

        for reference, p in ((wrapped, spaced), (with_title, titled)):
            blocks = build_prompt_blocks(f"see {reference} now", attachments=_att(p), max_image_bytes=10)
            assert blocks == [{"type": "text", "text": f"see {reference} {IMAGE_SIZE_NOTE} now"}]

    def test_a_hand_wrapped_plain_destination_is_a_mention(self, tmp_path):
        # The composer never wraps a plain path, so `<...>` around one is not a
        # spelling the channel writes: the text stays as typed and the kept-out
        # picture is announced on its own line instead.
        p = _png(tmp_path)
        text = f"see ![shot](<{p}>) now"

        blocks = build_prompt_blocks(text, attachments=_att(p), max_image_bytes=10)

        assert blocks == [{"type": "text", "text": f"{text}\nshot.png {IMAGE_SIZE_NOTE}"}]

    def test_a_typed_marker_is_escaped_even_without_image_support(self):
        # A backend that takes no images still reads the text, so a typed marker
        # must not claim an attachment there either.
        text = "see [image: shot.png] and /tmp/shot.png"

        assert build_prompt_blocks(text, allow_image=False) == [
            {"type": "text", "text": "see \\[image: shot.png] and /tmp/shot.png"}
        ]

    def test_a_non_raster_with_a_picture_suffix_gets_no_image_note(self, tmp_path, caplog):
        # The notes speak about images, so they are written only for bytes that
        # sniff as a raster -- a text file named like a picture stays plain text
        # whether it is over the size cap or past a full prompt.
        a = _png(tmp_path, "a.png")
        fake = tmp_path / "fake.png"
        fake.write_bytes(b"not a picture at all, just words\n" * 4)

        over_size = build_prompt_blocks(f"see {fake} now", attachments=_att(fake), max_image_bytes=10)
        assert over_size == [{"type": "text", "text": f"see {fake} now"}]

        with caplog.at_level("WARNING", logger="kiro_crew.acp.prompt_blocks"):
            past_cap = build_prompt_blocks(
                f"{a} {fake}", attachments=_att(a, fake), max_prompt_image_blocks=1
            )
        assert past_cap[0]["text"] == f"[image: a.png] {fake}"
        assert not [r for r in caplog.records if "limit of 1 blocks" in r.getMessage()]

    def test_a_user_typed_marker_cannot_pass_for_a_real_one(self, tmp_path):
        # Only the builder writes a bare marker: a typed one is escaped, so the
        # model cannot be told a picture is attached when none is.
        p = _png(tmp_path)

        blocks = build_prompt_blocks(
            f"see [image: shot.png] and {p} but [image gallery]", attachments=_att(p)
        )

        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == (
            "see \\[image: shot.png] and [image: shot.png] but [image gallery]"
        )
        assert build_prompt_blocks(f"[image not attached: x] {PROMPT_LIMIT_NOTE}") == [
            {"type": "text", "text": f"\\[image not attached: x] \\{PROMPT_LIMIT_NOTE}"}
        ]
        # Case does not make a typed marker honest, and the replay scrubber's
        # own marker (a replayed row's text) is not a forgery to escape.
        replayed = "[Image: x.png] then [image not carried into this context]"
        assert build_prompt_blocks(replayed) == [
            {"type": "text", "text": "\\[Image: x.png] then [image not carried into this context]"}
        ]

    def test_an_attachment_whose_spellings_sit_inside_another_still_gets_its_marker(self, tmp_path):
        # `/tmp/a.png` is a prefix of the attached `/tmp/a.png b.png`, so its
        # only span lies inside the longer picture's and is dropped by the
        # rewrite; the picture still rides, so it is announced on its own line
        # instead of silently losing its marker.
        short = _distinct_png(tmp_path, "a.png", 1)
        long = tmp_path / "a.png b.png"
        long.write_bytes(_image_bytes(size=(2, 1)))

        blocks = build_prompt_blocks(f"see {long}", attachments=_att(short, long))

        assert [b["type"] for b in blocks] == ["text", "image", "image"]
        assert blocks[0]["text"] == "see [image: a.png b.png]\n[image: a.png]"

    def test_a_sender_name_cannot_forge_a_second_marker(self, tmp_path):
        # The name is sender-supplied text written inside marker syntax: a
        # bracket in it must not close the marker early and open another, and
        # a backslash it already carries must not pair with the escape.
        p = _distinct_png(tmp_path, "one.png", 1)
        forged = [PromptAttachment(path=str(p), name="one] [image: forged.png")]
        pre_escaped = [PromptAttachment(path=str(p), name="one\\] \\[image: forged.png")]

        for attachments, expected in (
            (forged, "caption\n[image: one\\] \\[image: forged.png]"),
            (pre_escaped, "caption\n[image: one\\\\\\] \\\\\\[image: forged.png]"),
        ):
            blocks = build_prompt_blocks("caption", attachments=attachments)
            assert [b["type"] for b in blocks] == ["text", "image"]
            marker = blocks[0]["text"].split("\n", 1)[1]
            # A live bracket is one behind an even run of backslashes.
            assert len(re.findall(r"(?<!\\)(?:\\\\)*\[image:", marker)) == 1
            assert len(re.findall(r"(?<!\\)(?:\\\\)*\]", marker)) == 1
            assert blocks[0]["text"] == expected

    def test_a_shorter_attachment_never_rewrites_a_longer_attachment_it_prefixes(self, tmp_path):
        # `/tmp/a.png` is a prefix of the listed `/tmp/a.png b.png` and the space
        # is a delimiter, so the short marker could land inside the long path.
        # The long path keeps its text whether it is unreadable (nothing to
        # mark) or a picture a cap kept out (a note follows it): it is the only
        # reference the model has to that file. The short picture still rides,
        # announced on its own line.
        short = _distinct_png(tmp_path, "a.png", 1)
        missing = tmp_path / "a.png b.png"
        kept_out = tmp_path / "a.png c.png"
        kept_out.write_bytes(_image_bytes(size=(2, 1)))

        blocks = build_prompt_blocks(f"see {missing}", attachments=_att(short, missing))
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"see {missing}\n[image: a.png]"

        blocks = build_prompt_blocks(
            f"see {kept_out}", attachments=_att(short, kept_out), max_prompt_image_blocks=1
        )
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"see {kept_out} {PROMPT_LIMIT_NOTE}\n[image: a.png]"

    def test_marker_shaped_text_inside_a_listed_path_is_left_as_written(self, tmp_path):
        # A listed path whose spelling holds `[image:` is a file reference, not
        # a typed marker: escaping inside it would corrupt the only reference
        # the model has to a picture that stayed text (unreadable, or kept out
        # by a cap). Marker-shaped prose outside the path is still escaped.
        missing = tmp_path / "x[image:typed].png"
        kept_out = tmp_path / "y[image: kept].png"
        kept_out.write_bytes(_image_bytes(size=(2, 1)))
        a = _distinct_png(tmp_path, "a.png", 1)

        blocks = build_prompt_blocks(f"[image: fake] open {missing}", attachments=_att(missing))
        assert blocks == [{"type": "text", "text": f"\\[image: fake] open {missing}"}]

        blocks = build_prompt_blocks(
            f"open {kept_out} and {a}", attachments=_att(a, kept_out), max_prompt_image_blocks=1
        )
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == f"open {kept_out} {PROMPT_LIMIT_NOTE} and [image: a.png]"

    def test_marker_labels_are_unique_across_the_prompt(self, tmp_path):
        # A sender-chosen name that already reads `photo (2)` must not collide
        # with the label the second distinct `photo` would otherwise get.
        paths = []
        for i, name in enumerate(("p1.png", "p2.png", "p3.png")):
            p = tmp_path / name
            p.write_bytes(_image_bytes(size=(i + 1, 1)))
            paths.append(p)
        attachments = [
            PromptAttachment(path=str(paths[0]), name="photo (2)"),
            PromptAttachment(path=str(paths[1]), name="photo"),
            PromptAttachment(path=str(paths[2]), name="photo"),
        ]

        blocks = build_prompt_blocks("caption", attachments=attachments)

        assert [b["type"] for b in blocks] == ["text", "image", "image", "image"]
        labels = blocks[0]["text"].split("\n")[1:]
        assert labels == ["[image: photo (2)]", "[image: photo]", "[image: photo (3)]"]
        assert len(set(labels)) == 3

    def test_default_prompt_caps(self):
        assert MAX_PROMPT_IMAGE_BLOCKS == 20
        assert MAX_PROMPT_IMAGE_B64_BYTES == 12 * 1024 * 1024


class TestMediaTypeFromContent:
    def test_content_wins_over_a_misleading_suffix(self, tmp_path):
        p = tmp_path / "actually-a-jpeg.png"
        p.write_bytes(_image_bytes("JPEG"))

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [block["type"] for block in blocks] == ["text", "image"]
        assert blocks[1]["mimeType"] == "image/jpeg"

    @pytest.mark.parametrize(
        "name,raw",
        [
            ("notes.png", b"plain text"),
            ("vector.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
            ("cut.png", _PNG[:12]),
        ],
    )
    def test_non_raster_or_truncated_content_stays_a_path(self, tmp_path, name, raw):
        p = tmp_path / name
        p.write_bytes(raw)

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [block["type"] for block in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_riff_container_that_is_not_webp_stays_a_path(self, tmp_path):
        p = tmp_path / "audio.webp"
        p.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt ")

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert [block["type"] for block in blocks] == ["text"]

    def test_no_pillow_path_uses_the_sniffed_mime(self, tmp_path, monkeypatch):
        p = tmp_path / "renamed.png"
        original = _image_bytes("JPEG")
        p.write_bytes(original)
        monkeypatch.setattr(imaging, "_pil", lambda: None)

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_downscale_reencodes_by_content_not_by_name(self, tmp_path):
        p = tmp_path / "big.png"
        p.write_bytes(_image_bytes("JPEG", (MAX_IMAGE_EDGE_PX + 40, 10)))

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))

        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]).startswith(b"\xff\xd8\xff")

    def test_zero_edge_still_corrects_the_wire_mime(self, tmp_path):
        p = tmp_path / "renamed.png"
        original = _image_bytes("JPEG")
        p.write_bytes(original)

        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_edge=0)

        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]) == original


class TestSensitivePathGate:
    """Image bytes must travel through the centralized sensitive-path gate.

    The gate itself (``hooks.safe_read_file_bytes``: realpath canonicalization,
    ``is_sensitive_path``, ``O_NOFOLLOW``) has its own tests. What matters here
    is that this builder ROUTES through it and honours a refusal -- the paths
    reaching it come from the channel's attachment list, which for the dashboard
    is client-supplied JSON and so user-influenced.
    """

    def test_refused_read_is_not_inlined(self, tmp_path, monkeypatch):
        p = _png(tmp_path)
        monkeypatch.setattr(prompt_attachments, "safe_read_file_bytes", lambda raw: None)

        blocks = build_prompt_blocks(f"look at {p}", attachments=_att(p))

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

        monkeypatch.setattr(prompt_attachments, "safe_read_file_bytes", _spy)
        build_prompt_blocks(f"look at {p}", attachments=_att(p))
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
        monkeypatch.setattr(prompt_attachments, "safe_read_file_bytes", lambda raw: gate_bytes)

        blocks = build_prompt_blocks(f"look at {p}", attachments=_att(p))

        assert base64.b64decode(blocks[1]["data"]) == gate_bytes


class TestPlatformPathGrammar:
    """The path grammar is host-specific on purpose."""

    def test_posix_pattern_matches_posix_paths(self):
        assert image_refs._POSIX_PATH_RE.search("/tmp/a.png") is not None

    def test_posix_pattern_ignores_windows_shapes(self):
        r"""Prose like ``C:\shots\logo.png`` must NOT be a candidate on POSIX.

        Backslash and ``:`` are legal POSIX filename characters, so one merged
        pattern would make a merely-MENTIONED Windows path matchable -- and a
        file with that literal name can exist in the CWD, which would inline
        something the user only talked about.
        """
        assert image_refs._POSIX_PATH_RE.search(r"C:\shots\logo.png") is None
        assert image_refs._POSIX_PATH_RE.search(r"\\host\share\logo.png") is None

    @pytest.mark.parametrize(
        "text",
        [
            r"C:\Users\alice\AppData\Local\Temp\tmpabc.png",
            r"C:/Users/alice/AppData/Local/Temp/tmpabc.png",
            r"\\fileserver\team\diagram.jpg",
            "//fileserver/team/diagram.jpg",
            # A GitHub Actions Windows runner's %TEMP% resolves to the 8.3 SHORT
            # name of its profile ("RUNNER~1"); a long-named local user can be
            # "Admini~1" the same way. The tilde must be a path character or the
            # non-greedy body cannot cross it and the whole path fails to match.
            r"C:\Users\RUNNER~1\AppData\Local\Temp\kcabc\John Smith\tmpab12cd_4.png",
            r"C:\Users\Admini~1\AppData\Local\Temp\shot.png",
        ],
    )
    def test_windows_pattern_matches_native_absolute_paths(self, text):
        """The shapes the gateway actually produces on Windows.

        The forward-slash UNC form is what the dashboard composer serializes
        into message text (a markdown destination cannot carry raw
        backslashes), and Windows file APIs accept it verbatim. The ``~`` cases
        are the 8.3 short-name temp directory a CI runner (and a long-named
        local user) actually gets.
        """
        assert image_refs._WINDOWS_PATH_RE.search(text) is not None

    def test_windows_pattern_extracts_dashboard_unc_image_markdown(self):
        """A UNC upload serialized by the dashboard must yield the usable path.

        This is the sender-side wire form for a roaming-profile upload: the
        composer emits ``![image](//host/share/...)``. The extracted group must
        be the path itself (openable via ``open()`` on Windows), not a mangled
        span, or the agent silently receives no image block.
        """
        text = "![image](//fileserver/home/me/.kiro/crew/uploads/shot.png)"
        m = image_refs._WINDOWS_PATH_RE.search(text)
        assert m is not None
        assert m.group(1) == "//fileserver/home/me/.kiro/crew/uploads/shot.png"

    def test_windows_pattern_ignores_urls(self):
        """``//`` acceptance must not make ``https://host/x.png`` a candidate."""
        assert image_refs._WINDOWS_PATH_RE.search("see https://example.com/docs/logo.png") is None
        assert image_refs._WINDOWS_PATH_RE.search("see http://host/a.png here") is None

    def test_windows_pattern_requires_an_absolute_path(self):
        assert image_refs._WINDOWS_PATH_RE.search(r"shots\logo.png") is None

    def test_no_token_break_is_a_path_character(self):
        # A non-ASCII character that both ends a token and may sit inside a path
        # would let a directory named like a picture expose its prefix again.
        # (Space, tab and parentheses overlap on purpose: they end a token yet
        # are legal inside a quoted path.)
        from kiro_crew import image_refs

        breaks = re.compile(f"[{image_refs._TOKEN_BREAK}]")
        bodies = re.compile(f"{image_refs._PATH_CHARS}|{image_refs._WINDOWS_PATH_CHARS}")
        overlap = [
            hex(code)
            for code in range(0x80, 0x10000)
            if breaks.fullmatch(chr(code)) and bodies.fullmatch(chr(code))
        ]

        assert overlap == []

    @pytest.mark.parametrize(
        "text",
        [
            "看 C:/Users/me/a.png和C:/Users/me/b.png",
            r"看 C:\Users\me\a.png和C:\Users\me\b.png",
        ],
    )
    def test_windows_pattern_reads_a_glued_pair_as_one_token(self, text):
        # The drive colon is not a token break here: a second absolute path
        # glued on by prose stays inside the first path's token, as the POSIX
        # grammar reads the same spelling, so neither host attaches a picture.
        assert [m.group(1) for m in image_refs._WINDOWS_PATH_RE.finditer(text)] == [text[2:]]

    def test_windows_pattern_sees_a_separator_past_a_glued_drive_prefix(self):
        assert image_refs._WINDOWS_PATH_RE.search("C:/x/a.png和C:/y/final.txt") is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("see /tmp/a.png.Then we moved on", "/tmp/a.png"),
            ("see /tmp/a.png.backup now", None),
            ("see /tmp/a.png.v2 now", None),
            (r"see C:\x\a.png.Then we moved on", r"C:\x\a.png"),
            (r"see C:\x\a.png.backup now", None),
        ],
    )
    def test_a_period_before_a_capital_letter_ends_the_path(self, text, expected):
        # Extensions are lowercase and sentences start upper, so `.Then` is
        # prose glued to the path while `.backup` is a longer file name.
        rx = image_refs._WINDOWS_PATH_RE if text.startswith("see C:") else _POSIX_PATH_RE
        m = rx.search(text)

        assert (m.group(1) if m else None) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # A URL scheme's colon is not a drive colon: the body must not run
            # from a non-image path through prose into the URL's image suffix.
            (r"C:\Users\me\report.md and the banner is at https://example.com/logo.png", []),
            (
                r"C:\Users\me\shot.png and the banner is at https://example.com/logo.png",
                [r"C:\Users\me\shot.png"],
            ),
            ("C:/x/a.png和https://example.com/b.png", ["C:/x/a.png"]),
            ("C:/x/a.png和D:/y/b.png", ["C:/x/a.png和D:/y/b.png"]),
        ],
    )
    def test_windows_pattern_takes_only_a_standalone_drive_colon(self, text, expected):
        assert [m.group(1) for m in image_refs._WINDOWS_PATH_RE.finditer(text)] == expected

    def test_windows_pattern_lets_a_separated_drive_start_its_own_path(self):
        # Only a drive letter GLUED to the token continues it: after whitespace
        # the second drive is a path of its own, so a document path followed by
        # a picture yields exactly the picture and the scrubber keeps the prose.
        text = r"C:\docs\readme.txt and D:\tmp\shot.png"

        assert [m.group(1) for m in image_refs._WINDOWS_PATH_RE.finditer(text)] == [
            r"D:\tmp\shot.png"
        ]

    def test_posix_pattern_bounds_its_scans(self):
        # Body and guard scans each stop at the shared budget: a path past it
        # stays text, and a glued directory run past it is out of sight, so the
        # prefix stands.
        assert _POSIX_PATH_RE.search(f"/{'d' * 512}.png") is not None
        assert _POSIX_PATH_RE.search(f"/{'d' * 513}.png") is None
        assert _POSIX_PATH_RE.search(f"see /tmp/a.png{'版' * 512}/x") is None
        beyond = _POSIX_PATH_RE.search(f"see /tmp/a.png{'版' * 513}/x")
        assert beyond is not None and beyond.group(1) == "/tmp/a.png"

    @pytest.mark.parametrize("glue", ["\u2014", "\u2013", "\u2192", "\u2022", "\u30fb"])
    def test_windows_pattern_separates_two_paths_glued_by_punctuation(self, glue):
        text = f"look C:/x/a.png{glue}C:/y/b.png"

        assert [m.group(1) for m in image_refs._WINDOWS_PATH_RE.finditer(text)] == [
            "C:/x/a.png",
            "C:/y/b.png",
        ]

    def test_windows_pattern_starts_no_path_after_a_symbol(self):
        assert image_refs._WINDOWS_PATH_RE.search("look C:/x/a.png\U0001F4C1C:/y/b.png") is None

    def test_windows_pattern_reads_a_backslash_glued_pair_as_one_token(self):
        # A backslash is a Windows path character, so the glued spelling is one
        # (nonexistent) path, as a drive colon glued into a token continues it.
        text = r"look C:\x\a.png\C:\y\b.png"

        assert [m.group(1) for m in image_refs._WINDOWS_PATH_RE.finditer(text)] == [text[5:]]

    def test_windows_pattern_bounds_the_unc_host_scan(self):
        # The host segment is a forward scan like any other, so it stops at the
        # shared budget instead of walking an arbitrarily long run.
        assert image_refs._WINDOWS_PATH_RE.search(f"//{'h' * 512}/share/a.png") is not None
        assert image_refs._WINDOWS_PATH_RE.search(f"//{'h' * 513}/share/a.png") is None

    @pytest.mark.parametrize(
        ("rx", "a", "b"),
        [
            (_POSIX_PATH_RE, "/x/a.png", "/y/b.png"),
            (image_refs._WINDOWS_PATH_RE, "C:/x/a.png", "C:/y/b.png"),
        ],
    )
    def test_masked_code_separates_two_paths_on_both_grammars(self, rx, a, b):
        # The scrubber writes NUL over code before it scans, so a code span
        # between two paths is a boundary on both sides, as whitespace is.
        text = f"look {a}\x00\x00\x00{b}"

        assert [m.group(1) for m in rx.finditer(text)] == [a, b]


class TestUncProbeGate:
    """UNC-shaped candidates must never reach the filesystem un-gated.

    ``Path.is_file()`` on a UNC path makes Windows open an SMB connection to
    the named host, so untrusted message text (``\\\\evil\\share\\x.png`` or
    ``//evil/share/x.png``) would trigger an outbound credential probe. Only
    UNC paths under the gateway's own attachment roots may be probed.
    """

    @pytest.mark.parametrize(
        "raw,want",
        [
            (r"\\host\share\x.png", True),
            ("//host/share/x.png", True),
            (r"C:\Users\me\x.png", False),
            ("C:/Users/me/x.png", False),
            ("/tmp/x.png", False),
        ],
    )
    def test_unc_shape_detection(self, raw, want):
        assert hooks.is_unc_shape(raw) is want

    def test_attacker_host_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiro_crew.config.paths.peek_data_home", lambda: tmp_path / "home"
        )
        assert hooks.unc_probe_allowed(r"\\evil\share\x.png") is False
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False

    def test_unc_under_a_unc_data_home_is_allowed(self, monkeypatch):
        """Roaming profile: the data home ITSELF is a UNC share."""
        monkeypatch.setattr(
            "kiro_crew.config.paths.peek_data_home",
            lambda: Path(r"\\fileserver\home\me\.kiro\crew"),
        )
        allowed = hooks.unc_probe_allowed(
            r"\\fileserver\home\me\.kiro\crew\uploads\shot.png"
        )
        forward = hooks.unc_probe_allowed(
            "//fileserver/home/me/.kiro/crew/uploads/shot.png"
        )
        # normcase/normpath only fold separators and case on Windows, so the
        # cross-separator equivalence holds there; on POSIX the gate is never
        # consulted (the probe loop is os.name == "nt" scoped).
        if os.name == "nt":
            assert allowed is True
            assert forward is True

    def test_sibling_share_on_same_server_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.config.paths.peek_data_home",
            lambda: Path(r"\\fileserver\home\me\.kiro\crew"),
        )
        if os.name == "nt":
            assert hooks.unc_probe_allowed(r"\\fileserver\other\x.png") is False

    # --- kiro agents dir as a trusted UNC root ----------------------
    #
    # Forward-slash UNC spellings are used for every assertion that must hold
    # on the Linux CI box: ``normcase``/``normpath`` leave a ``//host/share``
    # spelling intact on POSIX, so the purely lexical gate behaves exactly as
    # it does on Windows. Backslash spellings only normalize on Windows, so
    # those assertions are additionally guarded like the older tests above.

    _UNC_KIRO_HOME = "//fileserver/profiles/alice/.kiro"

    def _patch_roots(self, monkeypatch, tmp_path, agents_dir):
        """Local data home + the given agents dir, isolating the new root."""
        monkeypatch.setattr("kiro_crew.config.paths.peek_data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: agents_dir)

    def test_unc_kiro_agents_dir_is_allowed(self, monkeypatch, tmp_path):
        """On a roaming-profile (UNC) home, a spec under the
        kiro agents dir passes the gate. The data home is patched LOCAL so the
        admission can only come from the new agents root."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/foo.json") is True
        if os.name == "nt":
            assert (
                hooks.unc_probe_allowed(r"\\fileserver\profiles\alice\.kiro\agents\foo.json")
                is True
            )

    def test_sibling_share_refused_for_agents_root(self, monkeypatch, tmp_path):
        """Same HOST, different share: proves the new root is prefix-anchored,
        not host-anchored."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed("//fileserver/other/agents/foo.json") is False

    def test_neighbour_directory_of_agents_root_refused(self, monkeypatch, tmp_path):
        """``agents-evil`` is not under ``agents``: the comparison must require
        a separator boundary, not a bare ``startswith``."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents-evil/foo.json") is False

    def test_local_agents_root_is_not_admitted(self, monkeypatch, tmp_path):
        """On an ordinary local home the added root admits nothing: UNC
        candidates stay refused (the ``is_unc_shape(rootn)`` skip), and the
        root itself is not admitted by exact match either -- the assertion
        that keeps the skip from being dropped for the new root."""
        local_agents = tmp_path / ".kiro" / "agents"
        self._patch_roots(monkeypatch, tmp_path, local_agents)
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert hooks.unc_probe_allowed(r"\\evil\share\x.png") is False
        assert hooks.unc_probe_allowed(str(local_agents)) is False
        assert hooks.unc_probe_allowed(str(local_agents / "foo.json")) is False

    def test_broken_agents_root_fails_safe(self, monkeypatch):
        """``kiro_agents_dir()`` raising (broken home resolution) must not take
        the gate down: the always-computable roots still apply and the function
        stays total."""

        def boom():
            raise RuntimeError("no usable home")

        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", boom)
        monkeypatch.setattr(
            "kiro_crew.config.paths.peek_data_home",
            lambda: Path("//fileserver/home/me/.kiro/crew"),
        )
        assert hooks.unc_probe_allowed("//fileserver/home/me/.kiro/crew/uploads/x.png") is True
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False

    def test_agents_root_is_resolved_once_per_configuration(self, monkeypatch, tmp_path):
        """``kiro_agents_dir()`` resolves
        ``KIRO_HOME`` (``Path.resolve()`` -- filesystem I/O, SMB on a UNC
        override), so the gate must NOT consult it per check. The root is
        memoized on the raw ``KIRO_HOME`` + accessor identity; repeated gate
        checks under one configuration hit the accessor exactly once."""
        calls: list[int] = []

        def counting_agents_dir():
            calls.append(1)
            return Path(self._UNC_KIRO_HOME + "/agents")

        monkeypatch.setattr("kiro_crew.config.paths.peek_data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", counting_agents_dir)
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/foo.json") is True
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/agents/bar.json") is True
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert len(calls) == 1

    def test_data_home_is_resolved_once_per_configuration(self, monkeypatch, tmp_path):
        """The data home is the OTHER resolving root, and it needs the same memo.

        ``data_home()`` is cheap only on its default-home branch. With
        ``KIROCREW_HOME`` set it calls ``_valid_override_home()`` first, on
        every call, which does ``Path(override).expanduser().resolve()`` --
        and a roaming profile is precisely when that override names a share, so
        the per-check cost is an SMB round-trip. ``config_dir()``'s own memo
        does not cover it: that memo sits behind the predicate.

        Same contract as the agents root above, asserted the same way: the
        accessor is consulted once per configuration, not once per check.
        """
        calls: list[int] = []

        def counting_data_home():
            calls.append(1)
            return Path(self._UNC_KIRO_HOME + "/crew")

        monkeypatch.setattr("kiro_crew.config.paths.peek_data_home", counting_data_home)
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: tmp_path / "agents")
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/crew/uploads/a.png") is True
        assert hooks.unc_probe_allowed(self._UNC_KIRO_HOME + "/crew/uploads/b.png") is True
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert len(calls) == 1

    def test_agents_root_failure_is_memoized_not_retried(self, monkeypatch, tmp_path):
        """A failing resolution is memoized as root-absent for the
        configuration: the gate must not re-run a resolve that can block on an
        SMB timeout on every subsequent check."""
        calls: list[int] = []

        def boom():
            calls.append(1)
            raise RuntimeError("no usable home")

        monkeypatch.setattr("kiro_crew.config.paths.peek_data_home", lambda: tmp_path / "home")
        monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", boom)
        assert hooks.unc_probe_allowed("//evil/share/x.png") is False
        assert hooks.unc_probe_allowed("//evil/share/y.png") is False
        assert len(calls) == 1

    class _NtOs:
        """Proxy for the ``os`` module that reports ``name == "nt"``.

        Installed onto ``hooks`` only (``monkeypatch.setattr(hooks, "os", ...)``)
        so ``validate_file_path``'s Windows-only UNC branch runs, while every
        other module keeps the real ``os``: a GLOBAL ``os.name`` patch makes
        ``pathlib.Path.home()`` raise ``RuntimeError`` on this POSIX box, which
        crashes ``security.is_sensitive_path``'s cache-key derivation.

        ``path.realpath`` is additionally stubbed to a LEXICAL no-op (review
        finding): the simulated-Windows tests must never hand the synthetic
        UNC host to a real resolver -- on a Windows host that resolution is
        itself the outbound SMB probe the gate exists to prevent.
        """

        name = "nt"

        class _LexicalPath:
            @staticmethod
            def realpath(p):
                return p

            def __getattr__(self, attr):
                return getattr(os.path, attr)

        path = _LexicalPath()

        def __getattr__(self, attr):
            return getattr(os, attr)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="simulates the Windows resolver on POSIX; on a real Windows host "
        "the downstream resolution of the synthetic UNC host would itself be "
        "the outbound SMB probe the gate exists to prevent",
    )
    def test_validate_file_path_accepts_unc_agents_spec(self, monkeypatch, tmp_path):
        """The surrounding gate: hooks' view of ``os`` is shimmed to report
        ``"nt"`` so ``validate_file_path`` consults the UNC gate on the Linux
        CI box; the gate itself is lexical, and the forward-slash spelling is
        the one ``normpath`` preserves on POSIX."""
        self._patch_roots(monkeypatch, tmp_path, Path(self._UNC_KIRO_HOME + "/agents"))
        monkeypatch.setattr(hooks, "os", self._NtOs())
        assert hooks.validate_file_path(self._UNC_KIRO_HOME + "/agents/foo.json") is not None
        assert hooks.validate_file_path("//evil/share/x.png") is None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="simulates the Windows resolver on POSIX; on a real Windows host "
        "the downstream resolution of the synthetic UNC host would itself be "
        "the outbound SMB probe the gate exists to prevent",
    )
    def test_read_agent_spec_under_unc_agents_dir(self, monkeypatch, tmp_path):
        """A spec under a UNC-shaped kiro agents dir
        parses instead of silently reading as absent (``None``).

        Windows-resolver simulation, stated per the task spec: hooks' view of
        ``os`` is shimmed to report ``"nt"`` (see ``_NtOs``) so
        ``validate_file_path`` consults the UNC gate, and the spec path reaches
        the reader through a stub whose ``resolve`` keeps the UNC spelling --
        on a real roaming-profile host ``Path.resolve`` keeps a UNC path UNC,
        while on this POSIX box it would collapse the leading ``//`` and dodge
        the gate entirely. The double-slash spelling of a real local file IS
        openable on Linux, so everything downstream of the gate (realpath,
        ``O_NOFOLLOW`` open, JSON parse) runs for real.
        """
        from kiro_crew.agent_discovery import _read_agent_spec

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "foo.json").write_text('{"name": "foo", "model": "m1"}', encoding="utf-8")
        unc_agents = "/" + str(agents)  # //local/... -- UNC-shaped
        unc_spec = unc_agents + "/foo.json"

        class _WindowsResolvedPath:
            """Duck-typed spec path whose ``resolve`` keeps the UNC spelling."""

            name = "foo.json"

            def resolve(self, strict=False):
                return Path(unc_spec)

        self._patch_roots(monkeypatch, tmp_path, Path(unc_agents))
        monkeypatch.setattr(hooks, "os", self._NtOs())
        # The real Windows descriptor witness keeps this as a UNC path. Linux's
        # /proc witness canonicalizes the openable ``//tmp`` stand-in to
        # ``/tmp``; model the Windows result so this cross-platform fixture
        # exercises the intended validated-path/opened-path equality.
        monkeypatch.setattr(hooks, "_fd_real_path", lambda _fd: unc_spec)
        assert _read_agent_spec(_WindowsResolvedPath()) == {"name": "foo", "model": "m1"}

    def test_untrusted_unc_text_is_never_stat_probed_on_windows(self, monkeypatch):
        """End-to-end: build_prompt_blocks must not touch the filesystem for a
        refused UNC candidate."""
        if os.name != "nt":
            pytest.skip("probe loop is Windows-scoped")
        probed: list[str] = []
        real_is_file = Path.is_file

        def spy(self):  # type: ignore[no-untyped-def]
            probed.append(str(self))
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", spy)
        prompt_blocks.build_prompt_blocks(
            r"look at \\evil\share\x.png please",
            attachments=[PromptAttachment(path=r"\\evil\share\x.png")],
        )
        assert not any("evil" in p for p in probed)

    def test_active_pattern_follows_the_host(self):
        expected = (
            image_refs._WINDOWS_PATH_RE if os.name == "nt" else image_refs._POSIX_PATH_RE
        )
        assert image_refs._PATH_RE is expected

    def test_natively_produced_path_is_inlined_on_this_host(self, tmp_path):
        """End-to-end guard against the gap Windows CI exposed.

        ``tmp_path`` yields backslash paths on Windows, which the POSIX-only
        grammar could not match -- so every image silently stayed prose on a
        supported, CI-tested platform.
        """
        p = _png(tmp_path, "native.png")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]


class TestPathsAdjacentToUrls:
    r"""A URL in the message must not disturb the appended image path.

    ``slack/events.py`` emits ``"<user text>\n<image path>"``. The path grammar
    (``kiro_crew.image_refs``, read by the history scrubber) must not let a
    leading URL chain across the newline into the path: with ``\s`` in its
    character class, ``see https://x.com/d\n/tmp/a.png`` matched as the single
    nonexistent path ``//x.com/d\n/tmp/a.png``, and ANY Slack message
    containing a link silently lost its image. The builder reads no grammar: it
    rewrites the KNOWN attachment path wherever it sits, so these shapes must
    all yield the block and the marker regardless of the URL beside them; the
    grammar's own guards are pinned directly.
    """

    def test_url_then_newline_then_path(self, tmp_path):
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs\n{p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "see https://example.com/docs\n[image: shot.png]"

    def test_url_then_space_then_path(self, tmp_path):
        """Same shape on one line."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see https://example.com/docs {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert blocks[0]["text"] == "see https://example.com/docs [image: shot.png]"

    def test_grammar_does_not_chain_a_url_into_the_path(self):
        """The scrubber's grammar must find the path, not a URL-prefixed span."""
        assert _POSIX_PATH_RE.findall("see https://example.com/docs\n/tmp/a.png") == ["/tmp/a.png"]
        assert _POSIX_PATH_RE.findall("see https://example.com/docs /tmp/a.png") == ["/tmp/a.png"]

    def test_url_ending_in_an_image_suffix_is_not_a_path(self):
        """A remote URL is not a local file and must not even be a candidate."""
        assert _POSIX_PATH_RE.search("see https://example.com/logo.png") is None

    def test_multiple_urls_do_not_break_a_trailing_path(self, tmp_path):
        p = _png(tmp_path)
        text = f"a https://x.com/1 b http://y.com/2/z\n{p}"
        blocks = build_prompt_blocks(text, attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_two_images_after_a_url_both_survive(self, tmp_path):
        a = _distinct_png(tmp_path, "a.png", 1)
        b = _distinct_png(tmp_path, "b.png", 2)
        blocks = build_prompt_blocks(f"ref https://x.com/d\n{a}\n{b}", attachments=_att(a, b))
        assert [x["type"] for x in blocks] == ["text", "image", "image"]

    def test_newline_is_not_part_of_a_path(self):
        r"""``\n`` must never be inside a captured path."""
        m = _POSIX_PATH_RE.search("/tmp/one\n/tmp/two.png")
        assert m is not None and "\n" not in m.group(1)

    def test_filename_with_spaces_still_matches(self, tmp_path):
        """Horizontal whitespace stays allowed -- this is why `\\s` was used."""
        p = _png(tmp_path, "my shot.png")
        blocks = build_prompt_blocks(f"look at {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_path_inside_markdown_image_syntax(self, tmp_path):
        """The dashboard emits `![image](<path>)`."""
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"![image]({p})", attachments=_att(p))
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
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        w, h = _decoded_size(blocks[1])
        # Longest edge capped, aspect preserved (4000x3000 -> 2000x1500).
        assert max(w, h) <= MAX_IMAGE_EDGE_PX
        assert (w, h) == (2000, 1500)

    def test_portrait_image_downscaled_on_its_long_edge(self, tmp_path):
        p = _sized_image(tmp_path, 1000, 4000)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert _decoded_size(blocks[1]) == (500, 2000)

    def test_image_within_cap_is_byte_identical(self, tmp_path):
        """At/under the cap the original bytes ride through untouched -- no
        needless re-encode (which would recompress and drift quality)."""
        p = _sized_image(tmp_path, 1600, 1200)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_custom_edge_param_is_honoured(self, tmp_path):
        p = _sized_image(tmp_path, 40, 20)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_edge=10)
        assert _decoded_size(blocks[1]) == (10, 5)

    def test_edge_zero_disables_downscale(self, tmp_path):
        """The escape hatch: a non-positive cap leaves bytes exactly as-is."""
        p = _sized_image(tmp_path, 4000, 10)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_edge=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_oversized_jpeg_keeps_jpeg_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_phone_photo_mpo_keeps_jpeg_both_ways(self, tmp_path):
        """A JPEG carrying MPF data (phone photo) decodes as Pillow format
        ``MPO``. Within the cap it rides through byte-identical as
        ``image/jpeg``; over the cap it is re-encoded as JPEG, not as the far
        larger PNG a format outside the table converts to."""
        pil = pytest.importorskip("PIL.Image")

        def _mpo(path, w, h):
            primary = pil.new("RGB", (w, h), (10, 20, 30))
            second = pil.new("RGB", (w // 2, h // 2), (40, 50, 60))
            primary.save(path, format="MPO", save_all=True, append_images=[second])
            with pil.open(path) as im:
                assert im.format == "MPO"
            return path

        small = _mpo(tmp_path / "portrait.jpg", 800, 600)
        blocks = build_prompt_blocks(f"see {small}", attachments=_att(small))
        assert blocks[1]["mimeType"] == "image/jpeg"
        assert base64.b64decode(blocks[1]["data"]) == small.read_bytes()

        big = _mpo(tmp_path / "wide.jpg", 3000, 1000)
        blocks = build_prompt_blocks(f"see {big}", attachments=_att(big))
        assert blocks[1]["mimeType"] == "image/jpeg"
        out = base64.b64decode(blocks[1]["data"])
        assert out.startswith(b"\xff\xd8\xff")
        with pil.open(io.BytesIO(out)) as im:
            assert im.format == "JPEG"
            assert im.size == (2000, 667)

    def test_oversized_gif_becomes_png_still(self, tmp_path):
        """GIF re-encodes to a PNG first frame: the vision model reads frame 0
        only, and palette rescaling is lossy, so a lossless still is faithful."""
        p = _sized_image(tmp_path, 3000, 100, fmt="GIF")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert blocks[1]["mimeType"] == "image/png"
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_bmp_is_capped(self, tmp_path):
        # A thin BMP stays under the 10 MB byte gate yet over the edge cap, so
        # the downscale (not the byte gate) is what fires.
        p = _sized_image(tmp_path, 2400, 80, fmt="BMP")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX

    def test_oversized_webp_keeps_webp_mime(self, tmp_path):
        p = _sized_image(tmp_path, 3000, 1000, fmt="WEBP")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
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
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
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
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        with pil.open(io.BytesIO(base64.b64decode(blocks[1]["data"]))) as out:
            w, h = out.size
            assert 0x0112 not in out.getexif()  # tag baked away, not carried
            assert h > w  # rotation applied to the pixels -> portrait
            assert max(w, h) <= MAX_IMAGE_EDGE_PX


def _noise_image(tmp_path, w, h, name="noise.png", fmt="PNG"):
    """An image that resists compression, so its encoded size tracks pixel count.

    Built from one bytes buffer rather than a list of per-pixel tuples: at
    2400x2400 that list is 5.76M three-tuples, ~390 MiB of transient peak for a
    17 MB image, and it was the largest single-test excursion in the suite. A
    worker's high-water mark is what the memory budget has to reserve for, so a
    spike nobody sees still costs every other worker headroom.
    """
    pil = pytest.importorskip("PIL.Image")
    rnd = random.Random(1234)
    img = pil.frombytes("RGB", (w, h), rnd.randbytes(w * h * 3))
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
        from kiro_crew.imaging import _b64_len

        for n in (0, 1, 2, 3, 4, 100, 1023, 4096):
            assert _b64_len(n) == len(base64.b64encode(b"x" * n))

    def test_image_inside_dimension_cap_but_over_budget_is_shrunk(self, tmp_path):
        """The exact production defect: dimensions are already legal, so the
        dimension pass is a no-op, yet the payload still exceeds the wire limit.
        """
        p = _noise_image(tmp_path, 900, 900)
        budget = len(base64.b64encode(p.read_bytes())) // 3
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_b64_bytes=budget)
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert len(blocks[1]["data"]) <= budget
        # Shrunk, not passed through: the bug was inlining the original here.
        assert base64.b64decode(blocks[1]["data"]) != p.read_bytes()
        assert max(_decoded_size(blocks[1])) < 900

    def test_image_within_budget_is_byte_identical(self, tmp_path):
        p = _sized_image(tmp_path, 100, 80)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_unshrinkable_image_falls_back_to_a_path(self, tmp_path):
        """Fail CLOSED: a budget no rendition can meet must leave the path as
        text rather than inline a payload the backend will reject forever."""
        p = _noise_image(tmp_path, 400, 400)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_b64_bytes=8)
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_zero_budget_disables_the_check(self, tmp_path):
        p = _noise_image(tmp_path, 120, 120)
        original = p.read_bytes()
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_b64_bytes=0)
        assert base64.b64decode(blocks[1]["data"]) == original

    def test_budget_applies_after_the_dimension_cap(self, tmp_path):
        """Both caps hold at once -- shrinking for bytes must not reintroduce an
        over-dimension rendition, and vice versa."""
        p = _noise_image(tmp_path, 2400, 2400, name="big.jpg", fmt="JPEG")
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_b64_bytes=400_000)
        assert max(_decoded_size(blocks[1])) <= MAX_IMAGE_EDGE_PX
        assert len(blocks[1]["data"]) <= 400_000

    def test_shrink_floor_is_respected(self, tmp_path):
        """The loop never grinds an image below the usable-accuracy floor; it
        gives up and hands back a path instead."""
        p = _noise_image(tmp_path, 1000, 1000)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p), max_image_b64_bytes=64)
        assert [b["type"] for b in blocks] == ["text"]


class TestSummarizePromptStructure:
    """Content-free outbound-request STRUCTURE diagnostics.

    The summary lets an operator tell a stale/invalid model id apart from a
    structurally malformed payload the next time a turn is rejected as
    "Improperly formed request" -- WITHOUT ever recording message content, so
    it is safe to log even though the kiro-cli data dir holds SSO tokens.
    """

    def test_counts_text_and_image_blocks(self):
        blocks = [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "x", "mimeType": "image/png"},
            {"type": "image", "data": "y", "mimeType": "image/png"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 3
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["image"] == 2

    def test_counts_empty_text_blocks(self):
        blocks = [
            {"type": "text", "text": "real content"},
            {"type": "text", "text": "   "},
            {"type": "text", "text": ""},
            {"type": "text", "text": "\n\t"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 4
        # Three of the four text blocks are blank/whitespace-only.
        assert out["empty_text_blocks"] == 3

    def test_text_block_missing_text_key_counts_as_empty(self):
        """A ``{"type": "text"}`` with no ``text`` key at all is as
        structurally suspect as one whose ``text`` is a blank string, so it
        folds into the empty count alongside present-but-blank text. This is
        exactly the malformed-payload signal the diagnostic exists to surface.
        """
        blocks = [
            {"type": "text", "text": "real content"},
            {"type": "text"},  # no text key at all
            {"type": "text", "text": None},  # present but not a string
            {"type": "text", "text": "   "},  # present but blank
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 4
        assert out["type_counts"]["text"] == 4
        # The missing key, the non-string, and the blank string all count.
        assert out["empty_text_blocks"] == 3

    def test_reports_tool_use_and_tool_result_imbalance(self):
        """A tool_result with no matching tool_use is the classic malformed
        transcript; the two top-level counts expose the imbalance directly."""
        blocks = [
            {"type": "tool_use", "id": "1"},
            {"type": "tool_use", "id": "2"},
            {"type": "tool_result", "tool_use_id": "1"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["tool_use"] == 2
        assert out["tool_result"] == 1
        assert out["tool_use"] != out["tool_result"]
        assert out["type_counts"]["tool_use"] == 2
        assert out["type_counts"]["tool_result"] == 1

    def test_reports_total_byte_size(self):
        blocks = [{"type": "text", "text": "hello world"}]
        out = summarize_prompt_structure(blocks)
        assert out["total_bytes"] == len(json.dumps(blocks))
        assert out["total_bytes"] > 0

    def test_summary_contains_no_message_content(self):
        """A content sentinel placed in a block's
        text must not appear anywhere in repr() of the summary."""
        sentinel = "SENTINEL_SECRET_TOKEN_ghp_deadbeef"
        blocks = [
            {"type": "text", "text": f"please look at {sentinel} now"},
            {"type": "image", "data": sentinel, "mimeType": "image/png"},
            {"type": "tool_use", "id": sentinel, "input": {"arg": sentinel}},
        ]
        out = summarize_prompt_structure(blocks)
        assert sentinel not in repr(out)
        # And the redaction did not cost the shape: still three blocks.
        assert out["block_count"] == 3

    def test_unknown_block_types_fold_into_other(self):
        blocks = [
            {"type": "text", "text": "x"},
            {"type": "audio", "data": "z"},
            {"type": "resource_link", "uri": "file:///x"},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["other"] == 2

    def test_malformed_block_list_does_not_raise(self):
        """A diagnostics helper must never break a live turn: odd inputs yield
        a partial/minimal summary instead of propagating an exception."""
        for bad in (
            [{"nonsense": 1}, None],
            "not a list at all",
            None,
            42,
            [None, None, None],
            [{"type": "text"}],  # text key missing
        ):
            out = summarize_prompt_structure(bad)
            assert isinstance(out, dict)
            assert "block_count" in out
            assert "type_counts" in out
            assert "total_bytes" in out

    def test_unserializable_content_still_yields_counts(self):
        """Content that json.dumps cannot serialize must not sink the summary:
        structural counts survive and total_bytes reports -1 (unknown)."""

        class _Unserializable:
            pass

        blocks = [
            {"type": "text", "text": "ok"},
            {"type": "image", "data": _Unserializable()},
        ]
        out = summarize_prompt_structure(blocks)
        assert out["block_count"] == 2
        assert out["type_counts"]["text"] == 1
        assert out["type_counts"]["image"] == 1
        # default=str is applied, so this actually serializes; but if a type
        # ever defeats even that, total_bytes falls back to -1 rather than
        # raising. Assert the summary is coherent either way.
        assert isinstance(out["total_bytes"], int)

    def test_empty_block_list(self):
        out = summarize_prompt_structure([])
        assert out["block_count"] == 0
        assert out["type_counts"] == {}
        assert out["empty_text_blocks"] == 0
        assert out["tool_use"] == 0
        assert out["tool_result"] == 0
        assert out["total_bytes"] == len(json.dumps([]))

    def test_non_list_argument_reports_coherent_size(self):
        """A non-list/tuple argument normalises to an empty block list, and
        ``total_bytes`` measures THAT normalised list -- not the raw argument.
        So the size stays coherent with the counts (``block_count: 0`` reads as
        an empty ``[]``) instead of "0 blocks, N bytes" describing a payload the
        counts claim is empty. This is exactly the malformed-argument path the
        diagnostic exists to serve."""
        empty_bytes = len(json.dumps([]))
        for bad in ("not a list at all", {"type": "text", "text": "x"}, 42, None):
            out = summarize_prompt_structure(bad)
            assert out["block_count"] == 0
            assert out["total_bytes"] == empty_bytes


class TestLinkedAncestorGate:
    """On Windows, a candidate beneath a linked ANCESTOR must be refused
    BEFORE the first filesystem probe -- ``is_file()`` resolves every
    ancestor, so the probe itself would traverse the link and open the SMB
    connection the lexical UNC screen exists to prevent.

    NOTE: under the module-local os patch the candidates come from the
    attachment list and stay host-native; the Windows CI shard exercises real
    backslash shapes via ``tmp_path`` (see
    ``test_natively_produced_path_is_inlined_on_this_host``)."""

    def _windows(self, monkeypatch):
        import types

        # Patch ONLY the predicate module's view of os (the gates read os.name
        # and os.path there; _PATH_RE was chosen at import time) --
        # patching the global os.name would make pathlib dispatch WindowsPath
        # everywhere on a POSIX test host.
        monkeypatch.setattr(prompt_attachments, "os", types.SimpleNamespace(name="nt", path=os.path))

    def test_linked_ancestor_candidate_is_refused_before_any_probe(self, tmp_path, monkeypatch):
        """Ordering IS the property: the leaf probe is wired to explode, so a
        regression that probes first fails loudly instead of silently."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_attachments, "first_linked_ancestor", lambda _p: str(tmp_path))

        def _boom(self):  # type: ignore[no-untyped-def]  # pragma: no cover
            raise AssertionError("is_file ran before the ancestor walk")

        monkeypatch.setattr(Path, "is_file", _boom)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        # The candidate is skipped, not inlined; the text keeps the path so
        # the turn still carries a usable reference.
        assert [b["type"] for b in blocks] == ["text"]
        assert str(p) in blocks[0]["text"]

    def test_bypassing_the_guard_restores_the_probe(self, tmp_path, monkeypatch):
        """Mutation check: with the walk reporting no link, the same candidate
        is probed and inlined again -- so the refusal above is attributable to
        the guard, not to some other screen."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_attachments, "first_linked_ancestor", lambda _p: None)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_the_walk_is_not_consulted_on_posix(self, tmp_path, monkeypatch):
        """macOS /tmp and /var are symlinks to /private/*; an unconditional
        walk would refuse every image staged in a temp dir there."""
        if os.name == "nt":
            pytest.skip("gate is active on Windows by design")

        def _boom(_p):  # pragma: no cover
            raise AssertionError("ancestor walk ran on POSIX")

        monkeypatch.setattr(prompt_attachments, "first_linked_ancestor", _boom)
        p = _png(tmp_path)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text", "image"]

    def test_a_leaf_link_is_refused_before_the_probe(self, tmp_path, monkeypatch):
        """The walk deliberately excludes the leaf, so the leaf gets its own
        junction-aware check -- is_file() FOLLOWS a final-component link."""
        p = _png(tmp_path)
        self._windows(monkeypatch)
        monkeypatch.setattr(prompt_attachments, "first_linked_ancestor", lambda _p: None)
        monkeypatch.setattr(prompt_attachments, "is_link_or_junction", lambda _p: True)

        def _boom(self):  # type: ignore[no-untyped-def]  # pragma: no cover
            raise AssertionError("is_file ran before the leaf link check")

        monkeypatch.setattr(Path, "is_file", _boom)
        blocks = build_prompt_blocks(f"see {p}", attachments=_att(p))
        assert [b["type"] for b in blocks] == ["text"]
