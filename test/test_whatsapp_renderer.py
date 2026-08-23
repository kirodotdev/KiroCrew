"""WhatsApp outbound rendering tests: Markdown→WhatsApp dialect + chunking."""

from __future__ import annotations

import asyncio

from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.whatsapp.renderer import (
    WHATSAPP_CHUNK_LIMIT,
    display_safe_text,
    render_chunks,
    render_chunks_off_loop,
    to_whatsapp_text,
)

#: Split across the assertion so this file never holds the contiguous key: an
#: absence assertion against a literal the source already contains proves nothing
#: about the literal a scanner would have to match.
KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def shown(text: str) -> str:
    """*text* as a WhatsApp client renders it, with its markup consumed.

    Asserting on the raw bytes is the mistake this whole module exists to
    prevent: ``AKIA*I*OSFODNN7EXAMPLE`` passes ``KEY not in out`` while the
    reader sees the key. The delivered form has to be collapsed first, by the
    same canonicaliser the screen scans through.
    """
    return canonicalize_display(text)


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


class TestDisplaySafety:
    """WhatsApp is the markup-CONSUMING channel whose own converter rewrites the
    delimiters, so a credential the driver's byte-level scan could not see is
    reassembled here and rendered as a key.
    """

    def test_a_credential_split_by_markup_never_renders_as_a_key(self):
        """``AKIA**I**…`` matches no credential pattern as written, survives the
        driver's stream scan, becomes ``AKIA*I*…`` in the dialect, and is an
        intact key on screen once the client eats the asterisks.
        """
        out = render_chunks(f"AKIA**I**{KEY[5:]}")
        assert out, "the reply must still be delivered"
        assert KEY not in shown("".join(out))

    def test_the_screen_runs_after_the_reductions_not_only_before(self):
        """Every reduction DELETES a span, which joins what sat on either side.

        A scan of the authored form sees ``AKIA<thinking>…</thinking>IOSF…`` and
        matches nothing; the reduction then hands the reader the joined key. This
        is the case a pre-conversion screen alone cannot catch, so it is what pins
        the screen's position at the END of the pipeline.
        """
        out = to_whatsapp_text(f"AKIA<thinking>a note</thinking>{KEY[4:]}")
        assert KEY not in shown(out)

    def test_no_ansi_escape_reaches_the_chat(self):
        """WhatsApp renders no escapes, so one would arrive as literal noise."""
        out = to_whatsapp_text("hello \x1b[31mred\x1b[0m world")
        assert "\x1b" not in out
        assert out == "hello red world"

    def test_ansi_is_stripped_before_the_conversion_reads_a_line(self):
        """The escape has to go before line structure is read, not after.

        Every rule in ``_convert_line`` is anchored or line-shaped, so a colour
        escape in front of a ``#`` hides the heading from it and the reader gets a
        literal hash. Stripping the escape later cleans the noise and leaves the
        heading unconverted.
        """
        assert to_whatsapp_text("\x1b[1m# Plan for today") == "*Plan for today*"

    def test_a_credential_split_by_an_ansi_escape_never_renders_as_a_key(self):
        """The strip is itself a transformation that REASSEMBLES the key, so it
        has to happen on the scanned side of the screen.
        """
        out = to_whatsapp_text(f"AKIA\x1b[0m{KEY[4:]}")
        assert KEY not in shown(out)

    def test_the_dialect_only_sink_is_screened_too(self):
        """``display_safe_text`` covers the text that never passes through the
        conversion -- a rejection note, an image caption, an approval prompt --
        which WhatsApp still renders markup in.
        """
        assert KEY not in shown(display_safe_text(f"AKIA`I`{KEY[5:]}"))

    def test_inline_code_is_never_reformatted(self):
        """An inline-code span is the only way to show text WhatsApp must leave
        alone, because the dialect has no escape character.

        A path is why it matters: rewriting ``__`` inside `` `/tmp/__init__.py` ``
        yields ``/tmp/*init*.py``, defeating the span that is there for exactly
        that. ``files.py``'s rejection note is built on this guarantee.
        """
        assert to_whatsapp_text("see `/tmp/__init__.py` there") == "see `/tmp/__init__.py` there"
        assert to_whatsapp_text("`a~~b~~c`") == "`a~~b~~c`"

    def test_emphasis_spanning_a_code_span_still_converts(self):
        """The guard is on the DELIMITER positions, not on overlap. Skipping the
        whole match here would leave visible ``**`` litter, which this module
        promises never to emit.
        """
        assert to_whatsapp_text("**a `b` c**") == "*a `b` c*"

    def test_a_clean_reply_keeps_its_formatting(self):
        """The screen downgrades markup only on a message that carries a secret;
        an ordinary reply must not lose its bold.
        """
        assert to_whatsapp_text("**keep** this _formatting_") == "*keep* this _formatting_"


class TestThinkingTags:
    """A ``<thinking>`` block is a model artifact, not part of the answer, and
    WhatsApp offers nothing to fold one behind.
    """

    def test_a_complete_block_is_not_delivered(self):
        out = to_whatsapp_text("Here is the answer.\n<thinking>scratch work</thinking>")
        assert "thinking" not in out
        assert "scratch work" not in out
        assert out == "Here is the answer."

    def test_a_still_arriving_block_is_hidden(self):
        """A live frame is a real send on this channel, so a block that has not
        closed yet would be shown once AND pushed as a notification carrying the
        model's scratchpad. An unterminated opener owns the remainder, the same
        direction ``hide_local_refs`` errs in.
        """
        out = to_whatsapp_text("The answer is 42. <thinking>but wait, maybe")
        assert out == "The answer is 42."

    def test_the_answer_around_a_block_survives(self):
        assert to_whatsapp_text("A <thinking>x</thinking>B") == "A B"


class TestTables:
    """A pipe table needs a monospace grid. Every WhatsApp bubble is a
    proportional font, so raw pipes are the least readable form of the same data
    on the most mobile surface in the product.
    """

    def test_a_pipe_table_becomes_labelled_bullets(self):
        out = to_whatsapp_text("| Name | Qty |\n|---|---|\n| Bolts | 4 |\n| Nuts | 12 |")
        assert out == "• *Name:* Bolts | *Qty:* 4\n• *Name:* Nuts | *Qty:* 12"

    def test_a_table_inside_a_code_fence_is_left_alone(self):
        """Fenced content is opaque: a code sample full of pipes is not a table,
        and the reduction cannot tell the difference, so it must never see it.
        """
        src = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        assert to_whatsapp_text(src) == src

    def test_a_lone_pipe_row_is_never_swallowed(self):
        """A separator row is what makes a run of pipe lines a table.

        Keying the header off the first pipe-bearing line instead consumes a lone
        ``| a | b |`` as a header for a table that never arrives, and the line
        vanishes from the reply with nothing reporting it.
        """
        out = to_whatsapp_text("| a | b |\nordinary prose")
        assert "| a | b |" in out
        assert "ordinary prose" in out

    def test_a_header_only_table_keeps_its_rows(self):
        """No data rows means nothing to label, so the source is worth more than
        a flattened form that would drop it.
        """
        out = to_whatsapp_text("| a | b |\n|---|---|")
        assert "| a | b |" in out

    def test_a_surplus_cell_is_kept_unlabelled(self):
        """A cell the author wrote outranks the label it lacks."""
        out = to_whatsapp_text("| a |\n|---|\n| one | two |")
        assert "one" in out and "two" in out


class TestMermaid:
    """A ```mermaid fence is diagram SOURCE. WhatsApp renders no image from text,
    and the dialect drops every info string, so without a reduction the reader
    gets unlabelled monospace source and nothing that says it was a diagram.
    """

    def test_a_graph_becomes_arrows_under_a_diagram_heading(self):
        out = to_whatsapp_text("```mermaid\ngraph TD\n  A[Start] -->|go| B[Done]\n```")
        assert out == "*Diagram*\n  Start → (go) Done"

    def test_a_sequence_diagram_becomes_arrows(self):
        out = to_whatsapp_text("```mermaid\nsequenceDiagram\n  Alice->>Bob: hi\n```")
        assert out == "*Diagram*\n  Alice → Bob: hi"

    def test_an_unreadable_diagram_keeps_its_source_and_still_says_it_is_one(self):
        """Inventing a reading of a grammar the reduction does not parse is worse
        than showing the source, but the reader must still learn it was a diagram
        -- the dropped info string was the only thing that said so.
        """
        out = to_whatsapp_text('```mermaid\npie title Votes\n  "a" : 10\n```')
        assert out.startswith("*Diagram*")
        assert "pie title Votes" in out
        assert out.count("```") == 2

    def test_a_mermaid_fence_nested_in_an_outer_block_stays_literal(self):
        """The reduction reads the OPENER's info string through the shared fence
        machine, so a mermaid fence that is content of a wider block is content.
        """
        out = to_whatsapp_text("````\n```mermaid\ngraph TD\n  A --> B\n```\n````")
        assert "graph TD" in out
        assert "*Diagram*" not in out


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


class TestFenceGrammarIsTheSharedOne:
    """Each case here corrupted the message under a channel-local backtick
    counter, and is correct only because the grammar now comes from
    ``messaging.split``. A regression reads as mangled code in a user's chat.
    """

    def test_a_four_backtick_block_is_code_not_prose(self):
        """A run of four backticks opens a block. Missing that made the body
        prose, so Markdown inside a code block got rewritten to WhatsApp
        markup -- the agent shows you ``*bold*`` where it wrote ``**bold**``.
        """
        out = to_whatsapp_text("````markdown\n**keep me literal**\n````")
        assert "**keep me literal**" in out
        assert "*keep me literal*" not in out.replace("**keep me literal**", "")

    def test_a_tilde_fence_is_code_not_prose(self):
        """``~~~`` is a fence too, and its body must survive the
        strikethrough rule (``~~x~~`` -> ``~x~``) that would otherwise eat it.
        """
        out = to_whatsapp_text("~~~python\n**literal** ~~struck~~\n~~~")
        assert "**literal** ~~struck~~" in out

    def test_delimiters_normalize_to_whatsapp_and_drop_the_info_string(self):
        """WhatsApp has one code marker and no language tags, so a retained
        ``python`` would render as the block's first line of code.
        """
        out = to_whatsapp_text("```python\nx = 1\n```")
        assert out == "```\nx = 1\n```"

    def test_a_split_code_block_never_leaves_an_unbalanced_chunk(self):
        """A hard cut inside a fence must seal the chunk and reopen the next.
        An odd delimiter count renders as a monospace block that never closes,
        swallowing the rest of the conversation.
        """
        big = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(1200)) + "\n```"
        chunks = render_chunks(big, limit=500)
        assert len(chunks) > 2
        unbalanced = [i for i, c in enumerate(chunks) if c.count("```") % 2 == 1]
        assert unbalanced == []

    def test_code_body_survives_splitting_byte_exact(self):
        """The delivered chunks must still contain every authored code line."""
        lines = [f"payload_{i} = {i * 7}" for i in range(400)]
        chunks = render_chunks("```\n" + "\n".join(lines) + "\n```", limit=600)
        joined = "\n".join(chunks)
        for line in lines:
            assert line in joined

    def test_a_nested_delimiter_keeps_content_even_though_whatsapp_flattens(self):
        """WhatsApp has a single delimiter, so a block CONTAINING a ``` line
        cannot nest and renders flat. Content is still preserved byte-exact --
        which is the part that matters, because the previous behaviour silently
        rewrote it. Pinned so the flattening stays a known platform limit
        rather than turning back into content corruption.
        """
        out = to_whatsapp_text("````\n**a**\n```\nb\n```\n````")
        assert "**a**" in out
        assert "b" in out


class TestOffLoopRendering:
    def test_off_loop_matches_the_sync_result(self):
        """The async wrapper exists to keep the splitter off the gateway's one
        event loop; it must not change what is delivered.
        """
        text = "\n\n".join(f"para {i} " + "q" * 800 for i in range(12))
        assert asyncio.run(render_chunks_off_loop(text, 4096)) == render_chunks(text, 4096)


class TestChunkCountIsBounded:
    """One chunk is one WhatsApp send, so chunk COUNT is a send-rate question.

    The shared splitter reopens a cut fence with the original opener line, which
    is correct and is what preserves a language tag. Its cost is the opener's
    length, so a pathological opener (a 5,000-backtick run) makes every chunk
    almost entirely scaffolding: measured directly on the shared splitter, a
    10 KB reply becomes 5,507 chunks and 50 MB of outbound text, which on this
    channel is 5,507 messages from the operator's own number.

    This channel is immune, and NOT by accident: dialect conversion runs first
    and rewrites every delimiter to a bare three-backtick marker, so the carry
    is always four characters. The immunity therefore lives in the ORDER, which
    is invisible at the call site -- reorder to split-then-convert and the
    exposure returns silently, with no test failing. That is what these pin.
    """

    def _amp(self, body: str, limit: int) -> float:
        chunks = render_chunks(body, limit=limit)
        return sum(len(c) for c in chunks) / max(1, len(body))

    def test_a_pathological_fence_opener_does_not_amplify(self):
        body = "`" * 5000 + "\nbody\n" + "x" * 5000
        assert self._amp(body, WHATSAPP_CHUNK_LIMIT) < 2.0
        # Also at a small budget, where scaffolding dominates soonest.
        assert self._amp(body, 64) < 2.0

    def test_a_long_info_string_does_not_amplify(self):
        body = "```" + "lang" * 1000 + "\n" + "x" * 5000 + "\n```"
        assert self._amp(body, WHATSAPP_CHUNK_LIMIT) < 2.0

    def test_a_tilde_opener_does_not_amplify(self):
        body = "~" * 5000 + "\n" + "x" * 5000
        assert self._amp(body, WHATSAPP_CHUNK_LIMIT) < 2.0

    def test_conversion_normalizes_every_delimiter_to_three_backticks(self):
        """The mechanism the bound rests on, asserted directly so a failure
        names the cause rather than only the symptom.
        """
        out = to_whatsapp_text("`" * 5000 + "\nbody\n" + "`" * 5000)
        delimiters = [ln for ln in out.split("\n") if set(ln) == {"`"}]
        assert delimiters, "expected the run to be recognised as a fence"
        assert all(ln == "```" for ln in delimiters), delimiters[:3]

    def test_the_bound_holds_across_adversarial_shapes(self):
        shapes = {
            "nested": "````d\n```\nx\n```\n````\n" * 300,
            "unbreakable_line": "```\n" + "y" * 20000 + "\n```",
            "alternating": "```\na\n```\n" * 2000,
        }
        for name, body in shapes.items():
            for limit in (WHATSAPP_CHUNK_LIMIT, 500, 64):
                amp = self._amp(body, limit)
                assert amp < 2.0, f"{name} at limit {limit} amplified {amp:.1f}x"
