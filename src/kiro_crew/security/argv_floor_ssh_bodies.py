"""The ssh self-target floor's reading of substitution bodies: each one bash runs is judged.

A ``$( )`` or backtick body is a command line bash runs before the outer command, and
the static splice in ``argv_floor_ssh`` reads only its first command (``$(echo hi; ssh
<this host> id)`` splices to ``hi``). So each body bash runs -- unquoted, inside double
quotes, in an unquoted heredoc, nested, or inside a ``bash -c`` / ``sh -c`` / ``eval`` /
here-string payload -- is judged here as a command line of its own, with
``_is_ssh_to_self``. Quote state, comments and heredoc spans come from the shared shell
reader. This reading only adds refusals on top of the line's own verdict.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the ssh floor in
``argv_floor_ssh`` and the frame walk in ``argv_floor_self``, both below it.
"""

from __future__ import annotations

import re

from .argv_floor_self import _quoted_assignment_scripts
from .argv_floor_ssh import (
    _EMPTY_EXPANSION_RE,
    _MAIN_READING,
    _is_ssh_to_self,
    _may_name_ssh_verb,
    _resolve_line_assignments,
    _splice_static_substitutions,
)
from .shell_commands import (
    _heredoc_body_substitutions,
    _without_quoted_heredoc_text,
    read_commands,
)
from .shell_normalizer import (
    _backtick_closer,
    _fold_line_continuations,
    _iter_shell_chars,
    _matching_close_paren,
    _nested_shell_payloads,
    _opens_comment,
    _self_tokens,
    _shell_tokens,
)

#: A ``<( )`` / ``>( )`` process-substitution opener whose ``(`` sits past a ``\``-newline
#: line continuation bash removes before parsing (``<``-``\``-newline-``(``). The adjacent
#: ``<(`` / ``>(`` spelling is caught by the literal checks in ``_substitution_reaches_self``.
_PROCSUB_CONTINUATION_RE = re.compile(r"[<>](?:\\\n)+\(")


def _live_substitutions(text: str) -> "list[tuple[int, int, str]]":
    """``(start, end, body)`` of each outermost ``$( )`` / backtick / ``<( )`` / ``>( )``
    substitution bash runs in *text*, in text order; ``start:end`` spans the opener to
    the closer.

    Unquoted or inside double quotes; never in single quotes, a comment, after an
    escape, as ``$$(`` or in a quoted heredoc. Heredoc bodies are blanked before the
    scan (as ``_double_quoted_substitution_bodies`` does), and the substitutions of an
    unquoted one come from ``shell_commands._heredoc_body_substitutions``, the scan
    ``read_commands`` reads them with.
    """
    subs: list[tuple[int, int, str]] = []
    scan = text
    if "<<" in text:
        for doc in [doc for cmd in read_commands(text, heredocs=True) for doc in cmd.heredocs]:
            blank = re.sub("[^\n]", " ", text[doc.start : doc.end])
            scan = scan[: doc.start] + blank + scan[doc.end :]
            if not doc.quoted:
                subs += [
                    (doc.start + s, doc.start + e, body)
                    for s, e, _at, body in _heredoc_body_substitutions(doc.body)
                ]
    i, state = 0, 0
    while i < len(scan):
        dollars = 0
        # Past the first pass, the scan resumes just after a substitution's closer
        # (or at a comment's newline, which is no ``#``). The closer is part of the
        # word, so a ``#`` right after it (``$(date)#$(...)``) is glued, not a
        # comment: one word character put in front of the rest tells the check so.
        rest = ("_" if i else "") + scan[i:]
        for step in _iter_shell_chars(scan[i:], state):
            at = i + step.offset
            dollars = dollars + 1 if step.state != 1 and step.text == "$" else 0
            if step.state == 0 and step.text == "#" and _opens_comment(rest, step.offset + (i > 0)):
                newline = scan.find("\n", at)
                i, state = (len(scan) if newline < 0 else newline), 0
                break
            if dollars % 2 and scan.startswith("(", at + 1) and not scan.startswith("((", at + 1):
                end, proven = _matching_close_paren(scan[at + 2 :], 0)
                stop = at + 2 + end if proven else len(scan)
                subs.append((at, stop, text[at + 2 : stop - proven]))
                i, state = stop, step.state
                break
            if step.state == 0 and step.text in "<>":
                # ``<( )`` / ``>( )`` process substitution: bash runs the inner command and
                # wires its output (or input) to a pipe, so the body is a command line of
                # its own, like ``$( )``. Only unquoted: bash opens no process substitution
                # inside double quotes. bash removes a ``\``-newline line continuation before
                # it parses, so ``<``-``\``-newline-``(`` is still an opener; skip the
                # continuation run to the ``(`` the way bash does, or the respelling escapes
                # the reading the adjacent form gets.
                paren = at + 1
                while scan.startswith("\\\n", paren):
                    paren += 2
                if scan.startswith("(", paren):
                    end, proven = _matching_close_paren(scan[paren + 1 :], 0)
                    stop = paren + 1 + end if proven else len(scan)
                    subs.append((at, stop, text[paren + 1 : stop - proven]))
                    i, state = stop, step.state
                    break
            if step.state != 1 and step.text == "`":
                closer = _backtick_closer(scan, at + 1)
                stop = closer + 1 if closer >= 0 else len(scan)
                subs.append((at, stop, text[at + 1 : stop - (closer >= 0)]))
                i, state = stop, step.state
                break
        else:
            break
    return sorted(subs)


def _line_may_name_ssh_verb(text_lower: str) -> bool:
    """The verb gate on the whole line, read both as written and statically spliced:
    the splice erases a list's later commands, and builds a verb glued from parts."""
    raw = _EMPTY_EXPANSION_RE.sub("", text_lower)
    spliced = _splice_static_substitutions(raw, escaped_too=True)
    return any(_may_name_ssh_verb(_resolve_line_assignments(t)) for t in (raw, spliced))


def _fold_outside_quoted_heredocs(text: str) -> str:
    """Fold ``\\``-newline line continuations the way bash does, in one heredoc-aware pass.

    bash folds a continuation while lexing -- so a split ``<<`` / ``<<-``, or a command
    word split before one, is seen whole -- and only THEN delimits a heredoc body, which
    for a quoted tag (``<<'EOF'``) it keeps literal. A ``<<`` is a heredoc only in a COMMAND
    context: at top level, in a ``$( )`` or backtick substitution or a ``( )`` subshell, or
    in such a substitution reopened inside double quotes. It is NOT one inside ``'...'``,
    directly inside ``"..."``, after a ``#`` comment, or in ``$(( ))`` / ``(( ))`` / ``${ }``.
    A context stack tracks exactly that. With no heredoc the output equals
    :func:`_fold_line_continuations`."""
    # A split operator (``<``-``\``-newline-``<``) has no literal ``<<``; test after dropping
    # continuations so the scanner still runs for it. A ``#`` sends text to the scanner too:
    # the plain fold is comment-unaware and would fold a ``\``-newline inside a ``# ...``
    # comment, which bash leaves literal, merging the comment into the next command.
    if "<<" not in text.replace("\\\n", "") and "#" not in text:
        return _fold_line_continuations(text)
    n = len(text)
    out: list[str] = []
    pending: "list[tuple[str, bool, bool]]" = []

    def _past(at: int) -> int:
        while text.startswith("\\\n", at):
            at += 2
        return at

    def _folded_delimiter(at: int) -> "tuple[str, bool, bool]":
        # Read ONLY the delimiter word, folding continuations, so each ``<<`` costs O(tag),
        # not O(rest-of-text). ``strip`` is a ``<<-``; ``quoted`` is a tag that carried a
        # quote or backslash (bash leaves such a body literal).
        j = _past(at)
        strip = j < n and text[j] == "-"
        if strip:
            j = _past(j + 1)
        while j < n and text[j] in " \t":
            j = _past(j + 1)
        raw: list[str] = []
        q: "str | None" = None
        while j < n:
            if q != "'" and text.startswith("\\\n", j):
                j += 2
                continue
            c = text[j]
            if q is None:
                if c in " \t\n" or c in "<>|&;()":
                    break
                if c in "'\"":
                    q = c
                elif c == "\\" and j + 1 < n:
                    raw.append(text[j : j + 2])
                    j += 2
                    continue
                raw.append(c)
                j += 1
            else:
                raw.append(c)
                j += 1
                if c == q:
                    q = None
        word = "".join(raw)
        tokens = _shell_tokens(word)
        return (tokens[0] if tokens else ""), strip, any(x in word for x in "'\"\\")

    def _consume_bodies(pos: int) -> int:
        nonlocal pending
        for tag, strip, quoted in pending:
            while pos < n:
                nl = text.find("\n", pos)
                end = n if nl < 0 else nl
                line = text[pos:end]
                while not quoted and nl >= 0 and (len(line) - len(line.rstrip("\\"))) % 2:
                    nl = text.find("\n", end + 1)
                    joined = n if nl < 0 else nl
                    line, end = line[:-1] + text[end + 1 : joined], joined
                seg = text[pos : end + 1] if nl >= 0 else text[pos:end]
                if (line.lstrip("\t") if strip else line) == tag:
                    out.append(seg)  # terminator line, as read
                    pos = end + 1 if nl >= 0 else n
                    break
                out.append(seg if quoted else _fold_line_continuations(seg))
                pos = end + 1 if nl >= 0 else n
            else:
                pos = n
        pending = []
        return pos

    # Each frame is [kind, closer, depth]. kind: cmd (heredocs live), dq, sq, ansi, skip
    # (``$(( ))`` / ``(( ))`` / ``${ }`` -- fold, no heredoc). closer pops the frame.
    stack: "list[list]" = [["cmd", None, 0]]
    last_paren_closed_subst = False
    i = 0
    while i < n:
        kind = stack[-1][0]
        if kind == "sq":  # single quote: literal, no fold, no nested anything
            if text[i] == "'":
                stack.pop()
            out.append(text[i])
            i += 1
            continue
        if kind == "ansi":  # $'...': backslash escapes, continuation literal
            if text[i] == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if text[i] == "'":
                stack.pop()
            out.append(text[i])
            i += 1
            continue
        if text.startswith("\\\n", i):  # fold a continuation (cmd, dq, skip)
            i += 2
            continue
        if text[i] == "\\" and i + 1 < n:  # escape pair kept together
            out.append(text[i : i + 2])
            i += 2
            continue
        ch = text[i]
        # Substitution and quote openers, in any folding context:
        if text.startswith("$((", i) or text.startswith("((", i):
            op = "$((" if text.startswith("$((", i) else "(("
            out.append(op)
            stack.append(["skip", "))", 2])
            i += len(op)
            continue
        if text.startswith("$(", i):
            out.append("$(")
            stack.append(["cmd", ")", 0, True])  # 4th slot: closes a $( substitution
            i += 2
            continue
        if text.startswith("${", i):
            out.append("${")
            stack.append(["skip", "}", 1])
            i += 2
            continue
        if ch == "`":
            if kind == "cmd" and stack[-1][1] == "`":
                stack.pop()
            else:
                stack.append(["cmd", "`", 0])
            out.append("`")
            i += 1
            continue
        if ch == '"':
            if kind == "dq":
                stack.pop()
            else:
                stack.append(["dq", '"', 0])
            out.append('"')
            i += 1
            continue
        if kind != "dq" and ch == "'":
            stack.append(["sq", "'", 0])
            out.append("'")
            i += 1
            continue
        if kind != "dq" and text.startswith("$'", i):
            stack.append(["ansi", "'", 0])
            out.append("$'")
            i += 2
            continue
        if kind == "skip":  # arithmetic / parameter expansion: track nesting, no heredoc
            closer = stack[-1][1]
            if closer == "))":
                if ch == "(":
                    stack[-1][2] += 1
                elif ch == ")":
                    stack[-1][2] -= 1
                    if stack[-1][2] <= 0:
                        stack.pop()
            elif closer == "}":
                if ch == "{":
                    stack[-1][2] += 1
                elif ch == "}":
                    stack[-1][2] -= 1
                    if stack[-1][2] <= 0:
                        stack.pop()
            out.append(ch)
            i += 1
            continue
        if kind == "cmd":
            if ch == ")" and stack[-1][1] == ")":
                popped = stack.pop()
                out.append(")")
                # G2: a ``)`` that closed a ``$(`` is part of a word (``$(x)#`` is one word),
                # so a ``#`` glued after it is not a comment; a subshell ``)`` is an operator.
                last_paren_closed_subst = len(popped) > 3 and popped[3] is True
                i += 1
                continue
            if ch == "(":
                stack.append(["cmd", ")", 0])
                out.append("(")
                i += 1
                continue
            prev = out[-1][-1] if out and out[-1] else "\n"
            # ``#`` opens a comment after a separator, after a word-ending ``)`` (a subshell
            # close or a ``case`` pattern) -- but NOT after a ``$(`` close, which is a glued
            # word. G1: bash discards a comment, so DROP it instead of copying it; copying
            # left a ``\``-newline inside the comment for the comment-unaware line judge to
            # fold into the next command, hiding a self-ssh on that line.
            comment_here = prev in ("\n", " ", "\t", ";", "&", "|", "(", "`") or (
                prev == ")" and not last_paren_closed_subst
            )
            if ch == "#" and comment_here:
                nl = text.find("\n", i)
                i = n if nl < 0 else nl
                continue
            if ch == "\n":
                out.append("\n")
                i += 1
                if pending:
                    i = _consume_bodies(i)
                continue
            if ch == "<":
                s2 = _past(i + 1)
                if s2 < n and text[s2] == "<":
                    s3 = _past(s2 + 1)
                    if s3 < n and text[s3] == "<":
                        out.append("<<<")  # here-string, not a heredoc
                        i = s3 + 1
                        continue
                    out.append("<<")  # heredoc operator, seen whole across a continuation
                    pending.append(_folded_delimiter(s2 + 1))
                    i = s2 + 1
                    continue
            if ch == ")":
                last_paren_closed_subst = False  # a case pattern or base-frame ``)``
            out.append(ch)
            i += 1
            continue
        out.append(ch)  # dq: emit; openers above handled $( / ` / nested "
        i += 1
    return "".join(out)


#: Commands whose here-document body or here-string a shell never runs: the body is
#: printed or written as DATA. ``git commit`` reading its message from stdin is one too.
_NONSHELL_CONSUMER_HEADS = frozenset({"cat", "tee", "echo", "printf"})

#: Command heads that make a line more than a plain command list: a group / compound
#: command / coproc / ``time`` / ``!`` prefix / (with a trailing ``()``) a function body.
#: Their presence means a consumer's output can reach a shell, so the line is never cut.
_COMPOUND_HEADS = frozenset(
    {
        "{",
        "}",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "coproc",
        "function",
        "select",
        "time",
        "!",
    }
)


def _is_nonshell_consumer(argv: "tuple") -> bool:
    """True for a command whose here-doc body / here-string is DATA it prints or writes,
    never a script a shell runs: ``cat``, ``tee``, ``echo``, ``printf``, or ``git commit``
    taking its message from stdin (``-F -`` / ``--file -`` / ``--file=-``)."""
    raws = [w.raw for w in argv]
    if not raws:
        return False
    if raws[0] in _NONSHELL_CONSUMER_HEADS:
        return True
    if raws[0] == "git" and "commit" in raws:
        for i, a in enumerate(raws):
            if a == "--file=-" or (a in ("-f", "--file") and raws[i + 1 : i + 2] == ["-"]):
                return True
    return False


def _steps_expand_nothing(steps) -> bool:
    """The steps are a single-quoted literal: a single-quote is present, no double-quote
    context, and no live ``$`` or backtick -- so a shell expands nothing in them."""
    return (
        any(s.state == 1 for s in steps)
        and not any(s.state == 2 for s in steps)
        and not any(s.active and s.char in "$`" for s in steps)
    )


def _scanner_cut_nonshell_literals(text: str) -> str:
    """Scanner-leg-only (never the belt, never the shared ``_without_quoted_heredoc_text``):
    drop the literal body a NON-SHELL consumer only prints or writes, so the per-frame
    descent does not read it as a script. Cuts a QUOTED-tag here-document
    (``<<'EOF'`` / ``<<"EOF"`` / ``<<\\EOF``) and a SINGLE-QUOTED here-string (``<<<'...'``)
    of a :func:`_is_nonshell_consumer` command only when that consumer is PROVABLY TERMINAL
    and UNGROUPED: its own separator ends the command (end-of-line / ``;`` / ``&&`` / ``||``,
    never ``|`` ``&`` or a frame opener/closer), it sits at the top level (not inside a
    ``( )`` subshell, a ``$( )`` / backtick substitution or a ``{ }`` group), and no ``>(`` /
    ``<(`` process substitution appears on the line. Anything else -- a body a shell reads
    (``bash <<'EOF'``, ``cat <<'EOF' | bash``, ``bash <<<'...'``), an UNQUOTED here-document
    whose ``$(...)`` runs, a grouped or piped consumer, a ``tee >(bash)`` -- is left in.
    Because it only REMOVES text the scanner would otherwise descend, it can lower only
    head-added refusals and never takes the floor below origin/main's belt reading."""
    if "<<" not in text:
        return text
    # A ``>(`` / ``<(`` anywhere on the line means a consumer's output could reach a shell
    # (``tee >(bash)``, ``cat > >(bash)``), so cut nothing on it.
    if ">(" in text or "<(" in text:
        return text
    cmds = read_commands(text, heredocs=True, nested=False)
    # Positive rule: the literal-consumer exemption applies ONLY to a PLAIN COMMAND LIST --
    # after the fold, every command head is an ordinary word (no compound keyword and no
    # function definition) and no separator is a pipe. A group, compound command (``{ }``,
    # ``if``/``while``/``for``/``case``, a function body), ``coproc``, ``time``/``!`` prefix
    # or a pipe can route a consumer's output to a shell, and a consumer-local check cannot
    # see an enclosing one, so none of them is ever cut. The head-check reads argv[0] only,
    # so a compound word as a mere ARGUMENT (``echo done``) still allows the line.
    for c in cmds:
        if c.separator in ("|", "|&"):
            return text
        if c.argv and (c.argv[0].raw in _COMPOUND_HEADS or c.argv[0].raw.endswith("()")):
            return text
    cuts: "list[tuple[int, int]]" = []
    depth = 0
    for i, cmd in enumerate(cmds):
        # Cut only a TERMINAL consumer (its own separator ends the command: end-of-line /
        # ``;`` / ``&&`` / ``||``) at frame depth 0 -- not inside a ``( )`` subshell or a
        # ``$( )`` / backtick substitution. (The plain-command-list check above has already
        # excluded ``{ }`` groups, compound commands, functions, ``coproc`` and pipes.)
        # And only the FIRST command of the list: any earlier command (``hash -p``,
        # ``alias``, a function definition, ``PATH=...``, ``enable -f``) can rebind the
        # consumer's name to a shell that runs the body, and no static read can rule
        # that out. Commands after the consumer cannot rebind it.
        terminal = cmd.separator in ("", "\n", ";", "&&", "||")
        if i == 0 and depth == 0 and terminal and _is_nonshell_consumer(cmd.argv):
            for doc in cmd.heredocs:
                if doc.quoted:
                    cuts.append((doc.start, doc.end))
            for word, target in cmd.redirects:
                if not word.raw.startswith("<<<"):
                    continue
                op = len(word.raw) - len(word.raw.lstrip("<"))
                glued = [s for s in word.steps if s.start >= word.start + op]
                if glued and _steps_expand_nothing(glued):
                    cuts.append((glued[0].start, word.end))
                elif not glued and target is not None and _steps_expand_nothing(target.steps):
                    cuts.append((target.start, target.end))
        if cmd.separator == "(" or "`" in cmd.separator:
            depth += 1
        elif cmd.separator == ")":
            depth = max(0, depth - 1)
    for start, end in sorted(cuts, reverse=True):
        text = text[:start] + text[end:]
    return text


def _folded_scanner_reaches_self(text_lower: str) -> bool:
    """The fold scanner leg: fold ``\\``-newline continuations, descend into every executed
    shell-script payload, and judge each line with :func:`_is_ssh_to_self`. This runs
    ALONGSIDE the unedited :func:`_substitution_reaches_self` (origin/main's reading);
    ``_reaches_self_over_ssh`` refuses if either refuses, so this leg only ADDS refusals
    and can never lose one main's reading makes.

    Each body is judged one level at a time: a nested body that the static splice can
    never resolve (one holding ``$``, a backtick or a paren) is replaced by one word,
    and is judged on its own when its turn comes. So each level is read once, and the
    work grows linearly with the nesting depth.
    """
    # Prefilter, widened for the fold: short-circuit only a line that names NO ssh verb AND
    # carries no ``\``-newline continuation. A split verb (``s?``-``\``-newline-``h``) is
    # hidden on the unfolded line and inside the outer ``'...'``, so a plain verb prefilter
    # would short-circuit a line the fold-and-judge would refuse; admitting any line with a
    # continuation lets the split verb reach the fold, while a data-consumer argv with no
    # continuation and no ssh verb still short-circuits here -- so the descent is not run
    # per payload and the argv sweep stays linear.
    if not (_line_may_name_ssh_verb(text_lower) or "\\\n" in text_lower):
        return False
    seen: set[tuple[str, bool]] = set()
    # (text, judge it, allow join): the caller judged the line itself.
    work = [(text_lower, False, True)]
    while work:
        text, judge, allow_join = work.pop()
        # bash removes a ``\``-newline line continuation while reading a script, before it
        # tokenizes, so fold each frame the way bash runs it: a verb split across a
        # continuation (``s?\``-newline-``h``) is read whole, and a continuation is never
        # mistaken for a command separator. Continuations still inside single quotes are
        # left to the level that strips those quotes, and a quoted heredoc body is left
        # literal, as bash leaves it.
        text = _fold_outside_quoted_heredocs(text)
        live = any(op in text for op in ("$(", "`", "<(", ">(")) or bool(
            _PROCSUB_CONTINUATION_RE.search(text)
        )
        subs = _live_substitutions(text) if live else []
        view = text
        for start, end, body in reversed(subs):
            if any(c in body for c in "$`()"):
                view = view[:start] + "_" + view[end:]
        if judge and _is_ssh_to_self(view):
            return True
        # Descend into every executable shell-script payload the text carries (``bash -c``
        # / ``sh -c`` / ``eval`` / ``env -S`` / here-string), at any depth, and JUDGE each
        # as a command line of its own. A self-ssh on a later line of a ``bash -c`` script,
        # a line a ``;`` ``&`` ``|`` separator hides from the line read, or in a heredoc that
        # script's shell runs, is not visible on the line itself: the whole-line separator
        # mask turns the script's own separators into data, so the line read never sees the
        # second command. The payload, read whole with its separators live, does. A printed
        # payload (``echo bash -c '...'``) is judged too, but the line read already refuses a
        # printed self-ssh, so this adds no allowed case; and a heredoc body a shell runs is
        # read, which is the goal, while a data reader's body the shell only prints is the
        # floor's documented over-refusal, as on the line itself. A joined frame must not
        # re-join the same words into shrinking suffixes.
        joined_here: set[str] = set()
        # The literal cut applies to the top-level line only (``judge`` is False there): a
        # body reached by descent is always read as a script, since every by-descent cut
        # tried leaked a body a shell runs.
        nested = _nested_shell_payloads(
            _self_tokens(
                text if judge else _scanner_cut_nonshell_literals(text), preserve_structure=True
            ),
            allow_join=allow_join,
            joined_out=joined_here,
        )
        # Admit each distinct payload once, updating ``seen`` as it is admitted: a frame
        # can hand back the SAME body many times (``bash bash ... -c 'echo x x ...; ssh'``),
        # and queuing each copy would re-tokenize one body over and over -- quadratic work
        # for one verdict. The per-call memo already serves a repeat read, but not queuing
        # the repeat at all keeps the descent linear in the distinct bodies.
        fresh: list[str] = []
        for p in nested:
            if (p, True) not in seen:
                seen.add((p, True))
                fresh.append(p)
        work += [(p, True, p not in joined_here) for p in fresh]
        for _start, _end, body in subs:
            body = _without_quoted_heredoc_text(body)
            if (body, True) not in seen:
                seen.add((body, True))
                work.append((body, True, True))
    return False


def _substitution_reaches_self(text_lower: str) -> bool:
    """True if a substitution body that bash runs in *text_lower* reaches this host,
    judged as a command line of its own.

    Each body is judged one level at a time: a nested body that the static splice can
    never resolve (one holding ``$``, a backtick or a paren) is replaced by one word,
    and is judged on its own when its turn comes. So each level is read once, and the
    work grows linearly with the nesting depth.
    """
    if not _line_may_name_ssh_verb(text_lower):
        return False
    seen: set[tuple[str, bool]] = set()
    # (text, judge it, allow join): the caller judged the line itself.
    work = [(text_lower, False, True)]
    while work:
        text, judge, allow_join = work.pop()
        live = "$(" in text or "`" in text
        subs = _live_substitutions(text) if live else []
        view = text
        for start, end, body in reversed(subs):
            if any(c in body for c in "$`()"):
                view = view[:start] + "_" + view[end:]
        if judge and _is_ssh_to_self(view):
            return True
        if live:
            # A payload with no substitution opener holds no body; the line's own
            # frame walk already judges the payload itself.
            # A joined frame must not re-join the same words into shrinking suffixes.
            joined_here: set[str] = set()
            nested = _nested_shell_payloads(
                _self_tokens(text), allow_join=allow_join, joined_out=joined_here
            )
            fresh = [p for p in nested if (p, False) not in seen]
            seen.update((p, False) for p in fresh)
            work += [(p, False, p not in joined_here) for p in fresh]
        for _start, _end, body in subs:
            body = _without_quoted_heredoc_text(body)
            if (body, True) not in seen:
                seen.add((body, True))
                work.append((body, True, True))
    return False


def _reaches_self_over_ssh(text_lower: str, raw_text: "str | None" = None) -> bool:
    """:func:`_is_ssh_to_self` on *text_lower*, on each quoted assignment script in it,
    read whole (the separator mask would turn a script's own separators into data),
    and on each substitution body bash runs in it or in those scripts. Either the
    quote-aware reading or main's splice reading (*_MAIN_READING*) of the line refusing
    refuses; the bodies are judged once, in the quote-aware reading. *raw_text* is the
    command as submitted, for the wrapper option words of the line itself."""
    scripts = _quoted_assignment_scripts(text_lower)
    if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
        map(_is_ssh_to_self, scripts)
    ):
        return True
    # Belt by construction: origin/main's own substitution reading, left byte-for-byte
    # unedited (``git diff origin/main`` shows no hunk in _substitution_reaches_self), runs
    # as one leg; the folding, script-descending scanner runs as the other. Refuse
    # if EITHER refuses, so the fold can only ADD refusals and never lose one main makes.
    if _substitution_reaches_self(text_lower) or any(map(_substitution_reaches_self, scripts)):
        return True
    if _folded_scanner_reaches_self(text_lower) or any(map(_folded_scanner_reaches_self, scripts)):
        return True
    token = _MAIN_READING.set(True)
    try:
        return _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
            map(_is_ssh_to_self, scripts)
        )
    finally:
        _MAIN_READING.reset(token)
