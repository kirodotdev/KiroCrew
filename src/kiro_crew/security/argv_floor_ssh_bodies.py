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
    _HEREDOC_RESET,
    _LITERAL_HOSTS,
    _MAIN_READING,
    _is_ssh_to_self,
    _may_name_ssh_verb,
    _resolve_line_assignments,
    _splice_static_substitutions,
    _ssh_reading_depth,
)
from .call_views import _CALL_VIEWS, once_per_call, sharing_views
from .shell_commands import (
    SUBST,
    _heredoc_body_substitutions,
    _without_quoted_heredoc_text,
    read_commands,
)
from .shell_normalizer import (
    _backtick_closer,
    _fold_line_continuations,
    _iter_shell_chars,
    _matching_close_paren,
    _modes_may_diverge,
    _nested_shell_payloads,
    _opens_comment,
    _self_tokens,
    _shell_tokens,
)


@once_per_call(fresh=list)  # type: ignore[arg-type]  # keyed on (text, preserve_structure)
def _leg_tokens(key: "tuple[str, bool]") -> "list[str]":
    """:func:`_self_tokens` for the two legs, read once per gate call: the second leg reuses
    the first leg's tokens."""
    text, preserve = key
    return _self_tokens(text, preserve_structure=True) if preserve else _self_tokens(text)


@once_per_call()  # type: ignore[arg-type]  # keyed on (frame, view, *reading state)
def _leg_judge(key: "tuple") -> bool:
    """:func:`_is_ssh_to_self` on one frame's view for the two legs, judged once per gate
    call. Keyed on the frame's own text, not the view alone: two nesting levels can reduce
    to the same view, and each level is still judged once, so the cost per level is even."""
    return _is_ssh_to_self(key[1])


def _tokens(text: str, preserve: bool = False) -> "list[str]":
    preserve = preserve and _modes_may_diverge(text)  # quote-free text: one key, both legs
    return _leg_tokens((text, preserve))  # type: ignore[arg-type]


def _judged(frame: str, view: str) -> bool:
    # The key holds every reading state ``_is_ssh_to_self`` keys its own verdict on.
    reading = (
        _MAIN_READING.get(),
        _LITERAL_HOSTS.get(),
        _HEREDOC_RESET.get(),
        _ssh_reading_depth.get(),
    )
    return _leg_judge((frame, view, *reading))  # type: ignore[arg-type]


#: A ``<( )`` / ``>( )`` process-substitution opener whose ``(`` sits past a ``\``-newline
#: line continuation bash removes before parsing (``<``-``\``-newline-``(``). The adjacent
#: ``<(`` / ``>(`` spelling is caught by the literal checks in ``_substitution_reaches_self``.
_PROCSUB_CONTINUATION_RE = re.compile(r"[<>](?:\\\n)+\(")


@once_per_call(fresh=list)
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
        # A bare ``((`` is the arithmetic COMMAND, so it opens only in a command frame: in
        # ``"..."`` or ``${...}`` it is literal text, and opening a frame there would make
        # the closing ``"`` open a new string and hide every later comment and line.
        if text.startswith("$((", i) or (kind == "cmd" and text.startswith("((", i)):
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
                last_paren_closed_subst = True  # a closed `...` is part of a word
            else:
                stack.append(["cmd", "`", 0])
                last_paren_closed_subst = False
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
                        last_paren_closed_subst = True  # a closed $(( )) is part of a word
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
                # A ``(`` glued to ``<`` / ``>`` (process substitution) or to an extglob
                # operator (``@ + ? * !``) opens a word-forming group: its ``)`` closes a
                # word, so a ``#`` right after it is not a comment.
                prior = out[-1][-1] if out and out[-1] else ""
                stack.append(["cmd", ")", 0, prior in "<>@+?*!" and prior != ""])
                out.append("(")
                i += 1
                continue
            last = out[-1] if out else "\n"
            # A ``\``-escaped character (``\ ``, ``\;``, ``\|``, ``\#`` ...) is part of the
            # current word, so a ``#`` right after it does not begin a word and is not a
            # comment: the escape pair counts as a word character here.
            prev = "w" if len(last) == 2 and last[0] == "\\" else (last[-1] if last else "\n")
            # ``#`` opens a comment after a separator, after a word-ending ``)`` (a subshell
            # close or a ``case`` pattern) -- but NOT after a ``$(`` close, which is a glued
            # word. G1: bash discards a comment, so DROP it instead of copying it; copying
            # left a ``\``-newline inside the comment for the comment-unaware line judge to
            # fold into the next command, hiding a self-ssh on that line.
            comment_here = prev in ("\n", " ", "\t", ";", "&", "|", "(") or (
                prev in (")", "`") and not last_paren_closed_subst
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
    taking its message from stdin (``-F -`` / ``--file -`` / ``--file=-``), or a ``gh`` pr/issue
    post reading its body from stdin (``--body-file -``)."""
    raws = [w.raw for w in argv]
    if not raws:
        return False
    if raws[0] in _NONSHELL_CONSUMER_HEADS:
        return True
    # Only a plain ``git commit``: a global option before it (``-c alias.x=!...``) can make
    # Git run a shell alias that reads stdin, so those are never exempt.
    if raws[0] == "git" and raws[1:2] == ["commit"]:
        for i, a in enumerate(raws):
            if a == "--file=-" or (a in ("-f", "--file") and raws[i + 1 : i + 2] == ["-"]):
                return True
    # ``gh pr|issue comment|create|edit|review --body-file -`` posts stdin as text. Only these
    # built-in verbs: gh refuses an alias that shadows a built-in, so none of them runs stdin.
    gh = raws[1:]
    while (
        raws[0] == "gh"
        and gh[:1]
        and (gh[0] in ("-r", "-R", "--repo") or gh[0].startswith("--repo="))
    ):
        gh = gh[1:] if "=" in gh[0] else gh[2:]  # ``gh --repo o/r pr …`` names the repo first
    if raws[0] == "gh" and gh[:1] in (["pr"], ["issue"]):
        if gh[1:2] and gh[1] in ("comment", "create", "edit", "review"):
            for i, a in enumerate(raws):
                if a == "--body-file=-" or (
                    a in ("-f", "-F", "--body-file") and raws[i + 1 : i + 2] == ["-"]
                ):
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
    # A later command that runs a shell or a path can run what the consumer wrote
    # (``cat >/tmp/x <<'EOF' … EOF`` then ``bash /tmp/x``), so nothing on the list is cut.
    if _list_may_run_a_body(text, cmds, descended=False):
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


_SCHEDULE_DATA_READERS = _NONSHELL_CONSUMER_HEADS | {"crontab"}
#: ``zsh`` is NOT here: it sources ``~/.zshenv`` before ``-c`` on every start (even
#: non-interactive, non-login), where a function can rebind the consumer, so ``zsh -c`` is
#: never a plain wrapper. ``bash``/``sh``/``dash``/``ksh`` read no startup file for a plain
#: non-interactive ``-c``. ``ksh``/``sh`` do under some options, so ``_STARTUP_AMBIGUOUS_SHELLS``
#: below holds them to a bare ``-c`` (``BASH_ENV``/``ENV`` already set is the sandbox's).
_PLAIN_SHELLS = frozenset({"bash", "sh", "dash", "ksh"})
#: ``ksh``/``sh`` (``sh`` may BE ksh) read a startup file through options the case-blind
#: scanner cannot distinguish (ksh ``-E`` = read ``$ENV``/``~/.kshrc``, lower-cases to the
#: same ``e`` as the harmless ``-e``), so for these a plain wrapper is the exact bare ``-c``
#: with no other option word at all.
_STARTUP_AMBIGUOUS_SHELLS = frozenset({"ksh", "sh"})
_SSH_WORDS = ("ssh", "scp", "sftp", "rsync")
_PLAIN_WORD_RE = re.compile(r"[A-Za-z0-9_./:=,%+-]+")
_CRON_FIELD_RE = re.compile(
    r"(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?(?:,(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?)*"
)


def _body_names_an_ssh_word(body: str, *, crontab: bool = False) -> bool:
    """True unless every word of *body* is plain text that names no ssh-family program.

    Fail closed: a later shell can assemble a verb from almost any shell syntax (quotes,
    backslashes, globs, bracket classes, brace lists, extglob groups), so a word is plain
    only when it holds letters, digits and ``_ . / : = , % + -`` and nothing else, or is a
    crontab field (``*``, ``*/5``, ``1-5``) among the five leading fields of a full crontab
    record, when *crontab* says the body is fed to ``crontab``. Any
    other word keeps the body as a script."""
    for line in body.splitlines():
        words = line.split()
        # A ``*`` field is a schedule field only on a full crontab record: five leading
        # fields and a command after them. Anywhere else (``* localhost id`` fed to ``at``,
        # a short line) a bare glob is a word a later shell expands.
        record = crontab and len(words) > 5 and all(_CRON_FIELD_RE.fullmatch(w) for w in words[:5])
        for position, word in enumerate(words):
            if record and position < 5:
                continue
            if not _PLAIN_WORD_RE.fullmatch(word):
                return True
            if any(verb in word.lower() for verb in _SSH_WORDS):
                return True
    return False


#: Short flags a plain ``bash -c`` wrapper may carry before ``c``: every one is a run-mode
#: toggle that reads or runs nothing of its own. ``-o``/``-O``/``+O`` take one bare-word
#: argument; a long option here takes none. Anything outside this set (``-s``, ``--rcfile``,
#: a second operand) keeps today's verdict.
#: Short flags a plain wrapper may carry. ``E`` is OUT: it is errtrace in bash but the ``rc``
#: option in ksh93 (read ``$ENV`` / ``~/.kshrc``). None of the rest reads a startup file
#: (``p`` reads only the system ``/etc/suid_profile``).
_WRAPPER_SHORT_FLAGS = set("abefhkmnptuvxBCHPT")
#: Only ``-o`` takes an optname; ``-O``/``+O`` (bash shopt, incl. ``extdebug``) are never
#: plain, so an ``-O``/``+O`` wrapper keeps today's verdict.
_WRAPPER_OPT_WORD = {"-o"}
#: The ``set -o`` option names that read or run nothing of their own. Any other optname
#: (``rc`` included) keeps today's verdict: no bare-word optname is accepted.
_WRAPPER_SET_OPTS = frozenset(
    {
        "errexit",
        "nounset",
        "pipefail",
        "noglob",
        "noclobber",
        "xtrace",
        "verbose",
        "posix",
        "braceexpand",
    }
)
#: ``--login`` / ``-l`` / ``-i`` are NOT here: each makes bash read a startup file
#: (``~/.bash_profile``, ``~/.bashrc``) before ``-c``, where a function can rebind the
#: consumer (``cat`` / ``crontab``) to a runner, so such a wrapper is not plain.
_WRAPPER_LONG_FLAGS = {"--noprofile", "--norc", "--posix"}


def _plain_wrapper_script(rest: "list[str]") -> "str | None":
    """The single ``-c`` script operand of a plain shell wrapper, or ``None`` if *rest* (the
    words after the shell) is not exactly: option words from the allowlist, one ``-c`` (its
    own word or the last letter of a short cluster), and one operand. ``-o``/``-O``/``+O``
    consume one bare-word argument; a word outside the allowlist, a second operand, or a
    second ``-c`` returns ``None`` so the wrapper keeps today's verdict."""
    i = 0
    seen_c = False
    while i < len(rest):
        w = rest[i]
        if w in _WRAPPER_OPT_WORD:
            arg = rest[i + 1 : i + 2]
            if not arg or arg[0] not in _WRAPPER_SET_OPTS:  # only inert set -o options
                return None
            i += 2
            continue
        if w in _WRAPPER_LONG_FLAGS:
            i += 1
            continue
        if w.startswith("-") and len(w) > 1 and not w.startswith("--"):
            cluster = w[1:]
            step = 1
            if "c" in cluster:
                if cluster[-1] != "c" or seen_c:  # c must end the cluster; only one -c
                    return None
                cluster = cluster[:-1]
                seen_c = True
            elif cluster[-1] == "o":  # a trailing -o takes the next word as its optname
                arg = rest[i + 1 : i + 2]
                if not arg or arg[0] not in _WRAPPER_SET_OPTS:
                    return None
                cluster = cluster[:-1]
                step = 2
            if any(ch not in _WRAPPER_SHORT_FLAGS for ch in cluster):
                return None
            i += step
            continue
        break  # the first non-option word is the operand
    operand = rest[i:]
    if not seen_c or len(operand) != 1:
        return None
    return operand[0]


def _is_lone_plain_wrapper(text: str) -> bool:
    """True when *text* is ONE command, ``bash -c <script>`` (or another plain shell),
    with no pipe, redirect, background, group, expansion or second command: nothing on it
    can read or run what its script prints or writes. Any doubt is False."""
    try:
        cmds = [c for c in read_commands(text, heredocs=True, nested=False) if c.argv]
    except Exception:  # noqa: BLE001  # an unreadable frame is never proven plain
        return False
    if len(cmds) != 1:
        return False
    cmd = cmds[0]
    words = [w.raw for w in cmd.argv]
    head = words[0].rsplit("/", 1)[-1]
    if head not in _PLAIN_SHELLS or cmd.separator not in ("", "\n") or cmd.redirects:
        return False
    # ksh/sh read a startup file through options the case-blind scanner cannot tell apart
    # (ksh ``-E`` rc vs ``-e`` errexit both lower to ``e``), so only a bare ``-c`` is plain
    # for them; bash/dash keep the full option grammar.
    if head in _STARTUP_AMBIGUOUS_SHELLS:
        script = words[2] if words[1:2] == ["-c"] and len(words) == 3 else None
    else:
        script = _plain_wrapper_script(words[1:])
    if script is None:
        return False
    # The script word: single-quoted (expands nothing here) or double-quoted with no expansion.
    return script.startswith("'") or (
        script.startswith('"') and not any(ch in script for ch in "$`")
    )


def _is_inert_trailing_command(c, *, descended: bool) -> bool:
    """True for a command that runs nothing of its own: bare ``:`` or ``true`` (any frame),
    and bare ``exit`` / ``exit <decimal>`` only in a DESCENDED frame. ``exit`` is NOT inert
    at the top level: a login bash runs ``~/.bash_logout`` on ``exit``, so one top-level line
    could write that file and the trailing ``exit`` run it. In a descended frame a ``>``
    redirect already fails closed, so the write-then-exit shape cannot form there."""
    if c.redirects:
        return False
    words = [w.raw for w in c.argv]
    if words in ([":"], ["true"]):
        return True
    if not descended:
        return False
    if words == ["exit"]:
        return True
    return len(words) == 2 and words[0] == "exit" and words[1].isdigit()


def _list_may_run_a_body(text: str, cmds, *, descended: bool) -> bool:
    """True unless EVERY command on *cmds* is a bare data consumer (``cat``/``tee``/``echo``/
    ``printf``/``crontab``, or ``git commit -F -`` / ``gh … --body-file -`` reading stdin)
    with no runnable syntax anywhere on the list. The one rule both literal cuts apply: a
    body is data only when nothing on the whole list -- recognised or not -- can run what a
    consumer wrote. Any other command, a path/glob/expansion in command position, or a
    command substitution (``$(``, backtick, ``<(``/``>(``) anywhere -- in an argument, a
    redirect word or a redirect target, masked by the SUBST placeholder or not -- keeps the
    body. A consumer ARGUMENT merely NAMED like a runner (``echo bash``) is harmless: an
    argument is never run, so it does not keep the body."""
    if _live_substitutions(text):  # a substitution bash runs, wherever it sits on the list
        return True
    for c in cmds:
        words = list(c.argv) + [w for pair in c.redirects for w in pair if w is not None]
        if any(ch in w.raw for w in words for ch in ("$", "`", SUBST)) or any(
            s in w.raw for w in words for s in ("<(", ">(")
        ):
            return True
        if not c.argv:
            if c.redirects:  # a redirect-only command is still a command; fail closed
                return True
            continue  # the empty tail after the last separator
        head = c.argv[0].raw
        if head == "crontab" or _is_nonshell_consumer(c.argv):
            continue
        if _is_inert_trailing_command(c, descended=descended):  # runs nothing of the body
            continue
        return True
    return False


def _cut_descended_data_bodies(text: str) -> str:
    """In a DESCENDED frame, drop the here-document and single-quoted here-string bodies that
    the frame's first pipeline only feeds to data consumers. A body stays a script when any
    command of that pipeline may run a shell (``bash <<EOF``, ``cat <<'EOF' | bash``,
    ``env bash``, a ``$var`` program), or when the body itself expands (an unquoted body
    holding ``$`` or a backtick). Only the first pipeline is cut, because an earlier command
    can rebind a consumer's name (``alias``, ``hash -p``, a function); a group, compound
    command or ``<(`` / ``>(`` on the frame cuts nothing. Removing text the scanner would
    otherwise read can lower only refusals this PR adds, never one origin/main's reading
    makes."""
    if "<<" not in text or ">(" in text or "<(" in text:
        return text
    cmds = read_commands(text, heredocs=True, nested=False)
    for c in cmds:
        if c.argv and (c.argv[0].raw in _COMPOUND_HEADS or c.argv[0].raw.endswith("()")):
            return text
        if c.separator == "(" or "`" in c.separator:
            return text
    # Fail closed on anything that could run the body later: any command on the frame that
    # may run a shell or a path (``bash /tmp/x``, ``./x``, ``source x``), and any output
    # redirect (``> /tmp/x``, ``2>&1``, ``&>``) on the frame.
    if _list_may_run_a_body(text, cmds, descended=True):
        return text
    for c in cmds:
        if any(">" in word.raw for word, _target in c.redirects):
            return text
    first = []
    for c in cmds:
        first.append(c)
        if c.separator not in ("|", "|&"):
            break
    cuts: "list[tuple[int, int]]" = []
    for cmd in first:
        for doc in cmd.heredocs:
            body = text[doc.start : doc.end]
            # Schedule fields are read as such only when every reader of the body is ``crontab``
            # or a printer/writer (``cat``, ``tee``): never ``at`` or another unknown reader.
            crontab = all(
                c.argv and c.argv[0].raw in _SCHEDULE_DATA_READERS
                for c in first
                if c.argv or c.redirects
            )
            if _body_names_an_ssh_word(body, crontab=crontab):  # a printed self-ssh stays refused
                continue
            # A ``$`` or backtick keeps the body even under a quoted tag: the consumer can be a
            # scheduler (``crontab -``) whose shell expands it later.
            if not any(ch in body for ch in "$`"):
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
    for start, end in sorted(cuts, reverse=True):
        text = text[:start] + text[end:]
    return text


def _folded_scanner_reaches_self(text_lower: str) -> bool:
    """The fold scanner leg: fold ``\\``-newline continuations, descend into every executed
    shell-script payload, and judge each line with :func:`_is_ssh_to_self`. This runs
    ALONGSIDE :func:`_substitution_reaches_self` (origin/main's reading, sharing its reads);
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
    # (text, judge it, allow join, data cut allowed): the caller judged the line itself.
    work = [(text_lower, False, True, True)]
    while work:
        text, judge, allow_join, cut_ok = work.pop()
        # bash removes a ``\``-newline line continuation while reading a script, before it
        # tokenizes, so fold each frame the way bash runs it: a verb split across a
        # continuation (``s?\``-newline-``h``) is read whole, and a continuation is never
        # mistaken for a command separator. Continuations still inside single quotes are
        # left to the level that strips those quotes, and a quoted heredoc body is left
        # literal, as bash leaves it.
        text = _fold_outside_quoted_heredocs(text)
        # A frame's children may drop a data body only while every frame above them is one
        # plain ``bash -c`` with nothing piped, redirected or run beside it.
        child_cut_ok = cut_ok and _is_lone_plain_wrapper(text)
        if judge and cut_ok:  # a body only a data consumer reads, and nothing can run, is data
            text = _cut_descended_data_bodies(text)
        live = any(op in text for op in ("$(", "`", "<(", ">(")) or bool(
            _PROCSUB_CONTINUATION_RE.search(text)
        )
        subs = _live_substitutions(text) if live else []
        view = text
        for start, end, body in reversed(subs):
            if any(c in body for c in "$`()"):
                view = view[:start] + "_" + view[end:]
        if judge and _judged(text, view):
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
            _tokens(text if judge else _scanner_cut_nonshell_literals(text), preserve=True),
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
        work += [(p, True, p not in joined_here, child_cut_ok) for p in fresh]
        for _start, _end, body in subs:
            body = _without_quoted_heredoc_text(body)
            if (body, True) not in seen:
                seen.add((body, True))
                work.append((body, True, True, False))
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
        if judge and _judged(text, view):
            return True
        if live:
            # A payload with no substitution opener holds no body; the line's own
            # frame walk already judges the payload itself.
            # A joined frame must not re-join the same words into shrinking suffixes.
            joined_here: set[str] = set()
            nested = _nested_shell_payloads(
                _tokens(text), allow_join=allow_join, joined_out=joined_here
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


# A backslash before a path-name character: on Windows PowerShell runs the line, and
# there it separates path components (``C:\Windows\System32\OpenSSH\ssh.exe``,
# ``.\ssh.exe``) instead of escaping the next character as POSIX shell rules do.
_PATH_SEPARATOR_BACKSLASH_RE = re.compile(r"\\(?=[\w.?*\[-])")
# The word at a command start, past a PowerShell call operator ``&``. A quoted word is
# taken whole (it may hold blanks).
_PROGRAM_WORD_RE = re.compile(r"""[ \t]*(?:&[ \t]*)?('[^']*'|"[^"]*"|[^\s;&|()<>'"]+)""")
_COMMAND_SEPARATORS = frozenset(";&|\n(")


def _with_backslash_path_separators(text: str) -> str:
    """*text* with each backslash before a path-name character read as ``/``, in the
    word where each command starts.

    The tokenizer drops an unquoted backslash and keeps a quoted one, and the program
    basename splits on ``/`` only, so neither reading isolates ``ssh.exe`` at the end of
    a Windows path. A backslash before a quote, a blank, a separator or another
    backslash is left as written, so the quoting the tokenizer reads is unchanged. An
    argument keeps its POSIX reading, so a file operand (``git add tools\\ssh.exe``) is
    not read as a program. A command starts at the line's start or after an unquoted,
    unescaped ``; & | ( newline``, so a separator inside a quoted argument
    (``git commit -m 'a; C:\\x\\ssh.exe'``) starts nothing. One pass, linear in *text*.
    """
    out: list[str] = []
    i, n, quote, at_start = 0, len(text), "", True
    while i < n:
        if at_start:
            at_start = False
            match = _PROGRAM_WORD_RE.match(text, i)
            if match is not None:
                word = match.group(1)
                out.append(text[i : match.start(1)])
                out.append(_PATH_SEPARATOR_BACKSLASH_RE.sub("/", word))
                i = match.end()
                continue
        ch = text[i]
        if quote == '"' and ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if quote:
            quote = "" if ch == quote else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif ch in "'\"":
            quote = ch
        elif ch in _COMMAND_SEPARATORS:
            at_start = True
        out.append(ch)
        i += 1
    return "".join(out)


def _reaches_self_over_ssh(text_lower: str, raw_text: "str | None" = None) -> bool:
    """:func:`_is_ssh_to_self` on *text_lower*, on each quoted assignment script in it,
    read whole (the separator mask would turn a script's own separators into data),
    and on each substitution body bash runs in it or in those scripts. Either the
    quote-aware reading or main's splice reading (*_MAIN_READING*) of the line refusing
    refuses, and so does the line read with Windows path backslashes as ``/``
    (:func:`_with_backslash_path_separators`); the bodies are judged once, in the
    quote-aware reading. *raw_text* is the command as submitted, for the wrapper option
    words of the line itself."""
    if _CALL_VIEWS.get() is None:  # one memo for both legs, also outside a gate call
        return sharing_views(_reaches_self_over_ssh)(text_lower, raw_text)
    scripts = _quoted_assignment_scripts(text_lower)
    if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
        map(_is_ssh_to_self, scripts)
    ):
        return True
    # Belt by construction: origin/main's own substitution reading, unedited except that its
    # two reads go through the per-call reads both legs share (pinned), runs as one leg; the folding, script-descending scanner runs as the other. Refuse
    # if EITHER refuses, so the fold can only ADD refusals and never lose one main makes.
    if _substitution_reaches_self(text_lower) or any(map(_substitution_reaches_self, scripts)):
        return True
    # Extra pass, ADDED to the readings above: escape the quote an own-line body
    # leaves open so the text after the closing line reads unquoted, catching a
    # self-ssh a pairing quote would otherwise bury. Run only when a here-document
    # is present; the readings above and the main reading below keep a body's
    # quotes as origin/main read them, so this pass only adds a denial.
    if "<<" in text_lower:
        token = _HEREDOC_RESET.set(True)
        try:
            if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
                map(_is_ssh_to_self, scripts)
            ):
                return True
        finally:
            _HEREDOC_RESET.reset(token)
    if _folded_scanner_reaches_self(text_lower) or any(map(_folded_scanner_reaches_self, scripts)):
        return True
    token = _MAIN_READING.set(True)
    try:
        if _is_ssh_to_self(text_lower, own_line=True, raw_text=raw_text) or any(
            map(_is_ssh_to_self, scripts)
        ):
            return True
    finally:
        _MAIN_READING.reset(token)
    # shortcut: only a command's first word and only the line itself get the path reading,
    # not an argument (``cmd /c C:\\...\\ssh.exe``), a quoted script or a substitution body;
    # widen it if a Windows path there is reported.
    path_text = _with_backslash_path_separators(text_lower)
    if path_text == text_lower:
        return False
    path_raw = None if raw_text is None else _with_backslash_path_separators(raw_text)
    return _is_ssh_to_self(path_text, own_line=True, raw_text=path_raw)
