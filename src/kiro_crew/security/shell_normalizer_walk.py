"""Shell text reader, lexical layer: characters, quotes, words and structure.

The part of :mod:`kiro_crew.security.shell_normalizer` that reads a command line the
way a shell's lexer does, before any token is attributed to a program: the quote and
escape state machine (``_iter_shell_chars``, ``_shell_quote_walk``), line
continuations, the structural matches a substitution or a ``case`` arm needs
(closing parens and backticks, comments, command position), substitution bodies,
word splitting and dequoting, statement separators, the push-token reads, and the
word views that peel brackets, parameter defaults and empty substitutions off a
word.

Nothing here decides anything, and nothing here imports ``shell_normalizer``: the
attribution layer there reads this one, never the reverse.
``kiro_crew.security.shell_normalizer`` re-exports every name a caller imports from
it, so that import path holds. New lexical reading belongs here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator


#: Any one of these in a view means it is not a single plain command: it can
#: open a command, an expansion or a redirection, or chain to another command.
#: An ALLOWLIST of inert text, not a blocklist of opener spellings -- see
#: condition 2.  The separators are here for the PASS 1 view specifically: a
#: Pass 2 segment never contains one (the splitter consumed it), but the
#: whole-string view does, and an exception must not speak for a compound
#: command whose later stage is an interpreter
#: (``grep '<destructive>' payload.py | python``).
_SHELL_ACTIVE_CHARS = frozenset("$`(){}<>|;&\n\r")


# Splits a command into independently-evaluatable segments.
# Splits on every shell separator that can chain commands or carve out a
# subshell:
#   ;  - sequential
#   |  - pipe (single)
#   || - OR
#   && - AND
#   &  - background operator (when not part of `&&`)
#   $( - subshell open
#   )  - subshell close
#   `  - backtick subshell (open AND close)
#   \n - statement separator in scripts / heredoc bodies
# The alternation is ordered so the multi-character forms (`&&`, `||`) are
# tried before their single-character counterparts (`&`, `|`).  The
# negative lookahead on `&(?!&)` is defensive — it ensures a lone `&`
# doesn't accidentally consume the leading `&` of a literal `&&` if the
# regex engine chose this branch first under some future reordering.
# Literal whitespace is NOT a separator — flag values (e.g. `-C /path`)
# must stay attached to their flag token.
_CMD_SPLIT_RE = re.compile(r"[;\n`]|\|\|?|&&|&(?!&)|\$\(|\)")


# ``[k]irocrew`` -- a one-character bracket class expands to that character, so it names
# the protected program.  Collapsed before comparison rather than folded into every name
# pattern, so a single rule covers the idiom wherever it appears in the word.
_ONE_CHAR_CLASS_RE = re.compile(r"\[(\w)\]")


def _debracket(text: str) -> str:
    """Collapse one-character bracket classes (``[k]irocrew`` -> ``kirocrew``)."""
    return _ONE_CHAR_CLASS_RE.sub(r"\1", text)


# Characters a shell uses to WRAP a program name rather than to spell it: the
# quote marks and the parentheses of a command substitution.  Peeled to a fixed
# point in ``_program_basename`` so no interleaving with a redirect hides a name.
_SHELL_WRAPPER_CHARS = "`\"'()"


def _strip_redirect(token: str) -> str:
    """The token with any ATTACHED redirection suffix removed.

    ``shlex`` keeps a redirect glued to its neighbour as one token, so
    ``kirocrew>/tmp/out`` arrives as a single word and a program comparison against
    it fails.  bash splits the redirect off before exec, so the program is the part
    before the first ``>``/``<``; the same applies to an operand
    (``token>/tmp/out``).  A leading fd number (``2>``) leaves an empty program,
    which no comparison matches -- correct, since that word is not a program.
    """
    for op in (">", "<"):
        if op in token:
            token = token.split(op, 1)[0]
    return token


def _substitution_program(token: str) -> str:
    """The program a command-substitution body resolves to.

    A substitution in program position is a RESOLVER -- ``$(which pkill)``,
    ``$(command -v bash)``, ``$(type -p kirocrew)`` -- and the program it resolves to
    is the resolver's final argument.  ``shlex`` splits an UNQUOTED body on its
    own spaces, so ``$(which pkill)`` already arrives as two words; a QUOTED body
    (``"$(command -v pkill)"``) arrives as one multi-word token instead.  Taking the
    last word makes both spellings compare as the same program.
    """
    return token.rsplit(None, 1)[-1] if token.split() else token


# program in a run that ``shlex`` handed over as a single word.
_CONTROL_OPERATOR_RE = re.compile(r"[;&|\n]+")
# A word that OPENS with a command substitution, past any quote or paren the shell
# strips first: its basename reading is the substitution body's program
# (``$(kirocrew`` reads as ``kirocrew``), which is what runs.  A BRACE before it is
# not in the class: ``{$(date +%F),current}.log`` is a brace group whose first
# alternative is the body's OUTPUT -- a filename -- and the body itself
# (``date +%F``) is judged on its own by the payload walk, which descends into
# every substitution wherever it sits (``ls {$(pkill -f kirocrew),y}`` is denied
# for the body).
_LEADING_SUBSTITUTION_RE = re.compile(r"^[\"'(\s]*(?:\$\((?!\()|`)")


# same glue-evasion as the empty-quote form (``ca""t`` -> ``cat``) that
# ``normalize_shell_command`` already undoes, but spelled with a substitution and
# placed MID-WORD, where a prefix-only strip never sees it.
_EMPTY_SUBST_RE = re.compile(r"\$\(\s*\)|`\s*`|\$\{\s*\}")


# through the expansion, so neither the literal name nor the expansion alone looks
# dangerous.  The assignment and the use are in the SAME command text, so the
# literal can be substituted back before any comparison.
_LOCAL_ASSIGN_RE = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)


_VAR_USE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


_COMPUTED_VALUE_RE = re.compile(r"\$\(|`|\$\{[^}]*\}")


def _is_computed_value(value: str) -> bool:
    """True if an assignment's right-hand side is produced by a substitution."""
    return bool(_COMPUTED_VALUE_RE.search(value))


def _split_glued_operators(tokens: "list[str]") -> "list[str]":
    """Split tokens on control operators glued to their neighbours.

    ``shlex`` splits on whitespace only, so ``X=<name>;$X`` arrives as one token and an
    assignment glued to the command that uses it is invisible to both.  Splitting keeps
    the operator itself as a token so argv-boundary logic still sees it.
    """
    out: list[str] = []
    for token in tokens:
        # ONLY split a token that begins with an assignment.  Splitting any token
        # carrying a separator would destroy a QUOTED target -- ``shlex`` has already
        # removed the quotes, so ``pkill -f '[;]*<name>'`` arrives as the single token
        # ``[;]*<name>`` and is indistinguishable from a real separator at this point.
        # The reported evasion is specifically an assignment glued to its use, so that
        # is the only shape split here.
        if not _LOCAL_ASSIGN_RE.match(token) or not _CONTROL_OPERATOR_RE.search(token):
            out.append(token)
            continue
        for piece in _CONTROL_OPERATOR_RE.split(token):
            if piece:
                out.append(piece)
            out.append(";")
        if out and out[-1] == ";":
            out.pop()
    return out


# ``${VAR:-kirocrew}`` / ``${VAR:+kirocrew}`` / ``${VAR-kirocrew}`` carry a LITERAL
# program name that the shell substitutes in.  The literal is the program that can
# actually run, so it is what the comparison must see.
_PARAM_DEFAULT_RE = re.compile(r"\$\{[^{}:+=?-]*(?::?[-+=?])([^{}]*)\}")


def _resolve_param_defaults(token: str) -> str:
    """Replace ``${VAR:-literal}`` style expansions with their literal text.

    Only the LITERAL branch is resolved -- that is the spelling that hands the shell
    a runnable program name without the name appearing bare in the command.  A
    variable-only expansion (``$X``, ``${X}``) carries no literal and is left alone;
    that case is covered by the raw-text half of the union, not here.
    """
    previous = ""
    while token != previous:
        previous = token
        token = _PARAM_DEFAULT_RE.sub(lambda m: m.group(1), token)
    return token


# Variables that conventionally hold a shell (or the running script) path.  Piping
# into ``$SHELL`` runs the piped text exactly as piping into ``bash`` does, and the
# expansion hides the program name from any basename comparison.
_SHELL_VAR_NAMES = frozenset({"shell", "bash", "zsh", "ksh", "0", "bash_execution_string"})


_SHELL_VAR_RE = re.compile(r"\A\$\{?([A-Za-z_0-9]+)\}?\Z")


def _is_shell_variable_reference(token: str) -> bool:
    """True if *token* is a variable that conventionally expands to a shell."""
    m = _SHELL_VAR_RE.match(token.strip(_SHELL_WRAPPER_CHARS))
    if m is None:
        return False
    return m.group(1).lower() in _SHELL_VAR_NAMES


def _substitution_depth_delta(token: str) -> int:
    """Net change in command-substitution nesting contributed by *token*.

    Used so a separator INSIDE ``$( … )`` is not mistaken for the end of the argv
    being scanned -- ``<name> $(true; echo <verb>)`` is one command, not two.

    KNOWN LIMIT: this counts characters on tokens ``normalize_shell_command``
    has already stripped the quotes from, so a QUOTED paren or backtick is
    indistinguishable from a real one here and a window bounded by this delta
    under-runs on decoyed input.  The bare-``kill`` window recovers by
    re-deriving its bodies from the raw text, where the quotes still exist
    (:func:`_bare_kill_raw_bodies`).
    """
    return token.count("$(") + token.count("`") // 2 - token.count(")")


def _ends_argv(token: str) -> bool:
    """True if *token* ends the current command's argv.

    ``|`` and ``;`` always separate commands.  ``&`` only does so as a token of
    its own -- ``2>&1`` is a redirection, and a redirection does not end an argv
    (bash accepts one anywhere in a simple command).  A ``#`` comment ends the
    argv too: everything after it is prose, not arguments.

    A function-body opener (``x(){`` or a bare ``{``) is a boundary as well: the words
    after it are a NEW command, not arguments to the definition.  Without that,
    ``x(){ echo <name> <verb>;}`` attributes the body to ``x(){`` instead of to ``echo``,
    and the data-consumer exemption that makes ``echo`` inert never applies.
    """
    if token.startswith("#"):
        return True
    if token.rstrip("{") in {"", "("} or token.rstrip("{").endswith("()"):
        return True
    if token in {"&", "&&", "||", ";", ";;", "\n"}:
        return True
    return "|" in token or ";" in token


def _substitution_bodies(text: str) -> "list[str]":
    """The body of each command substitution in *text*.

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
    bodies: list[str] = []
    i = 0
    while i < len(text):
        # PROCESS substitutions run their body as a command just as a command
        # substitution does -- ``cat <(kirocrew token)`` executes the inner command and
        # feeds its output through a pipe.  Same paren-nesting walk.
        if text.startswith(("<(", ">(", "$("), i):
            end, proven = _matching_close_paren(text, i + 2)
            bodies.append(text[i + 2 : end - 1] if proven else text[i + 2 :])
            i = end
            continue
        if text[i] == "`":
            end, proven = _matching_close_backtick(text, i + 1)
            bodies.append(text[i + 1 : end - 1] if proven else text[i + 1 :])
            i = end
        else:
            i += 1
    return bodies


# A shell removes ``backslash + newline`` while lexing (line continuation), so
# ``kirocrew \<newline>restart`` runs ``kirocrew restart``. ``shlex`` instead keeps
# the escaped newline as a literal in the token, so the floor pre-joins it to
# model the shell before tokenizing. Scoped to the floor's own tokenizer input
# (NOT a catalog-wide rewrite of the matched text): it only shapes the argv the
# self-protection predicates see, so it cannot over-block an unrelated rule.
_SHELL_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _shell_join_continuations(text: str) -> str:
    """Collapse bash ``\\<newline>`` line continuations, as the shell does pre-lex."""
    return _SHELL_LINE_CONTINUATION_RE.sub("", text)


def _fold_line_continuations(text: str) -> str:
    """Remove ``\\<newline>`` exactly where a shell removes it -- quote-aware.

    A shell folds a backslash-newline while lexing, so ``"r\\<newline>m" -rf /``
    runs ``rm -rf /``.  The deny tiers match text and ``_split_segments`` cuts on
    the newline, so without folding the continuation is severed before any view is
    built and every rule authored as a command shape misses that spelling.

    ``_shell_join_continuations`` above looks like the answer and is NOT: it is a
    bare regex that folds inside SINGLE quotes too, and its comment scopes it
    deliberately to the self-protection floor's tokenizer input rather than to the
    matched text of the whole catalog.  Applying it here would fold
    ``echo 'r\\<newline>m -rf /'`` -- which bash prints literally -- into a denial.

    The contexts were measured against bash rather than assumed (``printf %q`` on
    the resulting argv):

    ======================  ==================  ========
    spelling                bash argv           folded?
    ======================  ==================  ========
    ``A\\<nl>A BB``          ``<AA><BB>``        yes
    ``"A\\<nl>A" BB``        ``<AA><BB>``        yes
    ``'A\\<nl>A' BB``        ``<A\\<nl>A><BB>``   no
    ``$'A\\<nl>A' BB``       ``<A\\<nl>A><BB>``   no
    ``A\\<cr><nl>A BB``      ``<A\\r>`` + new cmd  no
    ======================  ==================  ========

    So: fold unquoted and inside double quotes; preserve inside single quotes and
    inside ANSI-C (``$'…'``) spans.  ``$"…"`` follows the double-quote rule, which
    falls out of the scan because only ``$'`` opens a preserving span.

    Only a BARE newline ends a continuation.  A ``\\`` before ``\\r\\n`` escapes the
    CR into a literal carriage return and the LF then ends the command, so the two
    lines stay apart -- see :func:`_continuation_width`.

    Runs BEFORE the ANSI-C decode, which is the shell's own order: continuations
    are removed while lexing, and the escape body is interpreted after -- so a
    preserved ``\\<newline>`` inside ``$'…'`` stays part of that literal.

    An unterminated quote simply runs to the end in that state; the scan never
    raises, because it feeds the permission gate.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    # None = unquoted, "'" = single, '"' = double, "$'" = ANSI-C.
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is None:
            if text.startswith("$'", i):
                quote = "$'"
                out.append("$'")
                i += 2
                continue
            if ch in "'\"":
                quote = ch
                out.append(ch)
                i += 1
                continue
            if ch == "\\" and i + 1 < n:
                folded = _continuation_width(text, i)
                if folded:
                    i += folded
                    continue
                # The backslash escapes the next character, so that character
                # cannot open a quote -- consume the pair together.
                out.append(text[i : i + 2])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                folded = _continuation_width(text, i)
                if folded:
                    i += folded
                    continue
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                quote = None
            out.append(ch)
            i += 1
            continue
        if quote == "$'":
            # A backslash escapes the next character (including the closing quote),
            # and a continuation inside this span is LITERAL -- both are consumed
            # as a pair, which preserves them.
            if ch == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == "'":
                quote = None
            out.append(ch)
            i += 1
            continue
        # Single quotes: nothing is special, not even a backslash.
        if ch == "'":
            quote = None
        out.append(ch)
        i += 1
    return "".join(out)


def _continuation_width(text: str, i: int) -> int:
    """Characters to drop for a continuation at *i*, or 0 if there is none.

    ``text[i]`` is known to be a backslash.  Only a backslash directly followed
    by a bare newline is a line continuation.  A backslash before ``\\r\\n`` is
    NOT: bash reads the backslash as escaping the CR into a literal carriage
    return, and the LF then ends the command -- measured, ``echo a\\`` + CRLF +
    ``echo b`` prints ``a`` then ``b`` as two commands, not one.  Folding it
    would join the two lines and hide a second-line command (e.g. a credential
    mint) from the argv check while bash still runs it.
    """
    if text.startswith("\\\n", i):
        return 2
    return 0


def _redirect_consumes_next(token: str) -> "tuple[bool, bool]":
    """Classify *token* as a shell redirection sitting in argv position.

    Returns ``(is_redirect, expects_separate_target)``. A redirection is removed
    from argv by the shell and may appear ANYWHERE in a simple command, so it is
    never a CLI operand: ``kirocrew 2>/tmp/x restart`` and ``kirocrew > /tmp/x
    restart`` both run ``restart``, and the residue (the fd ``2``, or the target
    ``/tmp/x``) must not be mistaken for the leading subcommand.

    Quoting is already resolved by tokenization, so a remaining ``<``/``>`` is an
    operator. The target rides in the SAME token for ``2>/tmp/x`` / ``2>&1`` /
    ``>>/tmp/x`` (``expects_separate_target`` False); a bare ``>`` / ``2>`` / ``>&``
    takes the NEXT token as its target (True). A leading fd number is part of the
    operator, not an operand.
    """
    cut = min((token.find(c) for c in "<>" if c in token), default=-1)
    if cut == -1:
        return (False, False)
    return (True, token[cut:].lstrip("<>&") == "")


# ── Feature-branch push gate ──
# ``_is_git_publish`` only detects that a command IS a ``git push``.  The
# decision of whether to ALLOW it is made by ``_is_push_to_protected_branch``
# at the single enforcement point in ``is_denied``.  The push detector is a
# pure predicate (no side effects); the deny audit (``_emit_deny_event``) and
# the allow audit (``_schedule_push_allow_audit``) are emitted by the caller so
# the SEL trail always reflects the FINAL outcome (never an allow for a command
# that is ultimately denied by a later glob pattern).

# Protected branch names that ``git push`` must never target directly.  A push
# to any of these (or a bare push, which may resolve to one) is blocked so the
# change goes through the normal PR/code-review flow.  Kiro Crew (OSS) uses
# ``main``; the ``mainline`` and legacy default-branch names are covered for
# internal/mirror clones.
#: Shell metacharacters that can be GLUED to a word without whitespace, so they
#: appear inside a naive ``split()`` token while bash treats them as operators.
#: ``(git push origin main)&`` hands the ref token ``main)&``, and stripping only
#: the parens left ``main)&``, which never equalled ``main`` -- so a
#: protected-branch push was allowed AND audited as a feature-branch push. A git
#: ref cannot legally contain any of these, so stripping them cannot swallow a
#: real branch name; a QUOTED paren is still preserved because both call sites
#: strip before removing quotes.
_SHELL_OPERATOR_CHARS = "()&;|<>"


class _ShellChar(NamedTuple):
    """One step of the shell's quote/escape state machine."""

    #: Offset in the source text where this step's raw ``text`` begins. Named
    #: ``offset`` rather than ``index`` because a NamedTuple field cannot shadow
    #: ``tuple.index``.
    offset: int
    #: The raw text consumed -- TWO characters for a backslash escape pair, so a
    #: consumer that rebuilds a word by appending ``text`` preserves the spelling.
    text: str
    #: The SIGNIFICANT character: for an escape pair, the character escaped.
    char: str
    #: True only where shell SYNTAX lives: unquoted and unescaped. An operator, a
    #: separator or whitespace is structure here and data everywhere else.
    active: bool
    #: Quote state AFTER this step: 0 normal, 1 single-quoted, 2 double-quoted.
    state: int
    #: Whether an open single quote is an ANSI-C ``$'...'``, where a backslash
    #: escapes rather than standing for itself.
    ansi: bool
    #: The text ended on a backslash with nothing to escape -- so whatever split
    #: this text off was itself escaped, and the word continues past it.
    trailing_escape: bool


def _iter_shell_chars(text: str, state: int = 0, ansi: bool = False) -> "Iterator[_ShellChar]":
    """THE shell quote/escape state machine. Every push-path reading of shell
    quoting walks through this one generator.

    Four readings each keeping their own copy would not agree. A word splitter
    with no ANSI-C awareness where the boundary walk has it reads the ESCAPED
    quote in ``git push origin feature > >(echo $'a\\'b') main`` as a real
    closer, reopens on the next quote, and fuses the trailing ``main`` into one
    unterminated word -- the boundary walk then proves its parenthesis
    correctly, but the protected refspec is already trapped inside the word it
    was handed. That is the two-scanners defect this module is structurally
    cured of: one machine, several consumers, no second opinion to drift from.

    Bash's rules, once: a backslash escapes the next character outside quotes,
    inside double quotes, and inside ``$'...'``, but is LITERAL inside a plain
    single quote; an escaped quote is data and closes nothing. The ``$'``
    lookback is within-text, which is correct because whitespace cannot sit
    between the ``$`` and its quote, and it is EXACT: only a literal, unpaired
    ``$`` (an odd run -- ``$$`` is the PID parameter, ``\\$`` is data) opens
    ANSI-C. That exactness is load-bearing now that the segment split and the
    program anchor read this state in the allow direction: a walk that ends
    "still open" where bash closed hides a separator or a ``git`` word.

    *state* and *ansi* resume a walk, which is what lets a quoted word spanning
    whitespace be read without desyncing.
    """
    i = 0
    n = len(text)
    dollar_run = 0  # consecutive LITERAL ``$`` immediately before this char
    while i < n:
        ch = text[i]
        if ch == "\\" and (state != 1 or ansi):
            dollar_run = 0  # an escaped ``$`` is data and introduces nothing
            if i + 1 >= n:
                yield _ShellChar(i, ch, ch, False, state, ansi, True)
                return
            yield _ShellChar(i, text[i : i + 2], text[i + 1], False, state, ansi, False)
            i += 2
            continue
        was_unquoted = state == 0
        if state == 0:
            if ch == "'":
                state = 1
                # ``$'`` opens an ANSI-C string only when the ``$`` is itself
                # literal and unpaired: an escaped ``\$`` is data, and in a run
                # of dollars the shell pairs them off (``$$`` is the PID
                # parameter), so only an ODD run leaves a ``$`` to introduce the
                # quote. The plain ``text[i - 1] == "$"`` lookback read
                # ``\$'foo\'`` as ANSI-C, kept the quote open across a ``;``, and
                # hid the publish behind it -- with the segment split and the
                # program anchor now reading this state, a false "still open" is
                # an ALLOW-direction error, not a mere over-flag.
                ansi = dollar_run % 2 == 1
            elif ch == '"':
                state = 2
        elif state == 1:
            if ch == "'":
                state = 0
        elif ch == '"':
            state = 0
        dollar_run = dollar_run + 1 if was_unquoted and ch == "$" else 0
        yield _ShellChar(i, ch, ch, was_unquoted, state, ansi, False)
        i += 1


# Where one shell WORD ends and the next begins, for the reserved words and the
# comment marker the span walk has to recognise. Bash reads ``case`` / ``esac`` as
# reserved only when they stand alone, so ``lowercase)`` must not arm the pattern
# rule, and ``a#b`` must not open a comment.
_SHELL_WORD_BREAK = frozenset(" \t\n;&|()<>`")


def _skip_continuations(text: str, index: int) -> int:
    """*index* advanced past any backslash-newline pairs sitting at it.

    The shell removes a line continuation while READING, before it recognises
    words at all, so one may sit between any two characters of a reserved word.
    """
    while text.startswith("\\\n", index):
        index += 2
    return index


def _word_at(text: str, index: int, word: str) -> bool:
    """True if *word* stands alone at *index* rather than sitting inside a longer word.

    Continuation-aware, because the shell folds ``\\`` + newline away before it
    reads a word: ``ca\\`` + newline + ``se`` IS the reserved word ``case``, and
    bash was measured running it as one. Matching byte-literally missed that
    spelling, and an unrecognised ``case`` leaves the pattern's ``)`` to close the
    body early -- reopening the very truncation this rule exists to stop, for the
    cost of two characters (found in review).
    """
    j = index
    for expected in word:
        j = _skip_continuations(text, j)
        if j >= len(text) or text[j] != expected:
            return False
        j += 1
    after_index = _skip_continuations(text, j)
    before = text[index - 1] if index else " "
    after = text[after_index] if after_index < len(text) else " "
    return before in _SHELL_WORD_BREAK and after in _SHELL_WORD_BREAK


def _opens_comment(text: str, index: int) -> bool:
    """True if the ``#`` at *index* starts a COMMENT rather than sitting in a word.

    A ``#`` comments only at the start of a word, so ``a#b`` is one ordinary word.
    The preceding character is read the way the shell reads it, which means the
    folds are stepped OVER and then the character in front of them is tested --
    what matters is what the fold leaves adjacent, not that a fold is there:

    * ``a\\`` + newline + ``#b`` folds to the single word ``a#b`` -- no comment;
    * ``:`` + space + ``\\`` + newline + ``#`` folds to ``: #`` -- a real comment,
      because a word BREAK ends up in front of the ``#``.

    Treating any preceding fold as "not a comment" got the second case wrong in
    the fail-OPEN direction: the comment was missed, so the walk read the ``)`` it
    hides as the closer and truncated the body before the payload -- reopening the
    bypass this rule exists to close, for the folded spelling (found in review).

    Offset 0 counts as a word start, since the text handed here begins just inside
    the opener.
    """
    j = index
    while text.endswith("\\\n", 0, j):
        j -= 2
    return j == 0 or text[j - 1] in _SHELL_WORD_BREAK


def _in_command_position(text: str, index: int) -> bool:
    """True if a command could START at *index* -- the previous real character separates.

    Tells the reserved word ``esac`` from the ordinary string ``esac``, which
    a command may pass as an argument.

    A backslash-newline is a line CONTINUATION, not a separator: ``echo \\`` then a
    newline then ``esac`` is the single command ``echo esac``, so that ``esac`` is an
    argument and must not disarm the pattern rule. Parity matters -- ``\\\\`` then a
    newline is a literal backslash followed by a real newline, which does separate.
    """
    k = _prev_significant(text, index)
    return k < 0 or text[k] in ";&|(\n"


def _prev_significant(text: str, index: int) -> int:
    """Offset of the previous REAL character before *index*, or -1.

    Blanks are stepped over, and a backslash-newline pair is a line
    CONTINUATION the shell removes while reading, so it is stepped over too --
    but only an ODD run of backslashes folds; an even run leaves a literal
    backslash before a real newline, which separates.
    """
    k = index - 1
    while k >= 0:
        if text[k] in " \t":
            k -= 1
            continue
        if text[k] == "\n":
            slashes = 0
            while k - 1 - slashes >= 0 and text[k - 1 - slashes] == "\\":
                slashes += 1
            if slashes % 2 == 1:
                k -= slashes + 1
                continue
        break
    return k


#: Reserved words after which bash still reads the NEXT word in command
#: position.  ``case`` is a reserved word ONLY in command position, so
#: ``if true; then case x in ...`` must arm the pattern rule while
#: ``echo case`` must not -- and the hand-through is INHERITED: ``then`` only
#: passes command position when it stands in command position itself
#: (``echo then case ...`` is three arguments).  Block ENDERS (``fi``,
#: ``done``, ``}``, ``esac``) are deliberately absent: bash refuses a keyword
#: directly after them (``fi case ...`` is a syntax error, measured), so not
#: arming there is exact.  Cross-pinned against
#: ``argv_floor._SHELL_RESERVED_WORDS`` by test, so the two keyword tables
#: cannot drift apart silently.
_KEEPS_COMMAND_POSITION = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "time", "coproc"}
)


def _prev_word(text: str, index: int) -> "tuple[str, int] | None":
    """The FOLDED word ending just before *index* and its start offset, else None.

    None means the previous real character is a separator or absent -- the
    caller has already classified those through :func:`_prev_significant`.
    Word boundaries are :data:`_SHELL_WORD_BREAK`, the same set the forward
    walk reads, so the two directions cannot disagree about where a word ends.

    A backslash-newline pair INSIDE the word is a line continuation bash
    removes while reading, so ``th\\`` + newline + ``en`` is the one word
    ``then`` and is returned folded -- stopping at the raw newline would read
    the fragment ``en``, misclassify the keeper, and reopen the early-close
    under-scan on ``th\\<newline>en case x in x) ...``.  Only the innermost
    pair of an ODD backslash run folds; an even run is escaped literal
    backslashes before a REAL newline, which separates.  The start offset is
    the first fragment's, so chained walks resume before the whole word.
    """
    k = _prev_significant(text, index)
    if k < 0 or text[k] in _SHELL_WORD_BREAK:
        return None
    parts: "list[str]" = []
    seg_end = k + 1
    while k >= 0:
        ch = text[k]
        if ch == "\n":
            slashes = 0
            while k - 1 - slashes >= 0 and text[k - 1 - slashes] == "\\":
                slashes += 1
            if slashes % 2 == 1:
                # Fold the innermost ``\\<newline>`` pair away; any even
                # remainder stays as literal word characters.
                parts.append(text[k + 1 : seg_end])
                k -= 2
                seg_end = k + 1
                continue
            break
        if ch in _SHELL_WORD_BREAK:
            break
        k -= 1
    parts.append(text[k + 1 : seg_end])
    return ("".join(reversed(parts)), k + 1)


def _arms_case_context(text: str, index: int, in_case_body: bool = False) -> bool:
    """True if the standalone ``case`` at *index* can be bash's reserved word.

    bash recognises ``case`` only in command position, and every measured
    non-command position -- ``echo case``, ``v=1 case``, ``command case``,
    ``eval case``, a redirect-target prefix -- either treats it as data or
    refuses the line outright, so the pattern-paren rule must not arm there:
    arming spans the body past the ``)`` bash actually closes on, which is the
    over-scan (false-positive) direction on ordinary commands that merely say
    the word.  Command position is CHAINED, not spelling-matched: a reserved
    word hands it through only when it holds it itself, a ``function NAME`` /
    ``coproc NAME`` prefix passes it to the definition body, and a POSIX
    ``f()`` definition (empty parens, blanks allowed) restores it -- all forms
    bash was measured spanning.  Every ambiguity ARMS: an over-armed span only
    feeds the extractors more text, while a missed arm reopens the early-close
    truncation this rule exists to prevent.

    *in_case_body* is the caller's live case counter: inside an armed case, a
    ``)`` before this word is a PATTERN TERMINATOR and the word opens the
    clause body -- command position (``case a in a) case b in ...`` spans in
    bash, and missing that arm desynchronises the flat counter into a span
    SHORTER than the ungated walk: the inner ``esac`` eats the outer arm and
    the outer's next pattern paren closes the body).
    Outside a case, the same ``)`` is a substitution closer mid-arguments
    (``echo $(foo) case x in y`` -- data) or a subshell join bash refuses, so
    not arming there is exact and keeps the over-arm fix.
    """
    at = index
    for _ in range(8):
        if _in_command_position(text, at):
            return True
        k = _prev_significant(text, at)
        # _in_command_position returned False, so text[k] is a real char
        # outside ";&|(\n".
        ch = text[k]
        if ch == "`":
            return True  # a backtick opens a command substitution body
        if ch in "<>":
            # A redirect prefix before a compound command is a bash syntax
            # error (measured): the line never runs, so not arming is exact.
            return False
        if ch == ")":
            if in_case_body:
                return True  # the ``)`` is a pattern terminator -- clause body
            # ``f() case`` / ``f ( ) case``: an EMPTY paren pair after a word
            # is a function definition whose body is command position (both
            # spellings measured spanning).  A subshell needs a separator
            # before another command, so content between the parens means no.
            j = _prev_significant(text, k)
            if j >= 0 and text[j] == "(":
                return True
            return False
        prev = _prev_word(text, at)
        if prev is None:
            return False
        w, start = prev
        if "\\" in w:
            return True  # folded spelling -- undecidable cheaply, arm (long is safe)
        if w.startswith("-"):
            # An OPTION word is transparent: the decision rests on what
            # precedes it.  ``time -p case`` / ``time -- case`` then chain to
            # ``time`` (a keeper -- bash's grammar reads the reserved word
            # there, so the arm fails long on the substitution spelling its
            # own parser refuses), while ``echo -n case`` chains to ``echo``
            # and correctly stays data.
            at = start
            continue
        two_back = _prev_word(text, start)
        if two_back is not None and two_back[0] in ("function", "coproc"):
            # *w* is the definition/coproc NAME.  This check runs BEFORE
            # keeper semantics because bash accepts any reserved word as a
            # ``function`` name (``function do`` / ``function time`` parse,
            # measured) -- reading such a name as the keeper would chain to
            # ``function``, which keeps nothing, refuse the arm, and truncate
            # the substitution body at the pattern ``)`` bash spans past.
            at = two_back[1]
            continue
        if w in _KEEPS_COMMAND_POSITION:
            at = start  # inherited: the keeper must hold position itself
            continue
        return False
    return True  # chain too deep to decide -- arm, the long direction


def _matching_close_paren(text: str, open_end: int) -> "tuple[int, bool]":
    """``(index just past the matching ``)``, proven)`` for a paren opened before
    *open_end*, walked QUOTE-AWARELY through :func:`_iter_shell_chars`.

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

    ``proven`` is False when the parens never balance before the text ends. The
    caller must fail CLOSED on that: for an extractor the safe reading is the
    whole remainder (scan more, never less). All three call sites are extractors,
    so a span that reaches too far only feeds them text to inspect.
    """
    depth = 1
    cases = 0
    pos = 0
    state = 0
    ansi = False
    while pos <= len(text):
        jumped = False
        for step in _iter_shell_chars(text[pos:], state, ansi):
            off = pos + step.offset
            if off < open_end:
                continue
            if not step.active:
                continue
            ch = step.char
            # A ``#`` opens a comment only at the START of a word, and the
            # character before it is read as the shell reads it -- a folded
            # continuation leaves ``a\`` + newline + ``#b`` one ordinary word.
            if ch == "#" and _opens_comment(text, off):
                newline = text.find("\n", off)
                if newline == -1:
                    return (len(text), False)
                pos = newline + 1
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
                    return (off + len(step.text), True)
        if not jumped:
            break
    return (len(text), False)


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


def _resolved_word_view(word: str) -> str:
    """*word* with parameter defaults resolved, empty substitutions collapsed,
    then one-character bracket classes removed -- the composed per-word
    transform the bare-kill raw window searches each de-quoted word through.

    Construct-span rewrites only: a pattern-position word is an ERE whose own
    characters (``zz|kirocrew``, ``>kirocrew``) must never read as boundaries,
    which is why this is not ``_normalize_operand``.  A rewrite can DESTROY a
    de-quote-visible name (``${PATH/usr/|kirocrew|zz-}`` resolves to an empty
    default), so callers search the BARE word too -- this view is additive.
    """
    return _debracket(_EMPTY_SUBST_RE.sub("", _resolve_param_defaults(word)))


def _cut_at_operator(token: str) -> str:
    """*token* up to the first GLUED shell operator, with leading ones removed.

    ``strip`` is not enough: an operator can sit in the MIDDLE of a naive
    ``split()`` token, so ``(git push origin mainline)>log`` hands the ref token
    ``mainline)>log`` -- which ends in ``g``, so stripping removed nothing and the
    ref never equalled ``mainline``. bash parses that as the ref ``mainline``
    followed by the operator ``)`` and the redirection ``>log``, so cutting at the
    first operator is what reproduces its reading.

    Quote state is TRACKED rather than bailed on. Inside quotes these characters
    are literal and a ref may legitimately contain them -- ``git push origin
    '(main)'`` targets a branch actually named ``(main)``, which is not protected
    and must stay pushable. But a quoted ref can still carry an operator OUTSIDE
    its quotes: ``(git push origin 'main')`` hands the ref token ``'main')``,
    whose trailing ``)`` is unquoted. Returning early on the mere PRESENCE of a
    quote left that ``)`` in place, so the ref resolved to ``main)``, never
    equalled ``main``, and the protected push was allowed AND audited as a
    feature-branch push -- reopening the exact class this cut exists to close.
    Cutting only at operators outside quotes satisfies both readings at once.

    An UNBALANCED quote leaves the remainder read as quoted, so nothing is cut.
    That is safe because such a token is not executable as written: bash has an
    unterminated quote and never runs the push. If a later quote in the command
    balances it, the shell folds the span into one word whose ref likewise no
    longer equals a protected name.

    Quotes are PRESERVED in the result; both call sites remove them afterwards
    (see ``_dequote_token``), which is what keeps ``'(main)'`` a literal ref.

    Escapes are honoured through the shared walk, so ``ma\\)in`` keeps its
    literal paren instead of being cut at it -- bash hands git the ref ``ma)in``.
    """
    out: list[str] = []
    for step in _iter_shell_chars(token):
        if step.trailing_escape:
            out.append(step.text)
            break
        if step.active and step.char in _SHELL_OPERATOR_CHARS:
            if not out:
                # Leading operator, e.g. the ``(`` of ``(git push ...``: bash
                # treats it as punctuation before the word, so drop and continue.
                continue
            break
        out.append(step.text)
    return "".join(out)


class _ShellWalk(NamedTuple):
    """What one pass of the shell's quote/escape state machine observed."""

    #: The text split at its UNQUOTED ``<`` ``>`` ``&`` ``(`` ``)`` operators.
    pieces: list[str]
    #: True when at least one such operator was seen outside quotes.
    saw_operator: bool
    #: True when the text ends on a backslash that escapes the next character —
    #: i.e. whatever split this text off was itself escaped.
    trailing_escape: bool
    #: Quote state at the end: 0 normal, 1 single-quoted, 2 double-quoted. Feed
    #: it back in to resume the walk across a whitespace boundary.
    end_state: int
    #: Whether the still-open single quote was an ANSI-C ``$'...'``.
    end_ansi: bool
    #: Unquoted ``(`` minus unquoted ``)``. Quoted parens contribute NOTHING,
    #: which is what makes a process-substitution boundary provable.
    paren_delta: int


def _shell_quote_walk(text: str, state: int = 0, ansi: bool = False) -> _ShellWalk:
    """The ONE quote/escape state machine the push scan reads shell text with.

    Every signal the scan derives from shell quoting comes from this single pass,
    so no two readings of the same text can disagree: the operator split and the
    fragment signal (:func:`_push_token_shell_read`), and the unquoted paren
    depth that proves a process-substitution boundary
    (:func:`_git_push_args`). A second, quote-UNAWARE paren count is exactly how
    ``git push origin feature > >(echo '(' ) main`` published a protected branch:
    the quoted ``(`` inflated the depth, so the closing ``)`` never returned it
    to zero and the trailing ``main`` was swallowed into the substitution.

    Uses the shell's own rules, via :func:`_iter_shell_chars`, which owns the
    state machine: a backslash escapes the next character outside quotes, inside
    double quotes, and inside ``$'...'`` ANSI-C strings, but is LITERAL inside
    plain single quotes; an ESCAPED quote is data, not a delimiter.

    *state* and *ansi* resume a walk across a whitespace boundary, because a
    quoted word can span one (``>(echo 'a b')``).
    """
    pieces: list[str] = []
    buf: list[str] = []
    saw_operator = False
    trailing_escape = False
    paren_delta = 0
    for step in _iter_shell_chars(text, state, ansi):
        state, ansi = step.state, step.ansi
        if step.trailing_escape:
            trailing_escape = True
            break
        if step.active and step.char in "<>&()":
            saw_operator = True
            if step.char == "(":
                paren_delta += 1
            elif step.char == ")":
                paren_delta -= 1
            if buf:
                pieces.append("".join(buf))
                buf = []
            continue
        buf.append(step.text)
    if buf:
        pieces.append("".join(buf))
    return _ShellWalk(pieces, saw_operator, trailing_escape, state, ansi, paren_delta)


def _push_token_shell_read(token: str) -> "tuple[list[str] | None, bool]":
    """One quote/escape-state walk over a RAW (pre-dequote) token, returning
    ``(operator_pieces, open_state)``.

    ``operator_pieces`` — the token split at unquoted ``<`` ``>`` ``&``, or
    None when it carries none (the common case). A mid-word operator means
    the shell hands git a DIFFERENT word than this scan sees: ``main>log`` is
    the argument ``main`` plus the redirection ``>log``, i.e. it pushes main.
    The caller scans each piece as a refspec candidate so a protected name
    cannot hide behind operator glue; quoted operators are data and produce
    no split.

    ``open_state`` — True when the shell's quote/escape state has not
    RETURNED TO NORMAL by the token's end: an open quote or a trailing escape
    means the whitespace that split this token was itself quoted or escaped —
    the token is a FRAGMENT of a word fused across the split, the shape that
    let ``--push-option='ci skip'`` erase the floor tag. A complete word with
    escaped quotes therefore keeps its precise reading, both directions.

    A thin view over :func:`_shell_quote_walk`, which owns the state machine.
    ONE walk serves every signal (a review subtraction: the identical state
    machine briefly shipped twice); both consequences here are protective-only
    — a hit poisons the positional split, never widens an allow.
    """
    walk = _shell_quote_walk(token)
    return (
        walk.pieces if walk.saw_operator else None,
        walk.end_state != 0 or walk.trailing_escape,
    )


#: A token that BEGINS with a redirection: optional fd number, ``&``, or bash
#: NAMED descriptor ``{name}`` prefix, then ``<`` or ``>`` (doubled, or ``>|``
#: clobber, or ``>&``/``<&`` fd-dup). ``{name}>...`` is ALL redirection — read
#: as a word, the ``{name}`` becomes a phantom refspec and erases every tag.
#: ``<<-`` (the tab-stripping heredoc) folds its
#: ``-`` INTO the operator — left in the remainder it fakes a self-contained
#: token and the separated delimiter word becomes a phantom refspec
#: — while a ``-`` after an fd-dup (``>&-`` close, ``2>&1-`` move) is a
#: disposition the remainder correctly keeps. group(3) is whatever follows
#: the operator run — an ATTACHED target/fd makes the token self-contained;
#: an empty remainder means the shell takes the NEXT word as the
#: target/delimiter.
_PUSH_REDIRECTION_RE = re.compile(r"^([0-9]*|&|\{[A-Za-z_][A-Za-z0-9_]*\})(<<-|[<>][<>&|]*)(.*)$")


def _push_token_redirection(token: str) -> "tuple[bool, bool]":
    """(is_redirection, consumes_next_word) for a RAW token.

    Quotes and escapes are refused only where they could FOOL the grammar —
    the prefix/operator span. The redirection operator grammar itself admits
    no quote characters, so a quote can only ever sit in the TARGET group:
    ``>'log'`` is a plain redirection with a quoted target, and refusing the
    whole token for it pushes the shape into the fallback with the WRONG
    catalog identity. A token that is a fragment
    (open quote state / trailing escape) is still refused — the caller's walk
    poisons the split for those. The shell consumes a redirection before the
    program runs, so such a token is never an argv word — treating it as a
    positional is how ``git push origin </dev/null`` erases the single-arg
    tag.
    """
    m = _PUSH_REDIRECTION_RE.match(token)
    if m is None:
        return (False, False)
    if _push_token_shell_read(token)[1]:
        return (False, False)  # fragment: the walk handles it protectively
    return (True, m.group(3) == "")


def _push_option_matches(token: str, names: "frozenset[str]") -> bool:
    """True when ``token`` is ``--`` plus a PREFIX of any option in ``names``.

    Git resolves an unambiguous long-option prefix to that option, so ``--mirr``
    is ``--mirror`` and ``--rep=origin`` is ``--repo=origin``. Matching flag
    literals exactly therefore missed every abbreviation, and the consequence is
    not "an unrecognised flag" but a MIS-CLASSIFICATION: an unmatched flag is
    skipped, the positional read shifts, and the push is attributed to a different
    (individually disableable) rule than the one that covers it.

    Testing the prefix against only the options we care about is EQUIVALENT to
    resolving against git's full option list and then intersecting, because a
    non-dangerous option can only add a candidate, never remove a dangerous one.
    So there is no need to carry git's whole option table here — verified over
    every prefix of every ``git push`` long option, and pinned by
    ``test_the_prefix_test_matches_a_full_option_table``.

    An ambiguous abbreviation therefore reads as dangerous (``--a`` matches
    ``all``), which is free: git refuses an ambiguous abbreviation itself, so the
    command never runs, and denying it cannot lose a push that would have
    succeeded. A fully-spelled unrelated flag is unaffected — ``--atomic`` is not a
    prefix of any dangerous option.
    """
    if not token.startswith("--"):
        return False
    name = token[2:].split("=", 1)[0]
    return bool(name) and any(opt.startswith(name) for opt in names)


# TRUE shell command separators (NOT command-substitution boundaries). Serves to
# scan the PRE-SPLIT text for substitution glued into a push target — see
# ``_is_push_to_protected_branch``. Applied through
# ``_split_push_command_segments``, which honours quoting; the pattern itself is
# retained as the separator vocabulary.
_CMD_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")


#: The same separators as spellings, longest first so ``&&``/``||`` win over a
#: single ``|``. A LONE ``&`` is deliberately absent: it is not a segment
#: separator here, and the argument scan reads it as operator glue that poisons
#: the positional split.
_SHELL_SEGMENT_SEPARATORS = ("&&", "||", ";", "|")


def _split_push_command_segments(text: str) -> list[str]:
    """Split *text* into the shell's TRUE command segments, honouring quoting.

    A separator only separates where the SHELL reads one. Inside quotes, or
    escaped with a backslash, ``;`` / ``|`` / ``&&`` / ``||`` are ordinary
    characters in the word — ``git push origin 'feature|x'`` publishes a branch
    literally named ``feature|x`` — and splitting there truncated the word
    mid-quote. The fragment then arrived with the shell state still open, which
    ``_push_segment_targets_protected`` reads as a word fused across the
    boundary, so an ordinary unprotected refname was denied by the protective
    fallback (and by its ungated sentinel, which no catalog row can switch off).

    A NEWLINE always separates, escaped or not, and the backslash is KEPT in the
    segment it ends. That is not an inconsistency: a backslash-newline VANISHES
    in bash, fusing the words on either side into one that neither segment can
    reconstruct (``origin ma\\`` + newline + ``in`` publishes MAIN), and the
    retained trailing escape is exactly the signal the cumulative-open-state
    check ungates on. Every other escaped separator survives as a LITERAL
    character in the word, so the fused word necessarily contains it and can
    never equal a protected branch name.

    Distinct from :func:`_split_shell_segments`, which serves the ``cd``-tracking
    pass: that one wants FEWER segments (a wrong split corrupts the tracked
    directory), leaves pipes joined because they do not move the directory, and
    emits an unquoted paren as its own segment. This one needs a pipe to separate
    — each side is a command whose push must be judged on its own — and needs the
    parens left inside the word, because the argument scan reads them as the
    operator glue they are. They are not interchangeable.

    Quote state comes from :func:`_iter_shell_chars`, the module's one shell
    state machine, so this cannot disagree with the word split or the boundary
    walk about where a quote ends.
    """
    segments: list[str] = []
    rest = text
    while True:
        buf: list[str] = []
        resume_at: int | None = None
        for step in _iter_shell_chars(rest):
            if step.trailing_escape:
                buf.append(step.text)
                break
            if step.char == "\n":
                # A NEWLINE always separates. When it was ESCAPED the backslash
                # is kept, because that is the splice signal: bash makes the
                # backslash-newline vanish, fusing the words on either side into
                # one that neither segment can reconstruct.
                if step.text.startswith("\\"):
                    buf.append("\\")
                resume_at = step.offset + len(step.text)
                break
            if step.active:
                separator = next(
                    (s for s in _SHELL_SEGMENT_SEPARATORS if rest.startswith(s, step.offset)),
                    None,
                )
                if separator is not None:
                    resume_at = step.offset + len(separator)
                    break
            buf.append(step.text)
        segments.append("".join(buf))
        if resume_at is None:
            return segments
        rest = rest[resume_at:]


# Shell expansions that fuse text INTO a word, so the literal command hides the
# real push target. Any of these inside a git-publish command is unverifiable
# -> deny (fail closed):
#   - command substitution   $(...)   and backticks  `...`
#   - parameter expansion     ${...}
#   - PROCESS substitution   <(...) / >(...)  is NOT here, deliberately: the
#     shell substitutes a /dev/fd path WORD, so it is unverifiable only where it
#     SURVIVES as an argv word. Matching it on the whole segment also denied the
#     shape where the shell REMOVES it — ``git push origin my-feature >
#     >(tee log.txt)`` is an ordinary feature push whose output is teed — so the
#     word-position reading lives in ``_push_segment_targets_protected``, which
#     sees the tokens that survive redirection removal.
#   - BRACE expansion         {a,b} / {1..5}  -- bash expands ``ma{i,i}n`` to
#     ``main`` and ``{main,x}`` to ``main x`` BEFORE git sees the token, so a
#     brace group containing a comma or ``..`` must be treated as ambiguous.
_AMBIGUOUS_EXPANSION_RE = re.compile(r"\$\(|\$\{|`|\{[^{}]*(?:,|\.\.)[^{}]*\}")


#: Process substitution, which the shell replaces with a ``/dev/fd`` path WORD.
#: Read in a word position it is unverifiable — mis-reading it as a removable
#: redirection shifts a value option's consumption onto the remote and
#: downgrades a protected push to the disableable single-arg row.
#: The operator adjacency is required, so a parenthesis inside a
#: refname stays data; a QUOTED spelling still matches and over-denies, the same
#: fail-closed posture the expansion regex takes for a quoted ``$(``.
_PROCESS_SUBSTITUTION_OPENERS = ("<(", ">(")


def _dequote_token(token: str) -> str:
    """Collapse shell quoting/escaping to the literal the shell passes to git.

    bash merges adjacent quoted/unquoted fragments into ONE word, so
    ``ma"in"``, ``m''ain`` and ``ma\\in`` all reach git as the literal
    ``main``. ``str.strip`` removes only the OUTERMOST quotes, leaving interior
    quote/backslash characters that make the token compare unequal to a
    protected name — an evasion of this gate. Remove ALL single/double quotes
    and backslash escapes so the comparison sees the shell-resolved word.

    Shell OPERATORS glued to the word are NOT cut here. ``_cut_at_operator`` is
    applied where an operator would hide a PROGRAM name (the ``git`` anchor in
    ``_git_push_args``); for an ARGUMENT, cutting destroyed the very evidence the
    argument scan reads. ``_push_token_shell_read`` splits a token at its
    unquoted operators and ``_push_segment_targets_protected`` scans every piece
    as a refspec candidate, so ``mainline)>log`` still resolves to the protected
    ``mainline`` — while an uncut token keeps the shape the scan classifies by:
    ``@(main)`` is extglob pathname expansion, ``origin>/dev/null`` is a
    remote-only push, and a lone ``&`` is a command boundary. Cutting first
    reduced all three to bare words and erased their tags.
    """
    return token.replace("'", "").replace('"', "").replace("\\", "")


def _split_shell_words(segment: str) -> list[str]:
    """Split *segment* into words at UNQUOTED, unescaped whitespace.

    ``str.split`` splits inside quotes too, which tears one shell word into
    fragments the scan then reads as separate arguments. Two consequences, both
    seen in practice: a wrapper's quoted payload (``bash -c '(cd /tmp && git push
    origin my-feature)'``) yielded a bare ``git`` token, so the OUTER line — which
    is not itself a push — was parsed as one and its fragmented ref denied an
    ordinary feature push; and a legitimately quoted refname arrived with the
    shell state open, which the fragment rule reads as a word fused across the
    split. Splitting the way the shell does removes the class: a quoted payload is
    ONE word, so it is left to the nested-payload reading that judges it properly.

    Quotes and escapes are PRESERVED in the words; the callers strip them
    (``_dequote_token``) and walk them (``_push_token_shell_read``) themselves.

    Quote state comes from :func:`_iter_shell_chars`, so ANSI-C ``$'...'`` reads
    the same here as everywhere else. A private copy of the state machine WITHOUT
    that awareness is what lets ``git push origin feature > >(echo $'a\\'b') main``
    publish a protected branch: the escaped quote closes its state, the next quote
    reopens it, and the trailing ``main`` fuses into one unterminated word --
    which the boundary walk, reading the same text correctly, cannot
    rescue because the refspec is already inside the word it was handed.
    """
    words: list[str] = []
    buf: list[str] = []
    for step in _iter_shell_chars(segment):
        if step.trailing_escape:
            buf.append(step.text)  # trailing escape: the fragment signal
            break
        if step.active and step.char.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
            continue
        buf.append(step.text)
    if buf:
        words.append("".join(buf))
    return words


# `NAME=value` prefix. `normalize_shell_command` keeps it as a single token, and
# the value is already $HOME-expanded by the time we see it.
#: ``NAME=value`` and ``NAME+=value``. The append form is a separate group so a
#: caller can add to what it already recorded instead of replacing it. Matching
#: only ``=`` means the whole ``NAME+=`` token fails to match, so the segment
#: reads as a command word rather than an assignment.
_SHELL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.DOTALL)


# Regex to strip empty-string concatenation: paired quotes ('' or "") that
# vanish (e.g. g""it -> git, ca''t -> cat).
_EMPTY_QUOTE_RE = re.compile(r'""|\'\'')

# Regex for $HOME or ${HOME} variable expansion.
_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME", re.IGNORECASE)
