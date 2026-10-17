"""A message already delivered is the predecessor every later seam is graded against.

A sealed chunk ending in a credential prefix, or a heading or blockquote marker
opening the message below it, shows a key no per-message scan reports. Every
sealing path inherits the seam guard, each delivery path counts the placeholder
the repair adds, and the last resort gives up only the span that hides the key.
"""

from __future__ import annotations

import ast
import re

import pytest
from split_credential_boundary_helpers import HEAD, KEY, SRC, TAIL, _name_of, _on_screen

from kiro_crew.messaging.display_safety import (
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
)
from kiro_crew.messaging.renderer import (
    _default_redactor,
    count_redaction_tags,
    repaired_after_a_sent_tail,
)
from kiro_crew.messaging.split import (
    _WHITESPACE_RUN,
    _made_collapse_clean,
    _redact_only_the_rejoined_span,
    _rejoins_a_key,
    offset_clear_of_a_sent_tail,
    split_markdown_safe,
)
from kiro_crew.telegram.renderer import _md_to_telegram_html, _split_markdown, _strip_md


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


class TestAHeadingMarkerAfterTheSeamCannotJoinAKey:
    """A field name sealed above ``#   : <value>`` reads as the assignment on screen.

    The splitter cuts at the line break, so the sealed message ends with the field
    name and the next begins with the heading marker. Telegram's HTML seal and its
    plain fallback both drop that marker, so the reader reads the name and then
    the value, while the marker keeps the two apart in every scan of the bytes.
    """

    NAME = "SecretAccessKey"
    VALUE = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"

    def _rotate(self, buf: str, tail: str, sent: list[str]) -> tuple[str, str]:
        pieces = _split_markdown(buf, 400)
        if len(pieces) <= 1:
            return buf, tail
        for piece in pieces[:-1]:
            repaired = repaired_after_a_sent_tail(tail, piece, _default_redactor)
            piece = repaired if repaired is not None else piece
            sent.append(piece)
            tail = piece
        return pieces[-1], tail

    def _frames(self) -> list[str]:
        """Stream the name, a line break and the marker, rotate, then the value.

        The rotation cuts at the line break, so the sealed message ends with the
        name and the retained tail is the marker alone. The value arrives after the
        seal, so no scan of the buffer at the cut could see it.
        """
        sent: list[str] = []
        buf, tail = self._rotate(("w " * 191) + f"{self.NAME}\n#   ", "", sent)
        buf, tail = self._rotate(
            f"{buf}: {self.VALUE} and more ordinary prose after it.", tail, sent
        )
        shown = repaired_after_a_sent_tail(tail, buf, _default_redactor) or buf
        return [*sent, shown]

    def _read_by_a_telegram_client(self, frames: list[str]) -> list[str]:
        html_frames = [re.sub(r"<[^>]+>", "", _md_to_telegram_html(frame)) for frame in frames]
        plain_frames = [_strip_md(frame) for frame in frames]
        return [frame.strip() for frame in (*html_frames, *plain_frames)]

    def test_the_marker_opens_the_frame_below_the_name(self) -> None:
        """The premise: the sealed message ends with the name, the marker follows."""
        frames = self._frames()
        assert len(frames) == 2
        assert frames[0].endswith(self.NAME)
        assert _default_redactor(frames[0]) == frames[0]

    def test_no_frame_and_no_adjacent_pair_shows_the_key(self) -> None:
        frames = self._frames()
        readings = self._read_by_a_telegram_client(frames)
        assert all(_default_redactor(frame) == frame for frame in readings)
        for above, below in zip(readings, readings[1:]):
            assert _default_redactor(above + below) == above + below, (above, below)
            assert _default_redactor(f"{above}\n{below}") == f"{above}\n{below}", (above, below)
        assert self.VALUE not in "".join(frames)

    def test_a_hard_cut_before_the_marker_is_refused_by_the_sequence_grade(self) -> None:
        """The same shape through the splitter's whole-sequence grade.

        The marker sits mid-line in the whole text, so no reading of the text as
        written drops it. A hard cut landing just before it opens a chunk with the
        marker at its start, where a client drops it.
        """
        text = "w" * 400 + f" {self.NAME} #   : {self.VALUE} and more ordinary prose after it."
        limit = len("w" * 400 + f" {self.NAME} ")
        assert _default_redactor(text) == text
        cut_blind = split_markdown_safe(text, limit)
        assert cut_blind[1].startswith("#")
        assert _rejoins_a_key(cut_blind, _default_redactor)
        chunks = split_markdown_safe(text, limit, redactor=_default_redactor)
        readings = self._read_by_a_telegram_client(chunks)
        for above, below in zip(readings, readings[1:]):
            assert _default_redactor(f"{above}\n{below}") == f"{above}\n{below}", (above, below)


class TestADiscordBlockquoteAfterTheSeamCannotJoinAKey:
    """A ``> ``/``>>> `` opening the message below a key prefix is graded whole.

    Discord removes a line-leading blockquote marker at render, so a credential
    whose prefix ends one message and whose suffix opens the next as ``> suffix``
    reads whole on screen. The canonical display reading collapses the marker, so
    the seam grade sees the joined key and refuses the pair.
    """

    HEAD = HEAD
    TAIL = TAIL

    def test_the_single_line_blockquote_marker_is_collapsed(self) -> None:
        joined = self.HEAD + "\n> " + self.TAIL
        # The marker is removed, so the canonical reading is the same as if the
        # halves were plainly adjacent -- which is what makes the seam graded.
        assert "> " not in canonicalize_display(joined)
        assert canonicalize_display(joined) == canonicalize_display(self.HEAD + "\n" + self.TAIL)

    def test_the_multiline_blockquote_marker_is_collapsed(self) -> None:
        joined = self.HEAD + "\n>>> " + self.TAIL
        assert canonicalize_display(joined) == canonicalize_display(self.HEAD + "\n" + self.TAIL)

    def test_a_mid_line_gt_is_left_untouched(self) -> None:
        # A ``>`` that is not a line-leading blockquote marker (a shell prompt, a
        # comparison in prose) must survive byte-exact.
        prose = "run a > b in the shell, plainly"
        assert canonicalize_display(prose) == prose

    def test_the_seam_grade_refuses_the_blockquote_pair(self) -> None:
        chunks = [self.HEAD, "> " + self.TAIL]
        assert _rejoins_a_key(chunks, _default_redactor)


class TestEverySealingPathInheritsTheSeamGuard:
    """One grader and one writer, in the sinks, not at the call sites.

    Five paths seal a segment as its own message -- a length rotation, an
    upload-hold rotation, a steer boundary, a degraded table chunk and the end of
    the turn -- so a guard written at one of them is not a guard on the channel.
    Both sinks run the same helper, and no caller carries a seam rule of its own.
    """

    TELEGRAM = "telegram/renderer.py"

    def _tree(self) -> ast.AST:
        # The facade and the owners its methods are bound from, read as one module.
        paths = [SRC / self.TELEGRAM, *sorted((SRC / "telegram").glob("renderer_*.py"))]
        return ast.Module(
            body=[
                node for path in paths for node in ast.parse(path.read_text(encoding="utf-8")).body
            ],
            type_ignores=[],
        )

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
