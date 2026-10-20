"""Discord's rotation, the delivery tallies and the table-aware split keep a key off the screen.

Discord seals every chunk but the last through one grader and one record writer,
WeCom and Slack add a repair's placeholders to what already shipped, Telegram's
table-aware split grades its assembled sequence, and the shared splitter offers
the prefix-stable streaming cut as a mode.
"""

from __future__ import annotations

import ast
import inspect

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

from kiro_crew.discord.client import DISCORD_MAX_TEXT
from kiro_crew.discord.renderer import _fit_platform_cap
from kiro_crew.discord.renderer import _redact_all as _discord_redact_all
from kiro_crew.messaging.split import bounded_for_delivery, split_markdown_safe
from kiro_crew.telegram.renderer import _split_markdown_table_aware, _table_blocks
from kiro_crew.whatsapp.renderer import render_chunks


def _discord_renderer_tree() -> ast.Module:
    """The Discord renderer's facade and the owners its methods are bound from, as one module."""
    paths = [SRC / "discord" / "renderer.py", *sorted((SRC / "discord").glob("renderer_*.py"))]
    return ast.Module(
        body=[node for path in paths for node in ast.parse(path.read_text(encoding="utf-8")).body],
        type_ignores=[],
    )


class TestDiscordRotatesWithoutHandingOverAKey:
    """Discord seals every chunk but the last as its own message, like Telegram.

    Its rotation redacts each segment alone, so a boundary between the halves of a
    key is a boundary neither message reports. Sized against the channel's real
    2000-character cap rather than a toy budget.
    """

    BUDGET = DISCORD_MAX_TEXT

    def _body(self) -> str:
        """Filler, then the key's halves either side of a line break."""
        return ("w " * 980) + HEAD + "\n" + TAIL + " and some trailing words after it"

    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        """The control: without the redactor this is exactly what ships."""
        chunks = split_markdown_safe(self._body(), self.BUDGET)
        assert len(chunks) > 1
        assert _rejoins(chunks)
        assert KEY in _on_screen(chunks)

    def test_the_rotation_cut_moves_instead(self) -> None:
        chunks = split_markdown_safe(self._body(), self.BUDGET, redactor=_discord_redact_all)
        assert len(chunks) > 1
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)
        for chunk in chunks:
            assert KEY not in chunk

    def test_every_character_still_ships(self) -> None:
        body = self._body()
        chunks = split_markdown_safe(body, self.BUDGET, redactor=_discord_redact_all)
        assert "".join(chunks).replace("\n", "") == body.replace("\n", "")

    def test_a_declined_cut_is_still_bounded(self) -> None:
        """The cap truncates a larger payload after every scan has run."""
        dense = " ".join(f"{HEAD} {TAIL}" for _ in range(120))
        chunks = bounded_for_delivery(
            split_markdown_safe(dense, self.BUDGET, redactor=_discord_redact_all),
            self.BUDGET,
            _discord_redact_all,
            _fit_platform_cap,
        )
        assert chunks
        assert max(len(chunk) for chunk in chunks) <= self.BUDGET

    def test_the_seam_record_has_exactly_one_writer(self) -> None:
        """One grader and one writer, so no sealing path carries its own rule."""
        tree = _discord_renderer_tree()
        writers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(inner, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute) and target.attr == "_sent_tail"
                    for target in inner.targets
                )
                for inner in ast.walk(node)
            )
        ]
        assert sorted(node.name for node in writers) == ["__init__", "_record_sent"], (
            "the predecessor record must be written in one place besides __init__, "
            "or a sealing path that forgets it ships an open seam"
        )

    def test_both_sinks_grade_against_the_message_above(self) -> None:
        """Two sinks show text -- the seal and the live frame -- and both must grade.

        Counted per sink, not merely present: the seal has two paths that show text,
        its ordinary one and its upload-recovery fallback, so asking only whether
        the function mentions the grade somewhere lets either path lose it while the
        other keeps the check satisfied.
        """
        tree = ast.parse((SRC / "discord" / "renderer.py").read_text(encoding="utf-8"))
        grades: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                # Both shapes: the direct call, and the REFERENCE handed to a worker
                # thread. Reading only direct calls sees one of the two sinks and
                # reports the offloaded one as ungraded.
                offloaded = _name_of(inner.args[0]) if inner.args else ""
                if _name_of(inner.func) == "_seam_safe" or (
                    _name_of(inner.func) == "to_thread" and offloaded == "_seam_safe"
                ):
                    grades[node.name] = grades.get(node.name, 0) + 1
        assert grades.get("_stream_live", 0) >= 1, "the live frame shows text ungraded"
        assert grades.get("_seal_current", 0) >= 2, (
            "the seal shows text on two paths -- its ordinary send and its "
            "upload-recovery fallback -- and each one needs its own grade"
        )

    def test_the_record_is_written_only_after_a_delivery_confirms(self) -> None:
        """Unsent text as the predecessor costs the NEXT message a leading span."""
        tree = ast.parse((SRC / "discord" / "renderer.py").read_text(encoding="utf-8"))
        grader = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_seam_safe"
        )
        assert not [
            inner
            for inner in ast.walk(grader)
            if isinstance(inner, ast.Assign)
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_sent_tail"
                for target in inner.targets
            )
        ], "the grader must not record: every send and edit below it can still fail"

    def test_the_upload_recovery_grades_before_it_splits(self) -> None:
        """The recovery branch shows text too, under a message already sealed.

        It is reached when the files-bearing chunk fails to land, and it records its
        own chunk as the next seam's predecessor -- so a recovery that skipped the
        grade would ship the un-repaired suffix of a key whose prefix is in the
        sealed message above, and then stand in as a graded predecessor.

        Read off the grade's position relative to the split, because the order is
        the property: grading after the cut would leave the boundary chosen already.
        """
        tree = ast.parse((SRC / "discord" / "renderer.py").read_text(encoding="utf-8"))
        seal = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_seal_current"
        )
        graded_at: list[int] = []
        split_at: list[int] = []
        recorded_at: list[int] = []
        for inner in ast.walk(seal):
            if not isinstance(inner, ast.Call):
                continue
            offloaded = _name_of(inner.args[0]) if inner.args else ""
            if _name_of(inner.func) == "_seam_safe" or offloaded == "_seam_safe":
                graded_at.append(inner.lineno)
            if offloaded in SPLITTERS or _name_of(inner.func) in SPLITTERS:
                split_at.append(inner.lineno)
            if _name_of(inner.func) == "_record_sent":
                recorded_at.append(inner.lineno)
        assert len(graded_at) >= 2, (
            "both the seal and its upload-recovery fallback show text, so both "
            "grade: one grade covers only the path that happened to be read"
        )
        assert recorded_at, "the recovery records a predecessor, so it must grade one"
        for split_line in split_at:
            assert any(
                grade_line < split_line for grade_line in graded_at
            ), f"the splitter call at line {split_line} runs before any seam grade"

    def test_the_reasoning_note_becomes_the_predecessor(self) -> None:
        """The note is its own message, so the answer is graded against IT.

        Without the record the answer's first frame grades against whatever preceded
        the reasoning, and a note ending in a credential prefix is completed by the
        answer's leading characters across two delivered messages. Read as the
        record's presence in the function that sends the note, and as the order:
        recording before the send confirms would make unsent text the predecessor.
        """
        tree = ast.parse((SRC / "discord" / "renderer.py").read_text(encoding="utf-8"))
        flush = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_flush_thinking"
        )
        sent_at = [
            inner.lineno
            for inner in ast.walk(flush)
            if isinstance(inner, ast.Call) and _name_of(inner.func) == "send_message"
        ]
        recorded_at = [
            inner.lineno
            for inner in ast.walk(flush)
            if isinstance(inner, ast.Call) and _name_of(inner.func) == "_record_sent"
        ]
        assert sent_at, "the reasoning note must be sent as a message of its own"
        assert recorded_at, (
            "the reasoning note is a delivered message, so it is what the next thing "
            "shown is graded against -- without the record that grade reads a "
            "predecessor the reader stopped looking at"
        )
        assert min(recorded_at) > max(sent_at), (
            "the record must follow the send: recording first makes unsent text the "
            "predecessor and costs the next message a leading span for nothing"
        )

    #: Every standalone send in the Discord renderer that neither grades its own
    #: seam nor records one, with the property that makes each safe. A send lands as
    #: its own message, so it is a seam on both sides -- and the two obligations are
    #: different: text CONTINUING the reply must be graded, and text a reply can
    #: follow must be recorded. A send that is neither is exempt for a reason, and
    #: naming the reason is what makes a NEW send answer the question instead of
    #: inheriting silence.
    PLAIN_SENDS = {
        "_maybe_send_redaction_notice": "a generated count, no model text, and it trails the reply",
        "on_prompt_choice": "ends with the literal '`?' after the tool name, so no key can meet it",
        "on_compaction": "one fixed literal",
        "_seal_rotated_part": "one fixed literal in place of a lost part, recorded for the next",
        "on_done": "the empty-turn placeholder, and the turn's last message either way",
    }

    def test_every_standalone_send_grades_records_or_says_why_not(self) -> None:
        """A new send is a new seam, and silence is not one of its options."""
        tree = _discord_renderer_tree()
        sends = {"send_message", "send_message_with_files"}
        answered = {"_stream_live", "_seal_current", "_land_sealed", "_flush_thinking"}
        stack: list[tuple[ast.AST, str]] = [(tree, "")]
        senders: set[str] = set()
        while stack:
            node, holder = stack.pop()
            for child in ast.iter_child_nodes(node):
                inner = holder
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name
                if isinstance(child, ast.Call) and _name_of(child.func) in sends:
                    senders.add(holder)
                stack.append((child, inner))
        assert senders, "the walk found no send at all, so it proves nothing"
        unanswered = sorted(senders - answered - set(self.PLAIN_SENDS))
        assert not unanswered, (
            f"{unanswered} send a message of their own without grading a seam, "
            "recording one, or naming why neither applies -- which is how the "
            "reasoning note came to grade the answer against a stale predecessor"
        )
        stale = sorted(set(self.PLAIN_SENDS) - senders)
        assert not stale, (
            f"{stale} are listed as plain sends but send nothing any more -- drop "
            "them so a later send cannot inherit an exemption written for them"
        )


class TestTheTallyKeepsWhatWasAlreadyDelivered:
    """A re-count over what shipped may ADD to the notice, never replace it.

    Both paths here deliver a reply across more than one message, and both had a
    subject narrower than the answer: WeCom re-counts a remainder that starts at
    the rotation offset, and Slack reads a ledger. Counting only that subject drops
    every placeholder already on screen.
    """

    def test_wecom_adds_the_repair_rather_than_overwriting(self) -> None:
        source = (SRC / "wecom" / "renderer.py").read_text(encoding="utf-8")
        assert "self._notice_creds += max(0, shipped_creds - before_creds)" in source
        assert "self._notice_urls += max(0, shipped_urls - before_urls)" in source
        assert 'self._notice_creds, self._notice_urls = count_redaction_tags("\\n".join(' not in (
            source
        ), "assigning the chunk count discards the placeholders already delivered"

    def test_the_wecom_baseline_is_measured_before_the_redaction(self) -> None:
        """A redacted baseline subtracts the markers that redaction itself added.

        The difference is the whole point of the addition: the display pass runs
        between the slice and the split, so measuring the redacted form nets its own
        placeholders to zero and the notice announces none of them. Read as the
        order of the two statements, because that is where the defect lives.
        """
        tree = ast.parse((SRC / "wecom" / "renderer.py").read_text(encoding="utf-8"))
        baseline_at: list[int] = []
        redact_at: list[int] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "baseline"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Subscript)
            ):
                baseline_at.append(node.lineno)
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _name_of(node.value.func) == "redact_for_target"
            ):
                redact_at.append(node.lineno)
        assert baseline_at, "the tally needs a slice of the answer as its baseline"
        assert redact_at, "the display redaction is the pass the baseline must precede"
        source = (SRC / "wecom" / "renderer.py").read_text(encoding="utf-8")
        assert "count_redaction_tags(baseline)" in source
        assert "count_redaction_tags(remainder)" not in source, (
            "the redacted remainder as baseline nets the display pass's own " "placeholders to zero"
        )

    def test_slack_records_every_chunk_it_posts(self) -> None:
        """The ledger IS the tally's subject, so a chunk left out of it is lost."""
        tree = ast.parse((SRC / "slack" / "renderer.py").read_text(encoding="utf-8"))
        fallback = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_render_fallback"
        )
        recorded = [
            inner
            for inner in ast.walk(fallback)
            if isinstance(inner, ast.AugAssign)
            and isinstance(inner.target, ast.Attribute)
            and inner.target.attr == "_delivered"
        ]
        assert len(recorded) >= 2, (
            "chunk 0 and every continuation must reach the delivery ledger, or an "
            "over-limit answer announces fewer placeholders than it shipped"
        )


class TestTheRepairStaysOnTheTableAwarePath:
    """The table-aware split grades its assembled sequence and keeps the key safe.

    A table run is atomic and budgets in SOURCE chars; the prose between tables
    budgets against the same cap. Production calls this with ONE cap for both
    (``_split_markdown_table_aware(text, src_cap, src_cap)``), so the seam between
    a table run and the prose after it -- which belongs to no single block's cut
    -- is graded once at the end and repaired if it would rejoin a key.
    """

    #: A table whose seam to the prose after it rejoins a key -- so the repair
    #: really does fire -- exercised at the single source cap production uses.
    CAP = 600
    BODY = (
        "col | val\n--- | ---\n"
        + "".join(f"r{i} | value number {i}\n" for i in range(12))
        + "last | "
        + HEAD
        + "\n\n"
        + TAIL
        + " then ordinary trailing prose with **bold** in it."
    )

    def test_the_repair_fires_on_this_body(self) -> None:
        """The premise: without the repair the seam hands the key over."""
        blocks = ["\n".join(lines) for _, lines in _table_blocks(self.BODY)]
        assert _rejoins(blocks)

    def test_the_table_rows_are_still_cut_at_row_boundaries(self) -> None:
        chunks = _split_markdown_table_aware(self.BODY, self.CAP, self.CAP)
        assert chunks
        assert not _rejoins(chunks)
        assert KEY not in _on_screen(chunks)
        table_chunks = [c for c in chunks if "|" in c]
        assert table_chunks, "the table run must survive as table text"
        for chunk in table_chunks:
            for line in chunk.splitlines():
                if line.strip():
                    assert "|" in line, "a row was cut mid-body and reads as prose"

    def test_the_repaired_body_still_honours_the_source_budget(self) -> None:
        """The repaired sequence stays under the source cap it was graded against."""
        chunks = _split_markdown_table_aware(self.BODY, self.CAP, self.CAP)
        assert max(len(chunk) for chunk in chunks) <= self.CAP

    def test_the_prose_keeps_its_markup(self) -> None:
        chunks = _split_markdown_table_aware(self.BODY, self.CAP, self.CAP)
        assert "**bold**" in "".join(chunks)


class TestTheStreamingCutIsPrefixStableInTheSharedSplitter:
    """The prefix-stable streaming cut is a mode on the shared splitter.

    A caller-side whole-body repair over the delivered sequence can revise a
    chunk a prior frame already sealed -- a promise to the client nothing may
    rewrite -- so the guarantee lives in the splitter's ``stable`` mode: redact
    the whole body, then cut at the budget and nowhere else, so chunk *i* is a
    function of the text before it alone.
    """

    def test_the_splitter_offers_the_prefix_stable_mode(self) -> None:
        assert "stable" in inspect.signature(split_markdown_safe).parameters

    def test_the_streaming_contract_still_has_a_home(self) -> None:
        """The behaviour the mode selects is reachable from the renderer."""
        assert "stable" in inspect.signature(render_chunks).parameters
