"""Each channel's own send path keeps the credential-aware cut bounded, stable and off the loop.

A declined cut stays under the transport cap, a streamed reply never revises a
delivered chunk, the grade runs in a worker thread, and the seam is read as the
client renders it, through WhatsApp's and Telegram's real send and rotation
functions.
"""

from __future__ import annotations

import ast

import pytest
from split_credential_boundary_helpers import HEAD, KEY, SRC, TAIL, _name_of, _on_screen, _rejoins

from kiro_crew.messaging.display_safety import joins_to_a_credential, redact_for_display
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.messaging.split import (
    _redact_only_the_rejoined_span,
    _rejoins_a_key,
    bounded_for_delivery,
    split_markdown_safe,
)
from kiro_crew.telegram.renderer import (
    _split_markdown,
    _split_markdown_bounded,
    _split_markdown_table_aware,
    _table_blocks,
)
from kiro_crew.whatsapp.renderer import _redact_all, render_chunks, to_whatsapp_text


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

    def test_the_stable_split_is_a_plain_prefix_stable_redacted_cut(self) -> None:
        """The stable split redacts, then cuts at the budget -- and nothing later.

        A sealed chunk is a promise to the client, so this branch runs no
        whole-body repair: it splits the display-redacted body at the budget, so
        chunk *i* is a function of the text before it alone. The cross-message
        seam (a chunk ending in a key PREFIX the next completes) is the turn
        renderer's job -- it knows which chunks are still unsealed and gives up
        only the completing span on a boundary it has not yet promised.
        """
        converted = to_whatsapp_text(self._dense())
        redacted, _ = redact_for_display(converted, _redact_all)
        expected = split_markdown_safe(redacted, self.LIMIT)
        assert render_chunks(self._dense(), self.LIMIT, stable=True) == expected
        # The delivered chunks carry no whole key on their own -- redaction ran
        # before the cut, so a key inside one message is a marker, not the key.
        assert KEY not in _on_screen(render_chunks(self._dense(), self.LIMIT, stable=True))

    def test_the_stable_split_never_revises_an_earlier_chunk_as_the_body_grows(self) -> None:
        """Prefix stability: appending text leaves every earlier chunk byte-exact.

        This is the property a sealed-chunk promise needs -- a later frame may add
        chunks but must not rewrite one the caller already sent.
        """
        body = self._dense()
        grown = body + " " + self._dense()
        short = render_chunks(body, self.LIMIT, stable=True)
        long = render_chunks(grown, self.LIMIT, stable=True)
        # Every chunk but the last of the shorter split is a prefix-stable boundary
        # that the longer split reproduces unchanged.
        assert long[: len(short) - 1] == short[:-1]

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
        """The two cuts the streaming contract is about.

        ``stable`` is the WhatsApp renderer's own branch -- redact the whole body,
        then cut at the budget and nowhere else -- written out here as the two calls
        it makes, because the shared splitter carries no mode for one caller.
        """
        if stable:
            redacted, _ = redact_for_display(text, _default_redactor)
            return split_markdown_safe(redacted, self.LIMIT)
        return split_markdown_safe(text, self.LIMIT, redactor=_default_redactor)

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
