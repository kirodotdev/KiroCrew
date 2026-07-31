"""ACP prompt-block construction and the image capability gate.

Regression coverage for the defect where EVERY channel shipped a filesystem
path as prose: ``AcpSessionHandle.prompt`` hardcoded a single text block, while
the only image encoder lived on ``AcpClient`` -- the path ``AcpProvider.start``
replaces. An image therefore never reached the model as vision input.
"""

from __future__ import annotations

import base64
import os

import pytest

from kiro_crew.acp import prompt_blocks
from kiro_crew.acp.prompt_blocks import (
    _POSIX_PATH_RE,
    IMAGE_MEDIA_TYPES,
    MAX_IMAGE_BYTES,
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
        p = _png(tmp_path)
        monkeypatch.setattr(prompt_blocks, "safe_read_file_bytes", lambda raw: b"FROM GATE")

        blocks = build_prompt_blocks(f"look at {p}")

        assert base64.b64decode(blocks[1]["data"]) == b"FROM GATE"


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
