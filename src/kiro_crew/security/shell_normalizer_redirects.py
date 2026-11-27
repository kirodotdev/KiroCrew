"""Shell text reader: where program text rides outside argv.

A payload reaches an interpreter through a redirection, a here-string, a heredoc, a
``-c`` script glued to its flag or split across tokens, a ``sed`` ``e`` command or a
bash array, as well as through an ordinary argument. This module reads those
carriers: the output-redirect grammar and its glue point, here-string and heredoc
markers, a command's stdin carriers and their program text, operand spans, the
``xargs`` here-string rebuild, and the ``-c`` carrier forms. It also holds the
here-document reset reading of the self floor (the text around each body) and the
leaf readers of what a command hands on: a brace body's alternatives, a pipe into an
evaluator, the command ``xargs`` builds, and a substitution body's protected name.

Part of :mod:`kiro_crew.security.shell_normalizer`, which re-exports every name a
caller imports from it. It reads only the owners below the reader
(:mod:`kiro_crew.security.shell_normalizer_walk`, the program and name vocabularies)
and never imports ``shell_normalizer``. New carrier shapes belong here.
"""

from __future__ import annotations

import contextvars
import re
from typing import TYPE_CHECKING

from . import shell_normalizer_programs as _programs
from . import shell_normalizer_walk as _walk
from .shell_normalizer_programs import _NESTED_SHELL_PROGRAMS, _NESTED_SHELL_VERBS
from .shell_normalizer_walk import (
    _SHELL_WRAPPER_CHARS,
    _ends_argv,
    _is_shell_variable_reference,
    _iter_shell_chars,
    _opens_comment,
    _shell_quote_walk,
)
from .vocabulary import _KILL_BY_NAME_PROGRAMS, _SELF_NAME_RE

if TYPE_CHECKING:
    from collections.abc import Iterator


# than grown one spelling at a time, so the boundary is stated instead of implied:
#
#   DESCRIPTOR (optional prefix)  digits -- every shell
#                                 ``&``    both streams (bash, zsh, ksh)
#                                 ``{name}`` automatic descriptor (bash 4.1+, zsh)
#                                 ``*``    all streams (PowerShell)
#   OPERATOR                      ``>`` or ``>>``
#   MODIFIER (optional suffix)    ``&``  duplicate (bash, zsh, ksh, csh)
#                                 ``|``  noclobber override (bash, zsh, ksh)
#                                 ``!``  noclobber override (zsh, csh, tcsh)
#
# NOT covered, deliberately and on the record: fish's historical ``^`` stderr prefix
# (removed in fish 3.0 and this module has no fish handling), and cmd.exe's ``n>&m``
# which the digit prefix already matches. If a shell outside that list reaches this gate,
# this set is where it has to be added.
#
# ``*`` is included on the FAIL-CLOSED rule this floor states for itself ("any maybe
# answers True -- the gate can over-trigger but never under-trigger"), because the two
# shells disagree and only one of them is safe to be wrong about. In PowerShell ``*>`` is
# the all-streams redirect, so the program arrives on stdin and must be scanned. In bash
# ``*`` is a GLOB that expands to filenames, so the first becomes the script -- measured:
# ``python *> out`` runs the globbed file, and it still does with a here-string present.
# Reading ``*>`` as a redirect therefore over-triggers under bash, which costs a denial
# of a command combining a glob-redirect with a payload-bearing carrier; reading it as a
# positional under PowerShell lets a credential mint through. Recorded as an accepted
# residual rather than left implicit.
#
# Matched on the RAW token because ``_normalize_operand`` leaves the descriptor behind
# (``2>&1`` -> ``2``, ``{fd}>&1`` -> ``{fd}``), which reads as an ordinary file name.
# The brace form requires a real identifier inside: ``{a,b}`` is a brace EXPANSION the
# shell resolves before redirect parsing, and must not be mistaken for a descriptor.
# No token matching this is ever a positional argument in the shell that spells it.
#
# No ``\A`` anchor: ``.match(raw, pos)`` anchors at *pos*, which is how a word holding a
# chain of glued redirects is walked in one pass instead of being re-sliced per operator.
_OUTPUT_REDIRECT_RE = re.compile(r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?>{1,2}[&|!]?")


# substitution, for use where a redirect ENDS an argument list rather than hiding
# a program. Testing only the first character missed every descriptor-prefixed
# spelling (``2>``, ``&>``, ``{fd}>``, ``1>``), which is exactly where a
# redirection is most often written -- so the descriptor read as an ordinary
# refspec and the command after the redirect was absorbed as arguments.
_REDIRECT_START_RE = re.compile(r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:>{1,2}[&|!]?|<{1,3})")


# NO depth cap.  A cap is a bypass: whatever number is chosen, one more nesting
# level defeats it.  Termination is guaranteed structurally instead -- a payload is
# a proper substring of the token that carried it, so it is STRICTLY SHORTER than
# its parent's source text, and a chain of strictly shorter strings is finite.  A
# visited set stops sibling wrappers re-walking the same payload.
# ``-c`` may arrive inside a COMBINED short-flag cluster: ``bash -xc '<script>'``
# and ``sh -ec '<script>'`` both run the next token as a script.  Matching only
# the exact spellings ``-c``/``-lc`` leaves every other cluster as a bypass.
# LOWERCASE only, deliberately: widening this class to ``[A-Za-z]`` makes an
# uppercase-clustered decoy (``-Cc``) the FIRST flag stop, which eats the stop
# through which a following ``--command``'s payload is found.
# Uppercase-clustered spellings are covered instead by
# ``_SHELL_COMMAND_GLUED_RE`` (glued) and the every-carrier sweep (spaced), so
# the flag stop set stays as narrow as it can be.  The
# protection is for the CASE-PRESERVING callers (the alt-traversal pass): the
# deny tiers lowercase their input first, where ``-Cc`` folds to ``-cc`` and
# eats the ``--command`` stop -- a residual there, not one this pattern can
# close.
_SHELL_COMMAND_FLAG_RE = re.compile(r"\A-[a-z]*c[a-z]*\Z")


# ``-c`` takes a VALUE, so a getopt-convention shell (``ksh``, ``zsh``) ends
# option parsing at the ``c`` and runs everything GLUED after it -- and the
# reading is deliberately OVER-approximated for the shells whose own parsers
# keep consuming cluster letters (bash's ``parse_shell_options``, dash's
# ``options()``), because extraction must cover the strictest interpreter the
# command could reach.  ``sh -c'rg . /path'`` reaches the token walk as
# ``-crg . /path`` once ``shlex`` strips the quotes.  ``_SHELL_COMMAND_FLAG_RE``
# anchors the WHOLE token as a bare flag cluster, so a token carrying the
# payload's own characters is rejected there and the payload never yielded.
# This companion pattern CAPTURES the glued remainder instead of weakening the
# flag pattern where it is used for pure flag detection.  Non-greedy, so the
# split happens at the FIRST lowercase ``c`` (``-ec'x'`` runs ``x`` under the
# getopt convention; the letters before the ``c`` are flags, either case:
# ``-Cc'x'`` clusters noclobber before the ``c``).
_SHELL_COMMAND_GLUED_RE = re.compile(r"\A-[A-Za-z]*?c(.+)\Z", re.DOTALL)


def _stdin_redirect_carriers(tokens: list[str], start: int, stop: int) -> "Iterator[str]":
    """Program text from the stdin REDIRECTIONS in ``tokens[start:stop]``.

    One walk over a token run, yielding whatever each stdin redirection puts on this
    interpreter's stdin.  The redirection families, from the shell grammar:

    * ``<<TAG`` / ``<<-TAG`` -- a heredoc; the BODY up to the matching tag is the program.
      An unterminated one runs to the end of the run, which over-yields, not under.
    * ``<<<WORD`` -- a here-string; the WORD itself is the program.
    * ``<WORD`` -- a file whose CONTENT is the program.
    * ``< <(cmd)`` -- process substitution; the command text is visible and spans tokens
      up to its closing paren, so it is yielded as a run.
    * ``<&N`` -- an fd dup, which carries no text at all; a documented residual.

    Walked as a RUN rather than "everything after the interpreter" because a
    redirection may appear ANYWHERE in a simple command -- BEFORE the program name
    (``<<'PY' python -``), after it, and GLUED TO IT with no space
    (``python3<<<'…'``, ``python3<prog.py``), all of which are ordinary bash reaching
    the same mint.  A token that carries a redirect
    after some other text is therefore classified from its first ``<`` onward: the
    text before it is the program name or an earlier operand, and the shell reads the
    rest as the redirection.

    The left-hand run is not split on a newline, so an earlier command's own stdin
    redirect is yielded too -- the same deliberate over-block the pipe producer has,
    and for the same reason.

    A heredoc's body ends at the LAST token equal to its tag, not the first.  Bash
    closes a heredoc only on a line that holds the delimiter ALONE, and line structure
    does not survive tokenizing -- so a body line that merely CONTAINS the word
    (``# EOF``, an ordinary Python comment) produced a token equal to the tag and closed
    the body early, leaving the real payload after it unscanned.
    The last occurrence is the delimiter that actually ends it; taking it
    over-yields only when the tag word recurs in a LATER command, which is the safe
    direction.
    """
    run = tokens[start:stop]
    idx = 0
    while idx < len(run):
        raw = run[idx].strip(_SHELL_WRAPPER_CHARS)
        if "<" in raw and not raw.startswith("<"):
            # A redirect GLUED to a preceding word: the shell reads everything from the
            # first `<` as the redirection, so classify that suffix. Without this the
            # interpreter's own token was excluded from the walk and
            # `python3<<<'import kiro_crew'` -- one word, no space -- was never scanned.
            raw = raw[raw.index("<") :]
        here = _here_string_payload(raw)
        if here is not None:
            # Checked before the heredoc branch, which would otherwise read `<<<payload`
            # as a tag and drop the payload.
            idx += 1
            if not here:  # a bare `<<<` puts its word next
                if idx >= len(run):
                    return
                here = run[idx].strip(_SHELL_WRAPPER_CHARS)
                yield run[idx]
                idx += 1
            else:
                yield here
            end = _operand_span_end(run, idx, here)
            yield from run[idx:end]
            idx = end
            continue
        marker = _heredoc_marker(raw)
        if marker is not None:
            # Checked before the plain-redirect branch below, which would otherwise read
            # the first `<` of `<<` as a stdin redirect.
            idx += 1
            if not marker:  # a bare `<<` splits its tag into the next token
                if idx >= len(run):
                    return
                marker = run[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            end = len(run)
            for j in range(len(run) - 1, idx - 1, -1):
                if run[j].strip(_SHELL_WRAPPER_CHARS) == marker:
                    end = j
                    break
            yield from run[idx:end]
            idx = end + 1
            continue
        if "<" in raw:
            target = raw.rsplit("<", 1)[1]
            if target.startswith("&"):
                idx += 1  # `<&N` fd dup: nothing on the command line to match
                continue
            idx += 1
            if not target:
                if idx >= len(run):
                    return
                target = run[idx].strip(_SHELL_WRAPPER_CHARS)
                yield run[idx]
                idx += 1
            else:
                yield target
            end = _operand_span_end(run, idx, target)
            yield from run[idx:end]
            idx = end
            continue
        idx += 1


def _stdin_program_text(tokens: list[str], i: int) -> "Iterator[str]":
    """The tokens that can carry the PROGRAM a stdin-reading ``python`` will run.

    ``tokens[i]`` is an interpreter that reads its program from stdin.  The shell can
    fill that stdin from exactly two families, and this yields those and nothing else:

    * a stdin REDIRECTION -- heredoc body, here-string word, redirected file or process
      substitution -- anywhere in the command: before the program name, after it, or
      glued to it (:func:`_stdin_redirect_carriers`).  Walked over the WHOLE frame in ONE
      pass, not per side of the interpreter: a marker and its body can straddle the
      program name (``<<EOF python - … EOF``), and splitting the walk lost that
      association entirely.  Only REDIRECT OPERANDS are
      yielded, so a neighbouring command's ordinary argument is still never program text;
    * a PIPE PRODUCER -- the tokens left of this interpreter, when a pipe feeds it.
      The pipe is NOT reliably its own token: the tokenizer splits on whitespace only,
      so ``echo '…'|python -`` glues the operator into a neighbouring word and
      ``_program_basename`` resolves the program from the LAST control-operator
      segment.  So the pipe is detected as a CHARACTER anywhere left of, or glued
      into, the interpreter token, and that token's own leading segment is producer
      text.  Requiring a standalone ``|`` token would miss all four no-space spellings
      and let the producer's payload through.

    Both families over-yield on the left: any pipe, or any earlier command's own stdin
    redirect, qualifies.  That is the safe direction -- a missed carrier is a bypass,
    an extra token is only a visible refusal (pinned by a test).

    Everything else in the frame is another command's argv.  Scanning THAT is the
    defect: a frame is not split on a newline, so an unrelated neighbour that
    merely names this package in a FILE PATH (``isort src/kiro_crew/mcp_core.py``
    followed by any ``python - <<'PY' … PY``) makes a harmless heredoc read as a
    credential mint -- with no ``token`` word anywhere in the command.

    Yields lazily so the caller's ``any()`` short-circuits: the cost stays O(frame)
    per interpreter token, the same bound the frame-wide scan had.
    """
    # A PIPE PRODUCER writes this interpreter's stdin, so its argv IS program text.
    glued_head, pipe_glued, _ = tokens[i].strip(_SHELL_WRAPPER_CHARS).rpartition("|")
    if pipe_glued or any("|" in t for t in tokens[:i]):
        yield from tokens[:i]
        if pipe_glued:
            yield glued_head
    yield from _stdin_redirect_carriers(tokens, 0, len(tokens))


def _glued_shell_command_payload(token: str) -> "str | None":
    """The script glued onto a ``-c`` short-option cluster, or ``None``.

    The quoted spellings (``-c'x'``, ``-c"x"``) normally lose their quotes to
    ``shlex`` before tokens reach here, but the fallback tokenizer keeps them, so
    one surviving outer quote layer is removed -- the payload comes back exactly
    as the spaced ``-c 'x'`` spelling would deliver it.  Only a layer that is
    provably a WRAPPER is removed: when the quote character also occurs inside
    the payload, the first and last characters may be two unrelated quotes
    (``'a' 'b'``), and stripping them would corrupt the reading.
    """
    match = _SHELL_COMMAND_GLUED_RE.match(token)
    if match is None:
        return None
    payload = match.group(1)
    if (
        len(payload) >= 2
        and payload[0] == payload[-1]
        and payload[0] in "'\""
        and payload[0] not in payload[1:-1]
    ):
        payload = payload[1:-1]
    return payload or None


def _is_glued_shell_command_token(token: str) -> bool:
    """MIRROR of the inline glued stop table in :func:`_nested_shell_payloads`.

    That table is built from the per-token payload cache
    (``glued_payloads[i] is not None``) rather than through a predicate call,
    so the payloads many shell tokens share are extracted once.  This mirror
    exists so the stop condition can be pinned directly by the predicate test;
    both spellings reduce to the same expression below, which is what keeps
    them from drifting.
    """
    return _glued_shell_command_payload(token) is not None


def _shell_c_carrier_glued(token: str) -> "str | None":
    """The remainder after the first lowercase ``c`` of a short-option carrier.

    The LOOSE recognition: ANY characters may precede the ``c`` (``-1c…``, a
    long cluster like ``-onoclobber`` glued ahead of it), because a
    getopt-convention parser consumes unknown letters rather than stopping, and
    because extraction deliberately over-approximates -- a junk payload
    re-tokenizes to text that matches no rule, while a missed one is a command
    nothing examines.  Returns ``""`` for a bare carrier (the payload is the
    NEXT token), or ``None`` when *token* is not a carrier at all.  Its one
    consumer is the every-carrier sweep in :func:`_nested_shell_payloads`.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return None
    position = token.find("c", 1)
    if position == -1:
        return None
    return token[position + 1 :]


# How far back from the end of a carrier's leading letter region the ambiguous
# ``c``-split scan reaches.  The true split's distance to the region's end is
# the payload's FIRST-WORD length -- a real program name -- so 64 covers any
# rule-relevant program with room to spare, while keeping the per-token scan
# O(window) and immune to cluster padding (padding only adds fake splits
# farther from the end, whose program words are runs of flag letters).
_CARRIER_SPLIT_WINDOW = 64


def _shell_c_carrier_payloads(token: str) -> "list[str]":
    """Every plausible split of a glued ``-c`` carrier, fold-ambiguity safe.

    The deny tiers LOWERCASE input before the walk, so ``-Cc'<script>'`` (a real
    zsh/ksh spelling: ``-C`` is noclobber, ``-c`` takes the script) folds to
    ``-cc<script>`` and the first-``c`` split misreads the boundary -- the
    payload comes back as ``c<script>``, whose program word matches no rule
    (the attacker can also write
    the folded spelling directly).  Which ``c`` was the option letter is
    unrecoverable after the fold, so the split is over-approximated: the FIRST
    ``c`` (getopt-correct for the unfolded spelling), plus, for every maximal
    run of consecutive ``c``\\ s, the split after the run's LAST and
    SECOND-TO-LAST ``c``.  The last-``c`` split reads the run as all flags; the
    second-to-last covers a payload whose own program name begins with one
    ``c`` (``cat``, ``curl``, ``cp``, ``chmod``, ``crontab`` -- no rule-covered
    program carries two).  Split positions are BOUNDED to the last
    ``_CARRIER_SPLIT_WINDOW`` characters of the leading letter region, which
    keeps the function linear WITHOUT opening a padding bypass: the true
    split's distance to the region's end equals the payload's first-word
    length -- a real program name, never longer than the window -- while
    cluster padding only pushes FAKE splits farther from the end, and a fake
    split's program word is a run of flag letters that matches no rule.
    Without the bound, a ~3 KB ``-acac…`` token made the candidate set
    quadratic and the synchronous deny scan outlived the loop watchdog.
    The first-``c`` split is always yielded
    regardless of the window: it is the LONGEST suffix, so the unanchored
    regex tier sees every shorter reading as a substring of it.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return []
    first = token.find("c", 1)
    if first == -1:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(payload: str) -> None:
        if payload and payload not in seen:
            seen.add(payload)
            out.append(payload)

    _add(token[first + 1 :])
    # Option letters are letters: the first non-letter character ends the
    # region where a ``c`` could have been the option that takes the script.
    # A ``c`` beyond it belongs to the payload's own text and splitting there
    # would shred the payload.
    region_end = 1
    limit = len(token)
    while region_end < limit:
        ch = token[region_end]
        if not ("a" <= ch <= "z" or "A" <= ch <= "Z"):
            break
        region_end += 1
    index = max(1, region_end - _CARRIER_SPLIT_WINDOW)
    while index < region_end:
        if token[index] == "c":
            run_start = index
            while index + 1 < region_end and token[index + 1] == "c":
                index += 1
            _add(token[index + 1 :])
            if index > run_start:
                _add(token[index:])
        index += 1
    return out


def _is_herestring_token(token: str) -> bool:
    """True where the herestring scan in :func:`_nested_shell_payloads` stops.

    Covers the spaced operator and the operator glued to its payload.  A
    SEPARATE stop from the command flag: with one shared table a herestring
    token EATS the stop through which a later ``-c``'s payload is found --
    ``bash <<<'x' -c '<script>'`` would yield only ``x`` while a real shell runs
    the script.  Independent tables scan
    each spelling in its own right, which is purely additive.
    """
    return token == "<<<" or token.startswith("<<<")


def _is_env_split_flag(token: str) -> bool:
    """True where the ``env -S`` scan in :func:`_nested_shell_payloads` stops."""
    flag = token.lower()
    return (
        flag in {"-s", "--split-string"}
        or (flag.startswith("-s") and len(token) > 2)
        or flag.startswith("--split-string=")
    )


def _sed_exec_replacement(token: str) -> str:
    """The replacement text of a ``sed`` ``s///e`` command, which GNU sed EXECUTES.

    ``sed 's/x/<name> <verb>/e'`` runs the replacement as a shell command.  Returns an
    empty string for any token that is not such a command, including an ordinary
    substitution without the ``e`` flag.
    """
    body = token.strip("\"'")
    if not body.startswith("s") or len(body) < 2:
        return ""
    delim = body[1]
    if delim.isalnum() or delim.isspace():
        return ""
    parts = body[2:].split(delim)
    if len(parts) < 3:
        return ""
    flags = parts[2]
    if "e" not in flags:
        return ""
    return parts[1]


# ``a=(<name> <verb>)`` -- a literal array assignment.  ``shlex`` splits on whitespace
# only, so the elements arrive as separate tokens with the parens glued on.
_ARRAY_ASSIGN_RE = re.compile(r"\A([A-Za-z_]\w*)=\((.*)\Z", re.DOTALL)


_ARRAY_EXPAND_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\[[@*]\]\}?")


def _array_assignments(tokens: "list[str]") -> "dict[str, str]":
    """Literal array assignments in *tokens*, as name -> the elements joined by a space.

    ``a=(<name> <verb>)`` tokenizes to ``['a=(<name>', '<verb>)']``, so the elements are
    gathered from the opening token up to the one that closes the paren.
    """
    arrays: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        match = _ARRAY_ASSIGN_RE.match(tokens[i])
        if not match:
            i += 1
            continue
        name, first = match.group(1), match.group(2)
        elements: list[str] = []
        closed = False
        for part in [first] + tokens[i + 1 :]:
            # The closing paren usually arrives with the next control operator glued on
            # (``<verb>);``), so split at the paren rather than testing the token's end.
            if ")" in part:
                stripped = part[: part.index(")")]
                if stripped:
                    elements.append(stripped)
                closed = True
                break
            if part:
                elements.append(part)
        if closed and elements:
            arrays.setdefault(name, " ".join(elements))
        i += 1
    return arrays


def _redirect_glue_point(word: str) -> "int | None":
    """Index where an OUTPUT redirect glued to the END of another word begins, else None.

    A redirect needs no whitespace in front of it, so it can ride on the back of any
    word: ``python -u> /dev/null <<< '<program>'`` is the flag ``-u`` plus ``> /dev/null``,
    and bash runs the here-string. The detector only recognised a redirect at the START of
    a word, so ``-u>`` fell through to "an ordinary interpreter flag", the redirect target
    in the next token became the script path, and the stdin program went unscanned. The
    ``<`` branch has always looked for its operator ANYWHERE in the word; this is the same
    rule for the ``>`` family, and that asymmetry was the gap.

    The word is SPLIT rather than skipped, because what precedes the redirect decides the
    answer and only the caller's own branches can classify it: ``-u`` is a flag and the
    scan continues, but ``script.py>out`` means the script supplies the program and the
    answer is False. Measured in bash: ``python script.py> out <<< '<program>'`` runs the
    script, not the here-string. Splitting and re-reading both halves reuses that
    classification instead of duplicating it, so the two cannot drift apart.

    None when the word has no ``>`` at all, or already begins with a redirect -- a leading
    file descriptor belongs to the redirect, and the shell only reads digits as one when
    they are the whole prefix (``2>err`` is fd 2; ``x2>err`` is the word ``x2``).
    """
    position = word.find(">")
    if position <= 0:
        return None
    if _OUTPUT_REDIRECT_RE.match(word) is not None:
        return None
    return position


def _here_string_payload(raw: str) -> "str | None":
    """The operand of a HERE-STRING (``<<<WORD``), ``""`` when the word is the next token.

    ``None`` when this is not a here-string.  A here-string feeds its operand to stdin
    verbatim, so for a stdin-reading interpreter that operand IS the program.

    Kept distinct from :func:`_heredoc_marker` because ``<<<`` also starts with ``<<``:
    reading it as a heredoc turned the payload into a DELIMITER and dropped it from the
    search entirely, so ``python - <<<'import kiro_crew'`` went unmatched.
    """
    if not raw.startswith("<<<"):
        return None
    return raw[3:]


def _heredoc_marker(raw: str) -> "str | None":
    """The delimiter TAG of a heredoc redirect token.

    Returns the tag for the attached spellings (``<<PY``, ``<<-PY``), ``""`` for a
    bare ``<<`` whose tag is the NEXT token, and ``None`` when this is not a heredoc --
    including a here-string (``<<<``), which is :func:`_here_string_payload`'s and must
    not be mistaken for a heredoc whose tag happens to start with ``<``.

    Read off the RAW token deliberately: ``_normalize_operand`` strips a redirection
    down to the empty string, which is why the heredoc branch in
    :func:`_python_reads_stdin` would otherwise be unreachable -- a bare
    ``python << 'PY' … PY`` is misread as running a SCRIPT named by the first
    word of the body.  Shared
    by the stdin DETECTOR and the program-text SCOPE so the two cannot disagree about
    where a heredoc body starts and ends.
    """
    if not raw.startswith("<<") or raw.startswith("<<<"):
        return None
    return raw[3:] if raw.startswith("<<-") else raw[2:]


def _xargs_here_string_rebuild(
    verb: str, tokens: "list[str]", xargs_index: int, verb_index: int
) -> "str | None":
    """The command an xargs-launched verb runs when a here-string feeds it.

    ``xargs ssh <<< localhost`` reaches ``ssh localhost``: bash removes the
    ``<<< word`` pair from the command's argv and delivers the word on stdin,
    and xargs turns stdin into ARGUMENTS for the program it launches --
    appended after the verb's own args, or substituted for every ``-I``/``-i``
    replacement token (round-35).  Every piece is in the source text, so the
    caller can judge the rebuilt command exactly like a directly-typed one.
    Returns ``None`` when no here-string feeds this simple command.  Tokens
    are kept verbatim (quotes and mask sentinels intact) so the rebuilt text
    walks the caller's own tokenizer unchanged.
    """
    payload: "str | None" = None
    consumed: "set[int]" = set()
    # A redirection can sit anywhere in the simple command, including BEFORE
    # the utility word (``<<< localhost xargs ssh``), so the scan covers the
    # whole simple command: back to the token after the previous separator,
    # forward to the next one.
    start = xargs_index
    while start > 0 and not _ends_argv(tokens[start - 1]):
        start -= 1
    for j in range(start, len(tokens)):
        tok = tokens[j]
        if _ends_argv(tok):
            break
        hs = _here_string_payload(tok)
        if hs is None:
            continue
        if hs:
            payload = hs
            consumed = {j}
        elif j + 1 < len(tokens) and not _ends_argv(tokens[j + 1]):
            payload = tokens[j + 1]
            consumed = {j, j + 1}
        break
    if payload is None:
        return None
    # xargs's own options decide HOW stdin becomes argv: an ``-I``/``-i``
    # replacement token is swapped in wherever it appears among the verb's
    # args; with none, the stdin words are APPENDED after them.
    replstr: "str | None" = None
    for k in range(xargs_index + 1, verb_index):
        opt = tokens[k].strip("\"'")
        if opt == "-I" and k + 1 < verb_index:
            replstr = tokens[k + 1].strip("\"'")
        elif opt.startswith("-I") and len(opt) > 2:
            replstr = opt[2:]
        elif opt in ("-i", "--replace"):
            replstr = "{}"
        elif opt.startswith("--replace="):
            replstr = opt.split("=", 1)[1] or "{}"
        elif opt.startswith("-i") and not opt.startswith("--") and len(opt) > 2:
            replstr = opt[2:]
    args: "list[str]" = []
    for j in range(verb_index + 1, len(tokens)):
        if j in consumed:
            continue
        tok = tokens[j]
        if _ends_argv(tok):
            break
        args.append(tok)
    if replstr is not None:
        # xargs substitutes the replacement token ANYWHERE inside an argument
        # (``ssh user@{}`` becomes ``ssh user@localhost``), so the rebuild
        # replaces substrings, not only whole tokens.
        args = [tok.replace(replstr, payload) if replstr in tok else tok for tok in args]
    else:
        args.append(payload)
    return " ".join([verb, *args]).strip()


def _operand_span_end(run: list[str], idx: int, text: str) -> int:
    """Index just past a redirect OPERAND that continues into later tokens.

    A redirect operand can open a substitution -- ``$( )``, ``<( )``, ``${ }`` or a
    backtick pair -- whose text carries whitespace, and the tokenizer splits on
    whitespace only.  So the operand is one shell WORD spread over several tokens, and
    scanning just the first of them read only ``$(printf`` out of
    ``<<<$(printf %s "import kiro_crew")``.

    Spans to the LAST token carrying a matching closer, not to the first that balances
    the count.  Balancing is not decidable here: ``normalize_shell_command`` strips
    quoting BEFORE this runs, so a quoted delimiter (``$(true ')'; printf …)``) is
    indistinguishable from a real one and a counting walk stopped early, leaving the
    payload after it unscanned.  The last closer cannot be
    undershot that way; it over-yields only when a LATER token happens to carry a closing
    character, which is the safe direction.
    """
    closers = ""
    if text.count("(") > text.count(")"):
        closers += ")"
    if text.count("{") > text.count("}"):
        closers += "}"
    if text.count("`") % 2 == 1:
        closers += "`"
    if not closers:
        return idx
    for j in range(len(run) - 1, idx - 1, -1):
        if any(c in run[j] for c in closers):
            return j + 1
    return len(run)


# Set for the self floor's EXTRA, additive here-document pass only (see
# :func:`_shell_payload_walk_uncached`): :func:`_self_tokens` then reads the command
# with the quote an own-line body leaves open escaped, so the text after the closing
# line reads unquoted. Off by default, so the ordinary pass reads a body's quotes
# exactly as origin/main does and this pass can only ADD a denial, never remove one.
# The ssh floor drives the same reset through its OWN flag (argv_floor_ssh).
_HEREDOC_RESET = contextvars.ContextVar("self_floor_heredoc_reset", default=False)


def _heredoc_body_quotes_literal(command: str) -> str:
    """The command with every here-document BODY (and its closing tag line) removed,
    to be read from a FRESH quote state -- the way bash consumes each body as literal
    data and reads what surrounds it with no quote open. What is kept is everything
    AROUND the bodies: each opener line, any command written BETWEEN two closing lines,
    and the final tail. The as-written reading (byte-identical to origin/main) still
    judges the body text. So a command a body's quote could otherwise pair across its
    closing line to bury -- between two bodies, or after the last one -- is read here
    as the command bash runs.

    Removing whole bodies is what closes the two-body case: a quote left open in one
    body cannot pair with a quote in a later body, because both bodies are gone, so a
    ``kirocrew restart`` sitting between ``cat <<A`` .. ``A`` and ``cat <<B`` .. ``B``
    is exposed, not swallowed. A self-restart or an ssh line that sits WITHIN a body
    (a note written with ``cat > f <<'EOF'``, a commit message fed to
    ``git commit -F -``) is part of a removed span and stays data, never a command.

    Spans come from :func:`read_commands`, which finds a body and its delimiter line
    the way bash does. An UNCLOSED body (empty closing text) runs to end-of-text, so
    there is nothing after it; a DEGENERATE delimiter holding a
    quote/backslash/substitution character (``<<'a"'``, ``<<"'"``, ``<<$(x)``) needs
    that character verbatim to match its closing line, so it is left to the as-written
    reading. A ``<<`` the reader held inside an open ``$(( ))`` / ``(( ))`` is a left
    SHIFT, not a here-document (``hd.arith_held``): its "body" is a fiction bash never
    reads, so it opens no text to expose and is skipped.

    Each comment of the kept text (a ``#`` at a word start outside any quote) is
    removed too, so a quote or a self command in it reads as nothing, as in bash. *command* is the text AS WRITTEN
    (case and continuations intact): bash matches a closing tag case-sensitively, so
    an ``eof`` line in an ``EOF`` body is body text, not its end.
    """
    from .shell_commands import read_commands  # the reader imports this module

    spans: "list[tuple[int, int]]" = []
    for cmd in read_commands(command, heredocs=True):
        for hd in cmd.heredocs:
            if hd.arith_held:
                continue
            end = hd.tail if hd.tail >= 0 else hd.end
            closing_tag = command[hd.end : end].lstrip("\t ")
            if end <= hd.start or not closing_tag:
                continue
            if any(ch in closing_tag for ch in "'\"`\\$()"):
                continue
            spans.append((hd.start, end))
    if not spans:
        return command
    # Remove every confirmed body-plus-closing-line span, keeping what sits AROUND
    # the bodies: each opener line, any command BETWEEN two closing lines, and the
    # final tail. bash reads all of those with no quote open, so a quote inside one
    # body can never pair with a quote in another body or after a closing line to
    # bury a command written between or after them (`cat <<A` .. `A` .. kirocrew
    # restart .. `cat <<B` .. `B`). A self-restart or self-ssh written INSIDE a body
    # is part of a removed span and stays data, as bash leaves it. Only the as-written
    # reading judges the body text, so this reading can add a denial, never drop one.
    spans.sort()
    kept_parts: "list[str]" = []
    pos = 0
    for start, end in spans:
        if start < pos:  # a nested/overlapping span is already inside a removed one
            pos = max(pos, end)
            continue
        kept_parts.append(command[pos:start])
        pos = end
    kept_parts.append(command[pos:])
    text = "".join(kept_parts)
    # Remove each comment of the kept text, found the way bash finds one: a ``#`` at
    # a word start outside any quote (the shared quote walk and ``_opens_comment``).
    # A ``#`` inside quotes or mid-word is data and stays, so a quoted ``# end'``
    # still closes its quote. A comment's quote opens nothing; it runs to its newline.
    comment_free: "list[str]" = []
    pos = 0
    while True:
        cut = next(
            (
                step.offset
                for step in _iter_shell_chars(text, start=pos)
                if step.active and step.char == "#" and _opens_comment(text, step.offset)
            ),
            -1,
        )
        if cut < 0:
            comment_free.append(text[pos:])
            return "".join(comment_free)
        comment_free.append(text[pos:cut])
        newline = text.find("\n", cut)
        pos = len(text) if newline < 0 else newline


# Where one gate call keeps the command as submitted, case and continuations intact,
# for the reset reading of the walk: bash matches a closing tag case-sensitively, so
# the bodies are found on the words as written. A tuple, since redirect views key on str.
_SUBMITTED_KEY = ("submitted_command",)


def _keep_submitted_command(command: str) -> None:
    """Keep *command* as submitted for the rest of this gate call (``is_denied``)."""
    from .shell_commands import _CALL_VIEWS  # circular import: the reader imports this module

    views = _CALL_VIEWS.get()
    if views is not None:
        views[_SUBMITTED_KEY] = command


def _cased_payload(cased: "str | None", source: str, payload: str) -> "str | None":
    """*payload* in the case it was written in *cased*, when it sits there verbatim."""
    at = source.find(payload) if cased is not None else -1
    found = cased[at : at + len(payload)] if cased is not None and at >= 0 else None
    return found if found is not None and found.lower() == payload else None


def _split_brace_alternatives(body: str) -> "list[str]":
    """Split a brace body on its TOP-LEVEL commas (``a,{b,c},d`` -> 3 parts)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for j, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[start:j])
            start = j + 1
    parts.append(body[start:])
    return parts


def _pipes_into_evaluator(tokens: "list[str]") -> bool:
    """True if this command pipes into a shell or evaluator.

    ``echo <name> <verb> | sh`` produces the dangerous command as TEXT and then
    hands it to something that runs it, so the "arguments are just data" reasoning
    does not hold: the data IS the command.
    """
    seen_pipe = False
    for token in tokens:
        if "|" in token:
            seen_pipe = True
        if seen_pipe and (
            _programs._program_basename(token) in _NESTED_SHELL_PROGRAMS
            or _programs._program_basename(token) in _NESTED_SHELL_VERBS
            or _programs._program_basename(token) == "xargs"
            or _is_shell_variable_reference(token)
        ):
            return True
    return False


def _xargs_reconstructed_command(tokens: "list[str]") -> str:
    """The command ``xargs`` will run, rebuilt from its argv plus the piped words.

    ``xargs`` appends the words it reads on stdin to the command given as its own
    arguments, so ``echo <verb> | xargs <name>`` executes ``<name> <verb>``.  Neither
    side contains a space, so the whole-token payload scan cannot see it; rebuilding the
    effective command line makes it visible to the ordinary argv checks.
    """
    pipe = next((i for i, tk in enumerate(tokens) if "|" in tk), -1)
    if pipe <= 0:
        return ""
    xargs_at = next(
        (
            i
            for i in range(pipe + 1, len(tokens))
            if _programs._program_basename(tokens[i]) == "xargs"
        ),
        -1,
    )
    if xargs_at == -1:
        return ""
    # Skip xargs' own options; everything after them is the command it runs.
    command = [tk for tk in tokens[xargs_at + 1 :] if not tk.startswith("-")]
    # The producer's literal words (its program name is not piped through).
    piped = [tk for tk in tokens[1:pipe] if not tk.startswith("-")]
    if not command:
        return ""
    return " ".join(command + piped)


def _protected_name_in_substitution(tokens: "list[str]", start: int) -> str:
    """The protected program name a substitution starting at *start* could produce.

    Scans forward until the substitution closes (``shlex`` splits it across tokens
    because it splits on whitespace only) and returns the product name, or a
    by-name kill program, if either appears inside it.  Returns "" when neither does.

    The depth is read from the shared quote-aware walk, with the quote state
    carried ACROSS tokens because ``shlex`` splits on whitespace and a quoted word
    can span one. A private ``str.count`` walk closed the substitution at a QUOTED
    ``)`` and stopped scanning there, so a name hidden after it was never seen --
    an UNDER-deny for this rule, not the over-deny a previous audit of this line
    recorded.
    """
    depth = 0
    state = 0
    ansi = False
    for token in tokens[start:]:
        walk = _shell_quote_walk(token, state=state, ansi=ansi)
        depth += walk.paren_delta
        state, ansi = walk.end_state, walk.end_ansi
        m = _SELF_NAME_RE.search(token)
        if m:
            return m.group(0)
        for verb in _KILL_BY_NAME_PROGRAMS:
            if verb in token:
                return verb
        if depth <= 0 and state == 0 and token is not tokens[start]:
            break
    return ""


def _output_redirect_scan(raw: str, start: int = 0) -> "tuple[str, int] | None":
    """``(target, end)`` for the OUTPUT redirect at *start* in *raw*, or None.

    ``python 2>&1 <<< '<program>'`` runs the here-string, but the detector had no branch
    for the ``>`` family at all: it handles ``<`` and heredocs off the raw token and let
    everything else fall through to "this is a script path". The unnumbered glued form
    only survived by accident, because ``_normalize_operand`` reduces ``>out.txt`` to the
    empty string and the loop skips empties -- while ``2>&1`` reduces to ``2``, a
    perfectly good file name, so the interpreter looked like it was running a script
    called ``2`` and the program on its stdin went unscanned.

    Every spelling is a redirect and none is ever a positional: an optional leading file
    DESCRIPTOR -- a number, ``&`` for both streams, or a ``{name}`` automatic descriptor
    -- then ``>`` or ``>>``, then an optional ``&`` for the duplicating form or ``|`` for
    the noclobber override.

    The target STOPS at the next redirect operator, and *end* is that position, because
    the shell starts a new redirect there: in ``python 2>/dev/null<<EOF`` the word is one
    token, and taking all of ``/dev/null<<EOF`` as the target swallows the heredoc marker
    and loses the program that arrives on stdin.

    Only at substitution depth ZERO, though. A redirect inside ``$(...)``, ``${...}`` or
    backticks belongs to that inner command and is not a boundary of this word:
    ``python 2>$(echo>/dev/null;printf /dev/null) <<< '<program>'`` really is
    ``python 2>/dev/null`` once the shell has run the substitution, and cutting the target
    at the inner ``>`` left the tail of the substitution to be read as a script path,
    which put the stdin program back out of view. The whole substitution is one shell
    WORD, and :func:`_operand_span_end` is what carries it across the tokens it spans.

    Depth counts every ``(`` and ``{`` INSIDE a substitution, but at depth zero only a
    ``$``-prefixed opener starts one. A subshell nested inside a substitution
    (``$( (true); printf /dev/null)``) closes with its own ``)`` -- counting the opener
    but not that one would drop the depth to zero early and reopen exactly the hole
    this closes. A BARE opener at depth zero is different: the tokenizer that feeds
    this scan strips quotes (``_self_token_frames`` shlex-splits, and the caller also
    edge-strips), so a quoted ``(`` -- one filename character to bash -- arrived here
    bare, opened a span that never closed, and the target ran past the ``<<<`` that
    should have ended it. The here-string was absorbed into the target and the program
    arriving on stdin went unscanned: measured in bash, ``python 2>'a)(b'<<<'<program>'``
    runs the program, and so does the unquoted-brace spelling ``python 2>a{b<<<'<program>'``
    (a bare ``{`` is an ordinary filename character). An UNQUOTED bare ``(`` cannot
    reach execution at all -- bash rejects ``2>a(b`` as a syntax error -- so at depth
    zero the only executable meaning of a bare opener is a filename character, and the
    walk now reads it as one. *raw* must reach here with its substitution delimiters
    intact; see the caller.

    Quote CHARACTERS in *raw* are data, never grammar. The tokenizer resolved quoting
    before this scan runs, so a quote character that survives is literal text from a
    spelling like ``2>"a'b"`` -- and reading it as grammar is the same defect in the
    opposite direction: a single-quote state opened on that data quote consumed the
    ``<<<`` to the end of the text and hid the operator (found in review, First
    Principles lane). The walk therefore steps over quote characters like any other
    filename character. Residuals, tracked rather than chased: a quoted ``'$('`` or
    a quoted backtick in a filename de-quotes to the same characters as real grammar
    and still holds or toggles a span -- indistinguishable without the quoting the
    tokenizer already destroyed, and closing that class needs quote-preserving
    tokenization at the frame level, not another rule here.

    An INDEX is returned rather than the remaining text so a word holding a chain of
    them (``>a>a>a...``) can be walked once. Re-slicing the word per operator was
    quadratic in its length, on a floor that runs for every command -- the same defect
    class this module pins against elsewhere, so it is not reintroduced here.
    """
    match = _OUTPUT_REDIRECT_RE.match(raw, start)
    if match is None:
        return None
    cut = match.end()
    depth = 0
    in_backtick = False
    while cut < len(raw):
        char = raw[cut]
        if char == "`":
            in_backtick = not in_backtick
        elif char in "({":
            # At depth ZERO an opener counts only when `$` precedes it: `$(`, `${` and
            # `$((` start substitutions, while a BARE `(` mid-word is never grammar in
            # a command that runs -- unquoted it is a bash syntax error, so the only
            # spelling that reaches execution is a quoted one, and the tokenizer that
            # feeds this scan strips quotes (see above). A bare `{` is an ordinary
            # filename character (measured: `python 2>a{b<<<'<program>'` runs the
            # program and writes the file `a{b`). INSIDE a substitution every opener
            # still counts, because a nested subshell closes with its own `)`.
            if depth or (cut > match.end() and raw[cut - 1] == "$"):
                depth += 1
        elif char in ")}" and depth:
            depth -= 1
        elif char in "<>" and not depth and not in_backtick:
            break
        cut += 1
    return raw[match.end() : cut], cut


def _rejoin_split_substitutions(tokens: "list[str]") -> "list[str]":
    """:func:`_rejoined_runs` as words: each run of tokens put back together."""
    return [word for word, _merged in _rejoined_runs(tokens)]


def _rejoined_runs(tokens: "list[str]") -> "list[tuple[str, bool]]":
    """``(word, rejoined)`` for *tokens* with each word the tokenizer split INSIDE its own command substitution
    put back together, as bash reads it: ``x=$(echo hi) kirocrew ...`` is one
    assignment word before the program, not ``x=$(echo`` and a program ``hi)``, and
    ``t="${u:+"'"}$(printf token)"`` stays one assignment. A token joins the next while
    a ``$(`` in the run is still open or the run holds an odd count of backticks. Each
    token is scanned once, so this is linear. A run that never closes is left as
    tokenized; the runs before it stay rejoined.
    The caller reads the result as an EXTRA frame beside the as-tokenized one, so this
    can only add a refusal.
    """
    out: "list[tuple[str, bool]]" = []
    run: "list[str]" = []
    depth = 0
    ticks = 0
    for token in tokens:
        run.append(token)
        ticks += token.count("`")
        i = 0
        while i < len(token):
            if token.startswith("$(", i):
                depth += 1
                i += 2
                continue
            if depth and token[i] == "(":
                depth += 1
            elif depth and token[i] == ")":
                depth -= 1
            i += 1
        if not depth and not ticks % 2:
            out.append((" ".join(run), len(run) > 1))
            run = []
    return out + [(token, False) for token in run]


# The ``${...}`` prefixes before a substitution opener in an assignment value, after
# an optional opening double quote (``T="${U:+x}$(...)"`` quotes the whole value).
_RAW_VALUE_PREFIX_RE = re.compile(r'"?(?:\$\{[^}]*\})*')


def _raw_word_scopes(command: str) -> "list[tuple[int, bool]]":
    """For each shell word of *command*, ``(start offset, at top level)``.

    A word starts where unquoted, unescaped whitespace ends, which is where
    ``shlex`` starts a token, so the list pairs with ``normalize_shell_command``'s
    tokens one for one (the caller checks the counts).  *At top level* means no
    ``(``/``{`` grouping is open in the raw text before it: not inside a subshell, a
    substitution body, a brace group or a function body.  Read from the raw text
    through the shared quote walk, so a quote the source closes cannot hide a paren
    the way a lone dequoted quote does.  If the line's grouping does not BALANCE (depth
    left open at the end, or it goes negative at some point -- a bare ``{``/``}`` word or
    a ``case`` pattern's ``)``), the running depth cannot place an assignment, so every
    word is treated as top level (FAIL-CLOSED): the alternative frame is built and can
    only ADD a refusal, never lose one (gpt/opus r29->r30).

    Every unquoted ``{`` that does not open a ``${`` parameter counts as a group,
    glued or not (``f(){ T=$(...); }``), and every unquoted ``}`` closes one, so a
    brace expansion closed in its own word (``{a,b}``) nets to nothing.  Telling a
    function body from brace expansion would take a parser, and an over-count only
    leaves an assignment unpaired, which keeps the baseline's binding.
    """
    words: list[tuple[int, bool]] = []
    depth = 0
    params = 0  # open ``${`` parameter braces: their ``}`` closes no group
    groups: "list[int]" = []  # depths of open ``{ ...; }`` groups (current shell)
    broken = False
    in_word = False
    for step in _walk._iter_shell_chars(command):
        if step.active and step.char.isspace():
            in_word = False
            continue
        if not in_word:
            in_word = True
            words.append((step.offset, depth == 0 and not broken))
        if step.char == "{" and command[step.offset - 1 : step.offset] == "$":
            params += 1
        elif step.char == "}" and params:
            params -= 1
        elif step.active and step.char == "{" and _opens_current_shell_group(command, step.offset):
            groups.append(depth)  # a ``{ ...; }`` group runs in this shell: no new scope
        elif step.active and step.char == "}" and groups and groups[-1] == depth:
            groups.pop()
        elif step.active and step.char in "({":
            depth += 1
        elif step.active and step.char in ")}":
            depth -= 1
        broken = broken or depth < 0
    # Fail-closed (gpt/opus r29->r30): an unbalanced line -- depth left open at the end,
    # or it went negative at some point (a bare ``{``/``}`` word, or a ``case`` pattern's
    # ``)``) -- means an EARLIER fake group would wrongly mark a real top-level ``T=`` as
    # nested and drop its alternative frame, allowing the mint this PR refuses. The
    # running depth cannot be trusted here, so treat every word as top level; the
    # alternative frame is then built and can only ADD a refusal. A balanced line (a real
    # ``{ …; }`` group or an uncalled ``f(){ …; }`` body) is unchanged -- its inner
    # assignment stays non-top-level and keeps main's binding, as before.
    if depth != 0 or broken:
        words = [(start, True) for start, _ in words]
    return words


def _spells_self_name(token: str) -> bool:
    """True if *token*'s basename carries a letter, so a glob there (``k*``,
    ``kirocre[w]``) spells some of the product's name.  A word of glob characters
    alone (``*``, ``test/*``) is a filename pattern, not the program, although
    :func:`_is_self_program` reads it as one that could expand to it."""
    return re.search(r"[a-z]", _programs._program_basename(token), re.IGNORECASE) is not None


def _assignment_word_starts(command: str, tokens: "list[str]") -> "list[tuple[int, bool]] | None":
    """``(start, at top level)`` per token: each assignment token placed by its unquoted
    ``NAME=`` at a word start of *command*, in order (``-1`` for any other token), or
    None when one cannot be placed. *At top level* means no ``(`` or function-body ``{``
    is open before it; a ``{ ...; }`` group runs in this shell, so it opens no scope."""
    depth_at: "dict[int, int]" = {}  # depth at each UNQUOTED offset
    depth = 0
    groups: "list[int]" = []
    for step in _walk._iter_shell_chars(command):
        if step.active:
            depth_at[step.offset] = depth
        if step.active and step.char == "{" and _opens_current_shell_group(command, step.offset):
            groups.append(depth)
        elif step.active and step.char == "}" and groups and groups[-1] == depth:
            groups.pop()
        elif step.active and (
            step.char == "(" or (step.char == "{" and command[step.offset - 1 : step.offset] != "$")
        ):
            depth += 1
        elif step.active and step.char in ")}":
            depth = max(depth - 1, 0)
    out: "list[tuple[int, bool]]" = []
    pos = 0
    for token in tokens:
        assign = _walk._LOCAL_ASSIGN_RE.match(token)
        if not assign:
            out.append((-1, True))
            continue
        name_re = re.compile(r"(?:\A|(?<=[\s;&|(]))" + re.escape(assign.group(1)) + "=")
        found = next((m for m in name_re.finditer(command, pos) if m.start() in depth_at), None)
        if found is None:
            return None
        out.append((found.start(), depth_at[found.start()] == 0))
        pos = found.end()
    return out


_COMMAND_START_RE = re.compile(
    r"(?:\A|[;&|\n({]|\)(?=\s)|(?<![\w-])(?:if|while|until|then|do|else|elif|time(?:\s+-p)?|!))\s*\Z"
)


def _at_command_start(command: str, at: int) -> bool:
    """True if offset *at* is where a command starts: after a separator, a ``(`` or
    ``{``, a case pattern's ``)``, or a reserved word a command follows (``if``,
    ``while``, ``until``, ``then``, ``do``, ``else``, ``elif``, ``!``, ``time [-p]``).
    The look-back is bounded, so a caller pays no rescan."""
    return bool(_COMMAND_START_RE.search(command[max(0, at - 256) : at]))


def _opens_current_shell_group(command: str, at: int) -> bool:
    """True if the ``{`` at *at* opens a ``{ ...; }`` group or a function body
    (``f() {``, ``f(){``, ``function f {``) that runs in the CURRENT shell, so an
    assignment in it is visible after its ``}`` or after a call. A function body is
    read as run: any later command may call it, as main's reader already assumes for a
    body it can read (``f(){ T=$(echo token); }; kirocrew $T`` is refused there)."""
    if command[at + 1 : at + 2] not in (" ", "\t", "\n"):
        return False
    before = command[max(0, at - 256) : at].rstrip()
    if re.search(r"(?:\bfunction\s+[\w-]+(?:\s*\(\s*\))?|\(\s*\))\s*\Z", before):
        return True
    return _at_command_start(command, at)


# An assignment whose value opens a substitution, as raw text.
_SUBST_ASSIGN_RE = re.compile(r"(?<![\w$])[A-Za-z_]\w*=[\"']?(?:\$\{[^}]*\})*[\"']?(?:\$\(|`)")
