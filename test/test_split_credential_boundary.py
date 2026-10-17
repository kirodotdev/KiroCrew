"""A cut may not hand the reader a credential neither chunk holds.

Two halves of a key, one per message, read as one key down the screen. The
per-chunk scan clears both halves, so nothing reports it: the reader sees the
key and the operator sees a clean send. The splitter therefore has to know about
credentials before it chooses a boundary, and every path that turns one chunk
into one message has to tell it, which is what the enumeration below enforces.

This module keeps the enumeration and the splitter core: the char and byte
splitters, ``chunk_for_transport``, the budget search, the terminal repair, the
last-step regrade and the dashboard legs. The seams under a delivered message,
each channel's own send path, the grade by every reading, and Discord's rotation
with the delivery tallies live in the ``test_split_credential_boundary_<topic>.py``
parts. The key, the source root and the screen readers they share live in
``split_credential_boundary_helpers.py``.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import NamedTuple

import pytest
from source_corpus import parsed_candidates, src_root
from split_credential_boundary_helpers import (
    HEAD,
    KEY,
    SPLITTERS,
    SRC,
    TAIL,
    _name_of,
    _on_screen,
    _rejoins,
)

from kiro_crew.messaging.display_safety import (
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
)
from kiro_crew.messaging.renderer import _default_redactor, chunk_for_transport, chunk_text
from kiro_crew.messaging.split import (
    _WHITESPACE_RUN,
    _redact_only_the_rejoined_span,
    _rejoins_a_key,
    _under_a_safe_budget,
    bounded_for_delivery,
    chunk_utf8_bytes,
    repaired_for_delivery,
    split_markdown_bytes,
    split_markdown_safe,
)
from kiro_crew.messaging.transport import TransportCapabilities

#: No splitter call needs an exception any more: an already-redacted argument (the
#: WhatsApp prefix-stable branch's ``redact_for_display`` body) is recognised as
#: guarded by ``_repaired_names``, so the universal assertion below stays universal
#: -- a call that neither passes a redactor nor cuts already-redacted text fails.
KNOWN_DELIVERY_MODULES = frozenset(
    {
        "dashboard/chat_mirror.py",
        "discord/renderer.py",
        "feishu/client.py",
        "messaging/renderer.py",
        "slack/renderer.py",
        "teams/renderer.py",
        "telegram/renderer.py",
        "webex/renderer.py",
        "wecom/renderer.py",
        "whatsapp/renderer.py",
    }
)


def _repaired_names(tree: ast.AST) -> set[str]:
    """Names bound to text that already carries the seam guarantee, in *tree*.

    Only :func:`repaired_for_delivery` counts: it is returned only once the form
    with every whitespace run removed reads clean, so no later cut can rejoin a
    key. The subject is then already safe to cut, so the cut needs no redactor and
    MUST NOT take one (a credential-aware cut may decline to cut, which a caller
    re-bounding to a hard transport cap cannot accept).

    ``redact_for_display`` / ``_display_safe`` are deliberately NOT here: they do
    no whitespace collapsing (``strip_ansi`` -> redactor -> ``_strip_format_chars``
    -> ``canonicalize_display``), so ``AKIA\\nIOSFODNN7EXAMPLE`` passes through them
    unredacted and a later no-redactor cut at that newline delivers the key whole
    across two messages. Certifying their output "safe to cut again" would weaken
    the universal ``test_every_splitter_call_passes_a_redactor`` gate to a property
    only ``repaired_for_delivery`` (and the splitter's own answers) establish. The
    WhatsApp prefix-stable branch instead routes its ``redacted`` through
    ``repaired_for_delivery`` before the no-redactor cut.

    Recognising the name keeps the gate closed instead of exempting the call.
    """
    guaranteed_by_call = {
        "repaired_for_delivery",
    }

    def _guaranteeing_call(value: ast.expr) -> bool:
        # ``x = f(...)`` or ``x = f(...).strip()`` -- unwrap trailing whitespace
        # trims, which cannot reopen a key, to reach the safety-establishing call.
        node: ast.expr = value
        while (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"strip", "rstrip", "lstrip"}
        ):
            node = node.func.value
        return isinstance(node, ast.Call) and _name_of(node.func) in guaranteed_by_call

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not _guaranteeing_call(node.value):
            continue
        for target in node.targets:
            # ``x = f(...)`` binds the whole return; ``x, _ = f(...)`` binds the
            # first element (the redacted body for the tuple-returning sinks).
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)) and target.elts:
                first = target.elts[0]
                if isinstance(first, ast.Name):
                    names.add(first.id)
    return names


class SplitterCall(NamedTuple):
    """One splitter call: which splitter, its keywords, and the function holding it."""

    splitter: str
    keywords: frozenset[str]
    function: str


def _splitter_calls(tree: ast.AST) -> list[SplitterCall]:
    """Every splitter call in *tree*, each with the function that holds it.

    Both shapes count. A renderer that offloads the splitter to a worker thread
    passes it as a REFERENCE to ``asyncio.to_thread`` and hands the arguments to
    ``to_thread``, so a check that only reads direct calls sees four of these
    seven sites and reports the rest as absent rather than as unguarded.

    A cut whose subject is already-repaired text is reported as guarded, because
    the repair carries the guarantee the redactor would be there to establish.

    The enclosing function is carried because an exemption is a claim about one
    call: without it the only key available is the module, and a module-wide key
    exempts calls no claim describes.
    """
    repaired = _repaired_names(tree)
    # A splitter call handed STRAIGHT to ``repaired_for_delivery`` as the pieces it
    # grades is a MEASUREMENT split, not a delivery: its boundaries are never sent
    # as-is -- ``repaired_for_delivery`` reads them to decide whether a cut at that
    # budget would rejoin a key, and the caller then delivers a re-split of the
    # returned collapse fixed point (or the already-safe source when it returns
    # ``None``). This is the same grade-then-resplit idiom ``messaging/renderer.py``
    # uses on the byte splitter; recognising it keeps the gate closed on a
    # genuinely-safe shape instead of forcing a redactor into a cut that must stay
    # prefix-stable. Only the call in the pieces position is spared -- the delivery
    # re-split still has to name a ``repaired_for_delivery`` result.
    measurement: set[int] = set()
    graded_sources: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _name_of(node.func) == "repaired_for_delivery"):
            continue
        if node.args:
            # The body handed to the grader as its SOURCE is grade-covered: the
            # grade returns a collapse fixed point when a cut would rejoin a key,
            # and ``None`` only when the body is ALREADY a fixed point safe to cut.
            # So the caller's fallback -- cutting this same body when the grade
            # returned ``None`` -- delivers a body the grader vouched for.
            source_name = _name_of(node.args[0])
            if source_name:
                graded_sources.add(source_name)
        if len(node.args) >= 2:
            measurement.add(id(node.args[1]))
    calls: list[SplitterCall] = []
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, holder = stack.pop()
        for child in ast.iter_child_nodes(node):
            inner = holder
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            if isinstance(child, ast.Call):
                keywords = {kw.arg for kw in child.keywords if kw.arg}
                if child.args and _name_of(child.args[0]) in repaired:
                    keywords.add("redactor")
                if child.args and _name_of(child.args[0]) in graded_sources:
                    # Cutting a body the grader vouched for (its ``None`` = safe).
                    keywords.add("redactor")
                if id(child) in measurement:
                    # The pieces this cut produced are graded, never delivered.
                    keywords.add("redactor")
                called = _name_of(child.func)
                if called in SPLITTERS:
                    calls.append(SplitterCall(called, frozenset(keywords), holder))
                elif called == "to_thread" and child.args:
                    offloaded = _name_of(child.args[0])
                    if offloaded in SPLITTERS:
                        calls.append(SplitterCall(offloaded, frozenset(keywords), holder))
            stack.append((child, inner))
    return calls


def _modules_that_split() -> dict[str, list[SplitterCall]]:
    """Every module under ``src/kiro_crew`` that calls a redactor-accepting splitter.

    DERIVED by walking the tree, not curated. A curated list of delivery paths has
    one failure mode that matters: a path nobody adds to it is not merely
    unchecked, it is invisible, and the paths most likely to be forgotten are the
    new ones. Reaching a splitter is a property of the code, so the population
    reads it off the code -- a module joins the moment it calls one, and the
    enforcement below then applies to it without anyone remembering to say so.

    ``messaging/split.py`` itself is skipped: it DEFINES these functions and its
    own recursive cut deliberately passes no redactor one level down.

    Parses only the files whose text spells a splitter's name
    (``test/source_corpus.py``'s shared, narrowed read): a call is recorded by the
    identifier it calls or hands to ``to_thread``, so a file without that identifier
    holds no call to record, and the narrowing drops no module. This runs at import,
    which is collection time here and the body of any test elsewhere that imports
    this module to drive one of its gates, so it must cost a few files, not the tree.
    """
    found: dict[str, list[tuple[str, set[str]]]] = {}
    root = src_root()
    for path, _text, tree in parsed_candidates(
        require_any=sorted(SPLITTERS), skip_syntax_errors=False
    ):
        relative = path.relative_to(root).as_posix()
        if relative == "messaging/split.py":
            continue
        calls = _splitter_calls(tree)
        if calls:
            found[relative] = calls
    return found


#: Resolved once at collection time: the tests below parametrize over it.
SPLITTING_MODULES = _modules_that_split()


def _unguarded(path: str) -> list[SplitterCall]:
    """Every call in *path* that passes no redactor."""
    return [call for call in SPLITTING_MODULES[path] if "redactor" not in call.keywords]


class TestEveryDeliveryPathIsEnumerated:
    """The enforcement: a path that forgets the redactor fails here.

    The population is derived from the tree, so "enumerated" means the code says
    so rather than that somebody listed it.
    """

    def test_the_walk_reaches_the_channels_that_deliver(self) -> None:
        """A derivation that finds nothing, or too little, must not pass quietly."""
        missing = sorted(KNOWN_DELIVERY_MODULES - set(SPLITTING_MODULES))
        assert not missing, (
            f"the module walk did not reach {missing}, so the population is smaller "
            "than the tree really holds and an unguarded call in a module it missed "
            "would pass unseen. If delivery genuinely moved out of one of these, "
            "move it in KNOWN_DELIVERY_MODULES too."
        )

    @pytest.mark.parametrize("path", sorted(SPLITTING_MODULES))
    def test_every_splitter_call_passes_a_redactor(self, path: str) -> None:
        """Universal, no exceptions: every splitter call is guarded.

        Guarded means one of two things ``_splitter_calls`` recognises: the call
        passes ``redactor=``, or its subject is already-redacted text (the output
        of ``repaired_for_delivery`` / ``redact_for_display``), which carries the
        same seam guarantee. A call that is neither chooses its boundary by length
        alone and can sever a key across two adjacent messages.
        """
        offenders = _unguarded(path)
        assert not offenders, (
            f"{path} calls {[call.splitter for call in offenders]} with no redactor in "
            f"{sorted({call.function for call in offenders})}, so the boundary is "
            "chosen by a length budget alone and a severed key reaches two "
            "adjacent messages. Pass redactor=, or cut already-redacted text."
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


class TestChunkForTransportGradesTheByteSeam:
    """``chunk_for_transport`` closes the seam on a byte-capped channel too.

    The byte splitter takes no redactor, and ``bounded_for_delivery`` grades only
    the slices IT cuts inside a chunk -- never the splitter's own inter-chunk
    seams. So the entry point has to grade the split SEQUENCE before bounding it,
    the same guarantee the char branch gets by threading the redactor into its
    splitter. Without it a credential straddling a byte-splitter boundary ships
    whole across two delivered messages on Webex and WeCom.
    """

    def _byte_capped(self, max_bytes: int) -> TransportCapabilities:
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES

        return dataclasses.replace(
            WEBEX_CAPABILITIES,
            max_message_bytes=max_bytes,
            max_message_chars=max_bytes,
        )

    def test_the_raw_byte_split_alone_would_hand_over_the_key(self) -> None:
        # Control: the byte splitter with no redactor leaves the key rejoinable
        # across a seam, which is exactly the leak the entry point must close. The
        # key straddles a line break, so the splitter cuts HEAD and TAIL into
        # adjacent chunks the reader reads as one key once the break is gone.
        text = f"{HEAD}\n{TAIL} trailing here to force more content and length beyond"
        raw = split_markdown_bytes(text, 11)
        assert len(raw) > 1
        assert _rejoins(raw)

    def test_chunk_for_transport_closes_that_seam(self) -> None:
        text = f"{HEAD}\n{TAIL} trailing here to force more content and length beyond"
        chunks = chunk_for_transport(text, self._byte_capped(11))
        assert len(chunks) > 1
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)

    def test_clean_byte_capped_text_is_unchanged(self) -> None:
        text = "a table row | another cell | a third cell that makes this long"
        chunks = chunk_for_transport(text, self._byte_capped(20))
        assert "".join(chunks) == text

    def _char_capped(self, max_chars: int) -> TransportCapabilities:
        # A char-capped channel: max_message_bytes=0 takes chunk_for_transport's
        # CHARACTER branch (messaging/renderer.py), the one F2 lives on.
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES

        return dataclasses.replace(
            WEBEX_CAPABILITIES, max_message_bytes=0, max_message_chars=max_chars
        )

    def test_the_byte_path_never_returns_a_rejoining_sequence(self) -> None:
        """F2: exhaustion must fall closed, not return the last unsafe sequence.

        The re-grade loop shrinks the byte budget toward the 1-byte floor and, if
        even the floor cannot separate the halves (a run wider than one byte between
        them), gives up the whitespace via the splitter's fail-closed answer rather
        than returning a sequence that still rejoins. Across budgets from the
        transport cap down to a few bytes -- including the heading-marker seam and a
        wide-space straddle -- no delivered sequence may read as a key, and the call
        must terminate.
        """
        from kiro_crew.messaging.renderer import _default_redactor as _dr

        head, tail = "AKIAIOSFOD", "NN7EXAMPLE"
        bodies = (
            head + "# " + tail + " x" * 20,  # heading-marker seam (F7 shape)
            head + " " * 40 + tail + " filler" * 20,  # wide-space straddle -> floor
        )
        for body in bodies:
            for budget in (7439, 512, 64, 20, 13, 9):
                chunks = chunk_for_transport(body, self._byte_capped(budget))
                assert chunks, "nothing delivered"
                for reading in (
                    canonicalize_display("".join(chunks)),
                    "".join(canonicalize_display(c) for c in chunks),
                ):
                    assert _dr(reading) == reading, (
                        f"byte path returned a rejoining sequence at budget {budget} "
                        f"for {body[:24]!r}"
                    )

    def test_a_byte_heading_marker_seam_clean_whole_text_is_regraded(self) -> None:
        """F7: a repair that returns the source unchanged must still be re-graded.

        ``repaired_for_delivery`` answers with the source UNCHANGED when no literal
        span collapses to a key and the whole-text canonical collapse reads clean.
        But this PR's ``^``-anchored heading rule is POSITION-dependent: a mid-line
        ``AKIA# NN7EXAMPLE`` is clean whole-text (the ``#`` is not line-leading), yet
        a byte cut that makes ``# NN7EXAMPLE`` piece-leading strips the ``#`` at
        render and rejoins the key across two messages. A single re-split of the
        unchanged repair reproduces that unsafe sequence, so the byte path must
        re-grade and shrink the budget until the delivered sequence is clean.
        """
        from kiro_crew.messaging.renderer import _default_redactor as _dr

        head, tail = "AKIAIOSFOD", "NN7EXAMPLE"
        # budget 10: the byte cut lands exactly so ``# NN7EXAMPLE`` is piece-leading
        # (measured; the OLD one-pass byte path leaked this exact fixture).
        body = head + "# " + tail + " x" * 20
        assert _dr(body) == body, "fixture must be clean whole-text (mid-line #)"
        chunks = chunk_for_transport(body, self._byte_capped(10))
        assert chunks, "nothing delivered"
        # The reader-view for a heading marker is the CANONICALISED per-frame form:
        # Webex/WeCom render a piece-leading ``#`` away, so ``_on_screen`` (which only
        # strips edge whitespace) does not see this seam -- canonicalise each frame.
        for reading in (
            canonicalize_display("".join(chunks)),
            "".join(canonicalize_display(c) for c in chunks),
        ):
            assert _dr(reading) == reading, (
                "the heading-marker seam rejoined the key across byte-capped messages "
                "-- the repaired re-split was not re-graded"
            )

    def test_the_char_branch_caps_an_oversized_fence_scaffolding_chunk(self) -> None:
        """F2: the char path must not ship a chunk OVER the budget.

        ``split_markdown_safe`` is documented to return a chunk over the budget by
        its fence scaffolding -- a logical line (here a long fence opener/info
        string) that admits no clean cut is placed whole, adding the reopener and
        synthetic closer on top. Every caller delivers each unit as its own message
        and the transport truncates a larger one with no signal, so the entry point
        must hand ``bounded_for_delivery`` the hard character cutter (``chunk_text``)
        rather than defaulting back to the splitter that produced the oversized
        chunk. Every delivered chunk must sit within the budget.
        """
        budget = 20
        # A no-clean-cut fence corpus: lines that admit no boundary clean on both
        # sides are placed whole with their reopen/closer scaffolding on top, so
        # ``split_markdown_safe`` returns chunks well over the budget (documented in
        # split.py). Same shape test_messaging_split.py uses for this regime.
        bare = "`" * 24 + "\n"
        fenced = "y" + "`" * 18 + "\n"
        text = "hi\n" + bare + "```py\n" + fenced + "```\n" + "z\n"
        # Premise: the plain splitter alone leaves over-budget chunks.
        raw = split_markdown_safe(text, budget)
        assert any(len(chunk) > budget for chunk in raw), "fixture has no oversized chunk"
        # chunk_for_transport must cap every delivered chunk to the budget.
        chunks = chunk_for_transport(text, self._char_capped(budget))
        assert chunks, "nothing delivered"
        assert all(
            len(chunk) <= budget for chunk in chunks
        ), f"an over-budget chunk survived: {[len(c) for c in chunks]}"


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
        """Every whitespace rewrite is in one of four named places.

        One reads to decide, two rewrite to repair, and one is the last resort
        behind the cutter-graded repair, reached only once every narrower repair
        still rejoins a key across the caller's own re-cut. A fifth would be a
        fresh way for a valid span to lose its formatting without anyone deciding
        it should.
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
            "_flattened_for_any_cut",
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

    def test_the_cutter_is_required(self) -> None:
        with pytest.raises(TypeError):
            repaired_for_delivery(self.SOURCE, self.SLICED, _default_redactor)

    def test_the_repair_fires_on_that_sequence(self) -> None:
        assert (
            repaired_for_delivery(self.SOURCE, self.SLICED, _default_redactor, lambda text: [text])
            is not None
        )

    def test_a_clean_sequence_needs_no_repair(self) -> None:
        assert (
            repaired_for_delivery(
                "ordinary text here and more of it",
                ["ordinary text here", "and more of it"],
                _default_redactor,
                lambda text: [text],
            )
            is None
        )

    @pytest.mark.parametrize("cap", [5, 9, 13, 20, 40])
    def test_the_repair_survives_any_re_cut(self, cap: int) -> None:
        """Why one grade per chunk is enough: the repair is safe to bound again."""
        repaired = repaired_for_delivery(
            self.SOURCE, self.SLICED, _default_redactor, lambda text: [text]
        )
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
        repaired = repaired_for_delivery(source, pieces, _default_redactor, lambda text: [text])
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
        fallback = repaired_for_delivery(source, pieces, _default_redactor, lambda text: [text])
        assert fallback is not None
        collapsed = _WHITESPACE_RUN.sub("", canonicalize_display(fallback))
        assert _default_redactor(collapsed) == collapsed

    def test_the_slack_path_runs_that_grade(self) -> None:
        tree = ast.parse((SRC / "slack" / "renderer.py").read_text(encoding="utf-8"))
        called = {_name_of(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert "repaired_for_delivery" in called


class TestTheDashboardHandlerLegsOffloadTheSplitter:
    """The dashboard proactive-send legs must not grade on the event loop.

    This PR threaded the credential-aware redactor into the char path
    (``messaging/renderer.py`` ``split_markdown_safe(..., redactor=redactor)``), so a
    ``chunk_for_transport`` call now runs the whole-text budget search (up to 128
    dense probes plus span-repair passes) rather than a plain length cut. The two
    dashboard handler legs -- ``_deliver_channel_dm`` and ``_send_to_channel_target``
    -- call it with model-authored, length-unchecked text on the gateway's single
    loop thread, where a large body would stall past the loop watchdog's 25s
    dump-then-exit alarm. Both must offload it, as the renderer's own send legs do.
    """

    def _handler_calls(self) -> dict[str, list[tuple[str, bool]]]:
        """Per function: each ``chunk_for_transport`` call and whether it is offloaded.

        Offloaded = it is the first argument to ``asyncio.to_thread`` rather than a
        direct call.
        """
        from kiro_crew.dashboard.handlers import messaging

        # The legs run from a messaging_api owner the facade composes, so the scan
        # parses the facade and every owner, and fails if a leg lives in a file it
        # does not parse.
        owners = sorted((SRC / "dashboard" / "messaging_api").glob("[!_]*.py"))
        assert owners, "the messaging_api owners were not found"
        files = [SRC / "dashboard" / "handlers" / "messaging.py", *owners]
        for leg in ("_deliver_channel_dm", "_send_to_channel_target"):
            held = Path(getattr(messaging, leg).__code__.co_filename)
            assert held.parts[-2:] in {path.parts[-2:] for path in files}, (leg, held)
        tree = ast.Module(
            body=[
                stmt for path in files for stmt in ast.parse(path.read_text(encoding="utf-8")).body
            ],
            type_ignores=[],
        )
        found: dict[str, list[tuple[str, bool]]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if _name_of(call.func) == "chunk_for_transport":
                    found.setdefault(node.name, []).append(("direct", False))
                elif (
                    _name_of(call.func) == "to_thread"
                    and call.args
                    and _name_of(call.args[0]) == "chunk_for_transport"
                ):
                    found.setdefault(node.name, []).append(("offloaded", True))
        return found

    def test_both_dashboard_legs_offload_the_splitter(self) -> None:
        calls = self._handler_calls()
        legs = {"_deliver_channel_dm", "_send_to_channel_target"}
        reached = legs & set(calls)
        assert reached == legs, (
            f"the dashboard proactive-send legs that chunk are {sorted(reached)}, "
            f"expected {sorted(legs)} -- a renamed/removed leg must update this gate"
        )
        for leg in legs:
            assert calls[leg], f"{leg} no longer chunks -- drop it from this gate"
            offloaded = [shape for shape, is_off in calls[leg] if is_off]
            direct = [shape for shape, is_off in calls[leg] if not is_off]
            assert offloaded and not direct, (
                f"{leg} calls chunk_for_transport ON THE LOOP ({direct}); a large "
                "model-authored body would stall the loop past the 25s watchdog. "
                "Offload it with asyncio.to_thread like the renderer's send legs."
            )
