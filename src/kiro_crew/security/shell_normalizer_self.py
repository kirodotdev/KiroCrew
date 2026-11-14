"""The product CLI's self-invocation recognizer, shared below its two callers.

Which argv word launches the product CLI (``kirocrew``, a path or glob that expands to it,
``python -m kiro_crew``), what operand words that CLI receives, and where its SUBCOMMAND
slot is. The argv floor's subcommand predicates (``argv_floor_self``) and the shell reader's
operand-binding gate (``shell_normalizer``) both read it, so it sits below both and imports
neither: only the lexical owners (``shell_normalizer_walk``, ``_redirects``, ``_programs``,
``_escapes``) and the name vocabulary. Every definition here moved whole from one of those
two callers, which re-export each name.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from . import shell_normalizer_programs as _programs
from .call_views import once_per_call
from .shell_normalizer_escapes import _decode_printf_escapes
from .shell_normalizer_programs import _PYTHON_PROGRAM_RE
from .shell_normalizer_redirects import _REDIRECT_START_RE, _split_brace_alternatives
from .shell_normalizer_walk import (
    _CONTROL_OPERATOR_RE,
    _EMPTY_SUBST_RE,
    _SHELL_WRAPPER_CHARS,
    _debracket,
    _ends_argv,
    _redirect_consumes_next,
    _resolve_param_defaults,
    _strip_redirect,
    _substitution_depth_delta,
)
from .vocabulary import _SELF_PROGRAM_RE, _SELF_PROGRAM_SPELLINGS

# Shell glob metacharacters, and the concrete spellings a glob could expand to.  A
# glob in the program name (``kiro[c]rew``) is resolved by the shell BEFORE exec, so
# it has to be tested for expandability rather than compared literally.
_GLOB_CHARS_RE = re.compile(r"[\[\]?*{}]")


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


@once_per_call()
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


@once_per_call()
def _is_self_program(token: str) -> bool:
    """True if *token* names the Kiro Crew CLI itself, bare or via a path.

    Also true when the name carries a shell GLOB the shell would expand to the
    executable -- ``./bin/kiro[c]rew``, ``kiro?rew``, ``kiro*rew``.
    """
    base = _programs._program_basename(token)
    if _SELF_PROGRAM_RE.match(base):
        return True
    return _glob_could_expand_to(base, _SELF_PROGRAM_SPELLINGS)


#: Module path of the product package, for the ``python -m kiro_crew ... token`` form.
#: Underscored, because that is the IMPORT name — `_SELF_PROGRAM_SPELLINGS` covers the
#: console script (`kirocrew`, `kiro-crew`) and deliberately does not admit `_`, since no
#: executable is spelled that way.
_SELF_MODULE_SPELLINGS = ("kiro_crew",)


#: The import name as it appears INSIDE a ``-c`` payload, for the VERB-GATED module check
#: (``_is_self_module_invocation``): the package named plus a ``token`` argv word is the
#: ``-m`` form written longhand. Deliberately NOT the inline-program gate, which asks for
#: the mint surface: the package name alone is in every ``src/kiro_crew/`` path a patch
#: script mentions as DATA.
_SELF_IMPORT_RE = re.compile(r"\bkiro_crew\b")


def _self_cli_operands(tokens: "list[str]", i: int, *, glued_words: bool) -> "list[str]":
    """Non-flag operand words the product CLI at program index *i* receives, in order.

    A token that stays a ``-``/``--`` word after quote/redirect normalization is a
    global flag and is skipped -- the self-protection top-level flags are all
    valueless (``-v``/``--verbose`` count, ``--no-jail`` bool), so a skipped flag
    never hides an operand behind it. A shell redirection (and its separate
    target, if any) is removed from argv by the shell and is skipped too, so
    ``kirocrew 2>/tmp/x restart`` still reads ``restart`` as the leading operand.
    Quoting is resolved by ``_normalize_operand``; the walk stops at the argv
    boundary so a chained later command's words are not attributed here.

    With *glued_words*, the word a redirection is glued to (``restart>/tmp/log``) is
    read as the operand the shell passes. Without it the whole token is skipped: the
    dequoted token cannot show whether a separator in its target was live
    (``status>x&&kirocrew restart``), so the floor matches either reading.
    """
    operands: "list[str]" = []
    depth = 0
    skip_target = False
    for later in tokens[i + 1 :]:
        is_redirect, expects_target = _redirect_consumes_next(later)
        if skip_target:
            # A separate redirection target (``> FILE``) is a filename: its bytes are
            # data, not an argv boundary, so a quoted ``;``/``|`` in it (``> 'a;b'``)
            # must NOT end the scan. Consume it without the boundary/depth bookkeeping.
            skip_target = False
            continue
        if is_redirect:
            # The redirection operator itself is not an operand and never ends the argv.
            skip_target = expects_target
            if not glued_words or _REDIRECT_START_RE.match(later):
                continue
            later = _strip_redirect(later)
        operand = _normalize_operand(later)
        # ANSI-C ($'...') and locale ($"...") quoting: shlex strips the quotes
        # but leaves the leading ``$``, so a flag hidden as ``$'-v'`` / the hex
        # ``$'\x2d\x76'`` reads as a non-flag operand and shoves the subcommand
        # to second place. Drop the ``$`` and decode the escapes to the value the
        # shell actually passes -- the same de-quoting _program_basename already
        # does for the program name.
        if operand.startswith("$") and not operand.startswith(("$(", "${")):
            operand = _decode_printf_escapes(operand[1:])
        if operand and not operand.startswith("-"):
            operands.append(operand)
        depth += _substitution_depth_delta(later)
        if depth <= 0 and _ends_argv(later):
            break
        depth = max(depth, 0)
    return operands


class _SelfModuleScan(NamedTuple):
    """One token list's normalized forms plus its module-flag stop index.

    ``norm[j]`` is what :func:`_normalize_operand` makes of token *j*, and ``stops[j]``
    is the first index at or after *j* where the module-flag scan in
    :func:`_self_module_name_index` stops.  Both are computed once per token list so
    the scan does not repeat them for every interpreter token in it.
    """

    norm: "list[str]"
    stops: "list[int]"


def _is_self_module_flag(tok: str) -> bool:
    """True where the module-flag scan in :func:`_self_module_name_index` stops.

    The attached spelling only stops when the regex actually matches: ``-msomething``
    that is not our module is an ordinary interpreter flag and the scan continues past
    it, so the regex is part of the stop condition rather than a check made after it.
    """
    return tok == "-m" or (
        tok.startswith("-m") and len(tok) > 2 and bool(_SELF_IMPORT_RE.search(tok[2:]))
    )


def _self_module_flag_scan(tokens: "list[str]") -> "_SelfModuleScan":
    """Precompute one token list's normalized forms and module-flag stop indexes.

    ``_self_module_name_index`` walked forward from each interpreter token to the first
    module flag, normalizing every token it passed.  Called once per interpreter token
    by ``_self_program_index``, that made the self-protection floor QUADRATIC in token
    count: a command of interpreter words with no module flag among them re-walked and
    re-normalized the whole tail every time.  Measured on the floor path, with one
    product word present so its keyword gate opens: 0.03 s / 0.12 s / 0.49 s / 1.92 s
    at 250 / 500 / 1000 / 2000 tokens -- about 4x per doubling, which reaches the
    gateway's loop watchdog well inside a command an agent could emit.  Both passes
    here are single and linear.
    """
    limit = len(tokens)
    norm = [_normalize_operand(token).strip("\"'") for token in tokens]
    stops = [limit] * (limit + 1)
    for index in range(limit - 1, -1, -1):
        stops[index] = index if _is_self_module_flag(norm[index]) else stops[index + 1]
    return _SelfModuleScan(norm=norm, stops=stops)


def _self_module_name_index(tokens: "list[str]", i: int, scan: "_SelfModuleScan") -> "int | None":
    """Index of the product module-name token in a ``python -m kiro_crew ...``
    invocation whose interpreter is at *i*, or None.

    Handles the separate (``-m kiro_crew``) and attached (``-mkiro_crew``) spellings,
    scanning past other interpreter flags. The ``-c`` inline-program form has no
    positional subcommand token (the program builds its own argv), so it is left to
    the credential-mint import gate rather than matched here.

    *scan* is REQUIRED, and must be :func:`_self_module_flag_scan` of the same *tokens*.
    It is not optional-with-a-fallback on purpose: this function is called once per
    token by a loop over those tokens, so a caller that could omit the scan could
    silently reintroduce the quadratic this precompute exists to remove.  Requiring it
    makes that a type error instead of a performance regression nobody notices.
    """
    limit = len(tokens)
    j = scan.stops[i + 1]
    if j >= limit:
        return None
    if scan.norm[j] == "-m":
        nxt = scan.norm[j + 1] if j + 1 < limit else ""
        return j + 1 if _SELF_IMPORT_RE.search(nxt) else None
    return j  # attached -mkiro_crew


def _self_program_index(tokens: "list[str]", i: int, scan: "_SelfModuleScan") -> "int | None":
    """The argv index whose trailing operands the product CLI receives when the token
    at *i* launches it: *i* itself for the direct ``kirocrew`` form, or the module-name
    index for ``python -m kiro_crew``; else None.

    *scan* is threaded through to :func:`_self_module_name_index` and is required for
    the reason given there.
    """
    if _is_self_program(tokens[i]):
        return i
    if _PYTHON_PROGRAM_RE.match(_programs._program_basename(tokens[i])):
        return _self_module_name_index(tokens, i, scan)
    return None
