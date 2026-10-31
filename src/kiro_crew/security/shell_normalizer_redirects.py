"""Shell text reader: where program text rides outside argv.

A payload reaches an interpreter through a redirection, a here-string, a heredoc, a
``-c`` script glued to its flag or split across tokens, a ``sed`` ``e`` command or a
bash array, as well as through an ordinary argument. This module reads those
carriers: the output-redirect grammar and its glue point, here-string and heredoc
markers, a command's stdin carriers and their program text, operand spans, the
``xargs`` here-string rebuild, and the ``-c`` carrier forms.

Part of :mod:`kiro_crew.security.shell_normalizer`, which re-exports every name a
caller imports from it. It reads only :mod:`kiro_crew.security.shell_normalizer_walk`
below it and never imports ``shell_normalizer``. New carrier shapes belong here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .shell_normalizer_walk import _SHELL_WRAPPER_CHARS, _ends_argv

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
# farther from the end, whose program words are runs of flag letters).  A first
# word LONGER than the window is a NAME with option letters glued to its front;
# the resolver reads it under every suffix the payload refers back to
# (``_GluedBindings``), so no further split is enumerated for it.
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
    A first word LONGER than the window is an assignment NAME (no rule-covered
    program is that long) with the option letters glued to its front, and WHICH
    letters is unrecoverable after the fold.  It is not enumerated here: yielding
    one payload per suffix the script refers back to made the candidate set grow
    with the square root of the token (k suffixes cost k*(k+1)/2 characters of
    references) and the scan O(n^1.5).  Instead the resolver reads the first
    word's binding under every referenced suffix (``_GluedBindings``) -- one
    frame, linear.  The first-``c`` split is always yielded regardless of the
    window: it is the LONGEST suffix, so the unanchored regex tier sees every
    shorter reading as a substring of it and the resolver sees the whole name.
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
