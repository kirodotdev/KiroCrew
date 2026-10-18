"""A repair is judged by every reading a client can render, not by the canonical one alone.

The seam grade scans the canonical form and every further reading. The repair
answers the reading that refused the cut, the last-resort flatten keeps an
existing redaction tag whole and rejects author lookalikes, and skipping the link
readings never changes the grade's answer.
"""

from __future__ import annotations

import ast

import pytest
from split_credential_boundary_helpers import HEAD, KEY, SRC, TAIL, _name_of

from kiro_crew.messaging import split as split_module
from kiro_crew.messaging.display_safety import (
    FURTHER_READINGS,
    _plain_reading,
    canonicalize_display,
    redact_for_display,
)
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.messaging.split import (
    _collapse_reads_as_a_key,
    _collapses_to_a_key,
    _flattened_for_any_cut,
    _rejoins_a_key,
    bounded_for_delivery,
    chunk_utf8_bytes,
    repaired_for_delivery,
    split_markdown_safe,
)


def _every_reading(pieces: list[str]) -> list[str]:
    """What a reader sees of *pieces* under the canonical and every further reading.

    Both ways each, as the seam grade takes them: the rendering of the delivered
    sequence, and the sequence of each piece's own rendering.
    """
    rendered = [piece.strip() for piece in pieces]
    readings: list[str] = []
    for render in (canonicalize_display, *FURTHER_READINGS):
        readings.append(render("".join(rendered)))
        readings.append("".join(render(piece) for piece in rendered))
    return readings


def _shows_a_key(pieces: list[str]) -> list[str]:
    """The readings of *pieces* the redactor changes, plus any piece not already safe."""
    dirty = [reading for reading in _every_reading(pieces) if _default_redactor(reading) != reading]
    dirty.extend(
        piece for piece in pieces if redact_for_display(piece, _default_redactor)[0] != piece
    )
    return dirty


class TestTheRepairIsJudgedByTheReadingThatRefusedTheCut:
    """A repair graded on one reading answers a refusal made on another with nothing.

    The seam grade scans the canonical form and every further reading. A repair
    step that grades the canonical form alone can find it clean, hand the source
    back unchanged, and let the same pieces travel: the reader of a client that
    consumes the markup then sees the key the grade refused.
    """

    #: One link to the canonical grammar, which collapses it to its label and drops
    #: the url with the key inside it. The narrower link grammar leaves the url as
    #: text, where the emphasis pass joins the key across the ``~~`` pair.
    PARENTHESISED_URL = "see [l](https://x/(a)/AKIA~~IOSFODNN7EXAMPLE~~) done"
    #: A field name and its assignment held apart by a heading marker the text
    #: keeps mid-line. A cut before the marker opens a piece with it, and the plain
    #: reading of that piece drops it.
    SECRET = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"
    HEADING_SEAM = f"Rotated. SecretAccessKey #   : {SECRET}"

    def test_the_link_shape_rejoins_across_a_blind_cut(self) -> None:
        """The premise: the source is one link, cut, it is a url with a key in it."""
        pieces = split_markdown_safe(self.PARENTHESISED_URL, 20)
        assert len(pieces) > 1
        assert _rejoins_a_key(pieces, _default_redactor)
        assert KEY in "".join(_every_reading(pieces))

    def test_the_link_shape_is_repaired_for_every_reading(self) -> None:
        delivered = bounded_for_delivery([self.PARENTHESISED_URL], 20, _default_redactor)
        assert all(len(piece) <= 20 for piece in delivered)
        assert KEY not in "".join(_every_reading(delivered))
        assert not _shows_a_key(delivered), delivered

    def test_the_link_shape_repair_is_not_the_source(self) -> None:
        pieces = split_markdown_safe(self.PARENTHESISED_URL, 20)
        repaired = repaired_for_delivery(
            self.PARENTHESISED_URL, pieces, _default_redactor, lambda text: [text]
        )
        assert repaired is not None
        assert repaired != self.PARENTHESISED_URL
        assert KEY not in "".join(_every_reading([repaired]))

    def test_the_heading_seam_is_clean_as_one_message(self) -> None:
        """The premise: whole, the marker sits mid-line and no reading drops it."""
        whole = self.HEADING_SEAM
        assert redact_for_display(whole, _default_redactor) == (whole, False)

    @pytest.mark.parametrize(
        ("cutter", "budget"), [(split_markdown_safe, 13), (chunk_utf8_bytes, 12)]
    )
    def test_the_heading_seam_rejoins_across_a_blind_cut(self, cutter, budget: int) -> None:
        pieces = cutter(self.HEADING_SEAM, budget)
        assert _rejoins_a_key(pieces, _default_redactor)
        assert self.SECRET in "".join(_every_reading(pieces))
        assert any("SecretAccessKey: " in reading for reading in _every_reading(pieces))

    @pytest.mark.parametrize(
        ("cutter", "budget"), [(split_markdown_safe, 13), (chunk_utf8_bytes, 12)]
    )
    def test_the_heading_seam_is_repaired_for_every_reading(self, cutter, budget: int) -> None:
        delivered = bounded_for_delivery([self.HEADING_SEAM], budget, _default_redactor, cutter)
        assert all(len(piece) <= budget for piece in delivered)
        assert not _shows_a_key(delivered), delivered
        assert not _rejoins_a_key(delivered, _default_redactor)

    def test_the_slack_and_telegram_repairs_take_their_cutter(self) -> None:
        """The judging reading is the caller's own re-cut, so the caller names it."""
        for path in ("slack/renderer.py", "telegram/renderer.py"):
            # Telegram's renderer is read with the owners its methods are bound from.
            owners = sorted((SRC / "telegram").glob("renderer_*.py")) if "telegram" in path else []
            paths = [SRC / path, *owners]
            tree = ast.Module(
                body=[
                    node
                    for source in paths
                    for node in ast.parse(source.read_text(encoding="utf-8")).body
                ],
                type_ignores=[],
            )
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _name_of(node.func) == "repaired_for_delivery"
            ]
            assert calls, path
            for call in calls:
                assert len(call.args) >= 4 or any(kw.arg == "cut" for kw in call.keywords), path

    @pytest.mark.parametrize("budget", [13, 20, 24])
    def test_keyless_text_of_the_same_shapes_is_byte_identical(self, budget: int) -> None:
        """The opposite failure: a repair that gives up markup or breaks with no key."""
        keyless = [
            "see [l](https://x/(a)/path~~gone~~) done and a second line\nfollows here",
            "Rotated. Heading #   : ordinary words after the marker",
        ]
        for text in keyless:
            for cutter in (split_markdown_safe, chunk_utf8_bytes):
                delivered = bounded_for_delivery([text], budget, _default_redactor, cutter)
                assert delivered == (cutter(text, budget) or [text]), (text, cutter, budget)


class TestTheLastResortRejectsAuthorTagLookalikes:
    LOOKALIKE = "[REDACTED: SecretAccessKey #   : " "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY]"

    def test_the_flatten_strips_markup_from_an_author_lookalike(self) -> None:
        flat = _flattened_for_any_cut(self.LOOKALIKE, _default_redactor)
        assert self.LOOKALIKE not in flat
        assert "#" not in flat
        assert all(
            not _rejoins_a_key([flat[:cut], flat[cut:]], _default_redactor)
            for cut in range(len(flat) + 1)
        )

    def test_a_small_delivery_budget_rejoins_no_key(self) -> None:
        delivered = bounded_for_delivery([self.LOOKALIKE], 48, _default_redactor, chunk_utf8_bytes)

        assert not _rejoins_a_key(delivered, _default_redactor)


class TestTheLastResortKeepsExistingRedactionTagWhole:
    """A tag an earlier pass wrote leaves the flatten byte for byte.

    The flatten removes every whitespace run so no seam has a break to drop, and
    a redaction tag carries one space. A tag that loses it matches the tag shape
    nowhere: the next markup strip takes its brackets, and the count of tags in
    the delivered text, which the redaction notice is built from, misses it.

    The flatten also drops the heading marker, so the redactor reads the
    assignment the heading seam hid and writes a second tag. A hard cap may cut
    that second tag in two; the first stays whole and no cut shows the key.
    """

    TAG = "[REDACTED: credential]"
    SOURCE = f"{TAG} {TestTheRepairIsJudgedByTheReadingThatRefusedTheCut.HEADING_SEAM}"

    def test_the_flatten_is_reached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The premise: the heading seam sends the byte cutter to the last resort."""
        reached: list[str] = []
        flatten = split_module._flattened_for_any_cut

        def spy(text: str, redactor) -> str:
            reached.append(text)
            return flatten(text, redactor)

        monkeypatch.setattr(split_module, "_flattened_for_any_cut", spy)
        bounded_for_delivery([self.SOURCE], 24, _default_redactor, chunk_utf8_bytes)
        assert reached == [self.SOURCE]

    def test_the_flatten_keeps_the_existing_tag_and_redacts_the_assignment(self) -> None:
        flat = _flattened_for_any_cut(self.SOURCE, _default_redactor)
        assert flat.startswith(self.TAG)
        assert flat.count(self.TAG) == 2
        assert " " not in flat.replace(self.TAG, "")

    @pytest.mark.parametrize("budget", [24, 48])
    def test_delivery_keeps_the_existing_tag_and_exposes_no_key(self, budget: int) -> None:
        delivered = bounded_for_delivery([self.SOURCE], budget, _default_redactor, chunk_utf8_bytes)
        assert all(len(piece) <= budget for piece in delivered)
        assert delivered[0].startswith(self.TAG)
        assert "".join(delivered).count(self.TAG) == 2, delivered
        assert not _shows_a_key(delivered), delivered
        assert not _rejoins_a_key(delivered, _default_redactor)


def _every_rendering() -> tuple:
    """The canonical form and every further reading, as the full grade takes them."""
    return (canonicalize_display, *FURTHER_READINGS)


class _RecordingRedactor:
    """A clean redactor that keeps every reading it was asked about, in order."""

    def __init__(self) -> None:
        self.readings: list[str] = []

    def __call__(self, text: str) -> str:
        self.readings.append(text)
        return text


class TestTheGradeSkipsLinkReadingsOnlyWhereTheyCannotDiffer:
    """Without a ``](`` the three link readings ARE the canonical form.

    Each link collapse is the identity on text holding no ``](``, and the
    canonical link grammar cannot match without one, so grading the balanced,
    first-close and link-free readings of such text scans the canonical reading
    three more times and can find nothing new. The grade drops them there, and
    only there: the answer has to equal the full five-rendering grade on every
    input, and a ``](`` that exists only in the JOIN, a ``]`` closing one chunk
    and ``(`` opening the next, still asks for every reading.
    """

    #: A keyless twin of ``KEY`` with the same length, cut at the same offsets, so
    #: every keyful shape below has a keyless shape of identical geometry.
    WORD = "ordinarywordsofprose"
    ASSIGNED = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"

    @staticmethod
    def _shapes(token: str, assigned: str) -> list[str]:
        head, tail = token[:10], token[10:]
        return [
            f"prefix {token} suffix",
            f"{head}**{tail}**",
            f"{head}~~{tail}~~ and more",
            f"`{head}`{tail} in code",
            f"{head}\n\n{tail} next paragraph",
            f"[{head}] ({tail}) brackets but no opener",
            f"[label] (https://x/{head}{tail}) held apart",
            f"see [l](https://x/(a)/{head}~~{tail}~~) done",
            f"[{head}](https://q/y){tail}",
            f"[label](https://x/{head}{tail}) trailing words",
            f"Rotated. SecretAccessKey #   : {assigned}",
            f"[label](https://x/(a)/{assigned}) and words",
        ]

    @classmethod
    def _texts(cls) -> list[str]:
        return cls._shapes(KEY, cls.ASSIGNED) + cls._shapes(cls.WORD, "ordinary-words-after-marker")

    @classmethod
    def _sequences(cls) -> list[list[str]]:
        sequences: list[list[str]] = []
        for text in cls._texts():
            for budget in (8, 13, 20, 24):
                for cutter in (split_markdown_safe, chunk_utf8_bytes):
                    pieces = cutter(text, budget)
                    if len(pieces) >= 2:
                        sequences.append(pieces)
        # The opener split across a seam: no piece holds ``](``, the join does.
        sequences.extend(
            [
                ["see [l]", f"(https://x/(a)/{HEAD}~~{TAIL}~~) done"],
                ["see [l]\n", f"(https://x/(a)/{HEAD}~~{TAIL}~~) done"],
                [f"[{HEAD}]", f"(https://q/y){TAIL}"],
                ["see [l]", "(https://x/(a)/ordinary~~words~~) done"],
            ]
        )
        return sequences

    def test_the_corpus_covers_both_answers_and_both_opener_cases(self) -> None:
        """The premise: the generated inputs exercise every branch the grade takes."""
        sequences = self._sequences()
        joined = ["".join(piece.strip() for piece in pieces) for pieces in sequences]
        assert any("](" in text for text in joined)
        assert any("](" not in text for text in joined)
        assert any(
            "](" in text and all("](" not in piece for piece in pieces)
            for text, pieces in zip(joined, sequences)
        )
        answers = {_rejoins_a_key(pieces, _default_redactor) for pieces in sequences}
        assert answers == {True, False}

    def test_the_sequence_grade_equals_the_full_five_rendering_grade(self) -> None:
        for pieces in self._sequences():
            expected = any(_default_redactor(r) != r for r in _every_reading(pieces))
            assert _rejoins_a_key(pieces, _default_redactor) is expected, pieces

    def test_the_collapse_grade_equals_the_full_five_rendering_grade(self) -> None:
        texts = self._texts()
        assert any("](" in text for text in texts) and any("](" not in text for text in texts)
        for text in texts:
            expected = any(
                _collapses_to_a_key(render(text), _default_redactor)
                for render in _every_rendering()
            )
            assert _collapse_reads_as_a_key(text, _default_redactor) is expected, text

    def test_a_link_free_sequence_scans_the_canonical_and_plain_readings_only(self) -> None:
        pieces = [f"prefix {HEAD}**", f"{TAIL}** suffix"]
        assert all("](" not in piece for piece in pieces)
        rendered = [piece.strip() for piece in pieces]
        recording = _RecordingRedactor()
        _rejoins_a_key(pieces, recording)
        assert len(recording.readings) == 4, recording.readings
        assert set(recording.readings) == {
            canonicalize_display("".join(rendered)),
            "".join(canonicalize_display(piece) for piece in rendered),
            _plain_reading("".join(rendered)),
            "".join(_plain_reading(piece) for piece in rendered),
        }

    @pytest.mark.parametrize(
        "pieces",
        [
            [f"see [l](https://x/(a)/{HEAD}~~", f"{TAIL}~~) done"],
            ["see [l]", f"(https://x/(a)/{HEAD}~~{TAIL}~~) done"],
        ],
        ids=["opener-inside-a-piece", "opener-only-in-the-join"],
    )
    def test_a_sequence_with_an_opener_scans_every_reading_both_ways(self, pieces) -> None:
        recording = _RecordingRedactor()
        _rejoins_a_key(pieces, recording)
        assert len(recording.readings) == 2 * len(_every_rendering()) == 10, recording.readings

    def test_a_link_free_collapse_scans_two_renderings_and_an_opener_scans_five(self) -> None:
        link_free, with_opener = _RecordingRedactor(), _RecordingRedactor()
        _collapse_reads_as_a_key(f"{HEAD}**{TAIL}** [x] (y)", link_free)
        _collapse_reads_as_a_key(f"[l](https://x/(a)/{HEAD}~~{TAIL}~~)", with_opener)
        assert len(link_free.readings) == 2, link_free.readings
        assert len(with_opener.readings) == len(_every_rendering()) == 5, with_opener.readings
