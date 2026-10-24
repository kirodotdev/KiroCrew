"""Shell text reader, span layer: where a substitution's body ends.

The part of :mod:`kiro_crew.security.shell_normalizer` that matches the closing
``)``, ``}`` or backtick of a command substitution, process substitution or
``${...}`` expansion, reading nested constructs, quotes, comments, ``case`` arms
and heredocs the way bash does, and the substitution BODIES the gate scans.
Arithmetic is not modelled: a ``<<`` shift inside ``$((``/``((`` reads as a
heredoc, which fails closed. It reads the lexical layer in
:mod:`kiro_crew.security.shell_normalizer_walk` and decides nothing.
``kiro_crew.security.shell_normalizer`` re-exports every name a caller imports
from it, so that import path holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .shell_normalizer_escapes import _decode_ansi_c_body
from .shell_normalizer_walk import (
    _arms_case_context,
    _in_command_position,
    _iter_shell_chars,
    _opens_comment,
    _ShellChar,
    _word_at,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def _substitution_bodies(text: str) -> "list[str]":
    """The body of each command substitution in *text* (see
    :func:`_substitution_body_entries` for the per-body suppress flag)."""
    return [body for body, _suppress in _substitution_body_entries(text)]


def _substitution_body_entries(text: str) -> "list[tuple[str, bool]]":
    """The body of each command substitution in *text*, each paired with a
    ``suppress`` flag for the payload walk.

    ``suppress`` marks an OVER-READ: a reading of an UNPROVEN (heredoc /
    unbalanced) span -- its full remainder, its glued tails, a later span's body
    -- or a capped span's heredoc tail. This walk continues past such a span and
    emits every later opener as its own entry, so the payload walk need not
    re-extract an over-read that is a suffix of its source, and re-extracting
    them is what makes a run of sequential heredocs O(n^2) at the 20 KB scan
    cap. The flag marks the ENTRY, never a value: a proven body, a capped
    segment, a data or unproven-backtick remainder is the only reading of its
    span and is always re-extracted, even when its text equals a suffix of the
    source, or a nested protected-branch push behind it would never be scanned.

    ``$(...)`` is scanned with paren nesting so a nested substitution closing
    first does not truncate the outer body; backticks are taken pairwise.  Only
    the BODY is returned -- a bare ``kill`` must not be attributed a name that
    merely appears in a LATER, unrelated command of the same line.

    The nesting walk is QUOTE-AWARE, through the same
    :func:`_matching_close_paren` span the git-publish boundary walk uses. A
    private, quote-unaware copy of it truncates the body at a QUOTED ``)``, and
    that loses the nested command entirely rather than merely mis-sizing the span:
    ``git push origin my-feature > >(X=')' git push origin main)`` extracts the
    body ``X='``, so the nested publish of a protected branch is never scanned
    and ``is_denied`` returns None for a command bash executes. An UNPROVEN span
    yields the whole remainder, which is the fail-closed direction -- scanning
    text that is not really in the body can only add findings.

    Quoting was not the whole of it. That span helper counted a ``)`` that shell
    COMMAND GRAMMAR also puts there as an ordinary character, and two such
    spellings were measured allowing a payload this module still refuses:
    a ``#`` comment (``$(: # )`` closes on a later line) and a ``case`` pattern
    (``$(case x in x) printf token;; esac)``). Both truncated the body before the
    verb, so the value assembled from it was never recognised. The closer for the
    BACKTICK form reads the same state machine now too, so the two spellings
    cannot disagree.
    """
    entries: list[tuple[str, bool]] = []
    # Openers are matched byte by byte, so one may sit inside a single quote,
    # where bash reads it as DATA. Such a bogus opener's fresh-context span can
    # swallow a REAL opener after it (``echo '$(' "$(echo ')'; bash -c
    # '<push>')"``), so a single-quoted opener must be stepped PAST rather than
    # trusted -- every later opener still gets its own reading. The quote state
    # at each opener is read from the text AROUND the spans only: bash parses a
    # span in a fresh context, so a ``'`` inside one (``"$(echo "'")"``) quotes
    # nothing after its ``)``. A flat walk through the span would carry that
    # ``'`` out and mark a later REAL opener as single-quoted. The walk advances
    # from the last span end (``walked``) to each opener, with the state the
    # opener had (a span leaves it as it was). Only a ``'`` opens a single quote
    # (``$'...'`` included), so text without one skips the walk -- unless it
    # holds a ``"`` and a process-sub opener, which bash reads as DATA inside
    # double quotes (``"<(echo '"``) while a ``$(`` there still opens.
    quoted = "'" in text or ('"' in text and ("<(" in text or ">(" in text))
    # That flat walk does not know a ``#`` comment or a heredoc body, where a
    # ``'`` is DATA too: ``# don't`` would leave it inside a single quote at a
    # REAL opener after it. With either present, a single-quoted reading is not
    # trusted to skip an opener, and its unproven remainder is scanned instead.
    flat_quotes = (
        quoted
        and "<<" not in text
        and not any(_opens_comment(text, k) for k, char in enumerate(text) if char == "#")
    )
    state, ansi, walked = 0, False, 0
    # The glued-tail over-reads of an UNPROVEN heredoc span are emitted ONCE, at
    # the first such opener; see that branch below for why.
    over_read = True
    # The past-the-close scan of a CLOSING capped span runs once per walk;
    # see that branch below.
    past_close = True
    i = 0
    while i < len(text):
        # PROCESS substitutions run their body as a command just as a command
        # substitution does -- ``cat <(kirocrew token)`` executes the inner command and
        # feeds its output through a pipe.  Same paren-nesting walk.
        if text.startswith(("<(", ">(", "$("), i):
            if quoted:
                for step in _iter_shell_chars(text[walked:i], state, ansi):
                    state, ansi = step.state, step.ansi
                walked = i
            # A process sub opens only unquoted; a ``$(`` opens in any state
            # but a single quote.
            data = state == 1 or (state != 0 and text[i] != "$")
            if data and flat_quotes:
                # Data to bash: skip the two opener bytes so the REAL opener
                # after this one is still reached, and do NOT walk a bogus span.
                i += 2
                continue
            if data:
                # The reading may be wrong, so take the whole remainder as the
                # body, as an unproven span does: it holds every later opener.
                entries.append((text[i + 2 :], False))
                break
            # bash parses the body in a fresh quoting context, so the span is
            # proven from the slice at NEUTRAL state, as the argv_floor walk
            # does. The matcher skips a NESTED substitution and a ``${...}``
            # expansion as units, so a ``"`` or ``)`` inside one of those stays
            # sealed inside it and the enclosing span closes at its OWN matching
            # ``)``. A heredoc body inside the span leaves it unproven.
            rel, proven, _pending, capped = _close_paren_walk(text[i + 2 :])
            end = i + 2 + rel
            if proven:
                # A proven span's nested openers are reached when the payload
                # walk recurses into this body, so skip PAST the span instead of
                # re-walking from every opener inside it.
                entries.append((text[i + 2 : end - 1], False))
                i = walked = end
                continue
            if capped:
                # A span capped by ``_MAX_SPAN_NESTING``: its deep interior can
                # not be proven. Its whole-remainder reading is not appended: it
                # would cost an O(n) slice at every opener -- O(n^2) on a RUN of
                # capped spans at the 20 KB scan cap -- and the capped segments
                # below plus the structural walk cover what it holds.
                #
                # A heredoc under the cap glues its body lines and the command
                # after them past a NEWLINE, and a capped span closed at a ``)``
                # on a heredoc BODY line has its real close later still, so the
                # post-newline commands are scanned from the span's first newline
                # to the remainder's last ``)``. The newline search is BOUNDED to
                # the span ``[i + 2, end)`` -- it is what GATES the costlier last-
                # ``)`` scan -- so a clean capped RUN, whose spans hold no newline,
                # pays one bounded miss per opener instead of the fresh whole-
                # remainder ``find``/``rfind`` (each O(n)) that made the run
                # quadratic; only a span that really spans lines pays the
                # unbounded ``rfind``.
                newline = text.find("\n", i + 2, end if end > i + 2 else None)
                if newline != -1:
                    last = text.rfind(")", i + 2)
                    if last > newline:
                        entries.append((text[newline + 1 : last], True))
                if text[end - 1 : end] == ")":
                    # CLOSES: but a data ``)`` consumed at the capped depth can
                    # make it close LATE -- at the ``)`` of a sibling opener bash
                    # runs after its own close (``... ; cat <(bash -c '<push>')``).
                    # That sibling's body is inside our span, so scan the span
                    # ONCE for its substitution segments, as the never-close
                    # branch does, bounded at the close so a run stays linear.
                    # The openers AFTER the close still matter too, so CONTINUE
                    # the one linear walk from the close.
                    entries.extend(_capped_tail_entries(text, i + 2, end - 1))
                    # The close is a guess in the OTHER direction too: a data
                    # ``)`` read at the cap (``${x:-)}``) closes the span
                    # EARLY, so the span's own later text -- a ``; bash -c
                    # '<publish>'`` before bash's real close -- sits past the
                    # guessed close, outside every opener the walk reaches. So
                    # the text from the close to the end is scanned ONCE per
                    # walk, every ``)`` in it a segment boundary that closes
                    # nothing (however many closes the guess was early by),
                    # and a launcher there is a segment of its own. One pass
                    # to the end covers every later capped span as well, so a
                    # run of them stays linear.
                    if past_close:
                        past_close = False
                        entries.extend(_capped_tail_entries(text, end, reopen=True))
                    i = walked = max(end, i + 2)
                    continue
                # NEVER closes: a data ``)`` consumed at the capped depth (a
                # ``${x:-(}`` default value, say) left the span open past where
                # bash closes it, so the later TOP-LEVEL opener bash runs as a
                # sibling -- a ``$(bash -c '<publish>')`` -- is hidden inside our
                # over-long span and no payload source is a publish, so the
                # git-publish floor never runs. Scan the remainder ONCE, flat and
                # quote-aware, emitting each substitution segment as its own
                # shallow entry so that hidden push IS read. Force-extracting the
                # remainder, or stepping back into it opener by opener, would
                # re-walk the deep interior at every level -- the O(n^2) an opener
                # run cannot afford at the 20 KB scan cap.
                entries.extend(_capped_tail_entries(text, i + 2))
                i = len(text)
                continue
            # The span is UNPROVEN (a heredoc or a genuine unbalance). The whole
            # remainder from the opener is the body over-read, read in the OUTER
            # quoting context -- scanning text not really in the body only adds
            # findings (fail-closed). It and the readings below are over-reads.
            entries.append((text[i + 2 :], True))
            # A span unproven because of a HEREDOC (or a genuine unbalance) has a
            # real close with text glued after it: a ``bash -c 'git push'`` there
            # can read as ``git push)`` in the remainder. So also scan the span up
            # to the LAST ``)``, and to the OUTER close, in the outer quoting
            # context with no unit skipping. These glued-tail over-reads span from
            # the opener to near the end, so on a RUN of sequential unproven
            # heredoc spans (``"$(cat <<E\n) "`` x N) re-queuing them at every
            # opener -- and letting the payload walk re-explode each near-full
            # remainder -- is the O(n^2) cost the gate cannot afford at the 20 KB
            # scan cap. They are emitted ONCE, at the FIRST unproven opener of
            # this body (``over_read``); the outer-context reading there already
            # covers every later span's glued tail, which is a SUFFIX of it. Each
            # later unproven opener contributes only its own short span body, and
            # the walk CONTINUES structurally past the real close so every opener
            # is reached in this one linear pass rather than through a chain of
            # shrinking remainders. The payload walk does not re-extract the
            # full-remainder over-read (a proper suffix of this body) -- its
            # nested subs are those same later openers, already emitted here.
            if over_read:
                over_read = False
                # The over-reads here are the perf-sensitive bodies: this walk
                # continues past the real close and emits this span's later
                # openers as their own entries, so they need no re-extraction
                # (a run of sequential heredocs is otherwise O(n^2) at 20 KB).
                last = text.rfind(")", i + 2)
                if last != -1:
                    entries.append((text[i + 2 : last], True))
                outer, _, _, _ = _close_paren_walk(text, _MAX_SPAN_NESTING, i + 2)
                if text[outer - 1 : outer] == ")":
                    entries.append((text[i + 2 : outer - 1], True))
            else:
                replaced = text[i + 2 : end - 1] if end - 1 > i + 2 else text[i + 2 :]
                entries[-1] = (replaced, True)
            # The readings above are SUPPRESSED over-reads whose nested openers
            # this walk does not emit (it jumps to ``end``); re-extracting those
            # near-full remainders is the O(n^2) a heredoc run cannot afford. But
            # a span unproven only by a HEREDOC still has a real close, and a REAL
            # opener bash RUNS can sit after the body inside it -- ``echo $(cat
            # <<E\nE\ncat <(bash -c '<push>'))`` runs the ``<(...)``. The
            # suppressed over-reads scan that text but never EMIT the ``<(...)``,
            # so its push is no payload source and the git-publish floor never
            # runs (measured ALLOWED, push and self-restart twin). Emit the
            # span's proven-close body ``text[i+2:end-1]`` NON-suppressed so the
            # payload walk reaches those nested openers. Bounded to this one span
            # (the walk still jumps to ``end``), so a run stays linear.
            if text[end - 1 : end] == ")" and end - 1 > i + 2:
                entries.append((text[i + 2 : end - 1], False))
            elif end >= len(text) and any(op in text[i + 2 :] for op in ("$(", "<(", ">(", "`")):
                # It never closes: an arithmetic ``<<`` read as a heredoc with no
                # terminator line (``$(echo $((1<<2))\ncat <(bash -c '<push>'))``)
                # runs the span to the end, so there is no close to bound a body
                # by and every reading above is suppressed. When an opener sits
                # in that remainder, scan it ONCE, flat, as the capped never-close
                # branch does, so each opener is its own entry; the walk ends
                # here, so a run stays linear. A real heredoc in that remainder
                # can hold a body line with an unbalanced quote (``it's``), which
                # the quote-aware pass reads as live and so swallows a later
                # ``<(...)``; the quote-blind pass reads past it.
                entries.extend(_capped_tail_entries(text, i + 2))
                entries.extend(_capped_tail_entries(text, i + 2, quote_blind=True))
                i = len(text)
                continue
            i = walked = max(end, i + 2)
            continue
        if text[i] == "`":
            if quoted:
                for step in _iter_shell_chars(text[walked:i], state, ansi):
                    state, ansi = step.state, step.ansi
                walked = i
            # A backtick opens command substitution in any quoting state but a
            # single quote, where bash reads it as a LITERAL (``echo '`'``). The
            # ``state`` was ignored here, so a single-quoted backtick opened a
            # bogus span whose body then carried that ``'`` over the REAL ``$(``
            # after it and hid a push. Step PAST a single-quoted backtick so the
            # opener after it is still reached; only when the flat-quote reading
            # is not trusted (a ``#`` comment or heredoc present) take the whole
            # remainder instead (fail-closed), as the ``$(`` data branch does.
            if state == 1:
                if flat_quotes:
                    i += 1
                    continue
                entries.append((text[i + 1 :], False))
                break
            end, proven = _matching_close_backtick(text, i + 1)
            # A proven backtick body ends before the close; an UNPROVEN backtick
            # takes the whole remainder. Neither is a ``suppress`` entry, so the
            # opener hidden inside the remainder is always re-extracted.
            if proven:
                entries.append((text[i + 1 : end - 1], False))
            else:
                entries.append((text[i + 1 :], False))
            i = walked = end
        else:
            i += 1
    return entries


def _capped_tail_entries(
    text: str,
    start: int,
    stop: "int | None" = None,
    reopen: bool = False,
    quote_blind: bool = False,
) -> "list[tuple[str, bool]]":
    """Shallow substitution-body entries for a capped span.

    One linear, quote-aware forward pass over ``text[start:stop]`` (the body of a
    ``$(`` / ``<(`` / ``>(`` span whose deep interior consumed a data ``)`` at
    :data:`_MAX_SPAN_NESTING`, so the span never proved a close or closed only
    LATE, at a sibling's ``)``). bash closes that span at its OWN matching ``)``
    and runs the later opener as a SIBLING, but our over-long span swallows it,
    so without this the push in a hidden ``$(bash -c '<publish>')`` or
    ``<(bash -c '<publish>')`` is never a payload source and the git-publish
    floor never runs. *stop* bounds the pass at a late close.

    Each maximal run of text that lies INSIDE a substitution -- between an opener
    and the next structural ``$(`` / ``<(`` / ``>(`` / ``)`` / backtick -- is
    emitted ONCE as its own entry, never a ``suppress`` one. The pass starts
    INSIDE the capped span itself (depth 1, a run open at *start*), so the span's
    OWN top-level text is a run too: ``"$($($(...:...)) ; bash -c '<publish>')"``
    keeps the publish at the outer body's depth, where the root tokenizer sees
    only one double-quoted word. A push verb is contiguous inside one such run,
    so the hidden publish is scanned, while the deep nest contributes only
    short, NON-overlapping segments: both the walk and the emitted text stay
    linear in the remainder, never re-walking the interior at every level (the
    O(n^2) an opener run cannot afford at the 20 KB scan cap).
    With *reopen* the pass starts just past a capped span's GUESSED close, whose
    real depth is unknown: a ``)`` that would close the pass's base level
    ends the run there and opens the next one instead, so every run up to the
    end of the text is emitted however early the guess was.
    The segments are read in a fresh (neutral) quoting context, the same
    fail-closed direction the unproven-span over-read uses -- scanning text that
    is not really in a body can only add findings, never drop one.
    With *quote_blind* NO quote is read at all: every opener, backtick and ``)``
    is a boundary. A span that never closes may hold a heredoc whose body line
    has an unbalanced quote (``it's``); read as live, that quote swallows the
    later ``<(...)`` bash runs. The blind pass cannot be fooled that way, and
    its runs are still disjoint, so it stays linear.
    """
    entries: list[tuple[str, bool]] = []
    depth = 1
    in_backtick = False
    seg_start = start
    # One entry per span this scan currently has open (the capped span itself
    # first), each the quote state ACTIVE at its opener. A ``)`` closes the top
    # span only when it is unquoted-and-unescaped (``step.active``), OR when that
    # span was opened inside a DOUBLE quote and this ``)`` is itself in a double
    # quote -- bash runs ``"$(...)"`` and closes it at a double-quoted ``)``. A
    # ``)`` that is merely double-quoted data (the ``)`` of ``echo ")"``, whose
    # opener was NOT dq) or escaped (``\)``, reported ``state 0`` but inactive)
    # closes nothing, so the launcher behind it stays in the run and is scanned.
    # The capped span is read from a neutral context, so its own opener state is
    # 0; its base entry is never popped so the index stays valid.
    open_states = [0]

    def flush(upto: int) -> None:
        nonlocal seg_start
        if seg_start != -1 and upto > seg_start:
            entries.append((text[seg_start:upto], False))
        seg_start = -1

    stop = len(text) if stop is None else stop
    if quote_blind:
        steps: "Iterable[_ShellChar]" = (
            _ShellChar(k, c, c, True, 0, False, False) for k, c in enumerate(text[start:stop])
        )
    else:
        steps = _iter_shell_chars(text[start:stop], 0, False)
    for step in steps:
        off = start + step.offset
        # Inside a single quote every ``)``/``$(``/backtick is LITERAL data, so
        # no boundary there -- the run accumulates through it.
        if step.state == 1:
            continue
        if text.startswith(("$(", "<(", ">("), off):
            flush(off)
            depth += 1
            open_states.append(step.state)
            seg_start = off + 2
            continue
        # A backtick opens/closes a substitution, and a ``)`` closes one we
        # opened, in any state but a single quote -- and inside a DOUBLE quote
        # ``_iter_shell_chars`` reports both inactive even though bash runs the
        # substitution there, so they are tested BEFORE the active guard, the way
        # the paren walk tests the backtick. Only a ``)`` for a span THIS scan
        # opened (``depth > 0``) is a boundary; a bare ``)`` at depth 0 is data.
        if step.char == "`":
            flush(off)
            in_backtick = not in_backtick
            seg_start = off + 1 if (in_backtick or depth > 0) else -1
            continue
        if step.char == ")" and depth > 0:
            # Boundary only when unquoted-and-unescaped, or a double-quoted ``)``
            # closing a span whose opener was also double-quoted. A double-quoted
            # ``)`` whose opener was NOT dq (``echo ")"``), and an escaped ``\)``
            # (``state 0`` but ``not active``), are DATA: the run accumulates
            # through them so a later ``; bash -c '<publish>'`` is still scanned.
            opener_dq = len(open_states) > 1 and open_states[-1] == 2
            if not (step.active or (step.state == 2 and opener_dq)):
                continue
            flush(off)
            if reopen and depth == 1:
                seg_start = off + 1
                continue
            depth -= 1
            if len(open_states) > 1:
                open_states.pop()
            seg_start = off + 1 if (depth > 0 or in_backtick) else -1
    flush(stop)
    return entries


# A span nested deeper than this is read WITHOUT opening fresh contexts for its
# inner constructs, so the walk never recurses past it; that reading is reported
# unproven. Python's own recursion limit would otherwise end a deep ``${a:-${a:-``
# chain with a RecursionError before the payload behind it is queued.
_MAX_SPAN_NESTING = 32


def _matching_close_brace(
    text: str, nest: int = 0
) -> "tuple[int, bool, list[tuple[str, bool]], bool]":
    """``(index just past the ``}``, proven, heredoc_pending, capped)`` for a ``${``
    opened just before *text*.

    A parameter expansion's body is quote-aware and nests: a ``}`` inside a quote
    (``${x:-"}"}``) or inside a nested ``${``/``$(`` is data, not the close.
    :func:`_close_paren_walk` skips the expansion as a unit through this walk,
    so its interior cannot close or re-quote the enclosing span.

    ``proven`` is False when the brace never closes (the index is then
    ``len(text)``), when a nested span is unproven, or past
    :data:`_MAX_SPAN_NESTING`; the enclosing walk then reports its own span
    unproven too. ``heredoc_pending`` is the LIST of heredocs a nested ``$(`` left
    open, whose bodies bash reads from the lines after the close; the enclosing
    walk skips them there. ``capped`` is as in :func:`_close_paren_walk`.
    """
    pos = 0
    state = 0
    ansi = False
    deep = nest >= _MAX_SPAN_NESTING
    proven = not deep
    pending: list[tuple[str, bool]] = []
    capped = deep
    while pos < len(text):
        jumped = False
        # Literal ``$`` steps right before this one (a walk resumes after a
        # ``)``/``}``/backtick/newline, never after a ``$``).
        run = 0
        for step in _iter_shell_chars(text[pos:], state, ansi):
            off = pos + step.offset
            prev_run, run = run, run + 1 if step.text == "$" else 0
            if step.state != 1 and not deep:
                # bash pairs a run of dollars off from the left (``$$`` is the
                # PID), so after an ODD run of literal dollars this ``$`` opens
                # nothing and the ``{``/``(`` after it is data. An escaped ``\$``
                # is one step whose text is not ``$``, so it pairs with nothing.
                lone = text[off] != "$" or prev_run % 2 == 0
                if lone and text.startswith("$(", off):
                    rel, ok, inner_pending, inner_capped = _close_paren_walk(
                        text[off + 2 :], nest + 1
                    )
                    proven = proven and ok
                    pending = pending + list(inner_pending)
                    capped = capped or inner_capped
                    pos, state, ansi = off + 2 + rel, step.state, step.ansi
                    jumped = True
                    break
                # A process sub opens only UNQUOTED: a ``"<("`` is data.
                if step.state == 0 and text.startswith(("<(", ">("), off):
                    rel, ok, inner_pending, inner_capped = _close_paren_walk(
                        text[off + 2 :], nest + 1
                    )
                    proven = proven and ok
                    pending = pending + list(inner_pending)
                    capped = capped or inner_capped
                    pos, state, ansi = off + 2 + rel, step.state, step.ansi
                    jumped = True
                    break
                if lone and text.startswith("${", off):
                    rel, ok, inner_pending, inner_capped = _matching_close_brace(
                        text[off + 2 :], nest + 1
                    )
                    proven = proven and ok
                    pending = pending + list(inner_pending)
                    capped = capped or inner_capped
                    pos, state, ansi = off + 2 + rel, step.state, step.ansi
                    jumped = True
                    break
                if step.char == "`":
                    # A backtick delimiter is escaped only by a backslash -- a
                    # single quote does NOT protect it -- so close on the
                    # bash-faithful ``_backtick_closer``; ``-1`` means no closer,
                    # so the rest is backtick data.
                    closer = _backtick_closer(text, off + 1)
                    pos = closer + 1 if closer != -1 else len(text)
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
            if not step.active:
                continue
            # A bare ``{`` is data: bash closes ``${x:-a{b}c}`` at the first
            # ``}``. Only a nested ``${`` (skipped above) nests. A newline while a
            # nested heredoc is pending leaves the expansion unproven (its body
            # starts on the next line, in the enclosing text), the fail-closed
            # direction.
            if step.char == "\n" and pending:
                proven, pending = False, []
            elif step.char == "}":
                return (off + len(step.text), proven, pending, capped)
        if not jumped:
            break
    return (len(text), False, pending, capped)


_DQ_ESCAPABLE = frozenset('$`"\\\n')


def _dq_unescape(raw: str) -> str:
    """Remove backslashes from a double-quoted (or ``$"…"``) body the way bash
    does: only before ``$``, a backtick, ``"``, ``\\`` or a newline (a
    backslash-newline pair is removed whole). Before any other character the
    backslash stays, so ``"E\\q"`` is the delimiter ``E\\q``, not ``Eq`` --
    dropping it closed a heredoc span early at an ``Eq)`` line bash reads as
    body data.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] in _DQ_ESCAPABLE:
            if raw[i + 1] != "\n":
                out.append(raw[i + 1])
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _heredoc_delimiter(text: str, pos: int) -> "tuple[str, bool, int] | None":
    """Parse the heredoc DELIMITER that starts at *pos* (just past ``<<``).

    Returns ``(word, strip_tabs, end)`` -- the delimiter after quote removal, the
    ``<<-`` tab-strip flag, and the index just past the delimiter token (the rest
    of the operator line is still shell source) -- or ``None`` when no delimiter
    can be read (an unterminated quote, or nothing before a metacharacter). A
    ``None`` leaves the enclosing span unproven and read to the end, the
    fail-closed direction.

    bash ends a heredoc body at a line equal to the delimiter AFTER quote
    removal, so ``<<'E'`` / ``<<"E"`` / ``<<\\E`` all terminate at a line ``E``;
    this unquotes the word the same way. ``<<-`` (``strip_tabs``) strips leading
    tabs from both the body lines and the terminator.
    """
    i = pos
    strip_tabs = False
    if i < len(text) and text[i] == "-":
        strip_tabs = True
        i += 1
    while i < len(text) and text[i] in " \t":
        i += 1
    word: list[str] = []
    started = False
    while i < len(text):
        c = text[i]
        if c == "$" and i + 1 < len(text) and text[i + 1] in "'\"":
            # ANSI-C (``$'…'``) or locale (``$"…"``) quoting: the ``$`` is part
            # of the quote, removed in the delimiter like a plain ``'``/``"``.
            # Reading ``$'E'`` as the literal ``$E`` left the body's ``E``
            # terminator unmatched, so the heredoc never closed and a harmless
            # ``rm -rf "$(cat <<$'E'…E\n)" /tmp/x`` was over-refused.
            quote = text[i + 1]
            i += 2
            started = True
            body: list[str] = []
            while i < len(text) and text[i] != quote:
                if text[i] == "\\" and i + 1 < len(text):
                    body.append(text[i])
                    i += 1
                body.append(text[i])
                i += 1
            if i >= len(text):
                return None  # unterminated ANSI-C / locale quote: no delimiter
            i += 1
            raw = "".join(body)
            # ``$'…'`` resolves its escapes; ``$"…"`` is a locale lookup
            # returning the string itself, unescaped by double-quote rules.
            word.append(_decode_ansi_c_body(raw) if quote == "'" else _dq_unescape(raw))
            continue
        if c == "'":
            started = True
            i += 1
            while i < len(text) and text[i] != "'":
                word.append(text[i])
                i += 1
            if i >= len(text):
                return None  # unterminated quote: no delimiter
            i += 1
            continue
        if c == '"':
            started = True
            i += 1
            dq: list[str] = []
            while i < len(text) and text[i] != '"':
                if text[i] == "\\" and i + 1 < len(text):
                    dq.append(text[i])
                    i += 1
                dq.append(text[i])
                i += 1
            if i >= len(text):
                return None
            i += 1
            word.append(_dq_unescape("".join(dq)))
            continue
        if c == "\\":
            if i + 1 >= len(text):
                return None
            word.append(text[i + 1])
            started = True
            i += 2
            continue
        if c in " \t\n;&|<>()`\"'":
            break
        word.append(c)
        started = True
        i += 1
    if not started or not word:
        return None
    return ("".join(word), strip_tabs, i)


def _skip_heredoc_bodies(text: str, start: int, heredocs: "list[tuple[str, bool]]") -> "int | None":
    """Skip the bodies of *heredocs* (in order) as DATA, from *start* (the first
    body line). Returns the index just past the last terminator line, or ``None``
    when a terminator is missing before the text ends.

    A heredoc body is stdin DATA, not shell source: NO quote, opener, backtick,
    paren or depth state is read inside it. Each body runs until a line equal to
    its delimiter word (``<<-`` strips leading tabs from the compared line); the
    bodies of several heredocs pending on one line are consumed in order. A
    missing terminator means the body runs to the end of the text, so the
    enclosing span is unproven and read whole -- the fail-closed direction.

    Inside a substitution bash also ends the LAST body at a line that starts with
    the delimiter glued to the span's ``)`` (``EOF)"``, a common commit-message
    spelling; bash-measured, with a warning). That returns the index of the
    ``)``, so the walk closes the span there.
    """
    pos = start
    for k, (word, strip_tabs) in enumerate(heredocs):
        while True:
            newline = text.find("\n", pos)
            end = len(text) if newline == -1 else newline
            line = text[pos:end]
            if strip_tabs:
                line = line.lstrip("\t")
            if line == word:
                pos = end if newline == -1 else newline + 1
                break
            if k == len(heredocs) - 1 and line.startswith(word + ")"):
                return end - len(line) + len(word)
            if newline == -1:
                return None
            pos = newline + 1
    return pos


def _close_paren_walk(
    text: str,
    nest: int = 0,
    open_end: int = 0,
    heredocs: bool = True,
) -> "tuple[int, bool, list[tuple[str, bool]], bool]":
    """:func:`_matching_close_paren` plus ``heredoc_pending`` and ``capped``.

    ``heredoc_pending`` is the LIST of heredocs still waiting for a body when the
    span closes (empty when none). bash reads those bodies from the lines AFTER
    the close, in the ENCLOSING span, so a nested walk passes the list up and the
    enclosing walk skips them at its next newline -- structurally, as DATA, the
    way a heredoc opened and closed inside one span is skipped. A non-empty list
    is truthy, so callers that only test ``if pending`` still read it as a flag.

    ``capped`` is True when this span (or a nested paren span) was read WITHOUT
    opening fresh contexts because the nesting reached :data:`_MAX_SPAN_NESTING`.
    A span unproven only because of the cap has no heredoc or glued tail -- its
    real structure is all substitutions the payload walk descends anyway -- so
    :func:`_substitution_bodies` skips its extra readings and avoids re-queuing
    near-duplicate bodies.

    With *open_end* the walk reads the whole *text* and counts only from that
    index, so the span is read in the quoting context of the text before it.
    :func:`_substitution_bodies` reads an unproven span that way as well, so it
    never scans less than that outer-context reading.

    HEREDOCS, structurally. A ``<<WORD`` marks a heredoc PENDING and records its
    delimiter (:func:`_heredoc_delimiter`); the rest of the operator line is
    still shell source. At the newline that ends the operator line, every pending
    body is skipped as DATA up to its terminator line (:func:`_skip_heredoc_bodies`)
    and the walk RESUMES normal reading after it, so the span's real ``)`` is
    reached and the operand after it is read. Reading body lines as data -- not
    as quotes, openers, backticks, parens or depth -- is what keeps a body ``)``,
    an unclosed ``${``/``$(``/``$((``/``(``/backtick, or an ANSI-C ``$'a\\'b`` on a
    body line from mis-sizing the span. A span that skips a body is left UNPROVEN
    regardless (the push floor over-reads the body, the fail-closed direction),
    but its close INDEX is the real ``)``. When a delimiter cannot be parsed, or
    no terminator line exists before the text ends, the span is unproven AND read
    to the end; the rm floor's backstop then fails closed on a home/root operand.

    A here-string ``<<<`` is one operator, not a heredoc. Arithmetic is NOT
    modelled: ``$((``/``((`` are read as plain parens, so a ``<<`` shift inside
    them is read as a heredoc like any other -- an over-read, the fail-closed
    direction."""
    depth = 1
    cases = 0
    pos = 0
    state = 0
    ansi = False
    deep = nest >= _MAX_SPAN_NESTING
    proven = not deep
    pending: list[tuple[str, bool]] = []
    # A ``<<`` whose delimiter could not be parsed: the span is unproven and read
    # to the end of the text (there is no way to know where the body ends).
    heredoc_bad = False
    capped = deep
    here_string_end = 0
    while pos <= len(text):
        jumped = False
        run = 0  # literal ``$`` steps right before this one, as in the brace walk
        for step in _iter_shell_chars(text[pos:], state, ansi):
            off = pos + step.offset
            prev_run, run = run, run + 1 if step.text == "$" else 0
            if off < open_end:
                continue
            # A nested substitution / expansion opens a FRESH quoting context, so
            # skip it whole as a unit and resume the OUTER quote state -- its
            # inner quotes and parens must not leak into THIS span. A ``$`` is
            # literal only inside a single quote (state 1); ``<(`` / ``>(`` are
            # process subs only when unquoted; a ``$`` paired into ``$$`` (the
            # PID) opens nothing. A nested span may leave heredocs PENDING -- bash
            # reads their bodies from the enclosing span, so its list is merged
            # into ours and skipped at our next newline.
            if step.state != 1 and not deep:
                lone = text[off] != "$" or prev_run % 2 == 0
                nested = None
                if lone and text.startswith("$(", off):
                    nested = _close_paren_walk(text[off + 2 :], nest + 1, heredocs=heredocs)
                elif step.state == 0 and text.startswith(("<(", ">("), off):
                    nested = _close_paren_walk(text[off + 2 :], nest + 1, heredocs=heredocs)
                elif lone and text.startswith("${", off):
                    nested = _matching_close_brace(text[off + 2 :], nest + 1)
                if nested is not None:
                    rel, ok, inner_pending, inner_capped = nested
                    proven = proven and ok
                    pending = pending + list(inner_pending)
                    capped = capped or inner_capped
                    pos = off + 2 + rel
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                if step.char == "`":
                    # Escaped only by a backslash, never by a single quote. A
                    # ``-1`` (no closer) is an UNTERMINATED backtick -- broken
                    # text, not a live substitution -- so mark the span unproven
                    # and step over just this one backtick (the fail-closed
                    # direction), rather than swallowing the rest as backtick data
                    # and hiding the operand after the span's real ``)``.
                    closer = _backtick_closer(text, off + 1)
                    if closer == -1:
                        proven = False
                        pos = off + 1
                    else:
                        pos = closer + 1
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
            if not step.active:
                continue
            ch = step.char
            # A ``#`` opens a comment only at the START of a word, and the
            # character before it is read as the shell reads it -- a folded
            # continuation leaves ``a\`` + newline + ``#b`` one ordinary word.
            if ch == "#" and _opens_comment(text, off):
                newline = text.find("\n", off)
                if newline == -1 or heredoc_bad:
                    return (len(text), False, pending, capped)
                if pending:
                    # The operator line ended in a comment; the pending bodies
                    # still begin on the line after this newline. Skip them.
                    proven = False
                    resumed = _skip_heredoc_bodies(text, newline + 1, pending)
                    if resumed is None:
                        return (len(text), False, pending, capped)
                    pending = []
                    pos = resumed
                    state, ansi = 0, False
                    jumped = True
                    break
                pos = newline + 1
                state, ansi = 0, False
                jumped = True
                break
            # A heredoc body is stdin DATA that starts on the line AFTER its
            # operator. ``<<`` marks it pending with its delimiter; a here-string
            # ``<<<`` is one operator whose later ``<`` open nothing. Every other
            # ``<<`` counts as a heredoc, an arithmetic shift (``$((1<<2))``,
            # ``$[1<<2]``) included -- over-reading is the safe side.
            if heredocs and ch == "<" and off >= here_string_end:
                if text.startswith("<<<", off):
                    here_string_end = off + 3
                    continue
                if text.startswith("<<", off):
                    parsed = _heredoc_delimiter(text, off + 2)
                    if parsed is None:
                        heredoc_bad = True
                        proven = False
                        continue
                    word, strip_tabs, dpos = parsed
                    pending.append((word, strip_tabs))
                    pos = dpos
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
            if ch == "\n" and (heredoc_bad or pending):
                if heredoc_bad:
                    return (len(text), False, pending, capped)
                # Skip every pending heredoc body as DATA, then resume reading
                # from after the last terminator. The span stays unproven (the
                # push floor over-reads the body), but its close index is now the
                # real ``)`` so the operand after it reaches the rm floor.
                proven = False
                resumed = _skip_heredoc_bodies(text, off + 1, pending)
                if resumed is None:
                    return (len(text), False, pending, capped)
                pending = []
                pos = resumed
                state, ansi = 0, False
                jumped = True
                break
            if _word_at(text, off, "case") and _arms_case_context(text, off, cases > 0):
                cases += 1
                continue
            if _word_at(text, off, "esac") and _in_command_position(text, off):
                cases = max(0, cases - 1)
                continue
            if cases and ch in "()":
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return (off + len(step.text), proven, pending, capped)
        if not jumped:
            break
    return (len(text), False, pending, capped)


def _matching_close_paren(text: str) -> "tuple[int, bool]":
    """``(index just past the matching ``)``, proven)`` for a paren opened just
    before *text*, walked QUOTE-AWARELY through :func:`_iter_shell_chars`.

    *text* is the slice after the opener, so the walk starts at neutral state:
    bash parses a substitution body in its own quoting context.

    THE one span computation for a substitution or subshell body. Both the
    git-publish boundary walk and the nested-payload EXTRACTOR read it, because a
    body extracted over a different span than the boundary proved is exactly how a
    nested command escapes the scan: ``git push origin my-feature > >(X=')' git
    push origin main)`` truncated the extracted body at the QUOTED ``)``, so the
    payload came back as ``X='`` and the nested publish of a protected branch was
    never scanned at all -- ``is_denied`` allowed a command bash runs.

    Quoting is not the only way a ``)`` reaches this walk as an ordinary
    character. Two COMMAND-GRAMMAR constructs put one there too, and each was
    measured allowing a payload this module still refuses:

    * a ``#`` COMMENT runs to the end of its line, so the ``)`` in
      ``$(: # )`` is commented out and the substitution closes on a LATER line.
      Counting it closed the body at ``: # ``, and the ``printf`` behind it that
      computed the credential-minting verb was never scanned.
    * a ``case`` PATTERN is terminated by ``)``. In
      ``$(case x in x) printf token;; esac)`` the pattern's ``)`` is not the
      substitution's, and reading it as one truncated the body to ``case x in x``.

    Between ``case`` and its ``esac`` both parens are therefore ignored, which
    keeps a ``(x|y)`` pattern balanced-neutral as well. ARMING is generous and
    DISARMING is strict on purpose: missing a real ``case`` closes the body EARLY,
    which is the bypass, while missing a real ``esac`` only runs it long, which is
    imprecision. So ``case`` arms on any standalone word IN COMMAND POSITION
    (:func:`_arms_case_context` -- bash only reads the reserved word there, so
    ``echo case x in y`` does not arm on ordinary commands, and every
    undecidable position still arms) and ``esac`` disarms only in command
    position -- an ``esac`` passed to a command as an ARGUMENT would otherwise
    end the rule early and let the next paren close.

    ``proven`` is False when the parens never balance before the text ends, when
    a heredoc body would start inside the span, or past
    :data:`_MAX_SPAN_NESTING`. The caller must fail CLOSED on that: for an
    extractor the safe reading is the whole remainder (scan more, never less).
    The index of an unproven span is where the walk closes when it reads any
    heredoc body lines as commands (``len(text)`` when it never closes), so a
    caller that keeps reading the ENCLOSING text, as the rm floor does, resumes
    there and still sees the words after the ``)``.
    """
    end, proven, _, _ = _close_paren_walk(text)
    return (end, proven)


def _heredoc_agnostic_close(text: str) -> int:
    """Index just past the ``)`` the walk reaches when ``<<`` opens NO heredoc
    (``len(text)`` when it never closes). A walk that keeps reading the
    ENCLOSING text resumes there when the heredoc-aware span never closes
    (``$((1<<2))`` + newline), so sibling commands after the real ``)`` are
    still read (bash reads that ``<<`` as a shift)."""
    return _close_paren_walk(text, heredocs=False)[0]


def _matching_close_backtick(text: str, start: int) -> "tuple[int, bool]":
    """``(index just past the closing backtick, proven)`` for a body at *start*.

    The paren walk above got quote awareness; the BACKTICK form was still taken
    pairwise with a plain ``find``, so a backtick the shell reads as a literal --
    ``` `A='`'; ...` ``` -- was counted as the closer and truncated the body there.
    One state machine now answers both spellings, which is what keeps them from
    drifting apart again.

    A backtick inside DOUBLE quotes really is a closer (``"`cmd`"`` runs ``cmd``),
    so only single-quoted and escaped ones are data. Read through the same
    ``in_single`` idiom the argv walk uses, rather than a second reading of the
    state.

    HARDENING, not a patched hole: no payload this module refuses was reachable
    through the pairwise version -- the strings it mis-read are ones bash itself
    rejects, because backticks do not nest unescaped. It is fixed so the two
    closers cannot disagree, not because a bypass was measured.  Sibling
    scanner: :func:`_backtick_closer` is the exact bash-faithful variant
    (backslash-only, returns ``-1``) -- reach for it when the caller needs the
    closer bash itself would pick rather than a fail-closed span.
    """
    for step in _iter_shell_chars(text[start:]):
        escaped = len(step.text) == 2
        in_single = step.state == 1 and not (step.char == "'" and step.active)
        if not escaped and not in_single and step.char == "`":
            return (start + step.offset + len(step.text), True)
    return (len(text), False)


def _backtick_closer(source: str, start: int) -> int:
    """Index of the backtick that CLOSES a substitution opened before *start*.

    Within backticks bash strips a backslash before ``$``, ``\\``` and ``\\\\``,
    so an escaped backtick is data and must not be taken as the closer --
    ``str.find`` did, and it truncated ``kill `printf '\\`' ; pgrep -f <name>```
    one clause short of the target's name (found in pre-push review, bash-
    measured: the inner command past the escaped backtick runs).  Quotes do NOT
    protect a backtick from closing, so this scan honours backslashes only.

    -1 when no unescaped closer exists before the text ends.  Sibling scanner:
    :func:`_matching_close_backtick` is the quote-aware over-approximating
    variant returning ``(index, proven)`` -- reach for that one when an
    unproven span must fail closed rather than fall through.
    """
    j = start
    n = len(source)
    while j < n:
        if source[j] == "\\":
            j += 2
            continue
        if source[j] == "`":
            return j
        j += 1
    return -1
