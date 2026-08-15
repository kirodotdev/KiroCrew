"""WhatsApp outbound rendering tests: Markdown→WhatsApp dialect + chunking."""

from __future__ import annotations

from kiro_crew.whatsapp.renderer import (
    WHATSAPP_CHUNK_LIMIT,
    render_chunks,
    to_whatsapp_text,
)


class TestDialect:
    def test_bold_and_strike_map_to_whatsapp_markers(self):
        assert to_whatsapp_text("**hi** and ~~gone~~") == "*hi* and ~gone~"
        assert to_whatsapp_text("__also bold__") == "*also bold*"

    def test_headings_become_bold_lines(self):
        assert to_whatsapp_text("## Plan for today") == "*Plan for today*"

    def test_bullets_become_dots(self):
        assert to_whatsapp_text("- one\n* two") == "• one\n• two"

    def test_links_keep_label_and_url(self):
        out = to_whatsapp_text("[docs](https://example.com/d)")
        assert out == "docs (https://example.com/d)"
        bare = to_whatsapp_text("[https://example.com](https://example.com)")
        assert bare == "https://example.com"

    def test_code_fences_survive_and_contents_untouched(self):
        src = "```python\n**not bold** - not a bullet\n```"
        out = to_whatsapp_text(src)
        assert "**not bold** - not a bullet" in out
        assert out.startswith("```") and out.endswith("```")

    def test_unterminated_fence_is_closed(self):
        out = to_whatsapp_text("```\ncode")
        assert out.count("```") == 2

    def test_blank_runs_collapse(self):
        assert to_whatsapp_text("a\n\n\n\nb") == "a\n\nb"


class TestChunking:
    def test_fits_in_one_message(self):
        assert render_chunks("hello") == ["hello"]

    def test_empty_yields_nothing(self):
        assert render_chunks("   ") == []

    def test_splits_at_block_boundaries(self):
        para = "x" * 3000
        chunks = render_chunks(f"{para}\n\n{para}", limit=4096)
        assert len(chunks) == 2
        assert chunks[0] == para and chunks[1] == para

    def test_oversized_block_is_hard_split(self):
        blob = "y" * (WHATSAPP_CHUNK_LIMIT + 100)
        chunks = render_chunks(blob)
        assert len(chunks) == 2
        assert "".join(chunks) == blob
        assert all(len(c) <= WHATSAPP_CHUNK_LIMIT for c in chunks)

    def test_every_chunk_respects_the_cap(self):
        text = "\n\n".join(f"paragraph {i} " + "z" * 900 for i in range(20))
        chunks = render_chunks(text, limit=4096)
        assert len(chunks) > 1
        assert all(len(c) <= 4096 for c in chunks)

    def test_code_fence_kept_intact_when_it_fits(self):
        code = "```\n" + "\n".join(f"line {i}" for i in range(50)) + "\n```"
        text = ("intro " * 600) + "\n\n" + code
        chunks = render_chunks(text, limit=4096)
        fenced = [c for c in chunks if "```" in c]
        assert len(fenced) == 1
        assert fenced[0].count("```") == 2
