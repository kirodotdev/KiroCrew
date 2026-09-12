"""Shell text reader: tokenizer, quote and escape state machine, argv attribution.

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

Imports only ``vocabulary``, the layer below, for the product's own name: the
tiers depend on the reader, never the reverse.
"""

from __future__ import annotations

import bisect
import os
import re
import shlex
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.trust_patterns import ENV_ASSIGNMENT_RE

from .vocabulary import (
    _KILL_BY_NAME_PROGRAMS,
    _SELF_NAME_RE,
    _SELF_PROGRAM_RE,
    _SELF_PROGRAM_SPELLINGS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


#: Any one of these in a view means it is not a single plain command: it can
#: open a command, an expansion or a redirection, or chain to another command.
#: An ALLOWLIST of inert text, not a blocklist of opener spellings -- see
#: condition 2.  The separators are here for the PASS 1 view specifically: a
#: Pass 2 segment never contains one (the splitter consumed it), but the
#: whole-string view does, and an exception must not speak for a compound
#: command whose later stage is an interpreter
#: (``grep '<destructive>' payload.py | python``).
_SHELL_ACTIVE_CHARS = frozenset("$`(){}<>|;&\n\r")


# Used to *split* a command into independently-evaluatable segments.
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


# Shell glob metacharacters, and the concrete spellings a glob could expand to.  A
# glob in the program name (``kiro[c]rew``) is resolved by the shell BEFORE exec, so
# it has to be tested for expandability rather than compared literally.
_GLOB_CHARS_RE = re.compile(r"[\[\]?*{}]")


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


#: Interpreter names that accept ``-m <module>``. Versioned spellings (``python3``,
#: ``python3.12``) and the Windows launcher included; ``.exe`` is stripped by
#: ``_program_basename`` before this is applied.
_PYTHON_PROGRAM_RE = re.compile(r"\Apy(?:thon)?[0-9.]*(?:\.exe)?\Z")


#: Interpreter flags that consume the NEXT token as their operand. Their operand must be
#: skipped when scanning for ``-m <module>``, or it terminates the scan and the mint slips
#: through (``python -X dev -m kiro_crew token``). ``-c`` and ``-m`` are deliberately absent:
#: both END the option list, and `-m` is what this scan is looking for.
#:
#: LOWERCASE, because the floor runs over an already-lowercased command (`_is_credential_mint`
#: takes `text_lower`), so a `-X` in the operator's shell reaches this set as `-x`. Storing the
#: uppercase spelling made every separate-operand form match nothing — the bypass stayed open
#: while the ATTACHED spellings (`-Xdev`) passed, which is the shape of a fix that looks tested.
#: Python's real flags are case-sensitive (`-x` skips the first line, `-X` sets an
#: implementation option), so this over-matches `-x` slightly: `python -x -m kiro_crew token`
#: would skip `-m` as an operand and MISS. Guarded by also treating a bare `-m` as the marker
#: on the next iteration — see the loop.
_PYTHON_OPERAND_FLAGS = frozenset({"-x", "-w", "-q", "--check-hash-based-pycs"})


#: Interpreter flags that take an INLINE PROGRAM as their operand: ``-c`` a statement string,
#: ``-`` / no flag a stdin script. ``python -c "from kiro_crew.cli import main; main()" token``
#: mints the identical token as ``python -m kiro_crew token`` — the payload is one argv word
#: carrying the import name, so the ``-m`` marker scan never fires and the "not a flag ⇒ not
#: the module shape" bail treated the payload as a script name and returned False. Same escape,
#: one flag over.
_PYTHON_INLINE_PROGRAM_FLAGS = ("-c",)


def _glob_to_regex(pattern: str) -> str:
    """Translate a shell glob into a regex that matches what it could expand to."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            out.append(".")
            i = close + 1
            continue
        if ch == "{":
            # ``kiro{c..c}rew`` expands to the real name, so a brace group stands for
            # whatever it can produce -- same treatment as a bracket class.
            close = pattern.find("}", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            out.append(".*")
            i = close + 1
            continue
        if ch == "?":
            out.append(".")
        elif ch == "*":
            out.append(".*")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


# Programs whose ``-c`` argument is a shell script: its text is a COMMAND, so a
# self-protection check has to look inside it rather than treat it as an operand.
_NESTED_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"})


_NESTED_SHELL_VERBS = frozenset({"eval", "source", "."})


_ENV_SPLIT_PROGRAMS = frozenset({"env"})


# product name appearing in their argv is a mention, not an invocation:
# ``echo kirocrew token`` prints two words.
#
# This list is deliberately a DENYLIST of data consumers rather than an ALLOWLIST
# of executors, because the two fail in opposite directions.  Many commands pass
# their remaining argv to an executor -- ``ssh host …``, ``docker exec c …``,
# ``sudo``, ``env``, ``nohup``, ``timeout``, ``runuser``, ``chroot``, ``pkexec``,
# ``systemd-run``, ``nice``, ``xargs`` -- and enumerating THOSE means a forgotten
# entry is a silent BYPASS.  Enumerating data consumers instead means a forgotten
# entry is a false positive: annoying, visible, and safe.  So the default for an
# unrecognised program is "this could execute the name".
_DATA_CONSUMER_PROGRAMS = frozenset(
    {
        "echo",
        "printf",
        "print",
        "cat",
        "tac",
        "tee",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "sed",
        "awk",
        "cut",
        "tr",
        "sort",
        "uniq",
        "wc",
        "nl",
        "fold",
        "column",
        "comm",
        "diff",
        "strings",
        "jq",
        "yq",
        "base64",
        "md5sum",
        "sha256sum",
        "xxd",
        "od",
    }
)


# program in a run that ``shlex`` handed over as a single word.
_CONTROL_OPERATOR_RE = re.compile(r"[;&|\n]+")


# same glue-evasion as the empty-quote form (``ca""t`` -> ``cat``) that
# ``normalize_shell_command`` already undoes, but spelled with a substitution and
# placed MID-WORD, where a prefix-only strip never sees it.
_EMPTY_SUBST_RE = re.compile(r"\$\(\s*\)|`\s*`|\$\{\s*\}")


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
# ``awk``'s ``system()`` and pipe-to-command, and GNU ``sed``'s ``e`` flag.
_SCRIPT_EXECUTES_RE = re.compile(r"system\s*\(|\|\s*[\"']|\|&|print\s*\||\bclose\s*\(|/e\b|\be\s*$")


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
    command_disqualified: "bool | None" = None,
) -> bool:
    """True if *token* is an ARGUMENT of a command that treats arguments as data.

    ``echo <name> <verb>`` prints two words -- a mention, not an invocation.

    The exemption is refused in two cases:

    * the token itself carries a control operator (``echo foo;kirocrew>/tmp/x``).
      ``shlex`` splits on whitespace only, so such a token is attributed to the
      PRECEDING command while the part after the operator is a new command that
      really runs.
    * the command pipes into a shell or evaluator (``echo … | sh``), where the
      printed text is executed rather than displayed.

    Inheriting the exemption in either case would turn a precision fix into a
    bypass.
    """
    if index <= 0:
        return False
    if _CONTROL_OPERATOR_RE.search(token):
        return False
    # Command-level guards, hoisted into ``_data_consumer_command_disqualified``.
    # *command_disqualified* lets a caller iterating one fixed argv charge them
    # once; ``None`` means "compute them here", which is what every caller that
    # asks about a single token does, so their behaviour is unchanged.
    if command_disqualified is None:
        command_disqualified = _data_consumer_command_disqualified(tokens)
    if command_disqualified:
        return False
    return programs[index] in _DATA_CONSUMER_PROGRAMS


def _argv_programs(tokens: "list[str]") -> "list[str]":
    """For each token, the program name of the command that token belongs to.

    Walks the argv tracking command boundaries (``_ends_argv``) and skipping
    leading ``VAR=value`` assignments, which precede the program rather than being
    it.  Used to ask "what command is this name an argument OF?" -- the difference
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


_PRINTF_ESCAPES = (("\\n", " "), ("\\t", " "), ("\\r", " "), ("\\v", " "), ("\\f", " "))


# ``printf`` / ``$'…'`` numeric escapes: octal (``\\NNN``, ``\\0NNN``), hex
# (``\\xHH``) and Unicode (``\\uHHHH``, ``\\UHHHHHHHH``).
#
# The Unicode widths are EXACT and CASE-SENSITIVE, as bash defines them: ``\\u``
# consumes at most 4 hex digits and ``\\U`` at most 8, so ``$'\\u0072f'`` is ``r``
# followed by a literal ``f`` -- NOT a 5-digit code point.  Reading more digits than
# the spelling allows is a bypass, because the wrong character replaces the two the
# shell actually passes.  The pattern therefore carries no ``re.IGNORECASE`` (which
# would conflate the two widths) and spells its own classes; ``\\x`` keeps accepting
# either case, as it always did.
#
# This requires the caller to have preserved case: see ``_deny_segment_views``,
# which decodes BEFORE lowercasing for exactly this reason.
_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})"
    r"|u([0-9a-fA-F]{1,4})"
    r"|U([0-9a-fA-F]{1,8})"
    r"|0?([0-7]{1,3}))"
)


# ANSI-C quoting (``$'...'``) spells octal as ``\nnn`` -- one to three octal digits
# TOTAL, where a leading zero is simply one of the three.  The ``\0nnn`` form above
# (zero plus up to three more digits) belongs to ``echo -e``/``printf %b`` ONLY;
# sharing that pattern here consumed a fourth digit, so ``$'\06777'`` -- which bash
# reads as ``\067`` ('7') followed by the literal ``77``, i.e. ``777`` -- decoded to
# a single non-ASCII byte and the deny view diverged from what the shell runs.
# Group order matches _NUMERIC_ESCAPE_RE so
# ``_numeric_escape_code`` reads either match.
_ANSI_C_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})" r"|u([0-9a-fA-F]{1,4})" r"|U([0-9a-fA-F]{1,8})" r"|([0-7]{1,3}))"
)


def _escape_code_is_inert(code: int) -> bool:
    """True for a code point that must be left ENCODED rather than decoded.

    A NUL cannot appear in an argv the shell builds.  A LONE SURROGATE is refused
    for a second reason: it is not a character bash can pass either, and a decoded
    one would travel into the SEL audit record, whose JSON encoder raises on it --
    turning a denial into a crash.
    """
    return code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF


def _numeric_escape_code(match: "re.Match[str]") -> "int | None":
    """The code point an octal / hex / Unicode escape resolves to, or None.

    Split out so :func:`_numeric_escape_char` and :func:`_decode_ansi_c_body` cannot
    disagree about what a match means -- the body decoder has to recognise a NUL to
    truncate at it, and re-deriving that separately is how two code paths drift.

    Octal is masked to ONE BYTE, which is bash's semantics and was measured rather
    than assumed: ``$'\\555'`` is ``m`` (0o555 & 0xFF == 0x6D), ``$'\\777'`` is
    0xFF, and ``$'\\400'`` masks to a NUL.  Converting the full value instead gave
    ``$'r\\555'`` the character ``u``-breve where bash passes ``rm``, so
    ``$'r\\555' -rf /`` ran while the view matched nothing.
    """
    hex_digits, u4_digits, u8_digits, octal_digits = match.groups()
    digits = hex_digits or u4_digits or u8_digits
    try:
        return int(digits, 16) if digits else (int(octal_digits, 8) & 0xFF)
    except (TypeError, ValueError):  # pragma: no cover - the pattern admits only digits
        return None


def _numeric_escape_char(match: "re.Match[str]") -> str:
    """One decoded character for an octal, hex or Unicode escape."""
    code = _numeric_escape_code(match)
    if code is None or _escape_code_is_inert(code):
        return match.group(0)
    return chr(code)


def _decode_printf_escapes(text: str) -> str:
    """Turn literal ``\\n``-style escapes into whitespace.

    ``printf 'kirocrew token\\n' | bash`` carries the newline as two characters, so
    re-tokenizing the payload glues the escape onto the verb and the comparison misses.
    ``printf`` (and ``echo -e``) expand these before the shell sees them, so the payload
    is decoded the same way first.

    Numeric escapes are decoded too, not just the named ones: ``\\040`` and ``\\x20`` are
    both a SPACE, so leaving them literal reopens exactly the separator gap the named
    escapes closed, and ``\\x6b`` can spell a character of the program name itself.  What
    the shell will actually run is the decoded text, so the comparison is made against
    that.

    The Unicode forms (``\\uHHHH``, ``\\UHHHHHHHH``) are decoded for the same reason and
    were missing: ``$'\\u0074\\u006f\\u006b\\u0065\\u006e'`` is the same word as the
    ``\\x``-spelled form this already caught, so the argv-structural floors compared
    against an encoded string and a spelling of a self-protection verb slipped past
    while its hex twin was refused.
    """
    for esc, sub in _PRINTF_ESCAPES:
        text = text.replace(esc, sub)
    return _NUMERIC_ESCAPE_RE.sub(_numeric_escape_char, text)


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
_SHELL_WORD_BREAK = frozenset(" \t\n;&|()<>")


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
    return k < 0 or text[k] in ";&|(\n"


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
    imprecision. So ``case`` arms on any standalone word and ``esac`` disarms only
    in command position -- an ``esac`` passed to a command as an ARGUMENT would
    otherwise end the rule early and let the next paren close.

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
            if _word_at(text, off, "case"):
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


# TRUE shell command separators (NOT command-substitution boundaries). Used to
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
        return _resolve_function_aliases(
            _resolve_local_assignments(
                normalize_shell_command(_fold_line_continuations(text_lower))
            )
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
        # not a name character), so an appended PROGRAM word was invisible here: `F=fi;
        # F+=nd; $F <fenced> -exec cat {} +` ran a `find` this resolver never saw, while
        # its single-assignment twin `F=fin; ${F}d` -- closed in an earlier round --
        # denied (found in review). Seven spellings did it, including an empty initial
        # value, repeated appends, a quoted tail and an append feeding a braced use.
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
        joined_here: set[str] = set()
        nested = _nested_shell_payloads(tokens, allow_join=allow_join, joined_out=joined_here)
        for payload in list(nested) + _substitution_bodies(source):
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

# ANSI-C (``$'…'``) and locale (``$"…"``) quoting.  Both are QUOTING forms whose
# value the shell computes before the program sees it, so they are resolved as part
# of tokenization.  Matched on the RAW text rather than after ``shlex``, which is
# what makes it safe: ``shlex`` removes the quotes but leaves the ``$`` glued to the
# content, and at that point ``$'/'`` -> ``$/`` is indistinguishable from a variable
# reference like ``$HOME``, so stripping the ``$`` post-hoc would eat real variables.
# Requiring the quote character here means a bare ``$HOME`` never matches.
#
# The negated classes EXCLUDE the backslash, and that is a ReDoS fix, not a style
# choice.  With ``[^']`` a backslash could match either alternative -- ``\\.`` (two
# characters) or the class (one) -- the textbook ambiguous quoted-string pattern, so
# an unterminated ``$'`` followed by a run of backslashes forces the engine through
# ~1.618**n tilings of that run.  This regex runs inside the PreToolUse gate on the
# full, uncapped command, so that is a hang, not a slowdown (measured: 9 ms at 24
# backslashes, growing ~1.6x per character).  Excluding the backslash makes the
# alternation unambiguous -- a backslash is always consumed by ``\\.`` -- while
# accepting exactly the same language.
_ANSI_C_QUOTE_RE = re.compile(r"\$'((?:\\.|[^'\\])*)'|\$\"((?:\\.|[^\"\\])*)\"", re.DOTALL)

# Single-character ANSI-C escapes that stand for a LITERAL character.  These are
# the ones bash resolves and a matcher must therefore see resolved: without them
# ``$'rm -rf \"/\"'`` keeps its backslashes and the rule does not match, while bash
# passes the plain quotes.
_ANSI_C_LITERAL_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "?": "?"}
# Escapes that stand for a control character.  Mapped to a SPACE rather than the
# character itself, which is what ``_decode_printf_escapes`` has always done for
# this family: the value of resolving them here is that a token boundary appears
# where the shell puts one, and a literal control byte in a matched view would
# only travel into the audit record.  Deliberate, and the reason this decoder is
# not simply "what bash produces".
_ANSI_C_SPACE_ESCAPES = frozenset("abefnrtvE")


def _decode_ansi_c_body(body: str) -> str:
    """Resolve the escapes inside one ``$'…'`` body, in a SINGLE left-to-right pass.

    One pass is the whole point.  Sequential ``str.replace`` calls let one
    substitution's OUTPUT be re-read as another's input: ``$'\\\\n'`` is an escaped
    backslash followed by the letter ``n`` (two characters), but a chain that
    resolves ``\\\\`` first and then looks for ``\\n`` collapses it to whitespace and
    invents a separator bash never passed.  Consuming each escape atomically here
    makes that impossible -- the same failure mode as the Unicode-width guess this
    file already carries a note about.

    Numeric forms keep bash's exact widths (``\\xHH``, ``\\nnn`` octal, ``\\uHHHH``,
    ``\\UHHHHHHHH``) and the inert guard, so a NUL or lone surrogate stays encoded.
    An unrecognised escape keeps both characters, as bash does.
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_LITERAL_ESCAPES:
            out.append(_ANSI_C_LITERAL_ESCAPES[nxt])
            i += 2
            continue
        if nxt in _ANSI_C_SPACE_ESCAPES:
            out.append(" ")
            i += 2
            continue
        if nxt == "c" and i + 2 < n and body[i + 2].isascii():
            # ``\cX`` is a CONTROL character, and ``\cI`` is a TAB -- so
            # ``bash -c $'rm\\cI-rf /'`` hands the inner shell a tab-separated
            # ``rm -rf /`` and it runs (measured,
            # the inner shell does split on it).  The mapping is MEASURED rather
            # than derived: ``ord(upper(X)) & 0x1F``, with ``?`` special-cased to
            # 0x7F -- an XOR-0x40 guess gets ``\\c0`` wrong (bash gives 0x10, not
            # ``p``).  The result is always a control character, so it takes the
            # same normalization to a SPACE as the named family above, which is
            # what puts a token boundary where the shell puts one.
            #
            # Restricted to a single ASCII character, because ``str.upper()`` is
            # not length-preserving outside it: ``"ß".upper()`` is ``"SS"``, and
            # ``ord`` of that raised ``TypeError`` straight out of the permission
            # gate on ``echo $'\\cß'`` -- a crash where a security decision belongs
            # (also BLOCKING, same lane).  A non-ASCII target falls through to the
            # unrecognised branch and keeps both characters, as bash does for a
            # spelling it does not define.
            target = body[i + 2]
            code = 0x7F if target == "?" else (ord(target.upper()) & 0x1F)
            if code == 0:
                # ``\c@`` is a NUL, and bash TRUNCATES the word there -- see the
                # numeric branch below for the measurement.
                return "".join(out)
            out.append(" ")
            i += 3
            continue
        match = _ANSI_C_NUMERIC_ESCAPE_RE.match(body, i)
        if match:
            # A NUL TRUNCATES the word -- bash cannot place one in an argv, and what
            # it does instead is stop there.  Measured on every spelling that can
            # reach zero: ``$'AA\\0junk'``, ``$'AA\\400junk'``, ``$'AA\\x00junk'``,
            # ``$'AA\\u0000j'`` and ``$'AA\\c@junk'`` all yield ``AA``, and
            # ``$'\\0AA'`` yields the empty word.  Leaving the escape encoded instead
            # was a bypass: ``$'dd\\0junk' if=/dev/zero of=/dev/sda`` ran the
            # destructive command while the view held ``dd\\0junk if=`` and matched
            # nothing.  The OTHER inert codes -- out
            # of range, lone surrogate -- keep the escape rather than truncating,
            # because bash does not produce them at all and guessing what it would do
            # is what the measurements above exist to avoid.
            numeric_code = _numeric_escape_code(match)
            if numeric_code == 0:
                return "".join(out)
            out.append(_numeric_escape_char(match))
            i = match.end()
            continue
        # Unrecognised: bash keeps the backslash and the character.
        out.append(body[i : i + 2])
        i += 2
    return "".join(out)


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
