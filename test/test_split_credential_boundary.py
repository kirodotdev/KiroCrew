"""A cut may not hand the reader a credential neither chunk holds.

Two halves of a key, one per message, read as one key down the screen. The
per-chunk scan clears both halves, so nothing reports it: the reader sees the
key and the operator sees a clean send. The splitter therefore has to know about
credentials before it chooses a boundary, and every path that turns one chunk
into one message has to tell it, which is what the enumeration below enforces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.messaging.display_safety import (
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
)
from kiro_crew.messaging.renderer import (
    _default_redactor,
    chunk_text,
    count_redaction_tags,
    repaired_after_a_sent_tail,
)
from kiro_crew.messaging.split import (
    _WHITESPACE_RUN,
    _made_collapse_clean,
    _redact_only_the_rejoined_span,
    _rejoins_a_key,
    _under_a_safe_budget,
    bounded_for_delivery,
    chunk_utf8_bytes,
    offset_clear_of_a_sent_tail,
    repaired_for_delivery,
    split_markdown_safe,
)
from kiro_crew.telegram.renderer import (
    _split_markdown,
    _split_markdown_bounded,
    _split_markdown_table_aware,
    _table_blocks,
)
from kiro_crew.whatsapp.renderer import _redact_all, render_chunks, to_whatsapp_text

#: AWS's own documented example key, the same fixture the display-safety tests
#: use, so no real credential shape is introduced anywhere.
KEY = "AKIAIOSFODNN7EXAMPLE"
HEAD, TAIL = KEY[:10], KEY[10:]

SRC = Path(kiro_crew.__file__).parent

#: Every path that delivers one splitter chunk as one message. A boundary there
#: is a seam between two messages a reader reads in order, so each of these must
#: pass its redactor at every splitter call. Enumerated rather than discovered:
#: a path that maintains none of the state is invisible to a search for the
#: state's names, and those are exactly the ones left open.
DELIVERY_PATHS = (
    "slack/renderer.py",
    "webex/renderer.py",
    "whatsapp/renderer.py",
    "teams/renderer.py",
    "wecom/renderer.py",
    "dashboard/chat_mirror.py",
    "telegram/renderer.py",
)

SPLITTERS = frozenset({"split_markdown_safe", "chunk_utf8_bytes"})


def _rejoins(chunks: list[str]) -> bool:
    """Does any boundary hand the reader a key neither chunk holds?

    The rendered pair, because a platform drops the whitespace at a message's
    edges: the test models the screen the same way the splitter does.
    """
    return any(
        joins_to_a_credential(chunks[i].rstrip(), chunks[i + 1].lstrip(), _default_redactor)
        for i in range(len(chunks) - 1)
    )


def _on_screen(chunks: list[str]) -> str:
    """What a reader scrolling the delivered messages reads, as one string.

    Each message's edge whitespace goes, because the platform drops it, and a
    chunk holding nothing else contributes nothing at all -- which is why it
    separates nothing.
    """
    return "".join(chunk.strip() for chunk in chunks)


def _name_of(node: ast.expr) -> str:
    """The bare name a call or reference resolves to, attribute access included."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _repaired_names(tree: ast.AST) -> set[str]:
    """Names bound to the output of :func:`repaired_for_delivery` in *tree*.

    Text that predicate returns is safe to cut again by contract -- it is only
    returned once the form with every whitespace run removed reads clean -- so a
    cut of it needs no redactor, and MUST NOT take one: a credential-aware cut may
    decline to cut at all, which is exactly what a caller re-bounding to a hard
    transport cap cannot accept. Recognising the name keeps the gate closed rather
    than granting the file a blanket exception.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _name_of(node.value.func) != "repaired_for_delivery":
            continue
        names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _own_body_calls(func: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Every call in *func*'s OWN body, nested function definitions excluded.

    A closure defined inside a coroutine is usually the thing handed to a worker
    thread, so a call inside it runs off the loop and is not an offender. Counting
    it would report the very pattern being asked for as a violation.
    """
    calls: list[ast.Call] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def _splitter_calls(tree: ast.AST) -> list[tuple[str, set[str]]]:
    """Every splitter call in *tree* as ``(splitter, keyword names)``.

    Both shapes count. A renderer that offloads the splitter to a worker thread
    passes it as a REFERENCE to ``asyncio.to_thread`` and hands the arguments to
    ``to_thread``, so a check that only reads direct calls sees four of these
    seven sites and reports the rest as absent rather than as unguarded.

    A cut whose subject is already-repaired text is reported as guarded, because
    the repair carries the guarantee the redactor would be there to establish.
    """
    repaired = _repaired_names(tree)
    calls: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        if node.args and _name_of(node.args[0]) in repaired:
            keywords.add("redactor")
        called = _name_of(node.func)
        if called in SPLITTERS:
            calls.append((called, keywords))
        elif called == "to_thread" and node.args:
            offloaded = _name_of(node.args[0])
            if offloaded in SPLITTERS:
                calls.append((offloaded, keywords))
    return calls


class TestEveryDeliveryPathIsEnumerated:
    """The enforcement: a path that forgets the redactor fails here."""

    @pytest.mark.parametrize("path", DELIVERY_PATHS)
    def test_the_path_still_reaches_a_splitter(self, path: str) -> None:
        calls = _splitter_calls(ast.parse((SRC / path).read_text(encoding="utf-8")))
        assert calls, (
            f"{path} reaches no splitter. If its delivery moved, move it in "
            "DELIVERY_PATHS too -- an empty list would otherwise pass the "
            "redactor check below by having nothing to check."
        )

    @pytest.mark.parametrize("path", DELIVERY_PATHS)
    def test_every_splitter_call_passes_a_redactor(self, path: str) -> None:
        calls = _splitter_calls(ast.parse((SRC / path).read_text(encoding="utf-8")))
        unguarded = [name for name, keywords in calls if "redactor" not in keywords]
        assert not unguarded, (
            f"{path} calls {unguarded} with no redactor, so the boundary is "
            "chosen by a length budget alone and a severed key reaches two "
            "adjacent messages."
        )


class TestACutCannotRejoinAKey:
    """The behaviour the enumeration protects."""

    def _across_a_line_break(self) -> str:
        """Text whose halves of a key sit on either side of a line break.

        Scanning this text as written finds nothing: the break separates the
        halves. The cut then removes the break, because sealing a chunk trims the
        whitespace that ended it.
        """
        return f"{'w ' * 18}{HEAD}\n{TAIL} and some trailing words"

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        """Without a redactor the seam is open -- the state this guards against."""
        chunks = split_markdown_safe(self._across_a_line_break(), 50)
        assert len(chunks) == 2
        assert KEY not in chunks[0] and KEY not in chunks[1]
        assert joins_to_a_credential(chunks[0], chunks[1], _default_redactor)
        assert KEY in "".join(chunks)

    def test_a_redactor_closes_the_seam(self) -> None:
        chunks = split_markdown_safe(self._across_a_line_break(), 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)
        assert not any(
            joins_to_a_credential(chunks[i], chunks[i + 1], _default_redactor)
            for i in range(len(chunks) - 1)
        )

    def test_each_chunk_also_reads_clean_on_its_own(self) -> None:
        """Both readings, because neither contains the other."""
        chunks = split_markdown_safe(self._across_a_line_break(), 50, redactor=_default_redactor)
        for chunk in chunks:
            assert KEY not in chunk
            assert HEAD not in chunk or TAIL not in chunk

    def test_a_key_on_one_long_line_is_a_marker_before_any_cut(self) -> None:
        text = f"{'w ' * 18}{KEY} trailing"
        chunks = split_markdown_safe(text, 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_key_hidden_in_markup_is_caught_too(self) -> None:
        """The client renders the link away and joins the halves on screen."""
        text = f"{'y' * 48}[{HEAD}](https://ex.test/z){TAIL} tail"
        chunks = split_markdown_safe(text, 50, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_reserve_still_applies(self) -> None:
        chunks = split_markdown_safe(
            self._across_a_line_break(), 50, reserve=10, redactor=_default_redactor
        )
        assert KEY not in "".join(chunks)

    def test_a_key_split_at_a_space_does_not_reach_two_messages(self) -> None:
        """A space at a boundary is as invisible as a line break.

        With the space in place the text is not a credential, so no scan of it
        objects. The cut lands on the space, the seal takes it, and the halves sit
        flush on screen.
        """
        text = f"{'w ' * 12}{HEAD} {TAIL} and some trailing words"
        unguarded = split_markdown_safe(text, 35)
        assert len(unguarded) == 2
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = split_markdown_safe(text, 35, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_moving_the_cut_keeps_every_character(self) -> None:
        """The repair searched for a budget before giving anything up."""
        text = f"{'w ' * 12}{HEAD} {TAIL} and some trailing words"
        guarded = split_markdown_safe(text, 35, redactor=_default_redactor)
        assert "".join(guarded) == text, "no character was dropped or collapsed"

    def test_a_key_no_budget_can_cut_safely_is_redacted(self) -> None:
        """The terminal: the key spans every boundary the budget can offer."""
        text = " ".join(KEY[i : i + 2] for i in range(0, len(KEY), 2))
        guarded = split_markdown_safe(text, 6, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_prose_is_returned_byte_for_byte(self) -> None:
        """The reduction is one-directional: clean text keeps its breaks."""
        text = "first line of prose\n\nsecond paragraph here\nand a third line"
        assert split_markdown_safe(text, 24, redactor=_default_redactor) == split_markdown_safe(
            text, 24
        )

    def test_a_break_no_boundary_falls_on_keeps_its_content(self) -> None:
        """The repair is keyed on a CHOSEN boundary, not on every candidate.

        Two lines that read as one key when glued are the shape that makes a
        candidate-by-candidate scan destructive: no single cut ever joins them,
        yet a scan of every break at once sees a key and the whole message loses
        its structure. Here the cut falls after the prose, so the break between
        the halves is never a boundary and the text comes back as written --
        inside one message a line break is a line break, which the reader sees.
        """
        text = f"{'prose words ' * 6}\n{HEAD}\n{TAIL}\n"
        guarded = split_markdown_safe(text, 80, redactor=_default_redactor)
        assert guarded == split_markdown_safe(text, 80)
        assert len(guarded) > 1, "the fixture is meant to have a real boundary"
        assert any("\n" in chunk for chunk in guarded)
        assert HEAD in "".join(guarded) and TAIL in "".join(guarded)


class TestTheByteBudgetCarriesTheSameGuarantee:
    """Webex measures bytes, and a byte budget knows nothing about keys either."""

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        chunks = chunk_utf8_bytes(f"{'x' * 50}{KEY} trailing", 60)
        assert KEY not in chunks[0] and KEY not in chunks[1]
        assert KEY in "".join(chunks)

    def test_a_redactor_closes_the_seam(self) -> None:
        chunks = chunk_utf8_bytes(f"{'x' * 50}{KEY} trailing", 60, redactor=_default_redactor)
        assert KEY not in "".join(chunks)

    def test_a_key_split_at_a_space_is_caught_here_too(self) -> None:
        text = f"{'w ' * 10}{HEAD} {TAIL} and trailing"
        unguarded = chunk_utf8_bytes(text, 30)
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = chunk_utf8_bytes(text, 30, redactor=_default_redactor)
        assert not _rejoins(guarded)
        assert KEY not in "".join(part.strip() for part in guarded)

    def test_clean_text_reassembles_exactly(self) -> None:
        text = "a table row | another cell | a third cell that makes this long"
        assert "".join(chunk_utf8_bytes(text, 20, redactor=_default_redactor)) == text

    def test_a_kept_newline_is_no_separator_once_rendered(self) -> None:
        """This splitter keeps the break; the client drops it when it renders.

        So a chunk ending in a newline is not a safe boundary just because the
        characters still hold one, and the grade has to read the stripped pair.
        """
        text = f"{'x' * 56}{HEAD}\n{TAIL} and trailing words"
        unguarded = chunk_utf8_bytes(text, 66)
        assert len(unguarded) == 2
        assert KEY not in unguarded[0] and KEY not in unguarded[1]
        assert KEY in unguarded[0].rstrip() + unguarded[1].lstrip()
        guarded = chunk_utf8_bytes(text, 66, redactor=_default_redactor)
        assert KEY not in "".join(part.strip() for part in guarded)


class TestAKeySpanningMoreThanTwoChunks:
    """A narrow budget puts a key across three chunks, or more.

    Each neighbouring pair then holds fragments that match nothing, so a grade
    asking only about neighbours clears every boundary while the screen shows the
    key whole. The reading that sees it is the whole sequence, which is what the
    grade has to be over.
    """

    #: Ten characters of key on either side of the run, so no budget at or below
    #: the ones used here can hold the whole key in one chunk.
    TEXT = f"aa{HEAD}{' ' * 10}{TAIL}bb"

    def test_neighbouring_pairs_alone_clear_a_key_on_screen(self) -> None:
        """The state a pairwise grade cannot see, measured on the raw splitter."""
        chunks = chunk_utf8_bytes(self.TEXT, 10)
        assert len(chunks) >= 3
        assert not _rejoins(chunks), "every neighbouring pair reads clean"
        assert all(KEY not in chunk for chunk in chunks), "no chunk holds the key"
        assert KEY in _on_screen(chunks), "yet the reader sees it whole"

    def test_the_byte_splitter_refuses_that_split(self) -> None:
        chunks = chunk_utf8_bytes(self.TEXT, 10, redactor=_default_redactor)
        assert KEY not in _on_screen(chunks)

    def test_the_character_splitter_refuses_that_split(self) -> None:
        chunks = split_markdown_safe(self.TEXT, 10, redactor=_default_redactor)
        assert KEY not in _on_screen(chunks)

    @pytest.mark.parametrize("budget", [6, 8, 10, 12, 14])
    def test_no_narrow_budget_delivers_the_key(self, budget: int) -> None:
        for chunks in (
            chunk_utf8_bytes(self.TEXT, budget, redactor=_default_redactor),
            split_markdown_safe(self.TEXT, budget, redactor=_default_redactor),
        ):
            assert KEY not in _on_screen(chunks)
            assert all(KEY not in chunk for chunk in chunks)

    def test_a_chunk_that_renders_to_nothing_is_no_separator(self) -> None:
        """The lossless splitter can place a whitespace-only chunk between halves.

        It renders to nothing, so it separates nothing on screen, and both of its
        own boundaries read clean against it.
        """
        text = f"aa{HEAD}{' ' * 40}{TAIL}bb"
        unguarded = chunk_utf8_bytes(text, 12)
        assert any(not chunk.strip() for chunk in unguarded), "a blank chunk exists"
        assert not _rejoins(unguarded)
        assert KEY in _on_screen(unguarded)
        guarded = chunk_utf8_bytes(text, 12, redactor=_default_redactor)
        assert KEY not in _on_screen(guarded)

    def test_the_grade_reads_the_whole_sequence(self) -> None:
        """Directly: three chunks, every neighbouring pair clean, key on screen.

        The middle chunk is a fragment of the key itself, so it matches nothing on
        its own and nothing when read against either neighbour.
        """
        chunks = [f"aa{KEY[:8]}", KEY[8:14], f"{KEY[14:]}bb"]
        assert _on_screen(chunks) == f"aa{KEY}bb"
        assert not _rejoins(chunks), "the neighbour reading clears it"
        assert _rejoins_a_key(chunks, _default_redactor)

    def test_a_key_inside_a_link_needs_the_per_side_reading(self) -> None:
        """Canonicalising the join drops the url and the key with it.

        Each half on screen is an unfinished link whose target stays visible, so
        the reading that canonicalises the sides FIRST is the one that sees it.
        """
        chunks = [f"[label](https://x/{HEAD}", f"{TAIL})"]
        assert canonicalize_display(_on_screen(chunks)) == "label"
        assert _rejoins_a_key(chunks, _default_redactor)

    def test_innocent_text_keeps_its_whitespace(self) -> None:
        """The wider grade may not push ordinary prose onto the flush path."""
        prose = " ".join(["word"] * 40)
        chunks = split_markdown_safe(prose, 30, redactor=_default_redactor)
        assert len(chunks) > 1
        assert " ".join(chunks).split() == prose.split()


class TestTheBudgetSearchProbesDensely:
    """A safe budget just below the caller's own may not be stepped over.

    Doubling the step alone reaches 7000, 6999, 6998, 6996, 6992 and onward, so a
    budget that cuts cleanly at 6997 is never tried and text with a safe cut goes
    to the last resort anyway.
    """

    #: The budgets a doubling walk visits from 7000.
    LADDER = {7000, 6999, 6998, 6996, 6992, 6984, 6968, 6936, 6872, 6744, 6488, 5976, 4952, 2904}

    def _cut(self, probed: list[int]):
        """A cut that rejoins a key at exactly the doubling budgets, clean elsewhere."""

        def cut(text: str, room: int) -> list[str]:
            probed.append(room)
            if room in self.LADDER:
                return [f"aa{HEAD}", f"{TAIL}bb"]
            return [text]

        return cut

    def test_a_budget_off_the_doubling_ladder_is_tried(self) -> None:
        probed: list[int] = []
        found = _under_a_safe_budget(self._cut(probed), "prose", 7000, _default_redactor)
        assert found == ["prose"]
        assert probed[-1] == 6997
        assert probed[-1] not in self.LADDER

    def test_the_ladder_budgets_are_all_rejected_first(self) -> None:
        probed: list[int] = []
        _under_a_safe_budget(self._cut(probed), "prose", 7000, _default_redactor)
        assert probed[:4] == [7000, 6999, 6998, 6997]


class TestTheTerminalKeepsWhatItCan:
    """When no budget cuts safely, only the key's own span is closed up.

    The wider flush drops every space, break and delimiter in the message. The
    span repair loses the whitespace inside the key and the key itself, and
    nothing else, so a long reply keeps its shape.
    """

    BODY = "Line one has **bold** text.\nLine two is prose.\n\n- a bullet\n- another\n"

    def _safe(self) -> str:
        text = f"{self.BODY}token {HEAD} {TAIL} end\n{self.BODY}"
        return redact_for_display(text, _default_redactor)[0]

    def test_the_text_needs_a_repair_at_all(self) -> None:
        """The premise: collapsing the whitespace reveals a key."""
        collapsed = _WHITESPACE_RUN.sub("", self._safe())
        assert _default_redactor(collapsed) != collapsed

    def test_the_prose_around_the_span_survives(self) -> None:
        repaired = _redact_only_the_rejoined_span(self._safe(), _default_redactor)
        assert repaired is not None
        assert repaired.startswith(self.BODY)
        assert repaired.endswith(self.BODY)

    def test_every_break_and_delimiter_survives(self) -> None:
        safe = self._safe()
        repaired = _redact_only_the_rejoined_span(safe, _default_redactor)
        assert repaired is not None
        assert repaired.count("\n") == safe.count("\n")
        assert repaired.count("**") == safe.count("**")
        assert repaired.count("- ") == safe.count("- ")

    def test_the_collapsed_form_still_reads_clean(self) -> None:
        """The promise a caller re-cutting these chunks relies on."""
        repaired = _redact_only_the_rejoined_span(self._safe(), _default_redactor)
        assert repaired is not None
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(repaired))
        assert _default_redactor(collapsed) == collapsed

    def test_the_search_path_loses_no_character(self) -> None:
        """A budget the search can meet costs the reader nothing at all."""
        safe = self._safe()
        chunks = _under_a_safe_budget(split_markdown_safe, safe, 120, _default_redactor)
        assert chunks is not None, "this text has a safe budget"
        assert "".join(chunks).replace("\n", "") == safe.replace("\n", "")

    def test_only_named_helpers_rewrite_whitespace(self) -> None:
        """Every whitespace rewrite is in one of three named places.

        Two read to decide, one rewrites to repair. A fourth would be a fresh way
        for a valid span to lose its formatting without anyone deciding it should.
        """
        import kiro_crew.messaging.split as split_module

        assert not hasattr(split_module, "_flush_whitespace")
        tree = ast.parse((SRC / "messaging" / "split.py").read_text(encoding="utf-8"))
        rewriters = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and _name_of(call.func) == "sub"
            and isinstance(call.func, ast.Attribute)
            and _name_of(call.func.value) == "_WHITESPACE_RUN"
        }
        assert rewriters == {
            "_collapses_to_a_key",
            "_redact_only_the_rejoined_span",
            "_made_collapse_clean",
        }, rewriters

    def test_the_terminal_fallback_is_a_collapse_fixed_point(self) -> None:
        """The answer a caller re-cuts without a grade holds no key under any cut.

        Reached only when the budget search fails AND no span of the literal text
        names the key, which is where declining and keeping the guarantee cannot
        both be had: the whitespace is what hides the key.
        """
        broken = f"`{HEAD}`\n`{TAIL}`"
        body = " ".join(broken for _ in range(8))
        chunks = split_markdown_safe(body, 30, redactor=_default_redactor)
        assert len(chunks) == 1, "the search must have failed for this to be the terminal"
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(chunks[0]))
        assert _default_redactor(collapsed) == collapsed
        for cap in (7, 11, 19, 31):
            assert KEY not in _on_screen(chunk_text(chunks[0], cap) or [chunks[0]])

    def test_a_split_that_exhausts_every_budget_keeps_its_breaks(self) -> None:
        """End to end, through the splitter, on text no budget can cut safely.

        The run between the halves is wider than a chunk, so some chunk renders to
        nothing at every budget and the key reads whole however the cut moves.
        """
        body = "first line\nsecond line\nthird line\n"
        text = f"{body}{HEAD}{' ' * 40}{TAIL}\n{body}"
        safe = redact_for_display(text, _default_redactor)[0]
        assert _under_a_safe_budget(chunk_utf8_bytes, safe, 12, _default_redactor) is None
        chunks = chunk_utf8_bytes(text, 12, redactor=_default_redactor)
        assert KEY not in _on_screen(chunks)
        assert "".join(chunks).count("\n") == text.count("\n")


class TestTheLastStepIsGradedToo:
    """A caller that cuts a chunk AFTER the splitter graded it regrades.

    A fixed-width slice of an oversized chunk is a boundary nothing graded, and
    each slice is posted as its own message, so the seam reopens at the last step.
    The repair's subject is the CHUNK the slices came from, never their
    concatenation: sealing trims the whitespace that ended a chunk and a fence
    spanning a seam contributes a synthetic closer and reopener, so a join is not
    the reply.
    """

    SOURCE = f"prefix {HEAD} {TAIL} suffix"
    SLICED = [f"prefix {HEAD}", f"{TAIL} suffix"]

    def test_a_blind_slice_reopens_the_seam(self) -> None:
        """The premise: no slice holds the key, the screen does."""
        assert all(KEY not in piece for piece in self.SLICED)
        assert KEY in _on_screen(self.SLICED)

    def test_the_repair_fires_on_that_sequence(self) -> None:
        assert repaired_for_delivery(self.SOURCE, self.SLICED, _default_redactor) is not None

    def test_a_clean_sequence_needs_no_repair(self) -> None:
        assert (
            repaired_for_delivery(
                "ordinary text here and more of it",
                ["ordinary text here", "and more of it"],
                _default_redactor,
            )
            is None
        )

    @pytest.mark.parametrize("cap", [5, 9, 13, 20, 40])
    def test_the_repair_survives_any_re_cut(self, cap: int) -> None:
        """Why one grade per chunk is enough: the repair is safe to bound again."""
        repaired = repaired_for_delivery(self.SOURCE, self.SLICED, _default_redactor)
        assert repaired is not None
        assert KEY not in _on_screen(chunk_text(repaired, cap) or [repaired])

    def test_the_repair_is_made_on_the_source_not_the_join(self) -> None:
        """Breaks the slicing dropped are still in the repair.

        Only the key's OWN span gives up its whitespace, which is the documented
        one-directional trade. Every other break survives, and the count is the
        discriminator: a repair rebuilt from the pieces would carry the join's
        losses instead.
        """
        source = f"one line\n\ntwo {HEAD}\n{TAIL} three\n\nfour line"
        pieces = split_markdown_safe(source, 20)
        assert "".join(pieces).count("\n") < source.count("\n"), "the join is lossy here"
        repaired = repaired_for_delivery(source, pieces, _default_redactor)
        assert repaired is not None
        assert repaired.count("\n") > "".join(pieces).count("\n")
        assert repaired.startswith("one line\n\ntwo ")
        assert repaired.endswith(" three\n\nfour line")

    def test_the_bound_grades_each_chunk_it_cuts(self) -> None:
        """A blind slice INSIDE one chunk is a boundary only this grade sees.

        The boundaries between the chunks handed in are the splitter's own, graded
        where the source was still available; the ones the bound creates are new.
        """
        chunk = f"{'x' * 28} {HEAD} {TAIL} trailing words"
        assert KEY not in chunk
        blind = chunk_text(chunk, 40) or [chunk]
        assert len(blind) > 1, "the blind slice is the premise"
        assert _rejoins_a_key(blind, _default_redactor), "the blind slice is the premise"
        delivered = bounded_for_delivery([chunk], 40, _default_redactor, chunk_text)
        assert KEY not in _on_screen(delivered)

    def test_the_fallback_is_a_collapse_fixed_point_too(self) -> None:
        """The delivery fallback MAKES the guarantee rather than asserting it.

        Reached when the pieces rejoin a key and no span of the literal text names
        it. Its answer is re-cut by the caller with no second grade, so a collapse
        of it that still read as a key would hand that caller a promise the text
        does not keep.
        """
        source = redact_for_display(f"`{HEAD}`\n`{TAIL}`", _default_redactor)[0]
        pieces = chunk_text(source, max(8, len(source) // 2)) or [source]
        assert _rejoins_a_key(pieces, _default_redactor), "the grade must fire"
        assert (
            _redact_only_the_rejoined_span(source, _default_redactor) is None
        ), "no span of the literal text names this key"
        fallback = repaired_for_delivery(source, pieces, _default_redactor)
        assert fallback is not None
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(fallback))
        assert _default_redactor(collapsed) == collapsed

    def test_the_slack_path_runs_that_grade(self) -> None:
        tree = ast.parse((SRC / "slack" / "renderer.py").read_text(encoding="utf-8"))
        called = {_name_of(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert "repaired_for_delivery" in called


class TestASealedPrefixCannotBeCompletedLater:
    """A sealed chunk ending in a credential PREFIX is the case a seal grade misses.

    The prefix matches nothing, so every scan passes it and the message goes out;
    the characters completing the key arrive afterwards. A sent message cannot be
    recalled, so the side still open is the one not yet delivered, and it gives up
    exactly the span that completes the key.
    """

    SENT = f"{'w ' * 20}{HEAD}"
    LATER = f"{TAIL} and then more ordinary trailing prose."

    def test_the_seal_grade_cannot_see_it(self) -> None:
        """The premise: nothing is wrong with the sealed chunk when it seals."""
        assert KEY not in self.SENT
        assert not _default_redactor(self.SENT) != self.SENT

    def test_the_pair_hands_the_reader_the_key(self) -> None:
        assert KEY in _on_screen([self.SENT, self.LATER])
        assert joins_to_a_credential(self.SENT.rstrip(), self.LATER.lstrip(), _default_redactor)

    def test_a_clean_pair_needs_no_offset(self) -> None:
        assert offset_clear_of_a_sent_tail(self.SENT, "ordinary prose.", _default_redactor) == 0
        assert repaired_after_a_sent_tail(self.SENT, "ordinary prose.", _default_redactor) is None

    def test_the_offset_covers_only_the_completing_span(self) -> None:
        offset = offset_clear_of_a_sent_tail(self.SENT, self.LATER, _default_redactor)
        assert 0 < offset <= len(TAIL) * 2
        assert not joins_to_a_credential(
            self.SENT.rstrip(), self.LATER[offset:].lstrip(), _default_redactor
        )

    def test_the_repair_closes_the_seam(self) -> None:
        repaired = repaired_after_a_sent_tail(self.SENT, self.LATER, _default_redactor)
        assert repaired is not None
        assert KEY not in _on_screen([self.SENT, repaired])

    def test_everything_after_that_span_is_the_reply(self) -> None:
        repaired = repaired_after_a_sent_tail(self.SENT, self.LATER, _default_redactor)
        assert repaired is not None
        assert repaired.endswith("ordinary trailing prose.")

    def test_a_rotation_no_longer_hands_it_over(self) -> None:
        """The reproduction, through the function Telegram rotates with."""
        sent: list[str] = []

        def rotate(buf: str, tail: str) -> tuple[str, str]:
            pieces = _split_markdown(buf, 400)
            if len(pieces) <= 1:
                return buf, tail
            for piece in pieces[:-1]:
                repaired = repaired_after_a_sent_tail(tail, piece, _default_redactor)
                piece = repaired if repaired is not None else piece
                sent.append(piece)
                tail = piece
            return pieces[-1], tail

        buf, tail = rotate(("w " * 199) + KEY[:4], "")
        buf, tail = rotate(buf + KEY[4:] + " and more ordinary prose after it.", tail)
        assert sent, "the first frame must have sealed something"
        # The retained tail is shown in the live bubble rather than sealed, and that
        # is where the renderer repairs it -- the same grade against the same record.
        shown = repaired_after_a_sent_tail(tail, buf, _default_redactor) or buf
        assert KEY not in "".join(part.strip() for part in sent) + shown.strip()

    def test_both_streaming_legs_offload_and_record_it(self) -> None:
        for path in ("telegram/renderer.py", "whatsapp/turn_renderer.py"):
            tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
            offloaded = {
                _name_of(node.args[0])
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _name_of(node.func) == "to_thread" and node.args
            }
            attributes = {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr == "_sent_tail"
            }
            assert attributes, f"{path} must remember what it already sent"
            assert offloaded & {
                "repaired_after_a_sent_tail",
                "_seam_safe",
            }, f"{path} must offload the repair"
        # WhatsApp shows text from two places -- the streaming flush and the final
        # pass -- and each one sits under a message already sent, so a guard on one
        # of them is not a guard on the channel.
        wa = ast.parse((SRC / "whatsapp" / "turn_renderer.py").read_text(encoding="utf-8"))
        repairs = [
            node
            for node in ast.walk(wa)
            if isinstance(node, ast.Call)
            and _name_of(node.func) == "to_thread"
            and node.args
            and _name_of(node.args[0]) == "repaired_after_a_sent_tail"
        ]
        assert len(repairs) >= 2, "both of WhatsApp's send paths must run the repair"
        recording = {
            node.name
            for node in ast.walk(wa)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__"
            for stmt in ast.walk(node)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "_sent_tail" for t in stmt.targets)
        }
        assert len(recording) >= 2, (
            "each send path must record what it sent, or the next chunk is graded "
            f"against a stale predecessor -- only {sorted(recording)} does"
        )


class TestEverySealingPathInheritsTheSeamGuard:
    """One grader and one writer, in the sinks, not at the call sites.

    Five paths seal a segment as its own message -- a length rotation, an
    upload-hold rotation, a steer boundary, a degraded table chunk and the end of
    the turn -- so a guard written at one of them is not a guard on the channel.
    Both sinks run the same helper, and no caller carries a seam rule of its own.
    """

    TELEGRAM = "telegram/renderer.py"

    def _tree(self) -> ast.AST:
        return ast.parse((SRC / self.TELEGRAM).read_text(encoding="utf-8"))

    def test_both_sinks_run_the_seam_helper(self) -> None:
        tree = self._tree()
        reached = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and (
                _name_of(call.func) == "_seam_safe"
                or (
                    _name_of(call.func) == "to_thread"
                    and call.args
                    and _name_of(call.args[0]) == "_seam_safe"
                )
            )
        }
        assert {"_seal_text", "_seal_chunk_html"} <= reached, reached

    def test_the_record_has_exactly_one_writer(self) -> None:
        """A second writer is a second place to forget, which is the whole defect."""
        tree = self._tree()
        writers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for stmt in ast.walk(node)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "_sent_tail" for t in stmt.targets)
        }
        assert writers == {"__init__", "_record_sent"}, writers

    def test_the_record_waits_for_a_confirmed_delivery(self) -> None:
        """Unsent text may not become the predecessor.

        Every send and edit path can fail. Recording before one confirms would grade
        the next message against text no reader ever saw, and the next delivered
        message would give up a leading span for a key nobody read.
        """
        tree = self._tree()
        repair = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_seam_safe"
        )
        assert not [
            stmt
            for stmt in ast.walk(repair)
            if isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "_sent_tail" for t in stmt.targets)
        ], "the repair must not record; _record_sent does, after a delivery confirms"
        recorders = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and _name_of(call.func) == "_record_sent"
        }
        assert {"_seal_text", "_seal_chunk_html"} <= recorders, recorders

    def test_the_degraded_tail_is_graded_after_the_re_split(self) -> None:
        """The tail sits under a chunk the same pass sealed, which is newer."""
        tree = self._tree()
        degraded = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_seal_without_rich"
        )
        assert [
            call
            for call in ast.walk(degraded)
            if isinstance(call, ast.Call)
            and _name_of(call.func) == "to_thread"
            and call.args
            and _name_of(call.args[0]) == "_seam_safe"
        ], "the re-split tail must be graded against what the reader can now see"

    def test_no_sealing_caller_carries_its_own_seam_rule(self) -> None:
        """Every seal reaches the guard through the sink, so none repeats it."""
        tree = self._tree()
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and _name_of(call.func) in {"_seal_current", "_seal_chunk_html", "_seal_text"}
        }
        assert callers, "the sealing paths are gone"
        bespoke = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in callers
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and (
                _name_of(call.func) == "repaired_after_a_sent_tail"
                or (
                    _name_of(call.func) == "to_thread"
                    and call.args
                    and _name_of(call.args[0]) == "repaired_after_a_sent_tail"
                )
            )
        }
        assert not bespoke, f"these callers duplicate the sink's guard: {bespoke}"


class TestEveryDeliveryPathCountsWhatItShipped:
    """A boundary repair adds a placeholder, and the notice has to see it.

    Each capped path tallied the SOURCE it cut, so a reply whose only redaction
    came from the repair announced none. Enumerated rather than discovered: the
    tally sites share no name and a search for one would miss them.
    """

    #: Each changed path and the expression its tally must read.
    TALLIES = [
        ("teams/renderer.py", 'count_redaction_tags("\\n".join(chunks))'),
        ("slack/renderer.py", "count_redaction_tags(self._delivered or clean_text)"),
        ("webex/renderer.py", "count_redaction_tags(delivered_text)"),
        ("wecom/renderer.py", 'count_redaction_tags("\\n".join(chunks))'),
        ("whatsapp/turn_renderer.py", 'count_redaction_tags("\\n".join(chunks))'),
    ]

    @pytest.mark.parametrize("path,expected", TALLIES)
    def test_the_tally_reads_the_delivered_text(self, path: str, expected: str) -> None:
        source = (SRC / path).read_text(encoding="utf-8")
        assert expected in source, f"{path} must count what shipped, not its source"

    def test_a_repair_adds_a_placeholder_the_notice_must_see(self) -> None:
        """The premise: the repair really does introduce one."""
        repaired = repaired_after_a_sent_tail(
            f"{'w ' * 20}{HEAD}", f"{TAIL} and trailing prose.", _default_redactor
        )
        assert repaired is not None
        assert count_redaction_tags(repaired)[0] == 1


class TestTheSealLoopRepairsWhatItSends:
    """WhatsApp seals chunk by chunk, and each one sits under the last.

    The forward grade asks whether THIS chunk severs a key; it says nothing about
    the message already sent, whose tail may have been a credential prefix that
    matched nothing when it sealed.
    """

    WA = "whatsapp/turn_renderer.py"

    def test_the_loop_repairs_before_it_seals(self) -> None:
        tree = ast.parse((SRC / self.WA).read_text(encoding="utf-8"))
        sealed_raw = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _name_of(call.func) == "_seal_chunk"
            and call.args
            and not isinstance(call.args[0], ast.Name)
        ]
        assert not sealed_raw, "a chunk must be repaired into a local before it is sealed"
        repairs = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and _name_of(call.func) == "to_thread"
            and call.args
            and _name_of(call.args[0]) == "repaired_after_a_sent_tail"
        ]
        assert len(repairs) >= 3, "the seal loop, the live tail and finalization each repair"

    def test_it_records_what_it_sealed_not_the_original(self) -> None:
        """Recording the original grades the next chunk against unseen text."""
        source = (SRC / self.WA).read_text(encoding="utf-8")
        assert "self._sent_tail = sealed.strip()" in source
        assert "self._sent_tail = rendered[index]" not in source


class TestTheLastResortKeepsUnrelatedFormatting:
    """Only the span that hides the key gives up its whitespace."""

    def _body(self) -> str:
        broken = f"`{HEAD}`\n`{TAIL}`"
        return f"First paragraph.\n\n- one bullet\n- another\n\n{broken}\n\nLast **bold** line.\n"

    def test_the_text_needs_the_last_resort(self) -> None:
        """The premise: neither the literal nor the canonical reading closes it."""
        body = self._body()
        assert _redact_only_the_rejoined_span(body, _default_redactor) is None
        canonical = redact_for_display(canonicalize_display(body), _default_redactor)[0]
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(canonical))
        assert _default_redactor(collapsed) != collapsed

    def test_the_answer_is_a_collapse_fixed_point(self) -> None:
        fixed = _made_collapse_clean(self._body(), _default_redactor)
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(fixed))
        assert _default_redactor(collapsed) == collapsed

    def test_the_breaks_outside_that_span_survive(self) -> None:
        """Every break but the one inside the key's own span, and all the prose.

        A blanket collapse would leave none of them. Emphasis goes because the
        answer is the CANONICAL form, which is ``redact_for_display``'s own
        one-directional trade and not a wider one taken here.
        """
        body = self._body()
        fixed = _made_collapse_clean(body, _default_redactor)
        assert fixed.count("\n") == body.count("\n") - 1, fixed
        assert "First paragraph." in fixed
        assert "- one bullet" in fixed
        assert "- another" in fixed
        assert "Last bold line." in fixed
        assert KEY not in fixed

    def test_a_blanket_collapse_would_keep_none_of_them(self) -> None:
        """The control, so the narrowing is measured rather than asserted."""
        body = self._body()
        blanket = redact_for_display(
            _WHITESPACE_RUN.sub("", canonicalize_display(body)), _default_redactor
        )[0]
        assert blanket.count("\n") == 0
        assert _made_collapse_clean(body, _default_redactor).count("\n") > 0


class TestWhatsAppsOwnSendPathStaysBounded:
    """A declined cut may not reach a transport that posts each chunk as a message.

    The non-stable split is what the channel's own sender uses, and it has no
    length bound of its own. The stable split is deliberately NOT bounded: its
    caller treats earlier chunks as delivered, and a bound would move a boundary
    under a message already sent.
    """

    LIMIT = 120

    def _dense(self) -> str:
        """A key in code spans either side of a break, repeated: no cut is clean."""
        return " ".join(f"`{HEAD}`\n`{TAIL}`" for _ in range(8))

    def test_the_splitter_alone_would_decline(self) -> None:
        converted = to_whatsapp_text(self._dense())
        chunks = split_markdown_safe(converted, self.LIMIT, redactor=_redact_all)
        assert len(chunks) == 1, "the premise is a declined cut"
        assert len(chunks[0]) > self.LIMIT, "and the declined answer is over the cap"

    def test_the_channel_send_path_bounds_it(self) -> None:
        chunks = render_chunks(self._dense(), self.LIMIT)
        assert len(chunks) > 1
        assert max(len(chunk) for chunk in chunks) <= self.LIMIT

    def test_no_key_reaches_the_screen_once_bounded(self) -> None:
        assert KEY not in _on_screen(render_chunks(self._dense(), self.LIMIT))

    def test_the_stable_split_keeps_the_budget_boundaries(self) -> None:
        """No extra bound runs there: its boundaries are the budget's, unchanged.

        A streaming caller treats all but the last chunk as delivered, so a bound
        that re-cut them would move a boundary under a message already sent.
        """
        converted = to_whatsapp_text(self._dense())
        assert render_chunks(self._dense(), self.LIMIT, stable=True) == split_markdown_safe(
            converted, self.LIMIT, redactor=_redact_all, stable=True
        )

    def test_ordinary_prose_is_untouched(self) -> None:
        prose = "One ordinary sentence. " * 3
        assert "".join(render_chunks(prose, self.LIMIT)) == to_whatsapp_text(prose)


class TestTelegramTableBlocksAreRedactedBeforeTheGrade:
    """The grade is sound only on chunks already a fixed point of its own scan.

    A table run bypasses the splitter, so without its own redaction a cell holding
    a whole credential makes the sequence grade fire for something no seam severed
    -- and the repair it reaches then flattens the message's markup.
    """

    BODY = (
        "col | val\n--- | ---\nrow | "
        + KEY
        + "\n\nordinary trailing prose with **bold** and `code` in it."
    )

    def test_the_key_never_reaches_the_screen(self) -> None:
        chunks = _split_markdown_table_aware(self.BODY, 600, 4000)
        assert KEY not in _on_screen(chunks)

    def test_the_message_keeps_its_markup(self) -> None:
        chunks = _split_markdown_table_aware(self.BODY, 600, 4000)
        joined = "".join(chunks)
        assert "**bold**" in joined
        assert "`code`" in joined

    def test_the_table_still_seals_as_its_own_block(self) -> None:
        chunks = _split_markdown_table_aware(self.BODY, 600, 4000)
        assert len(chunks) == 2, "the table run and the prose stay separate messages"


class TestTheBackfillCompositionIsOffloaded:
    """Splitting an uncapped history row may not run on the loop thread.

    The grade redacts and rescans each candidate boundary, an imported row carries
    no size cap, and the liveness watchdog exits the process after seconds of loop
    silence.
    """

    def test_the_units_are_composed_in_a_thread(self) -> None:
        tree = ast.parse((SRC / "dashboard" / "chat_mirror.py").read_text(encoding="utf-8"))
        offloaded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Await):
                continue
            names = {
                element.id
                for target in node.targets
                for element in getattr(target, "elts", [target])
                if isinstance(element, ast.Name)
            }
            call = node.value.value
            if "recent_turn_units" in names and isinstance(call, ast.Call):
                offloaded = _name_of(call.func) == "to_thread"
        assert offloaded, "the backfill units must be composed off the loop thread"


class TestAStreamedReplyNeverRevisesADeliveredChunk:
    """A streaming caller re-splits its growing body and seals all but the last.

    So chunk *i* must be decided by the text before it and nothing later. Searching
    the whole body for a safer budget breaks that: one more character can move a
    boundary under a message already sent, which a count of delivered chunks cannot
    detect and no later frame can take back.
    """

    #: The body that reproduced the revision, and the budget it happened at.
    PAD, LIMIT, CUT = 108, 120, 129

    def _body(self) -> str:
        return f"{'w' * self.PAD} {HEAD} {TAIL} trailing words after the key here"

    def _split(self, text: str, *, stable: bool) -> list[str]:
        return split_markdown_safe(text, self.LIMIT, redactor=_default_redactor, stable=stable)

    def test_searching_the_whole_body_revises_a_sealed_chunk(self) -> None:
        """The state a streaming caller cannot survive, on the searching path."""
        body = self._body()
        short = self._split(body[: self.CUT], stable=False)
        longer = self._split(body[: self.CUT + 1], stable=False)
        assert len(short) >= 2
        assert short[0] != longer[0], "one more character revised an already-sealed chunk"

    def test_the_stable_split_leaves_it_alone(self) -> None:
        body = self._body()
        short = self._split(body[: self.CUT], stable=True)
        longer = self._split(body[: self.CUT + 1], stable=True)
        assert len(short) >= 2
        assert longer[: len(short) - 1] == short[:-1]

    @pytest.mark.parametrize("cut", [40, 60, 80, 100, 120, 129, 140])
    def test_every_append_keeps_the_sealed_prefix(self, cut: int) -> None:
        body = self._body()
        short = self._split(body[:cut], stable=True)
        if len(short) < 2:
            pytest.skip("nothing sealed at this length")
        longer = self._split(body[: cut + 1], stable=True)
        assert longer[: len(short) - 1] == short[:-1]

    def test_the_stable_split_still_redacts(self) -> None:
        """Prefix stability is not bought by dropping the redaction."""
        chunks = self._split(f"{'w' * 40} {KEY} trailing", stable=True)
        assert all(KEY not in chunk for chunk in chunks)

    def test_the_streaming_renderer_asks_for_the_stable_split(self) -> None:
        tree = ast.parse((SRC / "whatsapp" / "turn_renderer.py").read_text(encoding="utf-8"))
        stable = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and any(kw.arg == "stable" for kw in node.keywords)
            and _name_of(node.func) == "render_chunks_off_loop"
        ]
        assert stable, "the streaming renderer must request the prefix-stable split"

    def test_it_grades_the_seam_before_counting_a_chunk_final(self) -> None:
        tree = ast.parse((SRC / "whatsapp" / "turn_renderer.py").read_text(encoding="utf-8"))
        reached = {_name_of(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)} | {
            _name_of(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _name_of(node.func) == "to_thread" and node.args
        }
        assert "joins_to_a_credential" in reached
        assert "repaired_after_a_sent_tail" in reached

    def test_the_finalization_repairs_against_what_was_already_sent(self) -> None:
        """The notice counts what shipped, and the repair subject is one chunk.

        A chunk sealed while its tail was only a credential PREFIX is already on
        screen, so the completion arriving later can be given up only on the side
        not yet sent. Each send is graded against the previous one and the shipped
        list is what the tally reads, so a reply whose only placeholder came from a
        repair does not announce none.
        """
        tree = ast.parse((SRC / "whatsapp" / "turn_renderer.py").read_text(encoding="utf-8"))
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        } | {
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert "_sent_tail" not in assigned, "the record is an attribute, not a local"
        attributes = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_sent_tail"
        }
        assert attributes, "the renderer must remember what it already sent"
        assert "shipped" in assigned, "the tally must read the sequence that shipped"


class TestTheGradeNeverRunsOnTheEventLoop:
    """Every site handing the splitter a redactor offloads it.

    The grade redacts and rescans the text once per candidate boundary and the
    search tries many budgets, so a reply where no cut is clean holds the thread
    for seconds. One loop carries every channel, every turn and the liveness
    heartbeat, and the watchdog exits the process when it goes quiet.
    """

    #: Each path, and the name its offload is expected to wrap.
    OFFLOADED = [
        ("webex/renderer.py", "_bounded_chunks"),
        ("slack/renderer.py", "_bounded"),
        ("teams/renderer.py", "split_markdown_safe"),
        ("wecom/renderer.py", "split_markdown_safe"),
        ("dashboard/chat_mirror.py", "_compose_units"),
        ("whatsapp/renderer.py", "render_chunks"),
        ("telegram/renderer.py", "_split_markdown_bounded"),
        ("whatsapp/turn_renderer.py", "repaired_after_a_sent_tail"),
    ]

    @pytest.mark.parametrize("path,target", OFFLOADED)
    def test_the_splitter_call_is_handed_to_a_thread(self, path: str, target: str) -> None:
        tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
        offloads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _name_of(node.func) == "to_thread"
            and node.args
            and _name_of(node.args[0]) == target
        ]
        assert offloads, f"{path} must hand {target} to a thread"

    #: Telegram's own splitters, every one of which scans for credentials now that
    #: the cut is credential-aware. The channel rotates from four places, so "one
    #: offload exists somewhere in the file" is not the guarantee that matters.
    TELEGRAM_SCANNERS = frozenset(
        {
            "_split_markdown",
            "_split_markdown_bounded",
            "_split_markdown_table_aware",
            "_degraded_table_chunks",
            "repaired_after_a_sent_tail",
        }
    )

    def test_no_telegram_coroutine_calls_a_scanner_directly(self) -> None:
        """Each rotation site, not just one of them, hands its cut to a thread."""
        tree = ast.parse((SRC / "telegram" / "renderer.py").read_text(encoding="utf-8"))
        offenders = [
            f"{node.name} -> {_name_of(call.func)}"
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            for call in _own_body_calls(node)
            if _name_of(call.func) in self.TELEGRAM_SCANNERS
        ]
        assert not offenders, (
            f"these coroutines run a credential scan on the event loop: {offenders}. "
            "Hand the call to asyncio.to_thread."
        )

    def test_the_repair_loop_is_bounded_by_a_constant(self) -> None:
        """Unbounded passes would cost thousands of whole-text redactions."""
        source = (SRC / "messaging" / "split.py").read_text(encoding="utf-8")
        assert "_DENSE_PROBES)" in source
        long_runs = "a " * 4000
        repaired = _redact_only_the_rejoined_span(long_runs, _default_redactor)
        assert repaired is not None, "clean text needs no passes at all"


class TestTheSeamGradeReadsWhatTheReaderSees:
    """The streaming seam is graded on rendered text, not characters as stored.

    A platform drops the whitespace at a message's edges, so a tail beginning with
    a tab separates nothing once the two messages sit on screen. The splitter's own
    predicate strips those edges for that reason, and the seam grade has to agree
    with it or the two answer different questions about the same boundary.
    """

    #: A tail that BEGINS with edge whitespace. The splitter never strips leading
    #: whitespace, and a tab is not a delimiter a hard cut avoids, so a remainder
    #: can start with one.
    SEAM = [f"prefix {HEAD}", f"\t{TAIL} rest"]

    def test_the_stored_reading_approves_the_pair(self) -> None:
        """The bypass: asking about the characters as stored clears this seam."""
        assert not joins_to_a_credential(self.SEAM[0], "".join(self.SEAM[1:]), _default_redactor)

    def test_the_reader_sees_the_key_whole(self) -> None:
        assert KEY in _on_screen(self.SEAM)
        assert all(KEY not in chunk for chunk in self.SEAM)

    def test_the_rendered_reading_refuses_it(self) -> None:
        rendered = [chunk.strip() for chunk in self.SEAM]
        assert joins_to_a_credential(rendered[0], "".join(rendered[1:]), _default_redactor)

    def test_the_streaming_renderer_grades_the_rendered_text(self) -> None:
        """Structural: the seal loop must strip edges before it grades."""
        tree = ast.parse((SRC / "whatsapp" / "turn_renderer.py").read_text(encoding="utf-8"))
        stripped_lists = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ListComp)
            and isinstance(node.elt, ast.Call)
            and _name_of(node.elt.func) == "strip"
        ]
        assert stripped_lists, "the seal loop must grade the rendered chunks"

    def test_both_grades_agree_on_this_seam(self) -> None:
        """The sequence grade and the seam grade answer the same question."""
        assert _rejoins_a_key(self.SEAM, _default_redactor)


class TestAHardByteCapIsNeverExceeded:
    """The splitter may answer with the text WHOLE, so a hard-capped transport
    has to bound the result itself.

    Declining to cut is the fail-closed answer when no budget is clean, and it
    costs the caller a chunk over its budget. A client that truncates a larger
    payload would drop the answer's tail with no notice, so that caller grades the
    sequence once more and cuts the repair back to its budget.
    """

    def test_webex_bounds_its_own_chunks(self) -> None:
        from kiro_crew.webex.renderer import WEBEX_MAX_TEXT, _bounded_chunks

        oversized = "x" * 9000
        body = f"[label](https://example.test/{oversized}/{HEAD} {TAIL})"
        chunks = _bounded_chunks(body)
        assert chunks
        assert all(len(chunk.encode()) <= WEBEX_MAX_TEXT for chunk in chunks)

    def test_a_single_oversized_chunk_is_still_bounded(self) -> None:
        """The case the grade alone skips: one chunk holds no boundary.

        The splitter's fail-closed answer is a list of ONE chunk, so a grade asked
        about it reports that nothing rejoins, and the oversized chunk would travel
        on unbounded -- which is the situation this bound exists for.
        """
        budget = 40
        oversized = ["x" * 300]
        out = bounded_for_delivery(oversized, budget, _default_redactor)
        assert len(out) > 1
        assert all(len(chunk) <= budget for chunk in out)
        assert "".join(out) == oversized[0], "every character still ships"

    def test_cutting_an_oversized_chunk_cannot_expose_a_key(self) -> None:
        """Applying the budget first is what lets the grade see the real shape."""
        budget = 40
        hidden = ["y" * 30 + f" {HEAD} {TAIL} " + "z" * 30]
        out = bounded_for_delivery(hidden, budget, _default_redactor)
        assert all(len(chunk) <= budget for chunk in out)
        assert KEY not in _on_screen(out)

    def test_text_already_inside_the_budget_is_untouched(self) -> None:
        assert bounded_for_delivery(["hello"], 40, _default_redactor) == ["hello"]

    def test_webex_keeps_ordinary_text_whole(self) -> None:
        from kiro_crew.webex.renderer import _bounded_chunks

        prose = "an ordinary answer with nothing to redact in it"
        assert "".join(_bounded_chunks(prose)) == prose

    @pytest.mark.parametrize(
        "path",
        [
            "webex/renderer.py",
            "teams/renderer.py",
            "wecom/renderer.py",
            "dashboard/chat_mirror.py",
            "telegram/renderer.py",
        ],
    )
    def test_every_capped_caller_runs_the_bound(self, path: str) -> None:
        """A transport that truncates has to bound the splitter's whole answer."""
        tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
        called = {_name_of(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        offloaded = {
            _name_of(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _name_of(node.func) == "to_thread" and node.args
        }
        assert "bounded_for_delivery" in (
            called | offloaded
        ), f"{path} sends the splitter's answer without bounding it"


class TestTheFramesAChannelActuallySends:
    """One channel end to end, through the function its client sends from."""

    def test_whatsapp_delivery_chunks_cannot_be_rejoined(self) -> None:
        frames = render_chunks(f"{'w ' * 18}{HEAD}\n{TAIL} and some trailing words", 50)
        assert frames
        assert KEY not in "".join(frames)
        for frame in frames:
            assert KEY not in frame
        assert not any(
            joins_to_a_credential(frames[i], frames[i + 1], _default_redactor)
            for i in range(len(frames) - 1)
        )


class TestTelegramRotatesWithoutHandingOverAKey:
    """The rotation cut, through the function the channel rotates with.

    A rotation seals every chunk but the last as its own message and redacts each
    segment on its own, so a boundary between the halves of a key is a boundary
    neither message reports. The channel's own budget floor is 400 characters, so
    the text here is sized against that rather than a toy budget.
    """

    #: Telegram's ``_MIN_SPLIT_LIMIT``, which is the smallest budget its bounded
    #: splitter will use, so a smaller one here would not exercise a real cut.
    BUDGET = 400

    def _body(self) -> str:
        """Filler, then the key's halves either side of a line break."""
        return ("w " * 180) + HEAD + "\n" + TAIL + " and some trailing words after it"

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        """The control: without the redactor this is exactly what ships."""
        chunks = split_markdown_safe(self._body(), self.BUDGET)
        assert len(chunks) > 1
        assert _rejoins(chunks)
        assert KEY in _on_screen(chunks)

    def test_the_rotation_cut_moves_instead(self) -> None:
        chunks = _split_markdown(self._body(), self.BUDGET)
        assert len(chunks) > 1
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)
        for chunk in chunks:
            assert KEY not in chunk

    def test_every_character_still_ships(self) -> None:
        body = self._body()
        chunks = _split_markdown(body, self.BUDGET)
        assert "".join(chunks).replace("\n", "") == body.replace("\n", "")

    def test_the_bounded_splitter_carries_the_same_guard(self) -> None:
        chunks = _split_markdown_bounded(self._body(), self.BUDGET)
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)

    def test_a_declined_cut_is_still_bounded(self) -> None:
        """A cut the splitter refuses may not travel as one oversized message."""
        dense = " ".join(f"{HEAD} {TAIL}" for _ in range(40))
        chunks = _split_markdown_bounded(dense, self.BUDGET)
        assert chunks
        assert max(len(chunk) for chunk in chunks) <= self.BUDGET

    def test_ordinary_prose_is_returned_unchanged(self) -> None:
        prose = "Some ordinary sentence. " * 40
        assert "".join(_split_markdown(prose, self.BUDGET)).replace("\n", "") == prose.replace(
            "\n", ""
        )

    #: A table whose last cell ends its line, then prose. Each block is cut on its
    #: own, so the boundary between them belongs to neither cut.
    ACROSS_TWO_BLOCKS = (
        "col | val\n--- | ---\nrow | " + HEAD + "\n\n" + TAIL + " then ordinary trailing prose."
    )

    def test_the_two_blocks_alone_hand_over_the_key(self) -> None:
        """The control: the per-block sequence really does rejoin it."""
        blocks = ["\n".join(lines) for _, lines in _table_blocks(self.ACROSS_TWO_BLOCKS)]
        assert len(blocks) == 2
        assert _rejoins(blocks)
        assert KEY in _on_screen(blocks)

    def test_the_table_aware_cut_grades_that_seam(self) -> None:
        chunks = _split_markdown_table_aware(self.ACROSS_TWO_BLOCKS, 600, 80)
        assert chunks
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)
