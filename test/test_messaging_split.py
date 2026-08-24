"""Contract tests for the shared fence-safe markdown splitter.

Every test names the contract item it pins so a future change to
``messaging/split.py`` can tell a deliberate behavior change from a regression.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

import pytest

from kiro_crew.messaging.split import (
    FENCE_BODY,
    FENCE_CLOSE,
    FENCE_OPEN,
    FENCE_OUTSIDE,
    _Fence,
    _safe_cut,
    iter_fence_lines,
    iter_fence_spans,
    split_markdown_safe,
)

# A line that opens or closes a fence. Used to strip fence scaffolding out of a
# reassembled split: the reopen duplicates the opener and the seal adds a
# closer, so those lines are the only text the splitter is allowed to invent.
_FENCE_START = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")

# A line a renderer reads as a fence OPENER, by the grammar split.py implements:
# <=3 spaces of indent plus a run of >=3 backticks whose info string holds no
# backtick, or a run of >=3 tildes followed by anything.
_FENCE_DELIMITER = re.compile(r"^ {0,3}(?:`{3,}[^`]*|~{3,}.*)$")

# A bare delimiter run, i.e. a candidate closer: nothing but the run itself.
_BARE_RUN = re.compile(r"^ {0,3}((?:`{3,})|(?:~{3,}))[ \t]*$")

# A bare delimiter run of four or more. Used to catch a fence CLOSER a cut
# invented: a synthetic closer always matches its opener's run length, so a
# longer bare run in the output can only have come from cut content.
_LONG_BARE_RUN = re.compile(r"^ {0,3}(?:`{4,}|~{4,})[ \t]*$")


def _payload(text: str) -> str:
    """Every non-fence, non-whitespace character of *text*, in order.

    Whitespace is dropped because sealing outside a fence trims it and a hard
    cut may land on a space; everything else must survive a split exactly.
    """
    return "".join(
        "".join(line.split()) for line in text.split("\n") if not _FENCE_START.match(line)
    )


def _reassembled(chunks: list[str]) -> str:
    """The payload of a split, measured per chunk.

    Measuring the concatenation instead would be wrong: a chunk's trailing
    fence line and the next chunk's opening prose land on the same line once
    joined, and the fence filter would then swallow real content.
    """
    return "".join(_payload(chunk) for chunk in chunks)


# Deliberately nasty: nested fences of different lengths, a tilde fence, a pipe
# table, blank-line runs, an unbreakable long line, CRLF, non-ASCII, and a fence
# left open at EOF.
NASTY = (
    "Intro paragraph with a | pipe in it.\n"
    "\n"
    "```python\n"
    "def f(x):\n"
    "        return x * 2  \n"
    "```\n"
    "\n"
    "Prose between blocks.\n"
    "\n"
    "\n"
    "````markdown\n"
    "```\n"
    "a nested block that must stay content\n"
    "```\n"
    "````\n"
    "\n"
    "| col a | col b |\n"
    "|-------|-------|\n"
    "| 1     | 2     |\n"
    "| 3     | 4     |\n"
    "\n"
    "~~~\n"
    "tilde fenced body\n"
    "~~~\n"
    "\n" + "U" * 900 + "\n"
    "\r\nwindows line\r\n"
    "héllo wörld 🎉 más texto\n"
    "\n"
    "```js\n"
    "// never closed\n"
    "const x = 1;\n"
)

# Small and table-dense: the dense prefix sweep needs many header/separator
# seams within a few hundred characters.
TABLE_CORPUS = (
    "Report follows below.\n"
    "\n"
    "| metric | value |\n"
    "|--------|-------|\n"
    "| alpha  | 1     |\n"
    "| beta   | 2     |\n"
    "\n"
    "More prose after the table with a | pipe.\n"
    "\n"
    "| second | table |\n"
    "|:-------|------:|\n"
    "| gamma  | 3     |\n"
)

# Fence-grammar seams the table corpus cannot reach. Swept every-prefix, each
# line is also exercised half-arrived, which is where a classification can still
# be invalidated: "```abc" is an opener until the next character is a backtick,
# and inside the ````py block the growing "`````x" crosses the four-run closer
# threshold and then leaves it again. Small budgets force hard cuts straight
# through those delimiter runs.
FENCE_CORPUS = (
    "lead\n"
    "```abc`rest\n"  # backtick in the info string — never a fence
    "````py\n"
    "z\n"
    "`````x\n"  # content, but its prefixes cross the closer threshold
    "````\n"
    "   ```lang\n"  # indented opener
    "body\n"
    "   ```\n"
    "~~~t`lde\n"  # a tilde info string may hold a backtick
    "~~~\n"
    "```language"  # unterminated opener at EOF
)

# Mid-line delimiter runs, which is the REMAINDER side of a hard cut: no line
# here is a fence delimiter, but a cut landing before a run leaves one starting
# the next chunk — "aaaaa```x" sealed at five emits "```x", a valid opener. The
# second line is the same run one character from being disqualified, which is
# where a remainder judged by PARSING it (rather than by its first character)
# classifies differently one character later and rewrites a sealed chunk. Every
# run here is short enough that some cut clears it at every budget, so the
# render-closed invariant holds unconditionally (unlike RESIDUE_RUN below).
REMAINDER_CORPUS = (
    "aaaaa```x\nz\n" "aaaaa```x`rest\n" "prose ``` more prose\n" "tail ~~~ tilde run\n"
)

# The same seam inside an open fence, where an invented remainder acts as a
# CLOSER: the block ends early and the chunk's own synthetic closer then reads
# as a fresh opener, so the chunk no longer renders closed.
FENCE_REMAINDER_CORPUS = "```py\nzzzz```\nq = 1\n```\n"

# A run no cut can clear at a small budget: every candidate width lands inside
# it, so cut selection has no clean-both-ways option and degrades (see
# test_an_uncuttable_run_degrades_rather_than_stalling).
RESIDUE_RUN = "x" + "`" * 10 + "\ntail\n"


def _run_line(run: str) -> str:
    """A line whose delimiter run reaches its end, so no cut is clean both ways.

    Every candidate width below the newline lands on a run character, which the
    remainder test rejects, and the prefix is never a delimiter because the line
    opens with ``x``. Cut selection therefore has no clean option at any budget
    that cannot hold the line, which is what the two-tier residue policy decides.
    """
    return "x" + run + "\n"


# Lines no cut clears, outside AND inside a fence, behind a short leading line
# that leaves a chunk barely started — the state in which the whole-line
# placement has to seal first to free the budget. Swept every-prefix at several
# reserves, since a placement spending the reserve headroom is reachable only
# when the caller set some aside.
_BARE_RUN_LINE = _run_line("`" * 24)  # 26 chars, outside any fence
_FENCED_RUN_LINE = "y" + "`" * 18 + "\n"  # 20 chars, inside the ```py block
NO_CLEAN_CUT_CORPUS = (
    "hi\n"  # short: leaves `used` under a quarter of larger budgets
    + _BARE_RUN_LINE
    + "```py\n"
    + _FENCED_RUN_LINE
    + "```\n"
    + "z\n"
)

# Whole-line placement measures the LINE ALONE against the full limit, so the
# two floors differ. At or above the longer line's own length no line of the
# corpus is ever cut, so no boundary can fabricate a delimiter — but a fenced
# placement still carries its scaffolding (a ```py reopen and newline, 6, plus a
# newline and closer, 4) on top, so its chunk can reach 30 and pass the limit.
NO_CLEAN_CUT_LINE_FLOOR = 26  # len(_BARE_RUN_LINE): no line is cut at or above
NO_CLEAN_CUT_SCAFFOLD = 10  # the widest reopen + closer the corpus can demand
NO_CLEAN_CUT_FLOOR = 30  # 6 + 20 + 4: no chunk passes the limit at or above

_PY_FENCE = _Fence(char="`", length=3, opener="```py")


# ---------------------------------------------------------------- item 1: grammar


def test_shorter_run_inside_longer_fence_is_content():
    """A ``` line inside a ````block is content, not a closer (item 1)."""
    text = "````markdown\n" + "```\nnested\n```\n" * 20 + "````\n"
    chunks = split_markdown_safe(text, 100)
    assert len(chunks) > 1
    # Sealing with the 4-backtick closer proves the inner ``` never closed it;
    # a parity counter would have flipped state on every nested line.
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n````")
    for chunk in chunks[1:]:
        assert chunk.startswith("````markdown\n")


def test_tilde_fence_opens_and_closes():
    text = "~~~python\n" + "value = 1\n" * 40 + "~~~\n"
    chunks = split_markdown_safe(text, 90)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n~~~")
    for chunk in chunks[1:]:
        assert chunk.startswith("~~~python\n")


def test_backtick_info_string_containing_a_backtick_is_not_an_opener():
    text = "``` `` inline `` ```\n" + "prose line\n" * 40
    chunks = split_markdown_safe(text, 100)
    assert len(chunks) > 1
    # Never entered a fence, so nothing is ever sealed with a synthetic closer.
    for chunk in chunks:
        assert not chunk.endswith("```")


def test_fence_indent_of_three_opens_but_four_does_not():
    body = "x = 1\n" * 40
    opened = split_markdown_safe("   ```py\n" + body + "   ```\n", 90)
    assert len(opened) > 1
    for chunk in opened[1:]:
        assert chunk.startswith("   ```py\n")  # indent carried verbatim

    not_opened = split_markdown_safe("    ```py\n" + body, 90)
    assert len(not_opened) > 1
    for chunk in not_opened:
        assert not chunk.endswith("```")


def test_closer_must_be_alone_on_its_line():
    text = "```py\n" + "a\n" * 20 + "``` trailing words\n" + "b\n" * 20 + "```\n"
    chunks = split_markdown_safe(text, 60)
    assert len(chunks) > 2
    # "``` trailing words" is content, so the fence stays open past it and every
    # continuation still reopens the ORIGINAL fence.
    for chunk in chunks[1:]:
        assert chunk.startswith("```py\n")


def test_a_hard_cut_never_invents_a_fence_opener():
    """A cut prefix must not read as an opener its own line does not (item 1).

    ``"```abc"`` cut out of ``"```abc`rest"`` is a valid opener while the whole
    line is not — a backtick in the info string disqualifies it — so a receiver
    would render a code block the source never contained.
    """
    text = "```abc`rest"
    assert not _FENCE_DELIMITER.match(text)  # no line of the source is a fence
    for limit in range(4, 12):
        chunks = split_markdown_safe(text, limit)
        assert "".join(chunks) == text
        for chunk in chunks:
            for line in chunk.split("\n"):
                assert not _FENCE_DELIMITER.match(line), (limit, chunks)


def test_a_hard_cut_inside_a_fence_never_invents_a_closer():
    """A cut prefix must not close a fence its own line does not (item 1).

    ``"``````a`b"`` is content inside a ``` ``` ``` block, but a cut inside its
    backtick run emits a bare run long enough to close it: the block ends early
    and the chunk's own synthetic closer then reads as a new opener. A bare run
    of four or more is the tell — the source holds none, and a synthetic closer
    always matches its 3-backtick opener exactly.
    """
    text = "```py\n``````a`b\nz = 1\n"
    assert not any(_LONG_BARE_RUN.match(ln) for ln in text.split("\n"))
    for limit in range(8, 60):
        chunks = split_markdown_safe(text, limit)
        for chunk in chunks:
            for line in chunk.split("\n"):
                assert not _LONG_BARE_RUN.match(line), (limit, chunks)


def test_a_hard_cut_remainder_never_invents_a_fence_opener():
    """A cut's REMAINDER must not open a fence its own line does not (item 1).

    The remainder starts a rendered line of the NEXT chunk, so it faces the
    grammar too: ``"aaaaa```x"`` sealed at five leaves ``"```x"`` opening that
    chunk, a valid opener although the source line's run is mid-line prose.
    """
    text = "aaaaa```x\nz\n"
    assert not any(_FENCE_DELIMITER.match(ln) for ln in text.split("\n"))
    for limit in range(1, 41):
        chunks = split_markdown_safe(text, limit)
        assert _reassembled(chunks) == _payload(text)
        for chunk in chunks:
            for line in chunk.split("\n"):
                assert not _FENCE_DELIMITER.match(line), (limit, chunks)


def test_a_hard_cut_remainder_never_invents_a_closer_inside_a_fence():
    """A cut's remainder must not close a fence its own line does not (item 1).

    ``"zzzz```"`` is content inside a ``` ``` ``` block — a closer must be alone
    on its line — but a cut landing before its run leaves a bare run opening the
    next chunk, which closes the block early and leaves that chunk's own
    synthetic closer reading as a fresh opener.

    Budgets start at the scaffolding floor, below which a chunk cannot hold its
    own reopen line plus closer and the documented over-budget regime applies.
    """
    text = "```py\nzzzz```\nq = 1\n"
    assert not any(_BARE_RUN.match(ln) for ln in text.split("\n"))
    for limit in range(_scaffolding_floor(text), 40):
        for chunk in split_markdown_safe(text, limit)[:-1]:
            assert _renders_closed(chunk), (limit, chunk)


def test_an_uncuttable_run_degrades_rather_than_stalling():
    """A run no candidate cut clears keeps today's cut, by design (item 8).

    Below width 11 every candidate lands inside the run, so no cut is clean on
    both sides and the widest prefix-clean one is taken — a later fragment can
    then be a bare run of its own. That residue is documented degradation, the
    same regime as a budget too small for a line's fence scaffolding: forward
    progress and content preservation come first, and a sealed chunk is not
    promised to render closed there.
    """
    assert not any(_FENCE_DELIMITER.match(ln) for ln in RESIDUE_RUN.split("\n"))
    for limit in range(1, 11):  # below the width that clears the run
        chunks = split_markdown_safe(RESIDUE_RUN, limit)
        assert 0 < len(chunks) <= len(RESIDUE_RUN)  # progress, no spin
        # Measured without _payload: the residue emits a chunk that LOOKS like a
        # fence line, which is precisely what that helper filters out.
        assert "".join("".join(c.split()) for c in chunks) == "".join(RESIDUE_RUN.split())
    # Given room to clear the run, the remainder guard applies as everywhere else.
    for limit in range(11, 40):
        for chunk in split_markdown_safe(RESIDUE_RUN, limit)[:-1]:
            assert _renders_closed(chunk), (limit, chunk)


def test_a_fragment_with_no_clean_cut_is_placed_whole_when_it_fits_the_limit():
    """No clean cut means the line is not cut at all, budget allowing (item 1).

    A shorter earlier line leaves too little room for this one, and every
    candidate cut in its run would hand the next chunk a fabricated ``` opener.
    Sealing the earlier line frees the whole budget, the line lands whole, and
    neither boundary invents a delimiter.
    """
    text = "a" * 9 + "\n" + _run_line("`" * 92 + "y")
    assert not any(_FENCE_DELIMITER.match(ln) for ln in text.split("\n"))
    chunks = split_markdown_safe(text, 100)
    assert chunks == ["a" * 9, "x" + "`" * 92 + "y\n"]
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert not _FENCE_DELIMITER.match(line), chunks


def test_a_whole_line_placement_may_spend_the_reserve_headroom():
    """The budget's one documented exception, asserted rather than ignored (item 3).

    The line fits ``limit`` but not ``limit - reserve``, and no cut in its run is
    clean, so it is placed whole into the reserve headroom. The exception is
    scoped: exactly one chunk uses it and it holds that one source line and
    nothing else. Here the line sits OUTSIDE any fence, so its chunk has no
    scaffolding to carry and stays inside the caller's ``limit`` — the fenced case,
    where the reopen and closer push it past ``limit``, is pinned by
    test_a_whole_line_placement_passes_the_limit_only_by_its_scaffolding.
    """
    limit, reserve = 100, 20
    line = _run_line("`" * 92)
    assert limit - reserve < len(line) <= limit  # only reachable via the reserve
    chunks = split_markdown_safe(line + "tail\n", limit, reserve=reserve)
    over = [c for c in chunks if len(c) > limit - reserve]
    assert over == [line.rstrip()]  # the whole line, sealed, and nothing else
    assert all(len(c) <= limit for c in chunks)
    for chunk in chunks:
        for ln in chunk.split("\n"):
            assert not _FENCE_DELIMITER.match(ln), chunks


def test_a_line_that_fits_the_limit_is_placed_whole_inside_an_open_fence():
    """Eligibility is the line's own length, not the scaffolded sum (item 1).

    A 1896-character line inside an open fence fits ``limit`` by itself but not
    once the chunk's reopen line and synthetic closer are counted with it. Gating
    on that sum refuses the placement and dirty-cuts the line instead, and the
    cut lands inside its backtick run: the deferred remainder is a bare ``` run
    at the start of a rendered line, which CLOSES a fence the source line only
    ever held content for. Measuring the line alone places it whole instead, and
    the chunk carries the scaffolding on top of the limit.
    """
    limit = 1900
    line = _run_line("`" * 1894)
    assert len(line) == 1896 <= limit  # fits alone; 4 + 1896 + 4 does not
    text = "```\n" + "A" * 100 + "\n" + line
    chunks = split_markdown_safe(text, limit)
    assert chunks == ["```\n" + "A" * 100 + "\n```", "```\n" + line]
    # The source line is content, and it stays content: the only bare runs in the
    # output are the scaffolding (the source opener, the reopen, the closer).
    assert _bare_runs_in_content(chunks) == []


def test_a_line_that_fits_the_limit_is_placed_whole_when_scaffolding_spends_the_budget():
    """R5: eligibility survives a budget the fence scaffolding consumes whole (item 1).

    At limit 8 a ``` fence's reopen line (4 characters) and its reserved closer
    (4 more) leave a fresh chunk NO room at all, and the ladder used to be
    skipped entirely there: cut selection never ran, the whole-line test never
    ran with it, and an 8-character line — exactly the limit — was dirty-cut one
    character per chunk. Eligibility now reads the line and ``limit`` alone, so
    the line is placed whole at 8 exactly as it is at 9, where the room
    arithmetic happens to leave a character to cut.
    """
    line = "x" + "`" * 6 + "\n"
    text = "```\n" + line + "z\n"
    assert len(line) == 8  # a room of zero at limit 8, one character at limit 9
    for limit in (8, 9, 10):
        chunks = split_markdown_safe(text, limit)
        assert any(line in c for c in chunks), (limit, chunks)  # placed, not cut
        assert _reassembled(chunks) == _payload(text), (limit, chunks)
        assert _bare_runs_in_content(chunks) == []  # nothing fabricated either
        for chunk in chunks[:-1]:
            assert _renders_closed(chunk), (limit, chunk)
    # The placement is keyed on how long the line turns out to be, so it must not
    # rewrite a chunk already sealed while the line is still arriving.
    _assert_prefix_stable(text, range(8, 11))


@pytest.mark.parametrize("reserve", [0, 1, 7])
def test_a_dirty_cut_requires_a_line_longer_than_the_limit(reserve):
    """Tier 3 fires for one reason only: the line is longer than ``limit`` (item 8).

    Both run lines of the corpus admit no clean cut, so each is either placed
    whole or dirty-cut. The choice tracks the line's own length against the full
    limit and nothing else — not the reserve, not the fence scaffolding, not how
    much of the chunk is already spent. The fenced line is the finding's case at
    an ordinary budget: at 20 characters it fits every limit from 20 up, yet its
    ``` py reopen and closer put the scaffolded sum over anything under 30.
    """
    # Inside a fence a trailing newline is content, so the line must survive
    # verbatim; sealing outside a fence trims it, so there the run is the witness.
    for limit in range(len(_FENCED_RUN_LINE), 61):
        chunks = split_markdown_safe(NO_CLEAN_CUT_CORPUS, limit, reserve=reserve)
        assert any(_FENCED_RUN_LINE in c for c in chunks), (limit, reserve, chunks)
    for limit in range(NO_CLEAN_CUT_LINE_FLOOR, 61):
        chunks = split_markdown_safe(NO_CLEAN_CUT_CORPUS, limit, reserve=reserve)
        assert any(_BARE_RUN_LINE.rstrip() in c for c in chunks), (limit, reserve, chunks)
        for chunk in chunks[:-1]:
            assert _renders_closed(chunk), (limit, reserve, chunk)
    # Only BELOW that floor does a line outgrow the limit, and only there can a
    # cut still hand the receiver a delimiter — the documented residue, which the
    # fix narrows to exactly that case rather than removing.
    assert any(
        not _renders_closed(chunk)
        for limit in range(4, NO_CLEAN_CUT_LINE_FLOOR)
        for chunk in split_markdown_safe(NO_CLEAN_CUT_CORPUS, limit, reserve=reserve)[:-1]
    ), reserve


def test_a_whole_line_placement_passes_the_limit_only_by_its_scaffolding():
    """The oversize the placement buys is bounded and localized (item 3).

    Between the line's own length and that length plus its fence scaffolding, the
    line is placed whole and its chunk passes ``limit`` — by the reopen line and
    the synthetic closer, never by more. Such a chunk is exactly reopen + that
    one source line + closer, and a chunk with no scaffolding to carry stays
    inside ``limit``. At or above the scaffolded floor nothing passes it at all.
    """
    source = set(NO_CLEAN_CUT_CORPUS.splitlines(keepends=True))
    for reserve in (0, 1, 7):
        for limit in range(NO_CLEAN_CUT_LINE_FLOOR, 61):
            chunks = split_markdown_safe(NO_CLEAN_CUT_CORPUS, limit, reserve=reserve)
            assert all(len(c) <= limit + NO_CLEAN_CUT_SCAFFOLD for c in chunks)
            if limit >= NO_CLEAN_CUT_FLOOR:
                assert all(len(c) <= limit for c in chunks), (limit, reserve)
            for chunk in chunks[:-1]:
                if len(chunk) <= limit:
                    continue
                lines = chunk.split("\n")
                assert _FENCE_DELIMITER.match(lines[0]), chunk  # the reopen
                assert _BARE_RUN.match(lines[-1]), chunk  # the synthetic closer
                assert len(lines) == 3, chunk  # and one source line between them
                assert lines[1] + "\n" in source, chunk
                # Scaffolding is the entire overrun.
                assert len(chunk) - len(lines[1] + "\n") == len(lines[0]) + 1 + len(lines[-1])


def _bare_runs_in_content(chunks: list[str]) -> list[tuple[int, str]]:
    """Bare delimiter runs a receiver reads as content, i.e. ones a cut invented.

    A chunk's own scaffolding is excluded: its first line may be the source's
    opener or the reopen, and a sealed chunk's last line is the synthetic closer.
    Anything else that reads as a bare run came from cut content. Sound only for
    a source whose ONLY delimiter line is its opening fence — elsewhere use
    ``_renders_closed``, which reads the grammar rather than matching lines.
    """
    found = []
    for i, chunk in enumerate(chunks):
        lines = chunk.split("\n")
        start = 1 if _FENCE_DELIMITER.match(lines[0]) else 0
        end = len(lines) - 1 if i < len(chunks) - 1 else len(lines)
        found += [(i, ln) for ln in lines[start:end] if _BARE_RUN.match(ln)]
    return found


def test_a_line_longer_than_the_limit_is_the_last_dirty_cut_left():
    """The residue now needs an unbreakable line over the FULL limit (item 8).

    At 1903 characters the line cannot be placed whole at any budget the caller
    allows, so the widest prefix-clean cut is taken: the prefix stays clean and
    the remainder opens a fence its source line never contained. That is the
    documented residue the module docstring keeps, and the only case left — a
    fragment with no clean cut that DOES fit the limit is placed whole instead.
    """
    limit = 1900
    line = _run_line("`" * 1902)
    assert len(line) > limit  # undeliverable whole at any budget
    chunks = split_markdown_safe(line + "tail\n", limit)
    assert "".join(chunks) == line + "tail\n"
    assert [len(c) for c in chunks] == [limit, 9]
    # The prefix side stays clean at every residue cut; only the remainder pays.
    assert not any(_FENCE_DELIMITER.match(ln) for ln in chunks[0].split("\n"))
    assert _FENCE_DELIMITER.match(chunks[1].split("\n")[0])


@pytest.mark.parametrize(
    "fence,frag,room,expected",
    [
        # Pulled back off the run: width 5 would emit the opener "```x".
        pytest.param(None, "aaaaa```x\n", 5, 4, id="opener-pulled-back"),
        # Already clean both ways — the run sits before the cut, not after it.
        pytest.param(None, "aaaaa```x\n", 8, 8, id="clean-both-ways"),
        # The closer direction, inside a fence: only width 1 clears the run.
        pytest.param(_PY_FENCE, "zz``````\n", 5, 1, id="closer-pulled-back"),
        # Residue: every candidate lands inside the run, so the widest
        # prefix-clean cut is taken and the remainder still delimits.
        pytest.param(None, "```\n", 2, 2, id="residue-bare-delimiter-line"),
        pytest.param(None, "x``````````\n", 8, 8, id="residue-long-run"),
        pytest.param(_PY_FENCE, "`" * 40 + "\n", 20, 2, id="residue-run-closes"),
    ],
)
def test_a_cut_is_chosen_clean_on_both_sides_or_falls_back(fence, frag, room, expected):
    """The widest clean-both-ways cut wins; the residue keeps today's cut (item 1).

    The fallback engages only for a fragment where EVERY candidate remainder
    begins with a space or a fence character, so no width is clean both ways.
    """
    assert _safe_cut(fence, frag, room) == expected


@pytest.mark.parametrize("frag", ["a```b\n", "```\n", "`" * 12 + "\n", "   ~~~x\n", "ab\n"])
def test_cut_selection_always_makes_forward_progress(frag):
    """No candidate search may return a zero-width cut (item 8)."""
    for fence in (None, _PY_FENCE):
        for room in range(1, len(frag)):
            assert 1 <= _safe_cut(fence, frag, room) <= room


# ------------------------------------------------------- item 2: language carry


def test_language_tag_is_carried_into_every_continuation():
    text = "```python\n" + "".join(f"line_{i} = {i}\n" for i in range(60)) + "```\n"
    chunks = split_markdown_safe(text, 200)
    assert len(chunks) > 1
    for chunk in chunks[1:]:
        assert chunk.startswith("```python\n")
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n```")


def test_synthetic_closer_matches_the_opener_run_length():
    text = "````diff\n" + "+ added line\n" * 40 + "````\n"
    chunks = split_markdown_safe(text, 120)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n````")
        assert not chunk.endswith("\n`````")
    for chunk in chunks[1:]:
        assert chunk.startswith("````diff\n")


# -------------------------------------------------- item 3: closer reservation


@pytest.mark.parametrize(
    "limit,reserve",
    [(60, 0), (60, 5), (100, 20), (137, 37), (400, 0), (400, 100)],
)
def test_reserve_is_honored_including_the_synthetic_closer(limit, reserve):
    text = "```python\n" + ("data = " + "x" * 40 + "\n") * 30 + "```\n"
    chunks = split_markdown_safe(text, limit, reserve=reserve)
    assert len(chunks) > 1
    # Every line here admits a cut clean on both sides, so the whole-line
    # exception pinned by test_a_whole_line_placement_may_spend_the_reserve_
    # headroom cannot engage and the budget holds with nothing carved out.
    assert all(len(c) <= limit - reserve for c in chunks)


@pytest.mark.parametrize("limit", [40, 61, 100, 257, 1000])
def test_every_chunk_fits_and_no_content_is_lost(limit):
    chunks = split_markdown_safe(NASTY, limit)
    assert all(len(c) <= limit for c in chunks)
    assert _reassembled(chunks) == _payload(NASTY)


@pytest.mark.parametrize(
    "corpus",
    [
        pytest.param(FENCE_CORPUS, id="fence"),
        pytest.param(TABLE_CORPUS, id="table"),
        pytest.param(NASTY, id="nasty"),
        pytest.param(REMAINDER_CORPUS, id="remainder"),
        pytest.param(FENCE_REMAINDER_CORPUS, id="fence-remainder"),
    ],
)
def test_every_sealed_chunk_renders_closed(corpus):
    """A sealed chunk must be self-contained on the receiver (item 3).

    This is the invariant an invented fence breaks: a cut prefix that reads as a
    delimiter either ends its chunk mid-block or leaves the synthetic closer
    reading as a fresh opener. Only budgets that can hold a chunk's own fence
    scaffolding are in scope — below that the splitter documents that it
    overruns the budget rather than not terminating, and the reopen line itself
    is subject to a hard cut.
    """
    floor = _scaffolding_floor(corpus)
    step = max(1, len(corpus) // 60)
    for limit in range(floor, floor + 40, 3):
        for cut in range(1, len(corpus) + 1, step):
            for chunk in split_markdown_safe(corpus[:cut], limit)[:-1]:
                assert _renders_closed(chunk), (limit, cut, chunk)


def _scaffolding_floor(text: str) -> int:
    """The smallest budget any chunk of *text* needs for fence scaffolding.

    A continuation chunk spends ``len(opener) + 1`` on its reopen line and
    reserves ``run + 1`` for the synthetic closer.
    """
    floor = 1
    for line in text.split("\n"):
        run = _FENCE_START.match(line)
        if run and _FENCE_DELIMITER.match(line):
            floor = max(floor, len(line) + 1 + len(run.group(0).lstrip()) + 1)
    return floor


def _renders_closed(chunk: str) -> bool:
    """True if a receiver applying the fence grammar ends *chunk* outside a fence.

    Deliberately reimplemented here rather than imported: the point is to check
    the splitter's output against the grammar, not against its own state machine.
    """
    char = ""
    length = 0
    for raw in chunk.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if char:
            closer = _BARE_RUN.match(line)
            if closer and closer.group(1)[0] == char and len(closer.group(1)) >= length:
                char, length = "", 0
            continue
        if _FENCE_DELIMITER.match(line):
            run = _FENCE_START.match(line)
            assert run is not None
            char, length = run.group(0).lstrip()[0], len(run.group(0).lstrip())
    return not char


# ---------------------------------------------------- item 4: prefix stability


@pytest.mark.parametrize("limit", [64, 137, 400])
def test_prefix_stability_over_a_growing_stream(limit):
    """Growing the input must not rewrite an already-sealed chunk (item 4)."""
    rng = random.Random(20260814)
    previous: list[str] | None = None
    cut = 0
    while cut < len(NASTY):
        cut = min(len(NASTY), cut + rng.randint(1, 40))
        chunks = split_markdown_safe(NASTY[:cut], limit)
        if previous is not None:
            sealed = previous[:-1]
            assert len(chunks) >= len(sealed)
            for i, chunk in enumerate(sealed):
                assert chunks[i] == chunk, f"chunk {i} rewritten at prefix {cut}"
        previous = chunks


def test_prefix_stability_holds_for_every_prefix_and_budget():
    """Exhaustive version of item 4 over a table-heavy corpus.

    Random growth alone is too coarse to catch a cut that peeks at the line
    AFTER it: the divergence needs a prefix ending part-way through exactly
    that line, at a budget where the seal lands there. Sweeping every prefix
    against every small budget covers those seams.
    """
    _assert_prefix_stable(TABLE_CORPUS, range(24, 121))


def test_prefix_stability_holds_across_fence_grammar_seams():
    """The same exhaustive sweep over fence-classification seams (item 4).

    Every prefix leaves the corpus' last line half-arrived, which is where a
    fence classification is still revocable, and the small budgets force hard
    cuts through delimiter runs.
    """
    _assert_prefix_stable(FENCE_CORPUS, range(6, 81))


@pytest.mark.parametrize(
    "corpus",
    [
        pytest.param(REMAINDER_CORPUS, id="remainder"),
        pytest.param(FENCE_REMAINDER_CORPUS, id="fence-remainder"),
    ],
)
def test_prefix_stability_holds_across_mid_line_delimiter_runs(corpus):
    """The exhaustive sweep over the remainder side of a hard cut (item 4).

    A cut pulled back off a delimiter run is the one decision that reads text
    AFTER the cut point, so it is also the one most able to break the streaming
    contract: every prefix ends a line half-arrived, where a run is still
    growing and a parse of the remainder would classify it differently one
    character later.
    """
    _assert_prefix_stable(corpus, range(3, 61))


@pytest.mark.parametrize(
    "text,extra,limit",
    [
        # An unterminated opener that one more character disqualifies.
        ("A" * 15 + "\nb\n```language", "`", 16),
        ("A" * 15 + "\nb\n```language", "`", 15),
        ("A" * 15 + "\nb\n```lang", "`", 13),
        ("A" * 15 + "\nb\n   ```lang", "`", 15),
        ("A" * 15 + "\nb\n``````lang", "`", 16),
        # The closer direction: a run too short to close the enclosing fence
        # until one more character lengthens it.
        ("````py\nz\n```", "`", 11),
        ("````py\nz\nz\n```", "`", 12),
        ("````py\nwwwwwwww\n```", "`", 14),
    ],
)
def test_an_unterminated_final_line_never_drives_a_seal(text, extra, limit):
    """One more character must not rewrite an already-sealed chunk (item 4).

    The last line of a stream has no newline yet, so its fence classification is
    still revocable: ``"```language"`` opens a block and ``"```language`"`` does
    not. Sealing on that tentative state rewrites the chunk before it.
    """
    sealed = split_markdown_safe(text, limit)[:-1]
    grown = split_markdown_safe(text + extra, limit)
    assert len(grown) >= len(sealed)
    for i, chunk in enumerate(sealed):
        assert grown[i] == chunk, f"chunk {i} rewritten by one more character"


@pytest.mark.parametrize("reserve", [0, 1, 3, 7, 12])
def test_prefix_stability_holds_where_lines_are_placed_whole(reserve):
    """The exhaustive sweep over whole-line placement, at reserves (item 4).

    Placing a line whole is the one decision keyed on how long the line turns
    out to be, so it is the one most able to rewrite a chunk already sent: every
    prefix here leaves that line half-arrived, at a length that has not yet
    reached the budget the decision is measured against. It stays stable because
    the placement never seals — it only buffers, into a chunk that is still the
    live tail while the line is arriving — and the seal that frees the budget for
    it reads cut cleanliness alone, which is settled once the fragment runs one
    character past the room. Reserves are swept because the placement can spend
    the reserve headroom, which the other sweeps (all at ``reserve=0``) cannot
    reach.
    """
    _assert_prefix_stable(NO_CLEAN_CUT_CORPUS, range(4, 61), reserve=reserve)


@pytest.mark.parametrize("reserve", [0, 1, 3, 7, 12])
def test_no_boundary_fabricates_a_delimiter_once_lines_fit_the_limit(reserve):
    """The boundary invariant, in the regime the residue no longer covers (item 1).

    Once each no-clean-cut line is no longer than the limit, every such line is
    placed rather than cut, so no chunk boundary can invent a fence delimiter on
    either side — including when a ``reserve`` puts the line over the working
    budget, which is where a cut used to be forced. The floor is the LINE's own
    length: between it and the scaffolded floor the placement still holds, paid
    for by a chunk that carries its reopen and closer past ``limit``.
    """
    for limit in range(NO_CLEAN_CUT_LINE_FLOOR, 61):
        ceiling = limit if limit >= NO_CLEAN_CUT_FLOOR else limit + NO_CLEAN_CUT_SCAFFOLD
        for cut in range(1, len(NO_CLEAN_CUT_CORPUS) + 1):
            chunks = split_markdown_safe(NO_CLEAN_CUT_CORPUS[:cut], limit, reserve=reserve)
            for chunk in chunks[:-1]:
                assert _renders_closed(chunk), (limit, reserve, cut, chunk)
            assert all(len(c) <= ceiling for c in chunks), (limit, reserve, cut)


def _assert_prefix_stable(corpus: str, limits: range, reserve: int = 0) -> None:
    for limit in limits:
        previous: list[str] | None = None
        for cut in range(1, len(corpus) + 1):
            chunks = split_markdown_safe(corpus[:cut], limit, reserve=reserve)
            if previous is not None:
                sealed = previous[:-1]
                assert len(chunks) >= len(sealed)
                for i, chunk in enumerate(sealed):
                    assert chunks[i] == chunk, f"limit {limit}: chunk {i} rewritten at {cut}"
            previous = chunks


# ------------------------------------------------------ item 5: cut preference


def test_prefers_a_paragraph_break_outside_a_fence():
    text = "A" * 40 + "\n\n" + "B" * 40
    chunks = split_markdown_safe(text, 60)
    assert chunks == ["A" * 40, "B" * 40]
    assert chunks[0] == chunks[0].rstrip()  # trailing blank line trimmed at seal


def test_falls_back_to_a_line_break_when_the_paragraph_break_is_too_early():
    text = "AAAAA\n\n" + "\n".join("B" * 20 for _ in range(4))
    chunks = split_markdown_safe(text, 60)
    # The blank line sits 7 chars in — below limit//2 — so the cut takes the
    # last line boundary instead.
    assert chunks[0] == "AAAAA\n\n" + "B" * 20 + "\n" + "B" * 20
    assert chunks[1] == "B" * 20 + "\n" + "B" * 20


def test_hard_cuts_when_no_boundary_sits_within_a_quarter_of_the_budget():
    text = "ab\n" + "C" * 100
    chunks = split_markdown_safe(text, 40)
    assert chunks[0] == "ab\n" + "C" * 37  # budget filled exactly
    assert "".join(chunks) == text


def test_inside_a_fence_cuts_only_at_line_boundaries():
    text = "```\n" + "\n".join("y" * 10 for _ in range(30)) + "\n```\n"
    chunks = split_markdown_safe(text, 60)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert line in ("", "```", "y" * 10), line


# --------------------------------------------------------- item 6: whitespace


def test_indentation_is_preserved_inside_a_fence():
    body = "".join(f"    indented {i:02d}\n" for i in range(30))
    chunks = split_markdown_safe("```python\n" + body + "```\n", 90)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            if "indented" in line:
                assert line.startswith("    indented"), repr(line)


def test_indentation_is_preserved_outside_a_fence():
    text = "".join(f"  - item {i:02d}\n" for i in range(40))
    chunks = split_markdown_safe(text, 100)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            if "item" in line:
                assert line.startswith("  - item"), repr(line)


def test_trailing_whitespace_survives_inside_a_fence():
    text = "```\n" + "code   \n" * 40 + "```\n"
    chunks = split_markdown_safe(text, 50)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            if line.startswith("code"):
                assert line == "code   ", repr(line)


# ------------------------------------------------------------- item 7: tables


def test_table_header_is_not_orphaned_from_its_separator():
    text = "prose line here\n" * 5 + "| aaa | b |\n|-----|---|\n" + "| 1   | 2 |\n" * 8
    chunks = split_markdown_safe(text, 100)
    assert len(chunks) > 1
    assert chunks[0].endswith("prose line here")
    assert chunks[1].startswith("| aaa | b |\n|-----|---|\n")


def test_table_falls_back_to_plain_lines_without_a_nearby_earlier_cut():
    row = "| aaaaaaaa | bbbbbbb |\n"
    text = row + "|----------|--------|\n" + row * 4
    chunks = split_markdown_safe(text, 24)
    # No earlier boundary exists, so the header is treated as a plain line.
    assert chunks[0] == row.rstrip()


# ------------------------------------------------ item 8: edges + termination


def test_empty_text_yields_no_chunks():
    assert split_markdown_safe("", 100) == []


@pytest.mark.parametrize(
    "text,limit,reserve",
    [
        ("short", 100, 0),
        ("short", 5, 0),
        ("short", 10, 5),
        ("short", 0, 0),
        ("short", -5, 0),
        ("short", 10, 10),
        ("short", 10, 99),
    ],
)
def test_text_within_budget_or_an_unusable_budget_is_returned_unchanged(text, limit, reserve):
    assert split_markdown_safe(text, limit, reserve=reserve) == [text]


@pytest.mark.timeout(20)
def test_terminates_on_an_unbreakable_long_line_inside_a_fence():
    text = "```python\n" + "x" * 10000 + "\n```\n"
    chunks = split_markdown_safe(text, 200)
    assert len(chunks) > 40
    assert all(len(c) <= 200 for c in chunks)
    assert _reassembled(chunks) == _payload(text)


@pytest.mark.timeout(20)
def test_terminates_on_a_long_backtick_run():
    text = "`" * 5000
    chunks = split_markdown_safe(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


@pytest.mark.timeout(20)
def test_terminates_when_the_budget_cannot_hold_the_fence_scaffolding():
    text = "```a-very-long-info-string-indeed\n" + "code\n" * 20
    chunks = split_markdown_safe(text, 12)
    # Chunks go over budget here (documented), but the loop must still finish
    # and must not spin: progress is at least one character per chunk. The
    # opener line itself is hard-cut, so its fragments show up as payload —
    # what matters is that no body content was dropped.
    assert 0 < len(chunks) < 400
    assert _reassembled(chunks).endswith("code" * 20)


def test_crlf_input_is_split_and_reopened_with_a_clean_opener():
    text = "```python\r\n" + "x = 1\r\n" * 40 + "```\r\n"
    chunks = split_markdown_safe(text, 80)
    assert len(chunks) > 1
    assert all(len(c) <= 80 for c in chunks)
    for chunk in chunks[1:]:
        # The carriage return is stripped from the recorded opener, so the
        # reopen is a valid fence line rather than "```python\r".
        assert chunk.startswith("```python\n")
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n```")


def test_final_chunk_keeps_an_unclosed_fence_open():
    text = "```python\n" + "x = 1\n" * 40
    chunks = split_markdown_safe(text, 80)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n```")
    assert chunks[-1].startswith("```python\n")
    assert not chunks[-1].rstrip().endswith("```")


# ------------------------------------------------------------ item 9: unicode


def test_limits_count_characters_not_bytes():
    text = "🎉 héllo wörld ünïcöde\n" * 40
    chunks = split_markdown_safe(text, 60)
    assert len(chunks) > 1
    assert all(len(c) <= 60 for c in chunks)
    assert _reassembled(chunks) == _payload(text)


def test_multibyte_content_inside_a_fence_is_not_corrupted():
    text = "```python\n" + 'label = "héllo 🎉"\n' * 30 + "```\n"
    chunks = split_markdown_safe(text, 100)
    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            if "label" in line:
                assert line == 'label = "héllo 🎉"', repr(line)


# --------------------------------------------------------- item 10: the oracle
#
# The exhaustive small-space sweep, and why it exists: whole-line eligibility was
# fixed three times by closing the reported instance — a guard on one arithmetic
# path — and each round found another path to a dirty cut that skipped it. This
# sweep pins the CLASS rather than an instance. At these sizes every seam of the
# grammar is reachable (openers, closers, runs that cross the closer threshold,
# mid-line runs, empty lines, an unterminated last line) crossed with every
# budget where a fence's scaffolding is comparable to the budget itself —
# including the room-of-zero regime, where the ladder used to be skipped whole.
#
# Everything below reads the fence grammar from the CONTRACT, never from
# ``split.py``: no helper of the module is imported here, so a grammar bug in the
# module cannot agree with itself into a pass.

_ORACLE_N = 7  # every string over the alphabet up to this many characters
_ORACLE_ALPHABET = ("`", "x", "\n")


def _ref_render(raw: str) -> str:
    """The line a receiver renders: the text without a trailing ``\\r``."""
    return raw[:-1] if raw.endswith("\r") else raw


def _ref_opener(raw: str) -> str | None:
    """The run *raw* opens a fence with, else ``None``.

    Up to three spaces of indent plus a run of at least three backticks or
    tildes, and a backtick fence's info string may hold no backtick.
    """
    line = _ref_render(raw)
    if not _FENCE_DELIMITER.match(line):
        return None
    run = _FENCE_START.match(line)
    assert run is not None
    return run.group(0).lstrip()


def _ref_closes(raw: str, run: str) -> bool:
    """True if *raw* closes an open fence whose opener run is *run*.

    The SAME character, at least as long, and nothing but trailing whitespace
    after it — so a ``` line inside a ````block closes nothing.
    """
    got = _BARE_RUN.match(_ref_render(raw))
    if got is None:
        return False
    return got.group(1)[0] == run[0] and len(got.group(1)) >= len(run)


def _ref_advance(state: tuple[str, str] | None, raw: str) -> tuple[str, str] | None:
    """The fence state after the rendered line *raw*, given *state* before it."""
    if state is not None:
        return None if _ref_closes(raw, state[0]) else state
    run = _ref_opener(raw)
    return (run, _ref_render(raw)) if run is not None else None


@dataclass(frozen=True)
class _SrcLine:
    """One rendered line of the source, with the fence state around it."""

    start: int  # offset of its first character
    stop: int  # offset just past its text, i.e. AT its newline when it has one
    text: str  # the line without its newline
    length: int  # its length WITH the newline — what eligibility measures
    before: tuple[str, str] | None  # (run, opener line) open before the line
    after: tuple[str, str] | None  # ... and after it
    kind: str  # "open", "close", or "content"


def _src_lines(text: str) -> list[_SrcLine]:
    """*text*'s rendered lines, each with the fence state around it.

    The state advances on a line's own transition, so an offset INSIDE a line
    carries the state BEFORE it — the same reference point a cut is judged
    against, and the state a continuation chunk reopens with.
    """
    out: list[_SrcLine] = []
    state: tuple[str, str] | None = None
    start = 0
    while True:
        nl = text.find("\n", start)
        stop = len(text) if nl < 0 else nl
        body = text[start:stop]
        kind, after = "content", state
        if state is None:
            run = _ref_opener(body)
            if run is not None:
                kind, after = "open", (run, _ref_render(body))
        elif _ref_closes(body, state[0]):
            kind, after = "close", None
        span = stop - start + (0 if nl < 0 else 1)
        out.append(_SrcLine(start, stop, body, span, state, after, kind))
        state = after
        if nl < 0:
            return out
        start = nl + 1


def _line_at(lines: list[_SrcLine], offset: int) -> _SrcLine:
    """The rendered line *offset* falls in; a line's newline belongs to it."""
    for line in lines:
        if line.start <= offset <= line.stop:
            return line
    raise AssertionError(offset)


def _oracle_audit(text: str, chunks: list[str], limit: int, reserve: int) -> str | None:
    """Account for *chunks* as legal output for *text*, or say what is wrong.

    A legal chunk is ``[reopen] + <a contiguous slice of the source> + [closer]``:
    the reopen line is there exactly when a fence is open at the boundary, the
    synthetic closer exactly when one is open after the slice, and a slice sealed
    OUTSIDE a fence may lose its trailing whitespace, which the seal trims there.
    The slices must tile the source in order and exactly — that IS the content
    preservation statement, and a plain ``"".join(chunks) == text`` cannot be it,
    because a fenced split legitimately adds the scaffolding above.

    Whether a chunk's trailing bare run is the source's own closer line or the
    chunk's synthetic one is not decidable from the text alone (``"```"`` reads
    the same either way), so every reading is explored and the split is accepted
    when one of them accounts for the whole source with no violation. Returns
    ``None`` on acceptance, else a one-line description of the violation.
    """
    lines = _src_lines(text)
    cap = limit - reserve
    # A source line longer than ``limit`` is the documented residue: no chunk can
    # hold it whole, so its cut may leave a delimiter behind and the chunks around
    # it are not promised to render closed. Everything else is judged strictly.
    residue = any(line.length > limit for line in lines)

    def skips(start: int) -> list[int]:
        """*start*, plus every offset reachable by dropping trimmed whitespace.

        A seal trims its chunk's trailing whitespace, and a chunk left with
        nothing but whitespace is dropped rather than emitted, so a boundary can
        sit any number of newlines further on than the last chunk ended. Trimming
        happens only OUTSIDE a fence; the alphabet's only whitespace is the
        newline, so nothing else can be dropped here.
        """
        out = [start]
        while _line_at(lines, out[-1]).before is None:
            here = out[-1]
            if here >= len(text) or text[here] != "\n":
                break
            out.append(here + 1)
        return out

    def readings(index: int, start: int) -> list[tuple[int, str, str]]:
        """Every legal reading of chunk *index* beginning at source *start*.

        Each is ``(slice length, reopen, closer)``, which together reconstruct the
        chunk, so the next boundary is always ``start + slice length``.
        """
        state = _line_at(lines, start).before
        body = chunks[index]
        reopen = ""
        if state is not None and index:
            reopen = state[1] + "\n"
            if not body.startswith(reopen):
                return []  # the reopen the contract requires is missing
            body = body[len(reopen) :]
        out: list[tuple[int, str, str]] = []
        last = index == len(chunks) - 1
        # (a) The chunk ends at a source boundary, carrying no synthetic closer.
        # Only the final chunk may do so with a fence still open: it is the live
        # tail and is never sealed.
        if text.startswith(body, start):
            end = start + len(body)
            if last or _line_at(lines, end).before is None:
                out.append((len(body), reopen, ""))
        # (b) The chunk ends with a synthetic closer alone on its last line, which
        # the seal precedes with a newline when the slice does not end in one.
        edge = body.rfind("\n") + 1
        for split in {edge, max(edge - 1, 0)}:
            piece, tail = body[:split], body[split:]
            open_at = _line_at(lines, start + len(piece)).before
            if last or open_at is None or not text.startswith(piece, start):
                continue
            if tail.lstrip("\n") == open_at[0] and tail.count("\n") <= 1:
                out.append((len(piece), reopen, tail))
        return out

    def violations(index: int, start: int, cut: bool, read: tuple[int, str, str]) -> list[str]:
        """What one reading of chunk *index* violates, if anything."""
        span, reopen, closer = read
        piece = text[start : start + span]
        found: list[str] = []
        # The budget: content never exceeds the greater of the working budget and
        # the caller's full ``limit``. The second is the whole-line placement,
        # which measures the line alone; the fence scaffolding is what may then
        # push the chunk itself past the budget, by exactly that scaffolding.
        if span > max(cap, limit):
            found.append(f"chunk {index} holds {span} characters of content")
        # THE CLASS. A line the caller's budget can hold whole is cut only where
        # the cut is clean on both sides, so the deferred remainder can never be
        # promoted to a line-start delimiter. A line longer than ``limit`` is the
        # documented residue and is exempt — that exemption is the ONLY one.
        line = _line_at(lines, start)
        if cut and line.start < start and line.length <= limit and text[start] in " `~":
            found.append(
                f"line {line.text!r} fits the limit but was cut at "
                f"{start - line.start} before {text[start]!r}"
            )
        # Every rendered line the receiver reads as a delimiter must be one the
        # source has, or the chunk's own scaffolding. ``None`` marks scaffolding.
        parts: list[tuple[str, int | None]] = []
        if reopen:
            parts.append((reopen[:-1], None))
        offset = start
        for seg in piece.split("\n"):
            parts.append((seg, offset))
            offset += len(seg) + 1
        if closer:
            if piece.endswith("\n"):
                parts.pop()  # the empty tail the closer's own line replaces
            parts.append((closer.lstrip("\n"), None))
        state: tuple[str, str] | None = None
        for seg, source in parts:
            kind = "content"
            if state is None:
                run = _ref_opener(seg)
                if run is not None:
                    kind, state = "open", (run, _ref_render(seg))
            elif _ref_closes(seg, state[0]):
                kind, state = "close", None
            if kind == "content" or source is None:
                continue
            at = _line_at(lines, source)
            if (source, seg, at.kind) == (at.start, at.text, kind) or at.length > limit:
                continue  # a source delimiter carried through, or the residue
            found.append(f"chunk {index} reads {seg!r} as a fence {kind} the source does not")
        if state is not None and index < len(chunks) - 1 and not residue:
            found.append(f"sealed chunk {index} does not render closed")
        return found

    memo: dict[tuple[int, int, bool, bool], list[str] | None] = {}

    def tile(index: int, start: int, cut: bool, strict: bool) -> list[str] | None:
        """The violations of the first tiling of ``chunks[index:]`` over
        ``text[start:]``, or ``None`` when no tiling of it exists at all.

        ``strict`` refuses a reading that violates anything, so an accounting
        with no violation is preferred over one that carries them.
        """
        if index == len(chunks):
            return [] if len(text) in skips(start) else None
        key = (index, start, cut, strict)
        if key not in memo:
            memo[key] = None
            for begin in skips(start):
                for read in readings(index, begin):
                    bad = violations(index, begin, cut and begin == start, read)
                    if strict and bad:
                        continue
                    rest = tile(index + 1, begin + read[0], True, strict)
                    if rest is not None:
                        memo[key] = bad + rest
                        break
                if memo[key] is not None:
                    break
        return memo[key]

    if tile(0, 0, False, True) is not None:
        return None
    faults = tile(0, 0, False, False)
    if faults is None:
        return "no reading of the chunks accounts for the source"
    return "; ".join(faults) or "unaccountable"


def _oracle_texts(n: int) -> list[str]:
    """Every non-empty string over the oracle alphabet up to *n* characters."""
    texts: list[str] = []
    frontier = [""]
    for _ in range(n):
        frontier = [t + ch for t in frontier for ch in _ORACLE_ALPHABET]
        texts += frontier
    return texts


_ORACLE_TEXTS = _oracle_texts(_ORACLE_N)


@pytest.mark.parametrize("limit", range(3, 17))
def test_the_oracle_accounts_for_every_split_in_the_small_space(limit):
    """The class-killer: every small string, at every small budget (item 1).

    For each split the audit must find an accounting that preserves the content
    exactly, fabricates no line-start delimiter a receiver reads as a fence,
    leaves every sealed chunk rendering closed, and keeps each chunk's content
    inside the budget — with one exemption, the documented residue of a source
    line longer than ``limit``, which no chunk can hold whole.
    """
    for reserve in range(5):
        if reserve >= limit:
            continue
        for text in _ORACLE_TEXTS:
            chunks = split_markdown_safe(text, limit, reserve=reserve)
            problem = _oracle_audit(text, chunks, limit, reserve)
            assert problem is None, (
                f"limit={limit} reserve={reserve} text={text!r}\n"
                f"chunks={chunks!r}\nproblem: {problem}"
            )


class TestIterFenceLines:
    """``iter_fence_lines`` is the per-LINE view of the same fence machine.

    It exists for the one question spans cannot answer -- which lines are the
    delimiters -- so a channel whose markup has a single code marker can rewrite
    those while leaving the body byte-exact. Every case below is one a
    channel-local backtick counter got wrong.
    """

    def _roles(self, text: str) -> list[tuple[str, str]]:
        return list(iter_fence_lines(text))

    def test_plain_text_is_all_outside(self) -> None:
        assert self._roles("a\nb") == [("a", FENCE_OUTSIDE), ("b", FENCE_OUTSIDE)]

    def test_open_body_close_are_distinguished(self) -> None:
        assert self._roles("```py\nx\n```") == [
            ("```py", FENCE_OPEN),
            ("x", FENCE_BODY),
            ("```", FENCE_CLOSE),
        ]

    def test_a_shorter_run_inside_a_longer_fence_is_body(self) -> None:
        """Fence content is opaque: only a run of the SAME character at least as
        long as the opener closes it, so the ``` line here is code.
        """
        roles = self._roles("````md\n```\nx\n```\n````")
        assert [r for _, r in roles] == [
            FENCE_OPEN,
            FENCE_BODY,
            FENCE_BODY,
            FENCE_BODY,
            FENCE_CLOSE,
        ]

    def test_tilde_fences_are_recognised(self) -> None:
        roles = self._roles("~~~\n~~struck~~\n~~~")
        assert [r for _, r in roles] == [FENCE_OPEN, FENCE_BODY, FENCE_CLOSE]

    def test_a_backtick_fence_is_not_closed_by_a_tilde_run(self) -> None:
        """Mismatched characters do not close each other."""
        roles = self._roles("```\n~~~\nstill code")
        assert [r for _, r in roles] == [FENCE_OPEN, FENCE_BODY, FENCE_BODY]

    def test_an_unclosed_fence_yields_no_close_role(self) -> None:
        """How a caller detects an unterminated block: it owns what to append."""
        roles = self._roles("```\ncode")
        assert [r for _, r in roles] == [FENCE_OPEN, FENCE_BODY]

    def test_lines_lose_only_their_terminator(self) -> None:
        """CRLF input keeps its content; only the line ending is stripped, so a
        caller can rejoin without inventing or losing characters.
        """
        assert self._roles("a\r\nb\n") == [("a", FENCE_OUTSIDE), ("b", FENCE_OUTSIDE)]

    def test_indented_delimiters_up_to_three_spaces_count(self) -> None:
        roles = self._roles("   ```py\nx\n   ```")
        assert [r for _, r in roles] == [FENCE_OPEN, FENCE_BODY, FENCE_CLOSE]

    def test_roles_agree_with_iter_fence_spans(self) -> None:
        """The two views drive one machine, so they must never disagree about
        which characters are inside a fence.
        """
        text = "intro\n````md\n```\nnested\n```\n````\nafter\n~~~\ntail"
        spans = list(iter_fence_spans(text))
        inside = {i for s, e in spans for i in range(s, e)}
        pos = 0
        for line, role in iter_fence_lines(text):
            # Re-derive this line's start offset the way the text is laid out.
            start = text.index(line, pos) if line else pos
            pos = start + len(line)
            expected_inside = role != FENCE_OUTSIDE
            assert (start in inside) is expected_inside, (line, role)
