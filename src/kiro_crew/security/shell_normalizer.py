"""Shell text reader: argv attribution, the payload walk and the normalized token view.

Every tier that judges a command line reads it through this module. Nothing here
decides anything: it reconstructs what a shell would see -- splitting a line into
statements and words, tracking quote and escape state, peeling redirections and
substitutions off a word, attributing a token to the program that receives it,
decoding the escape forms a printer would emit, and reading the expansion shapes
whose value is unknowable without running the line.

That division is deliberate and load-bearing. A reader that under-approximates
blinds every tier at once, and no OS sandbox sees shell text, so this layer
over-approximates on purpose and the tiers above it are where precision belongs.
Narrowing a matcher means narrowing the site that DECIDES, never this one.

The reader is four modules below this one, and this one keeps attribution -- which
program a token reaches, nested shell payloads, local assignments and function
aliases, the data-consumer exemption, the glob reading of a program name and the
normalized token view. ``shell_normalizer_walk`` reads characters, quotes, words and
structure; ``shell_normalizer_redirects`` reads the carriers that bring program text
outside argv; ``shell_normalizer_escapes`` decodes escapes; ``shell_normalizer_programs``
holds the program vocabulary. Every name a caller imports from this module still
resolves here, and a name a test patches lives in the same module as every reader of
it.

Imports only ``vocabulary``, the layer below, for the product's own name, and its own
four owners, which never import it: the tiers depend on the reader, never the reverse.
"""

from __future__ import annotations

import bisect
import os
import re
import shlex
from typing import TYPE_CHECKING

from kiro_crew.trust_patterns import ENV_ASSIGNMENT_RE

from .shell_normalizer_escapes import (  # noqa: F401  (also re-exported for importers)
    _ANSI_C_QUOTE_RE,
    _decode_ansi_c_body,
    _decode_printf_escapes,
)
from .shell_normalizer_programs import (  # noqa: F401  (also re-exported for importers)
    _DATA_CONSUMER_PROGRAMS,
    _ENV_SPLIT_PROGRAMS,
    _NESTED_SHELL_PROGRAMS,
    _NESTED_SHELL_VERBS,
    _PYTHON_INLINE_PROGRAM_FLAGS,
    _PYTHON_OPERAND_FLAGS,
    _PYTHON_PROGRAM_RE,
)
from .shell_normalizer_redirects import (  # noqa: F401  (also re-exported for importers)
    _ARRAY_EXPAND_RE,
    _OUTPUT_REDIRECT_RE,
    _REDIRECT_START_RE,
    _SHELL_COMMAND_FLAG_RE,
    _array_assignments,
    _glued_shell_command_payload,
    _here_string_payload,
    _heredoc_marker,
    _is_env_split_flag,
    _operand_span_end,
    _redirect_glue_point,
    _sed_exec_replacement,
    _shell_c_carrier_glued,
    _shell_c_carrier_payloads,
    _stdin_program_text,
    _xargs_here_string_rebuild,
)
from .shell_normalizer_walk import (  # noqa: F401  (also re-exported for importers)
    _AMBIGUOUS_EXPANSION_RE,
    _CMD_SPLIT_RE,
    _CONTROL_OPERATOR_RE,
    _EMPTY_QUOTE_RE,
    _EMPTY_SUBST_RE,
    _HOME_VAR_RE,
    _KEEPS_COMMAND_POSITION,
    _LEADING_SUBSTITUTION_RE,
    _LOCAL_ASSIGN_RE,
    _PROCESS_SUBSTITUTION_OPENERS,
    _SHELL_ACTIVE_CHARS,
    _SHELL_ASSIGN_RE,
    _SHELL_WRAPPER_CHARS,
    _VAR_USE_RE,
    _arms_case_context,
    _backtick_closer,
    _cut_at_operator,
    _debracket,
    _dequote_token,
    _ends_argv,
    _fold_line_continuations,
    _is_computed_value,
    _is_shell_variable_reference,
    _iter_shell_chars,
    _matching_close_paren,
    _opens_comment,
    _prev_word,
    _push_option_matches,
    _push_token_redirection,
    _push_token_shell_read,
    _redirect_consumes_next,
    _resolve_param_defaults,
    _resolved_word_view,
    _shell_join_continuations,
    _shell_quote_walk,
    _split_glued_operators,
    _split_push_command_segments,
    _split_shell_words,
    _strip_redirect,
    _substitution_bodies,
    _substitution_depth_delta,
    _substitution_program,
)
from .vocabulary import (
    _KILL_BY_NAME_PROGRAMS,
    _SELF_NAME_RE,
    _SELF_PROGRAM_RE,
    _SELF_PROGRAM_SPELLINGS,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# Shell glob metacharacters, and the concrete spellings a glob could expand to.  A
# glob in the program name (``kiro[c]rew``) is resolved by the shell BEFORE exec, so
# it has to be tested for expandability rather than compared literally.
_GLOB_CHARS_RE = re.compile(r"[\[\]?*{}]")


def _program_basename(token: str) -> str:
    """The program name a token invokes, with shell wrappers stripped.

    Strips quoting, command-substitution wrappers and any attached redirection
    before taking the basename, so an expansion-produced program name
    (``$(which pkill)``, ``"$(command -v bash)"``) or a redirect-glued one
    (``kirocrew>/tmp/out``) is compared as the program it resolves to rather than as
    literal punctuation.  Every program check goes through this -- comparing a raw
    ``os.path.basename`` lets ``$(which pkill) -f <name>`` past the kill rule.

    The layers are peeled to a FIXED POINT rather than once in a fixed order.
    A wrapper and a redirect interleave freely, and any single ordering leaves a
    hole for the interleavings it does not match: ``$(which kirocrew)>/tmp/out`` needs
    the redirect gone before its closing paren reaches the end of the word, while
    ``kirocrew)`` needs the paren gone with no redirect in play at all.  Looping until
    nothing changes makes the peel order-independent, which closes the class
    instead of whichever spelling a fixed order happened to cover.
    """
    if not token:
        return ""
    previous = ""
    substituted = False
    while token != previous:
        previous = token
        token = _strip_redirect(token)
        token = _resolve_param_defaults(token)
        token = _EMPTY_SUBST_RE.sub("", token)
        if token.startswith("$(") or token.startswith("`"):
            substituted = True
        token = token.removeprefix("$(").strip(_SHELL_WRAPPER_CHARS)
        # ANSI-C / locale quoting: ``$'name'`` and ``$"name"`` are just quoting
        # forms, so the ``$`` left behind after the quotes come off is not part of
        # the program name.  ``$(`` and ``${`` are handled above and below.
        if token.startswith("$") and not token.startswith(("$(", "${")):
            token = token[1:]
    if substituted:
        token = _substitution_program(token)
    # A control operator GLUED to the name (``true;kirocrew``, ``x&&kirocrew``)
    # means the program that actually runs is what follows the LAST operator --
    # ``shlex`` splits on whitespace only, so it hands the whole run over as one
    # word and a comparison against it matches nothing.  Taking the trailing
    # segment is what bash does; a trailing operator leaves an empty tail, so the
    # last NON-EMPTY segment is the one that names a program.
    segments = [s for s in _CONTROL_OPERATOR_RE.split(token) if s]
    if segments:
        token = segments[-1]
    return os.path.basename(token.rstrip("/"))


def _glob_could_expand_to(base: str, names: "tuple[str, ...] | frozenset[str]") -> bool:
    """True if *base* carries a shell glob that could expand to one of *names*.

    A glob in the program name is resolved by the shell BEFORE exec, so it has to be
    tested for expandability rather than compared literally.  ``[...]`` and ``?`` stand
    for one character and ``*`` for any run, so only a pattern that CAN name the target
    counts -- ``kiro[x]few`` still does not.
    """
    if not _GLOB_CHARS_RE.search(base):
        return False
    try:
        expandable = re.compile(_glob_to_regex(base), re.IGNORECASE)
    except re.error:
        return False
    return any(expandable.fullmatch(name) for name in names)


#: How deep a nested brace group is read before it is treated as "anything".  Bash
#: itself nests without limit, but a glob-shaped word is never legitimately more than
#: a few groups deep, and the translation below recurses once per level -- so a word
#: built to be hundreds of levels deep would otherwise exhaust the interpreter stack
#: and crash the gate instead of answering.  Past the cap the group reads as ``.*``,
#: the fail-closed direction (it can only over-match a protected name, never miss one).
_BRACE_NESTING_CAP = 8

#: How many brace ALTERNATION groups one word may translate before the rest read as
#: "anything".  Each ``{a,b}`` becomes a regex alternation whose branches are themselves
#: globs, and a run of them (``{*,*}{*,*}...``) is a regex with 2^N ways to match a short
#: name -- ``re`` explores every one before it can say no, so fifteen groups cost seconds
#: per name and stall the synchronous gate.  A legitimate program word has one or two.
#: Past the budget a group reads as ``.*``, the fail-closed direction, exactly as the
#: nesting cap does.
_BRACE_GROUP_BUDGET = 6

#: A brace SEQUENCE body: ``1..5``, ``a..z``, ``c..c``, optionally ``..<step>``.
_BRACE_SEQUENCE_RE = re.compile(
    r"\A(?:(-?[0-9]+)\.\.(-?[0-9]+)|([a-z])\.\.([a-z]))(?:\.\.-?[0-9]+)?\Z"
)


def _brace_pairs(pattern: str) -> "dict[int, int]":
    """Every ``{`` index in *pattern* mapped to the index of its closing ``}``.

    One linear pass with a stack; an unmatched ``{`` is simply absent and reads as a
    literal character.  This replaces a scan-to-the-end lookup per ``{``: with that
    lookup a word of N unbalanced braces cost O(N^2), and a 12,000-brace program word
    stalled the synchronous permission gate for tens of seconds -- long enough for the
    loop watchdog to hard-exit the gateway.  A pair table is the same answer in O(N).
    """
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for j, ch in enumerate(pattern):
        if ch == "{":
            stack.append(j)
        elif ch == "}" and stack:
            pairs[stack.pop()] = j
    return pairs


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


def _brace_group_regex(body: str, depth: int = 0, budget: "list[int] | None" = None) -> str:
    """The regex for what bash's brace expansion makes of ``{<body>}``.

    Bash expands exactly three shapes, and this mirrors them rather than reading
    every brace group as "anything" -- that reading turned every quoted jq filter
    (``--jq '{state}'``), awk program (``'{print $1}'``) and format literal
    (``{directory}``) into a word that could be a kill-by-name program or the
    product CLI, and a later product-named argument in the same argv then denied
    the whole command:

    * ``{a,b,c}`` -- one word per alternative, each itself a glob.
    * ``{x..y}`` -- a sequence: ``{c..c}`` is ``c``, ``{a..f}`` is one letter of
      the range, ``{1..9}`` is a run of digits (no protected name carries digits,
      so ``-?[0-9]+`` cannot over-match one; it is written out rather than left
      as ``.*`` so a future digit-bearing name is matched honestly).
    * anything else (no top-level comma, not a sequence) is NOT expanded at all:
      ``{state}`` reaches the program as the literal characters ``{state}``,
      braces included, and only an inner glob character still globs.

    *depth* counts enclosing groups; at ``_BRACE_NESTING_CAP`` the group is ``.*``
    rather than recursed into (see the cap's note).  *budget* is the word's remaining
    ``_BRACE_GROUP_BUDGET`` of alternation groups, shared across the whole translation;
    an alternation past it is ``.*`` too.
    """
    if depth >= _BRACE_NESTING_CAP:
        return ".*"
    parts = _split_brace_alternatives(body)
    if len(parts) > 1:
        if budget is None:
            budget = [_BRACE_GROUP_BUDGET]
        # Identical alternatives (``{*,*}``) are one alternative: bash produces the same
        # word twice, and a regex branch pair with identical branches is only a doubling
        # of the ways to fail.  Deduplicated BEFORE the budget is charged, so the pathological
        # spelling neither costs a group nor reaches the engine as a pair.
        branches = list(dict.fromkeys(_glob_to_regex(part, depth + 1, budget) for part in parts))
        if len(branches) == 1:
            return branches[0]
        if budget[0] <= 0:
            return ".*"
        budget[0] -= 1
        return "(?:" + "|".join(branches) + ")"
    seq = _BRACE_SEQUENCE_RE.match(body)
    if seq is not None:
        if seq.group(1) is not None:
            return "-?[0-9]+"
        lo, hi = sorted((seq.group(3), seq.group(4)))
        return f"[{lo}-{hi}]"
    return r"\{" + _glob_to_regex(body, depth + 1, budget) + r"\}"


def _glob_to_regex(pattern: str, depth: int = 0, budget: "list[int] | None" = None) -> str:
    """Translate a shell glob into a regex that matches what it could expand to.

    *depth* is the brace-nesting level this pattern sits at (see ``_BRACE_NESTING_CAP``),
    *budget* the word's remaining alternation groups (see ``_BRACE_GROUP_BUDGET``; a
    fresh translation starts a full budget).  Brace pairs are resolved once per pattern
    (``_brace_pairs``), so the translation is linear in the pattern whatever the braces
    do, and the regex it produces is bounded in how much backtracking it can demand.

    Only GLOB and BRACE syntax is read; everything else is literal.  A COMMAND
    substitution (``$(...)``, a backtick) is what the command it runs prints, which no
    static reading knows -- and this reader does not guess.  The substitution's BODY is
    judged on its own by the payload walk, which descends into every substitution
    wherever it sits (``tar czf x_$(pkill -f kirocrew).tgz`` is denied for the body);
    what it PRINTS is outside this floor, exactly as it is at program position, where
    ``_program_basename`` peels a leading substitution and ``$(printf pk)ill -f <name>``
    is the documented residual.  Reading the substitution as ``.*`` instead made every
    host-stamped filename (``logs_$(hostname)_*.tar.gz``) and every brace group with a
    substituted alternative (``{$(date +%F),current}.log``) under an ordinary program a
    word that "could be ``pkill``", and a product-named path later in the argv a
    self-kill -- three rounds of scope-review regressions, no kill among them.
    PARAMETER expansion (``${X}``, ``$X``) is literal for the same reason: reading it as
    anything turned every awk field list (``{print $6,$7}``) and every prose heredoc
    naming ``${name}`` into a possible verb.
    """
    out: list[str] = []
    pairs: "dict[int, int] | None" = None
    if budget is None:
        budget = [_BRACE_GROUP_BUDGET]

    def emit(piece: str) -> None:
        # ``.*.*`` is ``.*``: a run of "anything" pieces (``**``, a budgeted group after a
        # ``*``) is one piece, so the engine has one segment to place rather than a
        # polynomial number of ways to split a name across several.
        if piece == ".*" and out and out[-1] == ".*":
            return
        out.append(piece)

    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                emit(re.escape(ch))
                i += 1
                continue
            emit(".")
            i = close + 1
            continue
        if ch == "{":
            # ``kiro{c..c}rew`` and ``p{k,k}ill`` expand to the real name, so a brace
            # group stands for what bash's brace expansion can produce from it --
            # and only that (see ``_brace_group_regex``).
            if pairs is None:
                pairs = _brace_pairs(pattern)
            close = pairs.get(i, -1)
            if close == -1:
                emit(re.escape(ch))
                i += 1
                continue
            emit(_brace_group_regex(pattern[i + 1 : close], depth, budget))
            i = close + 1
            continue
        if ch == "?":
            emit(".")
        elif ch == "*":
            emit(".*")
        else:
            emit(re.escape(ch))
        i += 1
    return "".join(out)


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
            _program_basename(token) in _NESTED_SHELL_PROGRAMS
            or _program_basename(token) in _NESTED_SHELL_VERBS
            or _program_basename(token) == "xargs"
            or _is_shell_variable_reference(token)
        ):
            return True
    return False


# Constructs by which a text-processing tool RUNS a command rather than printing it:
# ``awk``'s ``system()`` and pipe-to-command, and GNU ``sed``'s ``e`` command.  The
# terminal ``e`` is a word of the script wherever a boundary precedes it (``e``, ``p;e``,
# ``--expression=e``, and a custom-delimiter address such as ``\-.-e`` or ``\- -e`` whose
# closing delimiter is ``-``).  The ``(?<!^-)`` guard excludes only the bare two-character
# option token ``-e``/``-E``, whose ``-`` is the token's first character: no consumer runs
# that option.
_SCRIPT_EXECUTES_RE = re.compile(
    r"system\s*\(|\|\s*[\"']|\|&|print\s*\||\bclose\s*\(|/e\b|(?<!^-)\be\s*$"
)


def _python_reads_stdin(later_tokens: list[str]) -> bool:
    """True if this ``python`` invocation runs its PROGRAM from stdin (a script/module does not).

    CPython reads its program from stdin for a bare interpreter (no positional) or an explicit
    ``-`` argument; ``-c CODE``, ``-m MOD``, and ``FILE`` all supply the program elsewhere.
    Walks the argument stream the way ``_is_self_module_invocation`` does so the corner cases
    line up: an operand-taking flag consumes its value (``-X dev`` — ``dev`` is not a script),
    a heredoc (the ``<<TAG`` marker, its BODY and the closing tag) is not an argument, and a
    pipe/redirect token ends this command's own arguments.

    The heredoc structure is read off the RAW token via :func:`_heredoc_marker`, because
    ``_normalize_operand`` strips a redirection to the empty string — which would leave the
    heredoc branch here unreachable and have ``python << 'PY' … PY`` (no ``-``) report FALSE,
    reading the first word of the BODY as a script path.  A redirect OPERAND is consumed
    through :func:`_operand_span_end` for the same reason the carrier scan uses it: a
    substitution operand is one shell WORD over several tokens, and skipping only the first
    leaves ``python <<< $(printf …)`` reading ``%s`` as a script path.  The two
    functions share that helper so the detector and the carrier scope agree on where
    an operand ends.
    """
    skip_next = False
    heredoc_tag: str | None = None
    expect_tag = False
    idx = 0
    while idx < len(later_tokens):
        tok = later_tokens[idx]
        idx += 1
        raw = tok.strip(_SHELL_WRAPPER_CHARS)
        if heredoc_tag is not None:
            # The body is program text on stdin, not an argument, and its CLOSING TAG
            # ends this command: the tokenizer drops the newline that follows, so
            # whatever comes after the tag belongs to the NEXT command. Reading it as
            # this interpreter's positional made `python <<PY … PY; echo ok` report
            # "runs a script named echo" and skipped the whole branch, so the heredoc's
            # payload went unscanned. The heredoc has
            # already supplied the program, so the answer here is simply True.
            if raw == heredoc_tag:
                return True
            continue
        if expect_tag:
            expect_tag = False
            heredoc_tag = raw
            continue
        here = _here_string_payload(raw)
        if here is not None:
            # A here-string supplies the program on stdin exactly as a heredoc does; its
            # operand is a redirect word, never this interpreter's positional -- and the
            # WHOLE operand, which a substitution spreads over several tokens.
            if not here:  # a bare `<<<` puts its word in the next token
                if idx >= len(later_tokens):
                    break
                here = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            idx = _operand_span_end(later_tokens, idx, here)
            continue
        marker = _heredoc_marker(raw)
        if marker is not None:
            if marker:
                heredoc_tag = marker
            else:
                expect_tag = True  # a bare `<<` splits its tag into the next token
            continue
        # Scanned on a form that keeps the SUBSTITUTION delimiters. `raw` has had
        # `_SHELL_WRAPPER_CHARS` stripped, and those include `(` and `)` -- so the word
        # `2>$(` (the tokenizer splits on the space inside `$( (true); printf x)`) arrived
        # here as `2>$`, with the opener gone. The scan then saw an ordinary one-character
        # target, never entered a substitution, and the tail of the substitution was read
        # as a script path, putting the stdin program back out of view. Quotes still come
        # off, since a quoted redirect is still a redirect.
        redirect_word = tok.strip("\"'")
        glue = _redirect_glue_point(redirect_word)
        if glue is not None:
            # The redirect rides on the back of another word (`-u>`). Split it and let the
            # loop read both halves, so the part BEFORE the redirect is classified by the
            # same flag/positional branches as any other word -- `-u` continues the scan,
            # `script.py` ends it. Once per word, since neither half can split again.
            later_tokens = [
                *later_tokens[:idx],
                redirect_word[:glue],
                redirect_word[glue:],
                *later_tokens[idx:],
            ]
            continue
        redirect = _output_redirect_scan(redirect_word)
        if redirect is not None:
            # An OUTPUT redirect and its target are not this command's arguments and say
            # nothing about where the program comes from, so the walk steps over both and
            # keeps looking, as for a stdin redirect. Falling through read the leftover
            # digits of `2>&1` as a script path, so `python 2>&1 <<< '<program>'` went unscanned.
            redirect_target, position = redirect
            # A chain of output redirects glued into ONE word (`>a>a>a...`) is walked
            # here, in place, to stay linear in the word length on a floor that runs
            # for every command.
            while position < len(redirect_word):
                further = _output_redirect_scan(redirect_word, position)
                if further is None:
                    break
                redirect_target, position = further
            remainder = redirect_word[position:]
            if remainder:
                # What is left starts with a STDIN operator (`2>/dev/null<<EOF`), which
                # the branches above know how to read. Hand it back as its own token --
                # once per word, not once per operator -- because swallowing it loses the
                # heredoc and with it the program on stdin.
                later_tokens = [*later_tokens[:idx], remainder, *later_tokens[idx:]]
            elif not redirect_target:
                if idx >= len(later_tokens):
                    break
                redirect_target = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            if redirect_target:
                idx = _operand_span_end(later_tokens, idx, redirect_target)
            continue
        if "<" in raw:
            # A stdin REDIRECT and its operand are not this command's arguments either,
            # and the redirect is what supplies the program: `python < prog.py` reads its
            # program from that file. The earlier walk stopped at the redirect and then
            # read the operand as a script path, so `python3 < $(printf …)` answered False.
            target = raw[raw.index("<") :].rsplit("<", 1)[1]
            if not target:
                if idx >= len(later_tokens):
                    break
                target = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            idx = _operand_span_end(later_tokens, idx, target)
            continue
        norm = _normalize_operand(tok).strip("\"'")
        if skip_next:
            skip_next = False
            continue  # value consumed by an operand-taking flag (`-X dev`)
        if not norm:
            continue
        if norm.startswith("<") or norm.startswith("|"):
            break  # a redirect/pipe boundary ends this command's argument list
        if norm == "-":
            return True
        if norm in _PYTHON_INLINE_PROGRAM_FLAGS or norm.startswith("-m") or norm.startswith("-c"):
            return False  # `-c`/`-m` supply the program, not stdin
        if norm in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        if len(norm) > 2 and norm[:2] in _PYTHON_OPERAND_FLAGS:
            continue  # attached operand, e.g. `-Xdev`
        if norm.startswith("-"):
            continue  # an ordinary interpreter flag
        return False  # a positional that is not `-` is a script path
    return True  # nothing but flags → bare interpreter reads stdin


def _data_consumer_command_disqualified(tokens: "list[str]") -> bool:
    """True where NO token in *tokens* can claim the data-consumer exemption.

    The three guards collected here read ONLY *tokens*, so their answer is a
    property of the whole command and is the same for every token in it.  They
    live in one function so a caller holding a fixed argv can charge them ONCE
    instead of once per candidate token: :func:`_data_consumer_exempt` is called
    per payload inside a loop over a fixed argv, and the ``_SCRIPT_EXECUTES_RE``
    sweep below is itself O(len(tokens)), so re-asking makes the enclosing walk
    quadratic in payload count (18k payloads, ~293s).

    Splitting them out cannot change any verdict: each is a pure function of
    *tokens* and each REFUSES the exemption, so hoisting alters only how often
    the same answer is computed, never what it is.
    """
    if _pipes_into_evaluator(tokens):
        return True
    # ``$(printf <name>) <verb>`` puts the consumer INSIDE a substitution that occupies
    # program position, so its OUTPUT is what runs -- the words are not inert data.
    if (
        tokens
        and tokens[0].lstrip("\"'").startswith("$(")
        or (tokens and tokens[0].lstrip("\"'").startswith("`"))
    ):
        return True
    # A "data consumer" that can EXECUTE is not one for this command.  ``awk`` has
    # ``system()`` and pipe-to-command; GNU ``sed`` has the ``e`` flag.  The exemption is
    # withdrawn per-command when the script text carries such a construct, rather than
    # dropping ``awk`` from the list entirely -- that would also refuse ordinary
    # ``awk '{print $1}' <file>``, and the list is deliberately a denylist of consumers so
    # that a mistake here costs a false positive, never a bypass.
    if any(_SCRIPT_EXECUTES_RE.search(tok) for tok in tokens):
        return True
    return False


def _data_consumer_exempt(
    index: int,
    token: str,
    programs: "list[str]",
    tokens: "list[str]",
    *,
    command_disqualified: bool,
) -> bool:
    """True if *token* is an ARGUMENT of a command that treats arguments as data.

    ``echo <name> <verb>`` prints two words -- a mention, not an invocation.

    The exemption is refused in two cases:

    * the token carries a control operator with a NEW PROGRAM after it
      (``echo foo;kirocrew>/tmp/x``).  ``shlex`` splits on whitespace only, so such
      a token is attributed to the PRECEDING command while the part after the
      operator is a new command that really runs.  An operator at the very END of
      the token (``cp $dir/*;``) starts no program inside the token: the word before
      it is still this data consumer's argument, and the next command's words are
      the next command's -- so that token IS exempt.  (This is the one place a
      trailing operator may excuse a token: gated on the program being a data
      consumer, a quoted ``'pkill;'`` or ``'kirocrew;'`` at PROGRAM position -- a
      symlink literally named with the operator -- never reaches it.)
    * the command pipes into a shell or evaluator (``echo … | sh``), where the
      printed text is executed rather than displayed.
    * the token OPENS with a command substitution (``ls $(kirocrew update)``,
      ``cat `kirocrew token```, quotes before it notwithstanding): the token's own
      basename reading IS the body's program, so excusing the token as data would
      excuse the program that runs.  A substitution LATER in the word
      (``cp report_$(hostname)_*.log <dir>``) or inside a BRACE GROUP
      (``mv {$(date +%F),current}.log <dir>``) is a filename around a body: the
      body is judged on its own by the payload walk, which descends into every
      substitution wherever it sits, and the word itself is a filename bash never
      runs.  Refusing those made two ops routines -- archiving host-stamped or
      date-stamped logs into a product-named scratch directory -- read as a
      self-kill (each confirmed newly-refused by the scope review).

    Inheriting the exemption in any of these cases would turn a precision fix into a
    bypass.
    """
    if index <= 0:
        return False
    if any(segment for segment in _CONTROL_OPERATOR_RE.split(token)[1:]):
        return False
    if _LEADING_SUBSTITUTION_RE.match(token):
        return False
    # The command-level guards live in ``_data_consumer_command_disqualified`` and
    # their verdict arrives here already computed. Every caller iterates one fixed
    # argv, so every caller can charge them once per argv rather than once per
    # token, and the parameter is REQUIRED so that a caller cannot fall back to
    # computing them per token -- the whole-argv sweep inside them is what makes
    # that shape quadratic in the token count.
    if command_disqualified:
        return False
    return programs[index] in _DATA_CONSUMER_PROGRAMS


def _argv_programs(tokens: "list[str]") -> "list[str]":
    """For each token, the program name of the command that token belongs to.

    Walks the argv tracking command boundaries (``_ends_argv``) and skipping
    leading ``VAR=value`` assignments, which precede the program rather than being
    it.  Asks "what command is this name an argument OF?" -- the difference
    between ``echo <name> <verb>`` (data) and ``ssh host <name> <verb>`` (executed).
    """
    programs: list[str] = []
    current = ""
    expect_program = True
    for token in tokens:
        if expect_program and token and not ENV_ASSIGNMENT_RE.match(token):
            current = _program_basename(token)
            expect_program = False
        programs.append(current)
        if _ends_argv(token):
            current = ""
            expect_program = True
    return programs


def _is_shell_command_flag(token: str) -> bool:
    """True if *token* is the shell flag whose next argument is a script."""
    return token == "--command" or bool(_SHELL_COMMAND_FLAG_RE.match(token))


def _is_not_double_dash(token: str) -> bool:
    """True where the ``--`` skip in :func:`_nested_shell_payloads` stops.

    Named for the same reason the other two stop conditions are: the precomputed
    index and the token the caller then reads must not drift apart.
    """
    return token != "--"


def _next_stop_indexes(tokens: "list[str]", is_stop: "Callable[[str], bool]") -> "list[int]":
    """For each index, the first index at or after it where *is_stop* holds.

    One backward pass, so a forward scan per program token becomes a lookup and the
    caller stays linear in token count.  Position ``len(tokens)`` means "no such
    token", which reads the same as the original loops running off the end.
    """
    limit = len(tokens)
    table = [limit] * (limit + 1)
    for index in range(limit - 1, -1, -1):
        table[index] = index if is_stop(tokens[index]) else table[index + 1]
    return table


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
        (i for i in range(pipe + 1, len(tokens)) if _program_basename(tokens[i]) == "xargs"),
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


def _normalize_operand(token: str) -> str:
    """A token reduced to the text the shell will actually pass along.

    Removes quoting, an attached redirection and empty substitutions, and truncates at
    the first control operator -- every wrapper that can sit on an operand without
    changing what the shell hands to the program.  Used for both the credential verb and
    the kill target, so a wrapper closed in one place cannot reopen in the other.

    The operator is a boundary, not a trailing nuisance: in ``<verb>;echo ok`` the shell
    passes ``<verb>`` and starts a new command, so stripping only from the END leaves the
    operand unrecognisable while the shell still runs it.
    """
    token = _resolve_param_defaults(token.strip(_SHELL_WRAPPER_CHARS))
    token = _EMPTY_SUBST_RE.sub("", token)
    token = _debracket(_strip_redirect(token).strip(_SHELL_WRAPPER_CHARS))
    return _CONTROL_OPERATOR_RE.split(token, 1)[0].strip(_SHELL_WRAPPER_CHARS)


# The product name as a WHOLE program name (bare or the tail of a path), which is
# what distinguishes ``bin/kirocrew token`` from ``cd kirocrew-wt-x``.


def _is_self_program(token: str) -> bool:
    """True if *token* names the Kiro Crew CLI itself, bare or via a path.

    Also true when the name carries a shell GLOB the shell would expand to the
    executable -- ``./bin/kiro[c]rew``, ``kiro?rew``, ``kiro*rew``.
    """
    base = _program_basename(token)
    if _SELF_PROGRAM_RE.match(base):
        return True
    return _glob_could_expand_to(base, _SELF_PROGRAM_SPELLINGS)


def _self_tokens(text_lower: str) -> "list[str]":
    """Tokenize the WHOLE command, resolving quoting before any splitting.

    Splitting the raw text into segments first (as the pattern passes do) is
    unsafe for these rules: it cuts on a ``;`` or ``|`` that is INSIDE a quoted
    argument, so ``pkill -f '[;]*kirocrew'`` loses its own target. ``shlex``
    resolves the quotes first, so a quoted separator stays part of one token.

    Line continuations are folded away FIRST, because the shell removes
    ``\\`` + newline while READING, before it tokenizes anything, so the two
    characters vanish rather than reaching the operator split as a ``[;&|\\n]+``
    SEPARATOR. Folding keeps an assignment and the invocation it feeds in one
    command: ``T=$(ca\\`` + newline + ``se …); kirocrew $T`` resolves ``$T`` and
    forms the ``kirocrew token`` argv pair the self-protection check needs, the
    same command bash assembles and runs.

    The fold is the quote- and escape-aware :func:`_fold_line_continuations`,
    NOT the bare :func:`_shell_join_continuations` regex. Only a LONE
    ``\\`` + newline is a continuation; an EVEN backslash run before the newline
    is an escaped literal backslash that ENDS the line, so bash starts a new
    command. ``true\\\\`` + newline + ``python -m kirocrew token`` runs the mint
    on the second line, and a bare regex that folds any ``\\`` before a newline
    would join the two, mangle the ``python`` token, and hide the mint from the
    argv check while bash still runs it. ``_fold_line_continuations`` folds only
    the lone case and leaves the escaped run intact, matching bash.
    """
    try:
        # shlex treats bare newlines as whitespace, which joins separate
        # commands into one argv. Preserve those boundaries after folding
        # continuations; quoted newlines remain part of their operand.
        command = _fold_line_continuations(text_lower)
        if "\n" in command:
            command = "".join(
                " ; " if step.active and step.char == "\n" else step.text
                for step in _iter_shell_chars(command)
            )
        return _resolve_function_aliases(
            _resolve_local_assignments(normalize_shell_command(command))
        )
    except Exception:
        return []


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


_FUNC_DEF_RE = re.compile(r"\A(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)")


def _resolve_function_aliases(tokens: "list[str]") -> "list[str]":
    """Substitute a shell FUNCTION name with the protected program it forwards to.

    ``x(){ <name> "$@";}; x <verb>`` never puts the program and the verb in one argv --
    the function body holds the program and the call site holds the verb.  A function
    whose body invokes a protected program is therefore treated as an alias for it, so
    the ordinary argv checks see ``<name> <verb>`` at the call site.

    Only a LITERAL body is inspected, and only the program it invokes is carried over;
    no attempt is made to model parameter positions.  Over-approximating is the safe
    direction -- the alias only matters where the function is called as a program.
    """
    aliases: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        # ``alias x=<name>`` then ``x <verb>`` is the same evasion as a function
        # wrapper: the definition holds the program and the call site holds the verb.
        if tokens[i] == "alias" and i + 1 < len(tokens):
            spec = _LOCAL_ASSIGN_RE.match(tokens[i + 1])
            if spec and spec.group(2):
                target = _normalize_operand(spec.group(2))
                if _is_self_program(target):
                    aliases[spec.group(1)] = "kirocrew"
                elif _program_basename(target) in _KILL_BY_NAME_PROGRAMS:
                    aliases[spec.group(1)] = _program_basename(target)
        m = _FUNC_DEF_RE.match(tokens[i])
        if m:
            # Walk the body until the closing brace, taking the first protected program.
            for body_token in tokens[i + 1 :]:
                base = _program_basename(body_token)
                if _is_self_program(body_token):
                    aliases[m.group(1)] = "kirocrew"
                    break
                if base in _KILL_BY_NAME_PROGRAMS:
                    aliases[m.group(1)] = base
                    break
                if "}" in body_token:
                    break
        i += 1
    if not aliases:
        return tokens
    return [aliases.get(tk, tk) for tk in tokens]


# ``${VAR:0}`` / ``${VAR^^}`` / ``${VAR/x/y}`` and friends TRANSFORM a variable's own
# value.  The ``:-``/``:+``/``:=``/``:?`` default forms are deliberately NOT matched here --
# those carry a literal of their own and are handled by ``_resolve_param_defaults``.
_PARAM_TRANSFORM_RE = re.compile(r"\$\{([A-Za-z_]\w*)(?::(?![-+=?])[^}]*|[#%^,/@][^}]*)\}")


# ``${!VAR}`` expands to the value of the variable NAMED by ``VAR`` -- one more hop
# than ``${VAR}``, through the same table.
_INDIRECT_VAR_USE_RE = re.compile(r"\$\{!([A-Za-z_][A-Za-z0-9_]*)\}")


def _mint_verb_in_substitution(tokens: "list[str]", idx: int) -> bool:
    """True if the substitution starting at *idx* prints the credential-minting verb.

    The program-name twin of this check answers "does this compute a protected
    PROGRAM?".  This one answers "does it compute the VERB?", for the spelling that
    hides that half instead (``T=$(printf <verb>); <name> $T``).
    """
    joined = " ".join(tokens[idx:])
    for body in _substitution_bodies(joined):
        if any(_is_mint_verb(word) for word in body.split()):
            return True
    return False


def _resolve_local_assignments(tokens: "list[str]") -> "list[str]":
    """Substitute ``$VAR`` uses with a literal assigned earlier in the same command.

    Only LITERAL right-hand sides are tracked, and only assignments that appear in
    this same command text -- there is no attempt to model the ambient environment.
    That is enough for the evasion it closes, where the attacker must supply both
    halves themselves.

    A value that REFERENCES an already-tracked variable is expanded before it is
    classified, so a name assembled across several assignments still resolves to the
    literal the shell will run.
    """
    values: dict[str, str] = {}
    out: list[str] = []
    # ``X=<name>;$X <verb>`` glues the assignment and the next command into ONE token,
    # because ``shlex`` splits on whitespace only.  Split on top-level control operators
    # first so the assignment is seen as an assignment and the use as a use.
    tokens = _split_glued_operators(tokens)
    for idx, token in enumerate(tokens):
        assign = _LOCAL_ASSIGN_RE.match(token)
        # ``NAME+=tail`` APPENDS, and the pattern above cannot match it at all (``+`` is
        # not a name character), so an appended PROGRAM word would be invisible here:
        # `F=fi; F+=nd; $F <fenced> -exec cat {} +` runs a `find` this resolver would never
        # see, while its single-assignment twin `F=fin; ${F}d` is denied. The spellings
        # include an empty initial value, repeated appends, a quoted tail and an append
        # feeding a braced use.
        #
        # Append is in the same closed class as those: the tail is a LITERAL, so what the
        # shell will run is decided by the text alone. That is what separates it from
        # `$(printf find)`, whose value needs a program run and is out of scope here.
        #
        # `_SHELL_ASSIGN_RE` is REUSED rather than a second append pattern being added:
        # it already carries the optional ``+`` group for this exact problem on the
        # path-tracking side, so there is one spelling of the rule in the module. It is
        # matched only in the branch the assignment pattern rejects, which keeps this
        # additive -- renumbering the shared pattern's groups instead would have rewritten
        # every existing reader of them for no behavioural gain.
        append = None if assign else _SHELL_ASSIGN_RE.match(token)
        if append and append.group(2):
            name, tail = append.group(1), append.group(3)
            if values and "$" in tail:
                # Same reason the assignment path expands first: a tail built from an
                # already-tracked variable is a literal once expanded, and left
                # unexpanded it reads as computed and the append is dropped.
                tail = _VAR_USE_RE.sub(
                    lambda m: values.get(m.group(1) or m.group(2), m.group(0)), tail
                )
            if _is_computed_value(tail):
                # No literal to concatenate. Over-approximate exactly as the assignment
                # path does: the value only matters where it is later used as a program,
                # so a wrong guess there is a refusal rather than a bypass.
                produced = _protected_name_in_substitution(tokens, idx)
                if produced:
                    values[name] = produced
                out.append(token)
                continue
            piece = tail.strip("\"'").rstrip(";&|")
            # `F=` records nothing, so an append to an unset name starts from empty
            # rather than being discarded -- that was one of the seven spellings, and
            # bash builds the value the same way.
            values[name] = values.get(name, "") + piece
            out.append(token)
            continue
        if assign and values and "$" in (assign.group(2) or ""):
            # A new value may be built FROM a variable already tracked
            # (``x=p; x=${x}kill``).  Expanding before classifying is what makes the
            # result a literal at all: left unexpanded it looks computed, the earlier
            # binding stays in place, and the reassignment is silently ignored.
            expanded = _VAR_USE_RE.sub(
                lambda m: values.get(m.group(1) or m.group(2), m.group(0)),
                assign.group(2),
            )
            if expanded != assign.group(2):
                token = f"{assign.group(1)}={expanded}"
                assign = _LOCAL_ASSIGN_RE.match(token)
        if assign and _is_computed_value(assign.group(2)):
            # ``X=$(printf <name>); $X <verb>`` COMPUTES the value, so there is no
            # literal to carry forward.  Resolve it conservatively instead: if the
            # substitution that produces it names a protected program anywhere, treat
            # the variable as holding that name.  Over-approximating here is the safe
            # direction -- the value only matters when ``$X`` is later used as a
            # program, and a wrong guess there is a refusal, not a bypass.
            produced = _protected_name_in_substitution(tokens, idx)
            if produced:
                values[assign.group(1)] = produced
            elif _mint_verb_in_substitution(tokens, idx):
                # ``T=$(printf <verb>); <name> $T`` computes the VERB rather than the
                # program.  Same reasoning as the program case: the value only matters
                # where it is later used, so binding it to the verb is the safe
                # over-approximation.
                values[assign.group(1)] = "token"
            out.append(token)
            continue
        if assign and assign.group(2):
            # A trailing ``;``/``&&`` belongs to the command structure, not the
            # value: ``shlex`` splits on whitespace only, so ``X=name;`` arrives
            # with the operator attached.
            value = assign.group(2).strip("\"'").rstrip(";&|")
            if value:
                values[assign.group(1)] = value
            out.append(token)
            continue
        if values and "$" in token:
            # ``${!V}`` is INDIRECT: it expands to the value of the variable NAMED by
            # ``V``, so resolving it takes two hops through the same table.  Done before
            # the ordinary substitution so what remains afterwards is a plain literal.
            # A TRANSFORMATION on a tracked variable (``${K:0}``, ``${K^^}``, ``${K/x/y}``)
            # still expands to something derived from the tracked value, but none of those
            # spellings are a plain ``${K}``.  Resolved to the value itself: the
            # transformation is not modelled, and over-approximating here is the safe
            # direction for the same reason it is for a computed value -- the result only
            # matters where it is used as a program or verb, and a wrong guess there is a
            # refusal, not a bypass.  The ``:-``/``:+``/``:=``/``:?`` DEFAULT forms are
            # excluded: they carry their own literal and are resolved separately.
            token = _PARAM_TRANSFORM_RE.sub(lambda m: values.get(m.group(1), m.group(0)), token)
            token = _INDIRECT_VAR_USE_RE.sub(
                lambda m: values.get(values.get(m.group(1), ""), m.group(0)), token
            )
            token = _VAR_USE_RE.sub(
                lambda m: values.get(m.group(1) or m.group(2), m.group(0)), token
            )
        out.append(token)
    return out


def _nested_shell_payloads(
    tokens: "list[str]",
    *,
    allow_join: bool = True,
    joined_out: "set[str] | None" = None,
) -> "list[str]":
    """Literal shell-script payloads carried as an argument inside *tokens*.

    Covers ``sh -c '<script>'`` / ``bash -c '<script>'`` (the payload is the
    first non-flag token after ``-c``) and ``eval '<script>'``.  Only LITERAL
    payloads are returned -- ``eval "$CMD"`` carries no visible script, and that
    case is covered by the regex tier running alongside this floor rather than by
    this function.

    *allow_join* suppresses the ``eval`` argument join, and *joined_out* collects
    the joined payloads this call produced. Both exist for
    :func:`_shell_payload_walk`, which must not let a JOINED frame join again: the
    joined text is strictly shorter than its parent, so it becomes a frame of its
    own, and if that frame joins too the walk builds a chain of shrinking suffixes
    -- N frames each costing an O(N) tokenize and an O(N) join. Measured on
    ``"eval " * 1280``: 65 s and growing ~5x per doubling, against 0.13 s before
    the join existed, which stalls the synchronous permission gate long enough for
    the watchdog to fire. Declining the second join costs no detection, because
    the join FUSES already-dequoted words in one step -- ``eval eval 'git' 'push
    origin main'`` is fused to ``git push origin main`` by the first join, so the
    publish is visible at the first joined frame and the chain would only
    re-derive suffixes of an answer already in hand.
    """
    payloads: list[str] = []
    # Both scans below look for the FIRST token after a program that satisfies a stop
    # predicate, handle it, and stop.  Walking forward per program made the function
    # QUADRATIC in token count: in a run of interpreter tokens with no flag among them
    # every one of them re-walks the whole tail, so a command padded with them stalls
    # the synchronous permission gate (measured: 13.2 s for 16 000 tokens, ~4x per
    # doubling).  The first-stop index is precomputed once per predicate in a single
    # backward pass instead, which makes the whole function O(N) while returning the
    # identical payload list -- the loops' only exits were that first stop token or the
    # end of the list, so nothing else can change.
    env_stop = _next_stop_indexes(tokens, _is_env_split_flag)
    # The herestring and the GLUED ``-c`` spelling each get their OWN stop table
    # rather than sharing the flag's: with a shared table, whichever spelling
    # comes first EATS the stop through which a later spelling's payload was
    # found -- ``bash <<<'x' -c '<script>'`` yielded only ``x``.  Splitting the
    # tables fixes the CROSS-spelling case; WITHIN one class each table still
    # reads only its first stop per shell token, which for short-cluster ``-c``
    # carriers is closed by the every-carrier sweep at the bottom of this
    # function.  Two stated residuals: ``--command`` carriers stay
    # first-stop-only (the sweep is scoped to short clusters), and herestrings
    # keep a first-occurrence residual (``bash <<<'a' <<<'b'`` yields ``a``; a
    # real shell applies the LAST redirect).
    #
    # The flag, herestring and glued stops are SPARSE (sorted index lists read
    # through bisect, payloads cached in dicts keyed by stop position), built in
    # one forward pass gated on a cheap prefix check.  Dense per-token tables
    # (and a regex call per token to fill them) made this function's constant
    # measurably heavier than the merge-base on interpreter-run shapes, and the
    # pre-existing linearity tests bound ABSOLUTE seconds on CI runners, not
    # growth rate.  A carrier-free command now allocates three empty lists and
    # runs one `startswith` per token -- less than the merge-base's own
    # per-token predicate regex.  Payloads are cached at their stop position
    # because many shell tokens can share one stop -- re-extracting there copies
    # the same length-M substring once per shell token, O(N*M) on
    # ``["bash"]*N + ["-c<payload>"]``; the cached string is one
    # object, so downstream dedup-set hashing stays linear too.
    limit = len(tokens)
    flag_stops: "list[int]" = []
    herestring_stops: "list[int]" = []
    glued_stops: "list[int]" = []
    glued_at: "dict[int, str]" = {}
    herestring_tail_at: "dict[int, str]" = {}
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            if _is_shell_command_flag(token):
                flag_stops.append(index)
            if not token.startswith("--"):
                glued = _glued_shell_command_payload(token)
                if glued is not None:
                    glued_stops.append(index)
                    glued_at[index] = glued
        elif token.startswith("<<<"):
            herestring_stops.append(index)
            if token != "<<<":
                herestring_tail_at[index] = token[3:]

    def _first_stop_at_or_after(stops: "list[int]", start: int) -> int:
        position = bisect.bisect_left(stops, start)
        return stops[position] if position < len(stops) else limit

    # ``--`` runs are precomputed for the same reason: a long run of them after the
    # command flag is walked once per program token otherwise, which is quadratic even
    # though the two scans above are not.
    past_dashes = _next_stop_indexes(tokens, _is_not_double_dash)
    # ``eval``'s argument join is bounded to one per walk; see the verb branch.
    joined_eval = False
    first_shell: "int | None" = None
    for i, token in enumerate(tokens):
        base = _program_basename(token)
        # A shell reached through a VARIABLE (``$SHELL -c '<payload>'``) runs the
        # payload exactly as a named shell does.  The recognizer already used for the
        # ``| $SHELL`` evaluator sink applies here too.
        if base in _NESTED_SHELL_PROGRAMS or _is_shell_variable_reference(token):
            if first_shell is None:
                # Recorded here, where the shell test has already been paid,
                # so the every-carrier sweep below needs no second scan that
                # re-derives program basenames token by token.
                first_shell = i
            j = _first_stop_at_or_after(flag_stops, i + 1)
            if j < limit:
                # ``bash -c -- '<script>'`` is legal: ``--`` ends option parsing
                # and the script is the token AFTER it.  Skip any run of them.
                k = past_dashes[j + 1]
                if k < limit:
                    payloads.append(tokens[k])
            # A HERESTRING feeds the script on stdin instead of as an argument
            # (``bash <<< '<script>'``), so its text is a command just the same.
            # Both the spaced and glued spellings arrive here.
            h = _first_stop_at_or_after(herestring_stops, i + 1)
            if h < limit:
                tail = herestring_tail_at.get(h)
                if tail is not None:
                    payloads.append(tail)
                elif h + 1 < limit:
                    payloads.append(tokens[h + 1])
            # The glued ``-c`` spelling (``-c'<script>'``, one token once shlex
            # strips the quotes) is looked up independently as well.  An
            # all-alpha cluster like ``-ecfoo`` satisfies BOTH ``-c`` readings --
            # it matches the bare-flag pattern (yielding the next token, as
            # before) AND carries a glued remainder a real shell would run -- so
            # both payloads are yielded rather than picking one interpretation.
            g = _first_stop_at_or_after(glued_stops, i + 1)
            if g < limit:
                payloads.append(glued_at[g])
        elif base in _ENV_SPLIT_PROGRAMS:
            # ``env -S '<script>'`` / ``env --split-string '<script>'`` splits the
            # payload into a command and runs it, so its text is a command line.
            j = env_stop[i + 1]
            if j < limit:
                # ``is_denied`` lowercases its input, so compare case-insensitively:
                # the real flag is ``-S`` but it arrives here as ``-s``.
                flag = tokens[j].lower()
                if flag in {"-s", "--split-string"}:
                    if j + 1 < limit:
                        payloads.append(tokens[j + 1])
                elif flag.startswith("-s") and len(tokens[j]) > 2:
                    payloads.append(tokens[j][2:])
                elif flag.startswith("--split-string="):
                    payloads.append(tokens[j].split("=", 1)[1])
        elif base in _NESTED_SHELL_VERBS or token in _NESTED_SHELL_VERBS:
            # ``--`` ends option parsing, so ``eval -- '<script>'`` runs the token
            # AFTER it. Taking ``tokens[i + 1]`` blindly yielded the literal ``--``
            # as the payload and the real script was never walked. Reuse the same
            # precomputed run-skip the ``-c`` branch above uses, so this stays O(1)
            # rather than becoming the third forward walk this function was made
            # linear to remove.
            j = past_dashes[i + 1]
            if j < limit:
                payloads.append(tokens[j])
                # ``eval`` CONCATENATES all of its arguments with a space and
                # evaluates the RESULT, so a command split across several words is
                # one command line at run time while no single word looks like one.
                # Taking only the first argument let
                # ``eval '<program>' '<verb and args>'`` through: the hooks saw the
                # bare program name and the publish never appeared. The joined form
                # is added ALONGSIDE the first argument, so the single-argument
                # reading is unchanged.
                #
                # ``eval`` only. ``source``/``.`` take a FILE as their first
                # argument and pass the rest as positional parameters, so joining
                # them would invent a command line bash never runs.
                #
                # Joined at most ONCE per walk: a join is O(N), so one per verb
                # token would be quadratic. One is enough, because it runs to the
                # END of the token list and therefore already spans every later
                # verb's own suffix.
                verb = base if base in _NESTED_SHELL_VERBS else token
                if verb == "eval" and j + 1 < limit and not joined_eval and allow_join:
                    joined_eval = True
                    joined = " ".join(tokens[j:])
                    payloads.append(joined)
                    if joined_out is not None:
                        joined_out.add(joined)
    # ``bash<<<'<payload>'`` glues the program, the operator and the payload into ONE
    # token, so the program never appears as a token of its own for the walk above to
    # recognise.  Split on the operator and check the left half.
    for token in tokens:
        if "<<<" not in token:
            continue
        head, _, tail = token.partition("<<<")
        if tail and _program_basename(head) in _NESTED_SHELL_PROGRAMS:
            payloads.append(tail)
    # EVERY ``-c`` carrier past the first shell token is swept, not only the
    # first-stop one.  The stop tables above read one token per spelling class,
    # so a decoy that satisfies the same predicate EATS the stop through which a
    # later carrier's payload was found (``ksh -onoclobber -c'<script>'`` stops
    # the glued table at ``-onoclobber``; ``bash -c 'a' -c '<script>'`` stops the
    # flag table at the first ``-c``).  The loose recognition here is the one the
    # alt-traversal pass's deleted local extractor used -- any prefix before the
    # first lowercase ``c`` (``-1c…``), payload glued or in the next token -- and
    # the sweep is a single forward pass from the first shell token (recorded
    # for free inside the main loop above), so the function stays O(N).
    # Additive only: a payload already collected above is not re-appended, so
    # consumers pinning exact payload lists are unchanged.
    if first_shell is not None:
        collected = set(payloads)
        for index in range(first_shell + 1, limit):
            token = tokens[index]
            if not token.startswith("-") or token.startswith("--"):
                continue
            glued = _shell_c_carrier_glued(token)
            if glued is None:
                continue
            if glued:
                # Glued payload: yield EVERY plausible split, not just the
                # first-``c`` one -- after the deny tiers' case fold, which
                # ``c`` took the argument is unrecoverable, and the wrong
                # split hid a protected payload behind one junk letter
                # (``-Cc'<script>'`` folded to ``-cc<script>``).
                for candidate in _shell_c_carrier_payloads(tokens[index]):
                    if candidate not in collected:
                        collected.add(candidate)
                        payloads.append(candidate)
            else:
                k = past_dashes[index + 1]
                bare_next = tokens[k] if k < limit else None
                if bare_next and bare_next not in collected:
                    collected.add(bare_next)
                    payloads.append(bare_next)
    # ``a=(<name> <verb>); "${a[@]}"`` runs the array's elements AS a command line.  The
    # expansion is one token, so the argv checks have no adjacent operands to compare --
    # the joined elements are handed to the payload walk instead, which re-tokenizes them.
    arrays = _array_assignments(tokens)
    if arrays:
        programs = _argv_programs(tokens)
        for index, token in enumerate(tokens):
            # Only an expansion in COMMAND position runs the elements.  As an ARGUMENT
            # they are just words -- ``echo ${a[@]}`` prints them -- so requiring the
            # expansion to be its own command's program keeps the data cases inert.
            if index >= len(programs) or programs[index] != token:
                continue
            for match in _ARRAY_EXPAND_RE.finditer(token):
                value = arrays.get(match.group(1))
                if value:
                    payloads.append(value)
    # GNU ``sed`` runs the REPLACEMENT of an ``s///e`` command as a shell command, so
    # that text is a payload.  It lives INSIDE one token, which is why withdrawing the
    # data-consumer exemption is not enough on its own: there are no two adjacent
    # operands for the argv checks to compare.
    for token in tokens:
        replacement = _sed_exec_replacement(token)
        if replacement:
            payloads.append(replacement)
    # A MULTIWORD alias replacement is a whole command line, not just a program name
    # (``alias x='kirocrew token'`` then ``x``), so hand it to the payload walk.
    for i, token in enumerate(tokens):
        if token == "alias" and i + 1 < len(tokens):
            spec = _LOCAL_ASSIGN_RE.match(tokens[i + 1])
            if spec and spec.group(2) and " " in spec.group(2).strip():
                payloads.append(spec.group(2))
    # A payload only matters if it looks like a command line rather than a bare
    # operand; a single word is already covered by the direct token scan.
    if _pipes_into_evaluator(tokens):
        # ``echo '<script>' | sh`` produces the command as TEXT and then hands it to
        # something that runs it, so the printed text is a payload exactly as a
        # ``-c`` argument is.
        # An escape can stand in for the separator (``printf '<name>\\040<verb>'``),
        # so the "is this a command line?" test is applied to the DECODED text -- otherwise
        # the token still looks like a single word and is never recognised as a payload at
        # all.  Splitting on any whitespace (not just a space) also admits a tab escape.
        for token in tokens:
            decoded = _decode_printf_escapes(token)
            if len(decoded.split()) > 1:
                payloads.append(decoded)
        # ``xargs`` is different in shape: it does not read a whole script, it APPENDS
        # the piped words to its own command.  ``echo <verb> | xargs <name>`` therefore
        # runs ``<name> <verb>`` even though neither half contains a space.  Reconstruct
        # what it will run: the xargs command line plus the producer's literal words.
        reconstructed = _xargs_reconstructed_command(tokens)
        if reconstructed:
            payloads.append(reconstructed)
    return [p for p in payloads if p.strip()]


# ``${a[@]}`` / ``${a[*]}`` / ``$a[@]`` -- the whole array as separate words.


def _shell_payload_walk(text_lower: str) -> "list[tuple[str, list[str]]]":
    """``(source, argv)`` for *text_lower* and every nested shell payload in it.

    ``bash -c "kirocrew token"`` tokenizes to ``['bash', '-c', 'kirocrew token']``
    -- the dangerous command is a single opaque token, so a direct scan cannot
    see it.  Re-tokenizing the payload and checking that view too closes the
    class rather than one spelling of it.

    Both the SOURCE text and its argv are returned because the two floors that
    consume this need different views of the same frame: the self-protection
    predicates match argv structurally, while the git-publish gate is a
    verb-anchored scan over command text.  Walking once and handing out both is
    what keeps the two floors from drifting -- a publish gate doing
    its own top-level-only text match would let every wrapper form
    (``bash -c '<push>'``, ``eval '<push>'``) bypass the ONLY enforcement
    pushes have.

    Descends to ANY depth.  A numeric depth cap is itself a bypass -- whatever the
    number, one more wrapper defeats it -- so the walk is bounded structurally: a
    payload lives inside one token of its parent and is therefore strictly shorter
    than the parent's source text, and a chain of strictly shorter strings is
    finite.
    """
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    # The third field is whether this frame may perform the ``eval`` argument join.
    # A frame PRODUCED by a join may not, which is what bounds the walk: see
    # ``_nested_shell_payloads``.
    pending: list[tuple[str, int, bool]] = [(text_lower, len(text_lower) + 1, True)]
    while pending:
        source, parent_len, allow_join = pending.pop()
        tokens = _self_tokens(source)
        if not tokens:
            continue
        out.append((source, tokens))
        # Every substitution body is itself a command line -- command substitution
        # (``$( )``, backticks) and PROCESS substitution (``<( )``, ``>( )``) alike, since
        # bash runs the inner command in all of them.  Walking them here means the
        # ordinary argv checks see ``cat <(kirocrew token)`` as the inner invocation.
        #
        # The bodies are read from the FOLDED source -- the same quote-aware fold
        # ``_self_tokens`` applied to build ``tokens`` -- not from the raw text.
        # ``_substitution_bodies`` recognises its openers byte-literally, and the
        # shell removes a ``\`` + newline while READING, before it lexes an opener,
        # so ``cat <\`` + newline + ``(bash -c '<name> <verb>')`` is a process
        # substitution to bash while the raw scan sees no ``<(`` at all and the body
        # was never walked: measured ALLOWED for all three parenthesised openers
        # while bash ran the mint. Folding the raw text here (rather than at the
        # walk's seed) reshapes nothing outside a substitution the tokenizer already
        # reads folded, and the fold preserves single-quoted and ANSI-C spans, so a
        # continuation that bash keeps literal stays literal in the body too.
        joined_here: set[str] = set()
        nested = _nested_shell_payloads(tokens, allow_join=allow_join, joined_out=joined_here)
        for payload in list(nested) + _substitution_bodies(_fold_line_continuations(source)):
            # Descend through EVERY literal payload, to any depth.  Termination is
            # structural, not a cap: a payload is carried inside one token of its
            # parent, so it is strictly shorter than the parent's source text.
            payload = _decode_printf_escapes(payload)
            if len(payload) >= parent_len or payload in seen:
                continue
            seen.add(payload)
            pending.append((payload, len(source), payload not in joined_here))
    return out


def _is_mint_verb(token: str) -> bool:
    """True if *token* is the credential-minting verb, however it is dressed."""
    return _normalize_operand(token) == "token"


def _decode_shell_quoted_literals(cmd: str) -> str:
    """Resolve each ``$'…'`` span, and reduce each ``$"…"`` to plain double quotes.

    ``rm -rf $'/'`` runs exactly what ``rm -rf /`` runs, and ``$'\\x2d\\x76'`` is
    ``-v``, so a matcher that has not resolved these is reading a spelling the
    shell never hands over.  An ANSI-C value is re-quoted with ``shlex.quote`` so a
    value containing whitespace or a quote stays ONE token through ``shlex.split``.

    ``$"…"`` is LOCALE TRANSLATION, and it is NOT ANSI-C -- measured, because
    treating the two alike is a bypass.  Bash
    gives ``$"\\r\\mAA"`` the word ``\\r\\mAA``, byte-identical to plain
    ``"\\r\\mAA"``: inside double quotes a backslash escapes only ``$``, `````,
    ``"``, ``\\`` and a newline, so ``\\r`` is a literal backslash-r and NOT a
    carriage return.  Decoding it as ANSI-C turned that ``\\r`` into whitespace and
    the command vanished from the view, while the inner shell of
    ``bash -c $"\\r\\m -rf /"`` resolves the backslashes in its OWN lexing pass and
    runs the destructive command (measured: it executes ``rmAA`` for
    ``bash -c $"\\r\\mAA"``).  So the ``$`` is dropped and the double-quoted text is
    left for ``shlex`` to resolve by double-quote rules -- which also keeps
    ``rm -rf $"/"`` reaching the rule, since bash's operand there is ``/``.
    """

    def _replace(match: re.Match[str]) -> str:
        ansi_c_body = match.group(1)
        if ansi_c_body is not None:
            return shlex.quote(_decode_ansi_c_body(ansi_c_body))
        return '"' + (match.group(2) or "") + '"'

    return _ANSI_C_QUOTE_RE.sub(_replace, cmd)


def _shell_tokens(cmd: str) -> list[str]:
    """Tokenize *cmd* the way a POSIX shell hands argv to a program.

    Quote removal, backslash de-escaping (``shlex`` POSIX mode), ANSI-C / locale
    quoting (``$'…'``, ``$"…"`` -- see :func:`_decode_shell_quoted_literals`),
    empty-string concatenation (``g""it`` -> ``git``) and whitespace-run collapsing
    -- and NOTHING else: no tilde, no ``$HOME``, no path resolution.  This is the
    shared core of two callers that need the same token identity but must stop at
    different points:

    * :func:`normalize_shell_command` continues on to expand ``~``/``$HOME``,
      because it feeds path matchers that decide FILE identity.
    * :func:`_deny_segment_views` stops here, because expansion is
      platform-dependent and would land the operator's real home path in the
      audit trail -- see that function.

    On parse failure (unbalanced quotes) falls back to whitespace splitting with
    quote/backslash stripping, so a hostile unterminated quote yields a degraded
    view rather than no view.
    """
    if not cmd or not cmd.strip():
        return []
    cmd = _decode_shell_quoted_literals(cmd)
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes or other parse errors — fall back to basic split.
        tokens = [t.strip("\"'\\") for t in cmd.split()]
    # Strip empty-string concatenation artifacts: ca""t -> cat, g''it -> git
    return [_EMPTY_QUOTE_RE.sub("", token) for token in tokens]


def normalize_shell_command(cmd: str) -> list[str]:
    """Normalize a shell command string into a resolved token list.

    Handles:
    - Shell quoting via shlex.split(posix=True)
    - Empty-string concatenation (g""it -> git, ca''t -> cat)
    - Tilde expansion (~/... -> /home/user/...)
    - $HOME / ${HOME} expansion to actual home directory
    - Backslash stripping (handled by shlex POSIX mode)

    Returns a list of resolved tokens.  On parse failure (unmatched quotes)
    falls back to basic whitespace splitting with quote/backslash stripping.

    Tokenization — everything up to and including the empty-quote collapse — is
    :func:`_shell_tokens`; the EXPANSION below is what makes this the path
    normalizer rather than the plain argv view the deny tiers use.
    """
    # NOTE: $HOME expansion happens AFTER tokenization (in the per-token loop
    # below), NOT here.  The previous pre-shlex expansion inserted the raw home
    # path (e.g. ``C:\Users\name`` on Windows) into the command string before
    # shlex.split(posix=True), which then consumed the backslashes as escape
    # characters — mangling the path so is_sensitive_path() could not match it.
    # Moving expansion to per-token mirrors how tilde (``~``) is already
    # handled: shlex strips quotes and produces a literal ``$HOME/...`` token,
    # which the loop then expands safely without backslash reinterpretation.

    home = os.path.expanduser("~")
    resolved: list[str] = []
    for token in _shell_tokens(cmd):
        # Expand $HOME/${HOME} per-token (after shlex, so Windows backslashes
        # in the expanded path are never reinterpreted as escape characters).
        # Uses a callable replacement to avoid re.error on Windows where the
        # home path contains ``\U`` which re.sub parses as a template escape.
        token = _HOME_VAR_RE.sub(lambda _m: home, token)

        # Expand tilde (shlex doesn't do tilde expansion)
        if token.startswith("~"):
            token = os.path.expanduser(token)

        resolved.append(token)

    return resolved
