"""The product's own command floors: the subcommands it must never be driven to run.

The termination floor (a kill whose target is the product, by pid or by name, through
``pkill``-style programs and computed words), the restart, update,
gateway-restart and destructive-cloud subcommand floors, the dev-mode out-of-root
confirmation, and the credential-mint predicates, which follow a payload through the
carriers that can hand it to an interpreter -- an inline program, a here-document, a
standard-input redirection, a nested shell -- and ask whether the product's package is
imported in it. Each one recognises the product as the argv's own program and the action
as its own subcommand, so the name appearing in a path or a message is not a verdict. A
new floor for one of the product's own subcommands goes here.

The frame walk (``_self_token_frames``, ``_shell_payload_sources``) and the static
reader of a flat command substitution (``_static_substitution_output``) are reused by
the ssh self-target floor above this module.
``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the shell reader,
the inline-payload checks and the shared name vocabulary below it. It also reuses
``argv_rules._program_args`` -- the shared execution-prefix wrapper table -- so the
git-pager floor keeps no second, divergent wrapper list (a shorter inline copy had let
``doas`` / ``pkexec`` / ... before ``git grep -O`` slip past); ``argv_rules`` is not an
argv-floor owner and does not import this module, so the reuse adds no cycle. The
git-global value options cannot be imported the same way: ``argv_floor_git`` owns
``_GIT_ARG_FLAGS`` but sits ABOVE this module in ``argv_floor._OWNERS`` (the base layer),
and the layer rule forbids importing upward, so the set is MIRRORED here and a test
exercises every ``argv_floor_git._GIT_ARG_FLAGS`` entry through this floor so it cannot
drift.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# The reader names a test instruments on the facade are reached as attributes of
# the reader module rather than bound here at import: the facade mirrors a patch
# onto the owning module's namespace, so a name bound into THIS namespace would
# keep resolving the unpatched object and the instrument would count nothing.
from . import shell_normalizer as _shell_normalizer
from .inline_payload import (
    _INLINE_DYNAMIC_EXEC_RE,
    _decoded_b64_literal_sources,
    _has_self_importing_inline_program,
)
from .shell_normalizer import (
    _PYTHON_INLINE_PROGRAM_FLAGS,
    _PYTHON_OPERAND_FLAGS,
    _PYTHON_PROGRAM_RE,
    _REDIRECT_START_RE,
    _SHELL_WRAPPER_CHARS,
    _argv_programs,
    _backtick_closer,
    _data_consumer_exempt,
    _debracket,
    _decode_printf_escapes,
    _ends_argv,
    _glob_could_expand_to,
    _is_mint_verb,
    _is_self_program,
    _iter_shell_chars,
    _matching_close_paren,
    _redirect_consumes_next,
    _resolve_param_defaults,
    _shell_join_continuations,
    _shell_payload_walk,
    _strip_redirect,
    _substitution_bodies,
    _substitution_depth_delta,
)
from .vocabulary import _KILL_BY_NAME_PROGRAMS, _SELF_NAME_RE

# ── Self-protection floor (argv-structural, not a regex) ──
# The two self-protection rules below are enforced by TOKENIZING the command
# rather than by matching its raw text.  A raw-string regex cannot decide these:
# the gap between the product name and the verb has to step over ordinary shell
# noise (a quoted verb, global flags, a redirect), but every character class wide
# enough to do that also steps over a filesystem path -- and "a path that
# contains the product name" is exactly the false positive these rules exist to
# stop.  Tokenizing resolves quoting and redirection BEFORE matching, so both
# sides can be exact.  See ``_is_credential_mint`` / ``_is_self_kill``.

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


def _is_self_module_invocation(tokens: list[str], i: int) -> bool:
    """True if ``tokens[i]`` is a ``python`` that runs the product IN-PROCESS.

    ``python -m kiro_crew token`` mints exactly the same signed dashboard token as
    ``kirocrew token``, but its argv PROGRAM is the interpreter, so neither the
    command-position regex (which matches ``kiro[-.]?crew``, not the underscored import
    name) nor ``_is_self_program`` sees it. The escalation is the point: that token
    authenticates every gateway route, including the ops autonomy-ceiling PUT, so the
    bypass let a prompt-injected agent raise its own security ceiling.

    Matched structurally, like the rest of the floor: an interpreter, then ``-m``
    (possibly after other interpreter flags), then the module. ``-m`` must be a separate
    token — ``python -mkiro_crew`` is also valid, so that spelling is checked too.

    ``-c`` is the SAME escape one flag over, and is matched here for that reason:
    ``python -c "from kiro_crew.cli import main; main()" token`` reaches the identical mint
    with the import name buried in an inline-program payload. The two forms differ only in
    how the interpreter is told to import the package, so they cannot be gated separately —
    an "anything that is not a flag means this is not the module shape" bail reads the
    payload as a script name and returns False.

    Interpreter flags that take a SEPARATE OPERAND (``-X dev``, ``-W ignore``, ``-Q new``)
    have their operand skipped. Stopping at the first token that does
    not begin with ``-`` bails on ``dev`` in ``python -X dev -m kiro_crew token`` and lets
    the mint through — the bypass this whole function exists to close, one flag
    deeper. Modelling which flags consume an operand is the fix; "stop at the
    first non-flag" is not expressible as a heuristic here, because an operand and a script
    path look identical.
    """
    if not _PYTHON_PROGRAM_RE.match(_shell_normalizer._program_basename(tokens[i])):
        return False
    skip_next = False
    inline_program_next = False
    for later in tokens[i + 1 :]:
        stripped = _shell_normalizer._normalize_operand(later).strip("\"'")
        if inline_program_next:
            # The payload of a `-c`: an inline program naming the package IS an import of it.
            # Checked before the flag logic because the payload is arbitrary text that may
            # begin with anything, including a `-`.
            #
            # Matched on the RAW token, not `stripped`: `_normalize_operand` truncates at the
            # first control operator, which is right for an operand the shell will split but
            # wrong for a quoted Python program whose `;` is a statement separator. Normalising
            # `"import sys; ...; from kiro_crew.cli import main"` down to `import sys` hid the
            # import and made this return False for a payload that plainly runs our code.
            if _SELF_IMPORT_RE.search(later.strip(_SHELL_WRAPPER_CHARS)):
                return True
            inline_program_next = False
            continue
        if skip_next:
            skip_next = False
            # `-m` is never a flag's operand: `python -x -m mod` passes `-m mod` to the
            # interpreter, so a token that IS the marker must be honoured rather than eaten.
            # This is what keeps the deliberate `-x` over-match above from opening a hole.
            if stripped != "-m" and not stripped.startswith("-m"):
                continue
        if stripped == "-m":
            continue
        if stripped.startswith("-m") and stripped[2:] in _SELF_MODULE_SPELLINGS:
            return True
        if stripped in _SELF_MODULE_SPELLINGS:
            return True
        if stripped in _PYTHON_INLINE_PROGRAM_FLAGS:
            inline_program_next = True
            continue
        # `-c<payload>` attached, the one-token spelling of the same thing. Raw for the same
        # truncation reason as the separate operand above.
        _raw = later.strip(_SHELL_WRAPPER_CHARS)
        if len(_raw) > 2 and _raw[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
            if _SELF_IMPORT_RE.search(_raw):
                return True
            continue
        if stripped in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        # An attached operand (`-Xdev`, `-Wignore`) needs no skip: it is one token.
        if len(stripped) > 2 and stripped[:2] in _PYTHON_OPERAND_FLAGS:
            continue
        # Only interpreter FLAGS may sit between; anything else means this is neither the
        # `-m <product>` nor the `-c <payload>` shape (`python script.py`).
        if not stripped.startswith("-"):
            return False
    return False


def _is_kill_by_name_program(token: str) -> bool:
    """True if *token* invokes ``pkill``/``killall``, including a globbed spelling."""
    base = _shell_normalizer._program_basename(token)
    if base in _KILL_BY_NAME_PROGRAMS:
        return True
    return _glob_could_expand_to(base, _KILL_BY_NAME_PROGRAMS)


# ``env -S`` splits its argument into a command and execs it.
# Programs that treat their arguments as DATA rather than executing them, so the
# Control operators that end one command and begin another.  Serves to find the
# An EMPTY substitution expands to nothing, so ``p$()kill`` runs ``pkill`` -- the
# An OUTPUT redirect. Two small sets, enumerated from the shells' own grammars rather
# The same descriptor vocabulary, widened to INPUT redirects and process
# ``X=kirocrew; $X token`` assigns the program name to a variable and invokes it


def _quoted_assignment_scripts(text_lower: str) -> "list[str]":
    """Each distinct quoted script, at any depth of *text_lower*, that opens with an assignment.

    The assignment resolver splits such a carrier payload at its inner operator (the
    test ``_split_glued_operators`` applies, reused here), so the shared walk only sees
    the script's first statement.  Read from the unresolved tokens, each script is a
    command line of its own, where its assignments resolve.
    Once per text inside one ``sharing_views`` call; each caller gets its own list.
    """
    from .shell_commands import _CALL_VIEWS  # local: the facade serves every module name

    memo = _CALL_VIEWS.get()
    key = ("quoted_assignment_scripts", text_lower)
    if memo is not None and key in memo:
        return list(memo[key])
    n = _shell_normalizer
    found = [text_lower]
    for text in found:  # extended while it runs, so a script's own scripts count
        for source in _shell_payload_sources(text) if "=" in text else ():
            tokens = n._self_tokens(source, resolve=False) if "=" in source else []
            for payload in n._nested_shell_payloads(tokens, allow_join=False):
                script = _decode_printf_escapes(payload)
                if (
                    n._LOCAL_ASSIGN_RE.match(payload)
                    and n._CONTROL_OPERATOR_RE.search(payload)
                    and len(script) < len(source)
                    and script not in found
                ):
                    found.append(script)
    if memo is not None:
        memo[key] = found[1:]
    return found[1:]


def _self_frames(text_lower: str) -> "list[tuple[str, list[str]]]":
    """The shared walk plus the walk of every quoted script in it that opens with an
    assignment, read whole so the assignment resolves as it does when typed directly.
    Once per text inside one ``sharing_views`` call, like the walk itself."""
    from .shell_commands import _CALL_VIEWS  # local: the facade serves every module name

    memo = _CALL_VIEWS.get()
    key = ("self_frames", text_lower)
    frames = None if memo is None else memo.get(key)
    if frames is None:
        frames = _shell_payload_walk(text_lower)
        seen = {source for source, _tokens in frames}
        for script in _quoted_assignment_scripts(text_lower):
            fresh = [frame for frame in _shell_payload_walk(script) if frame[0] not in seen]
            seen.update(frame[0] for frame in fresh)
            frames += fresh
        if memo is not None:
            memo[key] = frames
    return [(source, list(tokens)) for source, tokens in frames]


def _self_token_frames(text_lower: str) -> "list[list[str]]":
    """The command's own argv plus the argv of every nested shell payload."""
    return [tokens for _source, tokens in _self_frames(text_lower)]


def _shell_payload_sources(text_lower: str) -> "list[str]":
    """*text_lower* plus the source text of every nested shell payload in it."""
    return [source for source, _tokens in _shell_payload_walk(text_lower)]


# ── Self-protection floor short-circuit (perf) ──
# The floor predicates below re-tokenize the command and descend every nested
# shell payload (`_self_token_frames`), which is where the cost of the deny
# scan concentrates: it scales with NESTING COMPLEXITY, and each `is_denied`
# call runs the descent three times (mint once, kill twice). The common tool
# call — a tool name plus a path — can never fire either predicate, so the
# descent is pure waste there.
#
# The gate is a NECESSARY condition, deliberately wider than a raw
# `_SELF_NAME_RE` search. That narrower gate is UNSOUND: the
# predicates fire on inputs whose raw text never matches `kiro[-.]?crew` —
# `python -m kiro_crew token` (the underscored import spelling), `[k]irocrew
# token` (one-char bracket class), `kiro$()crew` (empty substitution),
# `kiro${x:-crew}` (parameter default), `bash -c "\x6birocrew token"` (printf
# escapes), `kiro?rew` (glob the shell expands before exec), and a `-c`
# payload reaching the CLI through `exec`/`b64decode` with no name at all.
# Every one of those is denied by the floor, so a gate that skipped them would
# be a real bypass, not an optimization.
#
# Sound formulation: the floor can only fire if, after the normalizations the
# predicates themselves apply (shlex quote-stripping, `_debracket`,
# `_resolve_param_defaults`, `_EMPTY_SUBST_RE`, `_decode_printf_escapes`,
# `_glob_could_expand_to`), the text yields a self name/module — or an inline
# dynamic-exec primitive stands in for it. Each normalization needs specific
# MACHINERY characters present in the raw text, so the union below is a
# superset of every firing path:
#   * the literal name in any spelling (`kiro[-._]?crew` — underscore included
#     for the module/import form, which `_SELF_NAME_RE` deliberately omits);
#   * any machinery character that lets a normalization synthesize the name or
#     a program spelling: glob/brace chars (`? * [ ] { }` — `_glob_could_expand_to`
#     admits e.g. `k*w` for the program AND `*kill` for the kill verbs),
#     `$` (substitutions, parameter defaults, ANSI-C quoting), backticks, and
#     `~` (tilde expansion — the kill predicates expanduser their targets, so
#     `pkill -f ~` IS a self-kill whenever $HOME lies under the product tree,
#     with no name and no other machinery in the raw text);
#   * printf numeric escapes (`\xHH`, `\NNN`) that can spell arbitrary
#     characters once `_decode_printf_escapes` runs on a nested payload;
#   * the dynamic-exec markers `_inline_payload_reaches_cli` accepts in place
#     of a literal import — checked on the raw text AND on the quote-stripped
#     text, because empty-quote glue hides the verb exactly as it hides the
#     name (`python -c "ex""ec(...)"` carries no name and no other machinery,
#     yet the floor denies it);
#   * the literal name once quotes, backslashes and a `\`+newline continuation
#     come off (`k""iro""crew`, `ki\rocrew`, `kiro\`+nl+`_crew` — shlex folds them).
# When none of these is present, no predicate can return True, so the descent
# is skipped. False positives (e.g. any `$VAR` in a command) merely fall back
# to the full scan — the safe direction.
#
# Matched WITHOUT re.IGNORECASE on purpose: the floor's own contract is that
# callers pass already-lowercased text (`is_denied` lowercases once), and the
# predicates' regexes are lowercase-only too.
_SELF_FLOOR_NAME_HINT_RE = re.compile(r"kiro[-._]?crew")


_SELF_FLOOR_MACHINERY_RE = re.compile(r"[?*\[\]{}$`~]|\\x[0-9a-f]|\\0?[0-7]{1,3}")


_SELF_FLOOR_QUOTE_JUNK_RE = re.compile(r"[\"'\\\\]")


def _self_floor_can_fire(text_lower: str) -> bool:
    """Cheap O(n) necessary condition for the self-protection floor predicates.

    Returns False only when ``_is_credential_mint`` and ``_is_self_kill`` are
    PROVABLY unable to fire on *text_lower*, so both can skip the recursive
    payload descent. Any "maybe" answers True and runs the full scan — the
    gate can over-trigger but never under-trigger (see the block comment above
    for the case analysis).
    """
    if _SELF_FLOOR_NAME_HINT_RE.search(text_lower):
        return True
    if _SELF_FLOOR_MACHINERY_RE.search(text_lower):
        return True
    if _INLINE_DYNAMIC_EXEC_RE.search(text_lower):
        return True
    # Quote/backslash glue is removable by the tokenizer, so the name AND the
    # dynamic-exec verb may only materialize once those come off:
    # `k""iro""crew token`, `"kirocrew" token`, `python -c "ex""ec(...)"`.
    # Both must be re-checked here -- testing only the name would let a glued
    # `exec(` payload skip the descent while the floor still denies it.
    stripped = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", _shell_join_continuations(text_lower))
    if _SELF_FLOOR_NAME_HINT_RE.search(stripped):
        return True
    return bool(_INLINE_DYNAMIC_EXEC_RE.search(stripped))


def _is_credential_mint(text_lower: str, *, raw_text: "str | None" = None) -> bool:
    """True if *text_lower* invokes the ``kirocrew token`` credential mint.

    The mint prints a signed dashboard access URL, so it is the escalation path
    this rule exists to close.  Matched on argv, which is what makes the
    ordinary shell forms unbypassable: ``kirocrew "token"`` (quoted verb),
    ``kiro""crew token`` (empty-string concatenation), ``kirocrew -v --no-jail
    token`` (global flags) and ``kirocrew >/tmp/out token`` (bash accepts a
    redirection anywhere in a simple command) all tokenize to an argv whose
    program is the product CLI and one of whose words is exactly ``token``.

    Does NOT match the word appearing in a path or another program's arguments:
    ``cd /workplace/user/kirocrew-wt-x && pytest test/test_token_auth.py`` has no
    argv whose PROGRAM is the CLI, and ``kirocrew doctor | grep token`` puts the
    word in ``grep``'s argv, not the CLI's.
    """
    # Perf short-circuit: the tokenize-and-descend below is the deny
    # scan's dominant cost, and it cannot produce a hit when the gate says the
    # input carries neither a self name nor the machinery to synthesize one.
    if not _self_floor_can_fire(text_lower):
        return False
    # Base64 is case-sensitive and the floor reads a lower-cased view, so the literals are
    # decoded from the command AS SUBMITTED when the caller has it; a lower-cased-only
    # caller gets literals that decode to nothing and so fold in nothing.  Each decoding
    # arrives paired with the literal it came from, so a payload borrows a decoding only
    # of bytes IT carries -- one command's decoder call does not lend its result to the
    # next command's payload.
    submitted = raw_text if raw_text is not None else text_lower
    decoded_literals = _decoded_b64_literal_sources(submitted)
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        # The command-level half of ``_data_consumer_exempt`` reads only *tokens*, so its
        # answer is the same for every token in this frame.  Held here and computed at
        # most once per FRAME rather than once per trigger token: that half contains an
        # O(len(tokens)) sweep, so re-asking it per trigger token makes the floor
        # quadratic in the trigger count.  ``None`` until the first trigger token needs
        # it, so a frame carrying none pays nothing.
        disqualified: "bool | None" = None
        for i, token in enumerate(tokens):
            # AN INLINE PROGRAM THAT NAMES THE MINT SURFACE IS DENIED WITHOUT NEEDING THE VERB
            # AS AN ARGV WORD, and it is checked FIRST because it does not depend on the
            # self-program/module gate below. Elsewhere the verb is the trigger, since
            # ``kirocrew doctor`` is legitimate and only ``kirocrew token`` mints -- reasoning
            # an inline program does not survive: ``-c`` and stdin both run arbitrary Python
            # with the interpreter's authority, so the payload can BUILD the verb rather than
            # pass it (``sys.argv.append('token')`` names no ``token`` word) or keep the whole
            # program off argv on stdin. So the gate is what the payload NAMES, and a payload
            # that only mentions the package as a path is untouched.
            if _has_self_importing_inline_program(tokens, i, decoded_literals):
                return True
            # Either the console script IS the program, or an interpreter runs the product as
            # a MODULE (`python -m kiro_crew ... token`). The module form mints the identical
            # token, and its argv program is the interpreter, so `_is_self_program` alone
            # missed it — the underscored import name is not a console-script spelling either,
            # so the regex tier could not see it.
            if not _is_self_program(token) and not _is_self_module_invocation(tokens, i):
                continue
            # The name is an ARGUMENT of a command that treats arguments as data
            # (``echo <name> <verb>`` prints two words) -- a mention, not a mint.
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
            # A program token ending with an operator (``kirocrew;``) is NOT skipped: the
            # quotes are already off these tokens, so ``'/tmp/kirocrew;' token`` (a symlink
            # literally so named) reads like a closed argv, and skipping the verb scan on
            # it is the mint.  Over-reading the next command's words is the safe direction.
            # Check each argument for the verb BEFORE testing whether it ends the
            # argv, then stop.  Order matters for the same reason it does in the kill
            # scan: ``if true; then <name> <verb>; fi`` hands the verb over as
            # ``<verb>;`` -- one token that both IS the verb and carries the boundary,
            # so testing the boundary first discards the very argument that names it.
            depth = 0
            inline_payload_next = False
            for later in tokens[i + 1 :]:
                if _is_mint_verb(later):
                    return True
                # The operand of `-c` is a quoted PROGRAM, so its `;` is data, not a command
                # separator. Letting `_ends_argv` see it ends the scan on the payload of
                # `python -c "from kiro_crew.cli import main; main()" token` — one token before
                # the verb — so the mint is permitted even though the interpreter check has
                # already matched.
                #
                # This skip is not what protects the `-c` form: a payload that imports
                # the CLI is denied above, before this loop runs, because it can construct the
                # verb internally. The skip covers the remaining case —
                # a payload that does NOT import us, followed by a real `token` argument.
                if inline_payload_next:
                    inline_payload_next = False
                    continue
                _operand = _shell_normalizer._normalize_operand(later).strip("\"'")
                if _operand in _PYTHON_INLINE_PROGRAM_FLAGS:
                    inline_payload_next = True
                    continue
                # `-c<payload>` attached: the payload is already inside this token, so it is
                # data in the same way — skip it without expecting a following one.
                if len(_operand) > 2 and _operand[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
                    continue
                # A separator NESTED in a command substitution is part of that
                # substitution, not the end of this argv: ``<name> $(true; echo <verb>)``
                # is still one command.  Only a top-level separator ends the scan.
                depth += _substitution_depth_delta(later)
                if depth <= 0 and _ends_argv(later):
                    break
                depth = max(depth, 0)
    return False


def _static_substitution_output(body: str) -> str:
    """The word a command substitution STATICALLY expands to, else a marker.

    ``$(echo kill)`` and ``$(printf kill)`` put the verb in COMMAND position
    through their output; the undecoyed spelling is already detected by the
    token walk (``kill)`` strips to a ``kill`` basename), so only the decoyed
    combination slips -- the raw window has no anchor for it (bash-measured).
    Resolution is deliberately narrow: ``echo``/``printf`` with a literal first
    operand, flags and format words skipped.  Anything dynamic returns
    ``"\x00"``, a word no program name matches, so an unresolvable generator can
    only under-anchor (miss goes to the remainder ledger), never conjure one.
    """
    tokens = body.split()
    if tokens and _shell_normalizer._program_basename(tokens[0]) in {"echo", "printf"}:
        for arg in tokens[1:]:
            operand = _shell_normalizer._normalize_operand(arg)
            if operand.startswith("-") or "%" in operand:
                continue
            return operand
    return "\x00"


# An assignment word (``k=kill``): bash only honours these BEFORE the first
# non-assignment word of a command, and their value is visible to LATER
# commands only (expansion happens before the assignment takes effect).
_RAW_ASSIGNMENT_RE = re.compile(r"([a-z_][a-z0-9_]*)=(.*)\Z")


def _kill_prefix_keeps_anchor(words: "list[tuple[str, bool]]", word: "list[str]") -> bool:
    """True when a glued substitution must NOT cost the word its kill anchor.

    ``kill$(B)`` runs the program ``kill`` whenever B expands to NOTHING at
    runtime -- ``$(:)``, ``$(true)``, any silent command -- which no static
    scan can decide, so the anchor decision fails toward detection: a FIRST
    word whose pre-glue prefix is exactly ``kill`` keeps its anchor
    (bash-measured: ``kill$(:) $(pgrep -f <name>)`` kills).  Only the first
    word, because the program position is what makes
    the prefix a program: ``echo kill$(printf x) $(pgrep -f <name>)`` hands
    every ``kill...`` word to echo as data, and eating the anchor there is
    what keeps that spelling allowed.  The glued word's OWN body sits at the
    anchor's index, inside the forward bound, so ``kill$(pgrep -f <name>)``
    -- whose program is ``kill<pids>``, not ``kill`` -- still attributes
    nothing from its glue.
    """
    return not words and _shell_normalizer._program_basename("".join(word)) == "kill"


def _bare_kill_raw_bodies(source: str) -> "list[str]":
    """Substitution bodies inside a bare ``kill``'s own argv, read from the RAW text.

    The token walk in :func:`_is_self_kill` bounds the same window with
    :func:`_substitution_depth_delta`, which counts parens on tokens
    ``normalize_shell_command`` has already stripped the quotes from -- so by the
    time the counter runs, a QUOTED close-paren is indistinguishable from a real
    closer and it ends the window early: ``kill $(printf ')' ; pgrep -f
    kirocrew)`` scored the quoted paren as depth -1, cut the argv at the ``;``,
    and dropped the ``pgrep`` clause that names the target -- while bash, whose
    substitution scan is quote-aware, runs that ``pgrep`` (measured).

    The raw text still HAS the quotes, so the window is re-derived here from the
    same quote-aware machinery the extractor uses (:func:`_iter_shell_chars`
    for the walk, :func:`_matching_close_paren` for each span): a separator
    splits a segment only OUTSIDE quotes and OUTSIDE any substitution span, and
    a body is attributed only when it sits AFTER a top-level word that resolves
    to ``kill`` in the SAME segment.  Both bounds carry weight: the segment
    bound keeps ``kill 123; echo $(cat /tmp/kirocrew)`` allowed (the
    substitution belongs to the ``echo``), and the forward bound keeps
    ``LOG=$(ls /tmp/kirocrew.log) kill 4242`` allowed (the substitution
    precedes the kill, so it is an environment word's value, not the kill's
    operand) -- the exact false positive the token walk's own scoping replaced.

    Three quoting rules are load-bearing, each bash-measured (pre-push review):

    * A substitution body is parsed in its OWN NEUTRAL quote context, because
      that is how bash reads ``$( )`` -- a ``$(`` inside double quotes closes at
      an interior ``)`` even though the outer quote is still open.  The walk
      then RESUMES with the outer quote state it carried into the opener, so
      ``echo "$(date)" ; kill $(...)`` keeps its ``;`` as a real separator and
      the kill segment is still scanned.
    * A backtick closer is found through an escape-skipping scan: within
      backticks a backslash escapes ``\\``` and the escaped backtick is DATA,
      so taking it as the closer would truncate the body before the clause
      that names the target.
    * ``&>`` / ``&>>`` (and the trailing ``2>&1`` form) are redirects of the
      SAME simple command, not separators -- splitting there discarded the
      ``kill`` word before its substitution was attributed.

    A word GLUED to a substitution (``kill$(x)``, ``LOG=$(x)``) is never the
    bare ``kill`` this scan attributes to: bash joins the expansion into the
    word, so the program it runs is not the literal word prefix.  Glued words
    are flagged and excluded from the kill match, which keeps
    ``echo kill$(printf kirocrew)`` -- where ``kill...`` is an argument of
    ``echo`` -- out of the deny set.

    Words collect what the shell PASSES, not what the operator typed: a quote
    character that is quote SYNTAX (an opener or closer -- the state machine's
    own transitions say which) is dropped, while a quote character that is DATA
    (inside the other quote type, or escaped) is kept.  Without that,
    ``k''ill`` reaches the comparison spelled with its splice and the kill is
    missed (bash-measured: the spliced spelling runs
    ``kill``).  A syntax quote still OPENS a word -- ``''#`` is the word ``#``,
    not a comment -- which the ``open_word`` flag carries.

    An UNPROVEN span (parens that never balance) takes the whole remainder as
    the body and ends the walk.  That cannot under-detect: bash cannot execute
    past an unterminated substitution either (the whole line is a syntax
    error), so there is no later command to lose -- while the remainder still
    reaches the name search attributed to the CURRENT segment, which is what
    keeps the decoyed-and-unbalanced spelling detected.

    A top-level ``#`` starting a word begins a comment, which ends at the next
    newline; the skip lands ON that newline so the segment boundary it carries
    is still honoured.

    This pass is a UNION with the token walk, never a replacement: the tokens
    carry resolutions the raw text does not (``p=$(pgrep -f kirocrew); kill $p``
    resolves ``$p`` at tokenization) and the raw text carries the quoting the
    tokens lost.  Keeping both is what guarantees no spelling either half
    detects is dropped.
    """
    bodies: list[str] = []
    words: list[tuple[str, bool]] = []  # (word, glued-to-a-substitution)
    tagged: list[tuple[int, str]] = []  # (index of the word the body belongs to, body)
    word: list[str] = []
    glued = False
    open_word = False  # a word has begun, even if only as quote syntax (``''``)

    def end_word() -> None:
        nonlocal glued, open_word
        if word:
            words.append(("".join(word), glued))
            word.clear()
        glued = False
        open_word = False

    aliases: dict[str, str] = {}

    def resolves_to_kill(w: str) -> bool:
        # The literal spelling, or a variable an EARLIER command assigned the
        # verb to: ``k=kill; $k $(...)`` reaches this walk spelled ``$k``,
        # while the token walk sees it resolved -- so the decoyed alias
        # spelling slips both union halves (bash-measured).  Both ``$k`` and
        # ``${k}`` count; the value check goes through the same basename read as
        # the literal.
        if _shell_normalizer._program_basename(w) == "kill":
            return True
        if not w.startswith("$"):
            return False
        name = w[1:]
        if name.startswith("{") and name.endswith("}"):
            name = name[1:-1]
        return _shell_normalizer._program_basename(aliases.get(name, "")) == "kill"

    def end_segment() -> None:
        end_word()
        # Anchor selection BEFORE recording this segment's assignments: bash
        # expands ``$k`` before the same command's ``k=...`` takes effect, so
        # ``k=kill $k ...`` must not see its own assignment.
        kill_at = next(
            (k for k, (w, g) in enumerate(words) if not g and resolves_to_kill(w)),
            None,
        )
        if kill_at is not None:
            bodies.extend(body for idx, body in tagged if idx > kill_at)
        # Only the assignment PREFIX is real: a ``k=kill`` in argument
        # position (``echo k=kill``) assigns nothing, and recording it would
        # let a later ``$k`` conjure a kill anchor out of printed text.
        for w, _g in words:
            assignment = _RAW_ASSIGNMENT_RE.match(w)
            if assignment is None:
                break
            aliases[assignment.group(1)] = assignment.group(2)
        words.clear()
        tagged.clear()

    def record_body(body: str) -> None:
        # A body glued onto an open word belongs to THAT word's index; a body
        # starting a word of its own sits at the next index.  Either way the
        # forward bound above compares against the kill word's index.
        tagged.append((len(words), body))

    i = 0
    n = len(source)
    state = 0
    ansi = False
    while i < n:
        jumped = False
        for step in _iter_shell_chars(source[i:], state, ansi):
            off = i + step.offset
            ch = step.char
            escaped = len(step.text) == 2
            in_single = step.state == 1 and not (ch == "'" and step.active)
            if not escaped and not in_single and ch == "$" and source.startswith("$(", off):
                # bash parses the body in a fresh context, so the span is
                # proven from the slice at NEUTRAL state -- and the walk
                # resumes with the OUTER state carried across the jump.
                rel, proven = _matching_close_paren(source[off + 2 :], 0)
                body = source[off + 2 : off + 1 + rel] if proven else source[off + 2 :]
                # An EMPTY substitution expands to NOTHING, so the word
                # CONTINUES across it -- ``kill$()`` runs ``kill`` (the same
                # glue-evasion ``_EMPTY_SUBST_RE`` undoes for the token walk;
                # bash-measured).  Marking it glued
                # instead hands the evasion a free pass: the glued word is
                # excluded from the kill match and the segment loses its anchor.
                if proven and not body.strip():
                    # The word is OPEN even when the expansion vanishes: a
                    # ``#`` right after ``$()`` is a word to bash (comments
                    # are lexed before expansion), not a comment.
                    open_word = True
                    i = off + 2 + rel
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                fresh_word = not word and not open_word
                if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                    glued = True
                record_body(body)
                if fresh_word:
                    # A word that IS a substitution stands where its OUTPUT
                    # stands: ``$(echo kill) $(pgrep -f <name>)`` runs kill.
                    # The synthetic word keeps positions honest too -- later
                    # bodies in the segment do not share this one's index.
                    words.append((_static_substitution_output(body), False))
                end_word()
                if not proven:
                    i = n
                    jumped = True
                    break
                i = off + 2 + rel
                state, ansi = step.state, step.ansi
                jumped = True
                break
            if not escaped and not in_single and ch == "`":
                closer = _backtick_closer(source, off + 1)
                body = source[off + 1 : closer if closer != -1 else n]
                # Empty backticks: same word-continuity rule as ``$()``.
                if closer != -1 and not body.strip():
                    open_word = True
                    i = closer + 1
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                fresh_word = not word and not open_word
                if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                    glued = True
                record_body(body)
                if fresh_word:
                    words.append((_static_substitution_output(body), False))
                end_word()
                if closer == -1:
                    i = n
                    jumped = True
                    break
                i = closer + 1
                state, ansi = step.state, step.ansi
                jumped = True
                break
            if step.active:
                if ch in "<>" and source.startswith("(", off + 1):
                    rel, proven = _matching_close_paren(source[off + 2 :], 0)
                    fresh_word = not word and not open_word
                    if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                        glued = True
                    record_body(source[off + 2 : off + 1 + rel] if proven else source[off + 2 :])
                    if fresh_word:
                        # A process substitution expands to a /dev/fd PATH,
                        # never to its own stdout -- no static output here.
                        words.append(("\x00", False))
                    end_word()
                    if not proven:
                        i = n
                        jumped = True
                        break
                    i = off + 2 + rel
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                if ch in "&|" and (
                    (word and word[-1] in "<>")
                    or (ch == "&" and not word and source.startswith(">", off + 1))
                ):
                    # The full redirect grammar audited against the separator
                    # set (``2>&1``, ``&>``, ``>|``): a ``&`` or ``|``
                    # riding a trailing ``<``/``>`` is a descriptor duplication
                    # or the noclobber override, and a leading ``&>``/``&>>``
                    # redirects both streams -- all redirects of THIS command,
                    # never separators of it.  ``;`` and newline appear in no
                    # redirect spelling, which closes the enumeration.
                    word.append(ch)
                    continue
                if ch in ";|&\n":
                    end_segment()
                    continue
                if ch == "#" and not word and not open_word:
                    newline = source.find("\n", off)
                    i = n if newline == -1 else newline
                    state, ansi = 0, False
                    jumped = True
                    break
                if ch in "()" or ch.isspace():
                    end_word()
                    continue
            if not escaped and ch in "'\"" and (step.active or step.state == 0):
                # Quote SYNTAX: an opener (active) or a closer (back at state
                # 0).  bash does not pass these on, so the word must not carry
                # them -- ``k''ill`` is the word ``kill``.  A quote that is
                # DATA (inside the other quote type, or escaped) falls through
                # and stays in the word.
                open_word = True
                continue
            word.append(ch)
            open_word = True
        if not jumped:
            break
    end_segment()
    return bodies


def _is_self_kill(text_lower: str) -> bool:
    """True if *text_lower* terminates a Kiro Crew process.

    Boolean wrapper over :func:`_self_kill_span`, so the verdict and the span the
    diagnostic reports are derived from one walk and cannot disagree.
    """
    return _self_kill_span(text_lower) is not None


def _self_kill_span(text_lower: str) -> "tuple[int, int] | None":
    """Where the self-kill was matched in *text_lower*, or ``None`` if none is.

    ``None`` means "not a self-kill"; a match ALWAYS yields ``(start, end)`` -- the
    located kill target, or the whole-command fallback when it cannot be placed
    honestly -- so :func:`_is_self_kill` reads ``is not None`` as the verdict. The
    span is a diagnostic aid and never changes whether the kill is refused.

    Two shapes: ``pkill``/``killall`` take the name in any argument (even a quoted
    pattern); bare ``kill`` takes PIDs, reaching the product only via a
    substitution that resolves the name (``kill $(pgrep -f kirocrew)``, a pidfile,
    backticks) -- a ``kill <pid>`` that merely mentions a product path is not one.
    """
    # Perf short-circuit: both loops below re-run the payload descent.
    # A kill can only target the product if the gate's necessary condition
    # holds, so a miss skips both descents.
    if not _self_floor_can_fire(text_lower):
        return None

    whole = (0, len(text_lower))
    # A located span is honest only where the matched token sits in the command
    # exactly as its resolved form reads. ANY normalization the floor applies
    # (quote/escape removal, parameter and command expansion, brace, tilde, glob,
    # printf escapes) makes the token a value whose raw form is absent
    # (``'kiro''crew'``, ``${X}crew``), so a plain search skips to an unrelated
    # later copy. Rather than chase each construct, locate ONLY in a command that
    # carries none of that machinery -- reusing the floor's own detectors so this
    # cannot drift -- where resolution is the identity. Everything else takes the
    # whole-command span: a conservative fallback is always correct.
    _verbatim = not (
        _SELF_FLOOR_MACHINERY_RE.search(text_lower) or _SELF_FLOOR_QUOTE_JUNK_RE.search(text_lower)
    )

    def _locate(source: str, frame: "list[str]", verb_i: int, target: str) -> "tuple[int, int]":
        """Offsets of the product NAME inside *target*, or the whole command.

        A diagnostic aid, so it is BEST-EFFORT location, not a fence: it points at
        the ``_SELF_NAME_RE`` match within the matched token when that can be placed
        cleanly, and otherwise returns the whole command. Anchoring on the name
        itself -- rather than the whole argv token -- is what keeps a glued operator,
        redirect or paren (``pkill kirocrew>/dev/null``) out of the span without a
        per-spelling denylist, which AGENTS.md calls a review smell. Located only
        when: the frame is TOP-LEVEL (a nested ``bash -c`` payload has no outer
        offset and is quoted anyway); ``_verbatim`` holds (no normalization
        machinery, so the token is a literal substring); the target occurs exactly
        ONCE (``echo kirocrew && pkill -f kirocrew`` is ambiguous); and that one
        occurrence is at/after the verb. Offsets index ``text_lower``; ``deny_gate``
        forwards them only while ``str.lower`` kept the length, so a width-changing
        lowercase (``İ`` U+0130) cannot shift them off the subject.
        """
        if source is not text_lower or not target or not _verbatim:
            return whole
        if text_lower.count(target) != 1:
            return whole
        name = _SELF_NAME_RE.search(target)
        if name is None:
            return whole
        cursor = 0
        for tok in frame[: verb_i + 1]:
            if not tok:
                continue
            found = text_lower.find(tok, cursor)
            if found < 0:
                return whole
            cursor = found + len(tok)
        pos = text_lower.find(target, cursor)
        if pos < 0:
            return whole
        return (pos + name.start(), pos + name.end())

    for source, tokens in _self_frames(text_lower):
        programs = _argv_programs(tokens)
        # Once per FRAME, not once per trigger token: see ``_is_credential_mint``.
        disqualified: "bool | None" = None
        for i, token in enumerate(tokens):
            if not _is_kill_by_name_program(token):
                continue
            # ``echo pkill kirocrew`` prints two words; it does not kill anything.
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(i, token, programs, tokens, command_disqualified=disqualified):
                continue
            # A program token ending with an operator (``pkill;``) is NOT skipped: the
            # quotes are already off, so ``'pkill;' -f kirocrew`` (a symlink literally so
            # named) reads like a closed argv, and the skip would be the kill.  A glob
            # ARGUMENT glued to an operator (``ls $dir/*;``) is ``_data_consumer_exempt``.
            # Check each argument for the target BEFORE testing whether it ends the
            # argv, then stop.  Order matters: the target is often a quoted pattern
            # whose own characters look like separators (``pkill -f '[;]*kirocrew'``),
            # so testing the boundary first would discard the very argument that
            # names the target.  Stopping after it keeps an unrelated later command
            # out of the match (``pkill other; echo kirocrew`` is not a self-kill).
            depth = 0
            for arg in tokens[i + 1 :]:
                # Search the raw arg, its normalized form, and its resolved-word view.
                # Normalizing alone is not enough: a pkill pattern is an ERE, so a ``>``
                # or ``|`` in it is part of the TARGET (``'>kirocrew'``, ``zz|kirocrew``)
                # and the operand view cuts there.  Raw alone misses an empty
                # substitution or a default (``kiro$()crew``, ``zz|kiro${x:-crew}``), which
                # the construct-only ``_resolved_word_view`` resolves.  Any hit counts.
                n = _shell_normalizer
                views = (_debracket(arg), n._normalize_operand(arg), n._resolved_word_view(arg))
                if any(_SELF_NAME_RE.search(view) for view in views):
                    return _locate(source, tokens, i, arg)
                depth += _substitution_depth_delta(arg)
                if depth <= 0 and _ends_argv(arg):
                    break
                depth = max(depth, 0)
    # Bare ``kill`` whose PID comes out of a substitution naming the product.
    # The VERB is matched on tokens (so ``/usr/bin/kill``, ``$(which kill)`` and a
    # quoted spelling all count -- a raw-text pattern anchored on separators sees
    # the ``/`` and misses the path-qualified form), while the substitution BODY is
    # taken from the whole string: segment splitting cuts on ``$(`` and ``)``,
    # which would separate the verb from its own substitution.
    for source, frame in _self_frames(text_lower):
        for i, token in enumerate(frame):
            if _shell_normalizer._program_basename(token) != "kill":
                continue
            # Scan only the substitutions in THIS kill's own argv.  Scanning the whole
            # command associated every substitution with any ``kill`` on the line, so
            # ``kill 123; echo $(cat /tmp/kirocrew)`` was denied for a substitution
            # belonging to a different command.
            own = [token]
            depth = 0
            for later in frame[i + 1 :]:
                own.append(later)
                # A separator INSIDE a substitution belongs to the substitution, not to
                # this command line: ``kill $(echo x; pgrep <name>)`` is ONE argument, so
                # ending the scan at that ``;`` would drop the half naming the target.
                depth += _substitution_depth_delta(later)
                if depth <= 0 and _ends_argv(later):
                    break
            # An operand of THIS kill that resolves to the protected name is a self-kill.
            # `kill` takes PIDs, so a bare name is not something a person types -- it gets
            # there by expansion, and the expansion that produces it is a lookup of our own
            # processes (``P=$(pgrep <name>); kill $P``).  Scoped to the kill's own argv by
            # the same walk that keeps ``kill 8123 && cp /tmp/<name>.json ~/`` allowed:
            # there the name is an operand of ``cp``, not of the kill.
            for operand in own[1:]:
                if _SELF_NAME_RE.search(_shell_normalizer._normalize_operand(operand)):
                    return _locate(source, frame, i, operand)
            for body in _substitution_bodies(" ".join(own)):
                # ``kill $(pgrep -f kiro${x:-crew})`` hides the name behind an
                # expansion whose literal branch the shell substitutes back in, so
                # resolve those defaults before searching.
                if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                    _resolve_param_defaults(body)
                ):
                    return _locate(source, frame, i, body)
        # The window above is bounded by ``_substitution_depth_delta`` on
        # DE-QUOTED tokens, so a quoted close-paren reads as a real closer and
        # closes the window early, dropping the clause that names the target
        # (``kill $(printf ')' ; pgrep -f kirocrew)``).  Re-derive the same
        # window from the RAW text, where the quotes still exist.  The counter
        # also scores a ``case`` PATTERN's ``)`` as a closer, so a lookup placed
        # after ``case ... esac`` in the body's list falls outside the token
        # window too -- only this raw window reaches it.  Search each body raw,
        # with parameter defaults resolved, AND per WORD of the ``_self_tokens``
        # view (de-quoting only exists as a product of tokenization, and that
        # view folds a ``backslash-newline`` split name whole and swallows an
        # untokenizable body instead of raising out of the gate): each word
        # searched bare AND through ``_resolved_word_view``.  Both members
        # carry weight: the composed transform reaches what de-quoting leaves
        # behind (``'kiro''[c]rew'``, ``kiro$()crew``, ``'kiro'${x:-crew}`` --
        # a name needing TWO transforms sits in the seam between
        # single-transform searches), while the bare search reaches a name the
        # transform DESTROYS (``${PATH/usr/|'kiro''crew'|zz-}``; the ``pkill``
        # leg searches the same view per argument).  See the helper's docstring for
        # why the transform is not ``_normalize_operand``.
        # This quote-aware fallback window is not anchored to a kill verb's argv
        # position, so there is no honest offset to report -- the whole command is
        # the standing span.
        for body in _bare_kill_raw_bodies(source):
            if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                _resolve_param_defaults(body)
            ):
                return whole
            for word in _shell_normalizer._self_tokens(body):
                if _SELF_NAME_RE.search(_debracket(word)) or _SELF_NAME_RE.search(
                    _shell_normalizer._resolved_word_view(word)
                ):
                    return whole
    return None


# ── Self-protection subcommand floor (argv-structural) ──────────────────────
# ``restart`` / ``update`` / ``gateway restart`` / ``cloud <destructive>`` each
# run a privileged self-action. The regex tier matches these on raw text, which
# the shell's own de-escaping defeats: ``kirocrew -\v restart`` (backslash escape
# -> ``-v``), ``kirocrew \restart`` (escaped subcommand letter) and
# ``kirocrew -\<newline>v restart`` (line continuation) all reach the shell as the
# plain command but split a token in the raw string the regex sees. Matching on
# the tokenized argv -- the same de-escaped, de-quoted view the kill/token floors
# use (``_self_token_frames``) -- resolves every such spelling before the check.
# The floor is a UNION with the regex tier, never a replacement: the regex still
# catches a payload the tokenizer cannot see into (``bash -c "kirocrew restart"``)
# and the ``python -m kiro_crew restart`` module form (``kiro.?crew`` + verb).
_SELF_CLOUD_DESTRUCTIVE_VERBS: frozenset[str] = frozenset(
    {"destroy", "stop", "start", "launch", "connect", "tunnel", "login", "logout"}
)


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
        operand = _shell_normalizer._normalize_operand(later)
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


def _operands_lead_with(operands: "list[str]", spec: "tuple[object, ...]") -> bool:
    """True if *operands* begins with the subcommand sequence *spec*.

    Each element of *spec* is an exact word, or a ``frozenset`` of accepted words
    (used for ``cloud <one of the destructive lifecycle subcommands>``).
    """
    if len(operands) < len(spec):
        return False
    for got, want in zip(operands, spec):
        if isinstance(want, frozenset):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


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
    norm = [_shell_normalizer._normalize_operand(token).strip("\"'") for token in tokens]
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
    if _PYTHON_PROGRAM_RE.match(_shell_normalizer._program_basename(tokens[i])):
        return _self_module_name_index(tokens, i, scan)
    return None


def _matches_self_subcommand(text_lower: str, spec: "tuple[object, ...]") -> bool:
    """True if the product CLI is invoked with leading operand words *spec*.

    Covers the direct form (``kirocrew`` as the argv program) and the module form
    (``python -m kiro_crew``), collecting operands after the CLI/module so the same
    shell de-escaping the regex tier cannot see is caught for both -- e.g.
    ``python -m kiro_crew -\\v restart``, which the interpreter-position regex misses.
    """
    if not _self_floor_can_fire(text_lower):
        return False
    for tokens in _self_token_frames(_shell_join_continuations(text_lower)):
        programs = _argv_programs(tokens)
        # Once per FRAME, not once per token, to keep the floor linear in token count.
        scan = _self_module_flag_scan(tokens)
        # Same reason, for the command-level half of ``_data_consumer_exempt``: see
        # ``_is_credential_mint``.
        disqualified: "bool | None" = None
        for i in range(len(tokens)):
            prog_idx = _self_program_index(tokens, i, scan)
            if prog_idx is None:
                continue
            # ``echo kirocrew restart`` / ``echo python -m kiro_crew restart`` print words.
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(
                prog_idx,
                tokens[prog_idx],
                programs,
                tokens,
                command_disqualified=disqualified,
            ):
                continue
            if any(
                _operands_lead_with(_self_cli_operands(tokens, prog_idx, glued_words=g), spec)
                for g in (False, True)
            ):
                return True
    return False


def _is_self_restart(text_lower: str) -> bool:
    """``kirocrew restart`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("restart",))


def _is_self_update(text_lower: str) -> bool:
    """``kirocrew update`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("update",))


def _is_self_gateway_restart(text_lower: str) -> bool:
    """``kirocrew gateway restart`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("gateway", "restart"))


def _is_self_cloud_destructive(text_lower: str) -> bool:
    """``kirocrew cloud <destructive>`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("cloud", _SELF_CLOUD_DESTRUCTIVE_VERBS))


def _is_self_skills_authority_retire(text_lower: str) -> bool:
    """``kirocrew skills authority-retire`` behind interposed shell dressing."""
    return _matches_self_subcommand(text_lower, ("skills", "authority-retire"))


#: A git config key that sets the pager: ``core.pager`` or any ``pager.<command>``.
_GIT_PAGER_CONFIG_KEY_RE = re.compile(r"^(?:core\.pager|pager\.[^=\s]+)$")


def _is_noop_pager_value(value: str) -> bool:
    """True if a pager VALUE runs no distinct program, so a pager set to it is allowed.

    The empty string runs nothing; ``cat`` copies its input unchanged (the value the
    interactive-liveness advice proposes and the product's own git calls use). The
    trusted absolute paths to cat that update-governance resolves ``core.pager=cat`` to
    are allowed too: the root-owned ``/usr/bin/cat`` / ``/bin/cat``, and on Windows the
    bare ``cat.exe`` plus the cat that Git for Windows installs under ``C:\\Program
    Files\\Git\\usr\\bin`` or, for a 32-bit install, ``C:\\Program Files (x86)\\Git\\usr\\bin``
    (matched with either slash and in any case, since ``shutil.which`` returns ``cat.EXE``
    from ``PATHEXT``). Owner ruling: every other named program is refused, because the pager
    name is swappable (an alias, a ``PATH`` shadow, a ``LESSOPEN`` hook), so an
    attacker-placed ``*/cat`` (``/tmp/x/cat``) stays refused.
    """
    if value in ("", "cat", "/usr/bin/cat", "/bin/cat"):
        return True
    folded = value.replace("/", "\\").lower()
    if folded == "cat.exe":
        return True  # the bare name with its Windows extension, resolved like ``cat``
    return any(
        folded == root + "\\git\\usr\\bin\\" + name
        for root in ("c:\\program files", "c:\\program files (x86)")
        for name in ("cat.exe", "cat")
    )


def _env_sets_git_pager(token: str) -> bool:
    """True if an environment word sets git's pager to a program that would run.

    ``GIT_PAGER`` / ``PAGER`` name the pager directly: a no-op value
    (:func:`_is_noop_pager_value`) runs no distinct program and is allowed, any other
    non-empty value is refused (the name is swappable, so no other value is trusted). A
    bare name with no ``=`` sets nothing and is allowed. ``GIT_CONFIG_PARAMETERS`` is
    git's own list of single-quoted ``'key=value'`` (or ``'key'='value'``) entries: it is
    read entry by entry, so a non-pager key (``'color.ui=never'``) is allowed and only a
    pager key set to a program is refused; a value that does not parse is refused closed.
    A ``GIT_CONFIG_KEY_n`` naming ``core.pager`` / ``pager.<cmd>`` is the env config trio,
    refused closed here (the caller pairs it with its ``GIT_CONFIG_VALUE_n``). The env-var
    NAME is matched case-insensitively; the pager value's case is preserved.
    """
    key, eq, value = token.partition("=")
    key_l = key.lower()
    if key_l in ("git_pager", "pager"):
        return bool(eq) and not _is_noop_pager_value(value)
    if key_l == "git_config_parameters" and eq:
        import shlex

        try:
            entries = shlex.split(value)
        except ValueError:
            return True  # an unparseable config string -- refuse closed
        for entry in entries:
            entry_key, has_value, entry_value = entry.partition("=")
            entry_key_l = entry_key.lower()
            if not _GIT_PAGER_CONFIG_KEY_RE.match(entry_key_l):
                continue
            if not has_value:
                return True  # a pager key with no value is =true, which pages
            if _is_noop_pager_value(entry_value):
                continue
            if entry_key_l != "core.pager" and entry_value.lower() in ("false", "no", "off", "0"):
                continue
            return True
        return False
    if key_l.startswith("git_config_key_") and eq:
        return bool(_GIT_PAGER_CONFIG_KEY_RE.match(value.lower()))
    return False


def _git_args_set_pager(args: "list[str]") -> "tuple[bool, bool, bool]":
    """Scan a git command's ORIGINAL-case arguments (everything after ``git``).

    Returns ``(route_refused, env_pager_unused, sets_core_pager)``. *route_refused* is
    True when the arguments alone open or configure a pager that would run a program;
    *env_pager_unused* is True when git would run no environment pager for this command
    (a git-global ``--no-pager`` / ``-P``, a ``pager.<sub>`` set to a no-program value for
    the subcommand being run, or a subcommand git never pages without ``-p``), so the
    caller skips the environment pagers; *sets_core_pager* is True when
    a git-global ``-c`` / ``--config-env`` sets ``core.pager`` (to any value), which git
    honours over ``PAGER``.

    Options are read in git's own grammar, which has two scopes. A GIT-GLOBAL option sits
    BEFORE the subcommand: ``-c k=v`` / ``--config-env`` set config, the value options
    (``-C``, ``--git-dir``, ``--work-tree``, ``--namespace``, ``--super-prefix``,
    ``--exec-path``) consume the next word, and ``-p`` / ``--paginate`` / ``-P`` /
    ``--no-pager`` toggle paging. After the subcommand the subcommand's OWN grammar
    applies, so a ``-c`` that is ``git grep``'s count flag, or a ``-P`` that is its
    Perl-regexp flag, is never mistaken for a git-global pager control. That scoping is
    the whole point of this rewrite: a flat scan over-refused ``git grep -c core.pager``
    (grep count + a pattern) and under-refused ``git -c core.pager=p log -P`` (log's -P
    wrongly cancelling the real global config).

    ``git grep -O`` / ``--open-files-in-pager`` opens the matching files in a program and
    is ALWAYS refused -- ``-O`` is read case-sensitively (lowercase ``-o`` prints matching
    text and runs no pager), ``grep``'s operand-taking short options (``-e`` / ``-f`` /
    ``-m`` / ``-A`` / ``-B`` / ``-C``) consume their operand (attached, or the next token),
    so an ``O`` in a pattern is not read as the pager option, and scanning stops at a
    ``--`` operand terminator. ``--no-pager`` / ``-P`` never cancel ``grep -O`` (it opens
    files whatever the output paging does).

    A ``core.pager`` set to a no-op value (empty / ``cat``), or a ``pager.<cmd>`` set to a
    no-op or a git false (``false`` / ``no`` / ``off`` / ``0``), runs no distinct program
    and is allowed; any other named program is refused (the name is swappable). A key with
    no value is ``=true`` and runs a pager, so it is refused; ``--config-env`` sources the
    value from an env var the floor cannot read, so a pager key there is refused closed.
    A program-naming config is cancelled only by a git-global ``--no-pager`` / ``-P``.
    """
    git_false = frozenset(("false", "no", "off", "0"))
    grep_operand_opts = frozenset("efmABC")  # git grep short opts that take an operand
    # git-GLOBAL options that take a SEPARATE-token operand before the subcommand, so the
    # subcommand scan must step over that operand (``git --attr-source HEAD grep -O`` must
    # not read ``HEAD`` as the subcommand). The shared core is argv_floor_git._GIT_ARG_FLAGS
    # (mirrored here -- argv_floor_self is the BASE argv-floor layer in argv_floor._OWNERS
    # and the layer rule test_argv_floor_facade forbids importing the higher argv_floor_git;
    # a test pins every _GIT_ARG_FLAGS entry); git accepts more value-taking globals the
    # floor must also consume, so those are added on top, documented.
    global_value_opts = frozenset(
        {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}  # _GIT_ARG_FLAGS mirror
    ) | frozenset({"--super-prefix", "--exec-path", "--attr-source", "--config-env"})

    def _pager_key(key_l: str) -> bool:
        return key_l == "core.pager" or (
            key_l.startswith("pager.") and bool(_GIT_PAGER_CONFIG_KEY_RE.match(key_l))
        )

    def _value_runs_program(key_l: str, value: str, *, env_sourced: bool, has_eq: bool) -> bool:
        if env_sourced or not has_eq:
            return True  # --config-env value is unknowable; no '=' means =true
        if key_l == "core.pager":
            return not _is_noop_pager_value(value)
        return not _is_noop_pager_value(value) and value.lower() not in git_false

    # Config assignments collapse to the LAST value per key (git honours the last ``-c``
    # for a key), so ``-c core.pager=less -c core.pager=cat`` ends at the no-op ``cat``.
    config_state: "dict[str, bool]" = {}

    def _record_config(spec: str, *, env_sourced: bool) -> None:
        key, eq, value = spec.partition("=")
        key_l = key.lower()
        if _pager_key(key_l):
            config_state[key_l] = _value_runs_program(
                key_l, value, env_sourced=env_sourced, has_eq=bool(eq)
            )

    def _grep_opens_pager(rest: "list[str]") -> bool:
        j = 0
        while j < len(rest):
            token = rest[j]
            if token == "--":
                return False
            low = token.lower()
            if low.startswith("--"):
                name = low[2:].split("=", 1)[0]
                if name and "open-files-in-pager".startswith(name):
                    return True
                if name in ("regexp", "file") and "=" not in token:
                    j += 2  # the pattern / file is a separate-token operand
                    continue
                j += 1
                continue
            if token.startswith("-") and len(token) > 1:
                consumed_next = False
                for pos, ch in enumerate(token[1:]):
                    if ch == "O":
                        return True
                    if ch in grep_operand_opts:
                        consumed_next = pos + 2 >= len(token)
                        break
                j += 2 if consumed_next else 1
                continue
            j += 1
        return False

    def _config_writes_pager(cfg_args: "list[str]") -> bool:
        """``git config`` WRITES a pager key to a program -- the legacy ``git config
        <key> <value>`` and the git 2.46+ ``git config set <key> <value>`` grammars.

        A read / unset / list (dashed flag OR the subcommand verb), a no-op / git-false
        value, and a key with no value are allowed; a redirection (``2>/dev/null`` / ``>
        file``; a ``| cat`` or ``|| true`` is already split off upstream) is removed, so
        ``git config core.pager`` with a trailing redirect stays a READ.
        """
        read_verbs = frozenset(
            (
                "get",
                "get-all",
                "get-regexp",
                "get-urlmatch",
                "list",
                "unset",
                "unset-all",
                "rename-section",
                "remove-section",
                "edit",
                "get-color",
                "get-colorbool",
            )
        )
        reading_flags = frozenset(
            (
                "--get",
                "--get-all",
                "--get-regexp",
                "--get-urlmatch",
                "--unset",
                "--unset-all",
                "--list",
                "-l",
                "--edit",
                "-e",
                "--remove-section",
                "--rename-section",
                "--get-color",
                "--get-colorbool",
                "--name-only",
            )
        )
        # Options that CONSUME a following operand, so a key/value after them is read
        # correctly. ``--value`` / ``--comment`` are git 2.46+ ``config set`` options
        # (``git config set --value '.*' core.pager ./p`` must not read ``.*`` as the key).
        value_flags = frozenset(("--file", "-f", "--blob", "--type", "-t", "--value", "--comment"))
        positionals: list[str] = []
        i = 0
        while i < len(cfg_args):
            tok = cfg_args[i]
            is_redirect, separate_target = _redirect_consumes_next(tok)
            if is_redirect:
                i += 2 if separate_target else 1
                continue
            tok = _strip_redirect(tok)  # drop an attached redirect suffix (key>/tmp/x)
            if tok == "":
                i += 1
                continue
            if tok.lower() in reading_flags:
                return False
            if tok in value_flags and i + 1 < len(cfg_args):
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            positionals.append(tok)
            i += 1
        if positionals and positionals[0].lower() in read_verbs:
            return False  # `git config get|list|unset ...` reads, writes nothing
        if positionals and positionals[0].lower() == "set":
            positionals = positionals[1:]  # `git config set <key> <value>`
        if len(positionals) < 2:
            return False  # a read, or a key with no value
        key_l, value = positionals[0].lower(), positionals[1]
        if not _GIT_PAGER_CONFIG_KEY_RE.match(key_l):
            return False
        if key_l == "core.pager":
            return not _is_noop_pager_value(value)
        return not _is_noop_pager_value(value) and value.lower() not in git_false

    # Subcommands git never runs through a pager unless ``-p`` / ``--paginate`` forces it:
    # plumbing reads and the write / transport porcelain. Listing, log and diff commands
    # (``log``, ``show``, ``diff``, ``branch``, ``tag``, ``stash``, ``blame``, ...) and any
    # unknown word, which may be an alias, are left out, so they still count as paging.
    non_paging = frozenset(
        (
            "add",
            "am",
            "apply",
            "bisect",
            "cat-file",
            "checkout",
            "cherry-pick",
            "clean",
            "clone",
            "commit",
            "describe",
            "fetch",
            "for-each-ref",
            "fsck",
            "gc",
            "hash-object",
            "init",
            "ls-files",
            "ls-remote",
            "ls-tree",
            "merge",
            "merge-base",
            "mv",
            "prune",
            "pull",
            "push",
            "rebase",
            "remote",
            "reset",
            "restore",
            "rev-list",
            "rev-parse",
            "revert",
            "rm",
            "show-ref",
            "status",
            "submodule",
            "switch",
            "symbolic-ref",
            "update-index",
            "update-ref",
            "version",
            "worktree",
        )
    )
    disables_pager = False
    forced_paging = False
    # Phase 1: git-global options, up to the subcommand (the first bare word, or a
    # value option's value). A ``--`` here means no subcommand at all. ``-P`` / ``-p``
    # (and ``--no-pager`` / ``--paginate``) are evaluated last-wins.
    index = 0
    n = len(args)
    while index < n:
        token = args[index]
        if token == "--":
            index = n
            break
        if token in ("-P", "--no-pager"):
            disables_pager = True
            forced_paging = False
            index += 1
            continue
        if token in ("-p", "--paginate"):
            disables_pager = False  # a later pagination switch replaces the earlier
            forced_paging = True
            index += 1
            continue
        if token == "-c" and index + 1 < n:
            _record_config(args[index + 1], env_sourced=False)
            index += 2
            continue
        if token.startswith("-c") and not token.startswith("--") and len(token) > 2:
            _record_config(token[2:], env_sourced=False)
            index += 1
            continue
        if token.lower() == "--config-env" and index + 1 < n:
            _record_config(args[index + 1], env_sourced=True)
            index += 2
            continue
        if token.lower().startswith("--config-env="):
            _record_config(token[len("--config-env=") :], env_sourced=True)
            index += 1
            continue
        if token in global_value_opts and index + 1 < n:
            index += 2
            continue
        if token.startswith("--"):  # a long flag (with or without an attached =value)
            index += 1
            continue
        if token.startswith("-") and len(token) > 1:  # an unknown short global flag
            index += 1
            continue
        break  # the subcommand
    configs_pager = any(config_state.values())

    # Phase 2: the subcommand. ``grep`` can OPEN a pager; ``config`` can WRITE one. Both
    # run a program regardless of output paging, so neither is cancelled by ``-P`` / ``-p``.
    opens_or_writes = False
    sub = ""
    if index < n:
        sub = args[index].lower()
        rest = args[index + 1 :]
        if sub == "grep":
            opens_or_writes = _grep_opens_pager(rest)
        elif sub == "config":
            opens_or_writes = _config_writes_pager(rest)
    route_refused = opens_or_writes or (configs_pager and not disables_pager)
    # Whether an ENVIRONMENT pager (GIT_PAGER / PAGER / the env trio) can run at all for
    # this command. It cannot under ``--no-pager`` / ``-P``; nor when a ``pager.<sub>``
    # for the command being run is set to a no-program value (``-c pager.log=false``
    # turns log paging off, ``pager.log=cat`` pages through cat); nor, unless ``-p``
    # forces paging, for a subcommand git never pages (``rev-parse``, ``status``,
    # ``commit``, ...). An unknown word may be an alias that pages, so it still counts.
    env_pager_unused = (
        disables_pager
        or config_state.get("pager." + sub) is False
        or (not forced_paging and (not sub or sub in non_paging))
    )
    return route_refused, env_pager_unused, "core.pager" in config_state


def _strip_heredoc_bodies(text: str) -> str:
    """*text* with the body of every here-document fed to a non-shell program removed.

    A here-document body is DATA on the consuming program's standard input, not a
    command: ``git commit -m "$(cat <<'EOF'`` followed by a message that quotes
    ``GIT_PAGER=less git log`` runs no pager. The body is dropped only when no word of the
    command that opens it names a shell (``bash <<EOF`` runs its body, so that body is
    kept and still read). A body is dropped only up to its own delimiter line (``<<-``
    lets that line carry leading tabs); when no delimiter line follows, the rest of the
    text is left as it is. Each line is read once, so the pass is linear.
    """
    if "<<" not in text:
        return text
    opener = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
    boundary = re.compile(r"\$\(|`|;|\|\||&&|\||&|\(")
    shells = frozenset(("sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "busybox"))
    lines = text.split("\n")
    out: "list[str]" = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in opener.finditer(line):
            words = boundary.split(line[: match.start()])[-1].split()
            keep = any(w.rsplit("/", 1)[-1].lower() in shells for w in words)
            word, dash = match.group(3), match.group(1) == "-"
            end = next(
                (
                    j
                    for j in range(i, len(lines))
                    if (lines[j].lstrip("\t") if dash else lines[j]) == word
                ),
                None,
            )
            if end is None:
                out.extend(lines[i:])
                return "\n".join(out)
            out.extend(lines[i : end + 1] if keep else lines[end : end + 1])
            i = end + 1
    return "\n".join(out)


def _join_quoted_newlines(text: str) -> str:
    """*text* with every newline that sits INSIDE a quoted word turned into a space.

    A newline inside quotes is data in the word (a multi-line ``git commit -m '...'``),
    not a command boundary, so the segment split must not cut the word there and read
    its second line as a command. A newline outside quotes, escaped or not, is kept as
    it is. Applied per frame, after the payload walk has already unwrapped an
    ``sh -c '...'`` payload, so a newline that separates the payload's own commands is
    outside quotes by then and still separates. One pass over the text.
    """
    if "\n" not in text:
        return text
    return "".join(
        " " if step.text == "\n" and step.state != 0 else step.text
        for step in _iter_shell_chars(text)
    )


def _strip_line_comments(text: str) -> str:
    """*text* with every shell ``#`` line comment removed, newlines kept.

    A ``#`` starts a comment only at a word boundary outside quotes (``a#b`` is one
    word), and the comment runs to the end of its line. Because a comment can only
    begin where no quote is open, the shell state machine is re-entered on the text
    AFTER each comment, so a stray quote inside dropped comment text cannot corrupt the
    reading of the real command that follows. The newline that ends a comment is kept,
    so line structure (and any trailing-escape splice signal) survives.
    """
    out: "list[str]" = []
    rest = text
    while rest:
        cut: "int | None" = None
        for step in _iter_shell_chars(rest):
            if (
                step.active
                and step.char == "#"
                and _shell_normalizer._opens_comment(rest, step.offset)
            ):
                out.append(rest[: step.offset])
                nl = rest.find("\n", step.offset)
                cut = len(rest) if nl < 0 else nl
                break
        if cut is None:
            out.append(rest)
            break
        rest = rest[cut:]
    return "".join(out)


def _is_git_pager(text_lower: str, *, raw_text: "str | None" = None) -> bool:
    """True if a git command opens or configures a pager, a program git would run.

    ``git grep -O`` / ``--open-files-in-pager`` runs the pager on the matching files,
    and ``core.pager`` / ``pager.<cmd>`` set through ``-c``, ``--config-env``,
    ``GIT_PAGER``, ``PAGER``, ``GIT_CONFIG_PARAMETERS`` or a ``GIT_CONFIG_KEY_n`` env
    word all name the program git pages through. The agent chooses that program, so each
    is a route to run an arbitrary command through git; the operation is refused whatever
    the pager VALUE names (the name is swappable, so there is no program allowlist), and
    inside a nested ``eval`` / ``sh -c`` payload too.

    Refused ONLY when a program actually runs. The no-program forms are allowed: the
    lowercase ``git grep -o`` (prints matching text, not a pager), ``core.pager`` /
    ``GIT_PAGER`` / ``PAGER`` set to the empty string, ``pager.<cmd>`` set to a git false
    (``false`` / ``no`` / ``off`` / ``0``), and ``--no-pager`` / ``-P`` on a paging command
    (which does not, however, cancel ``grep -O``).

    *raw_text* is the command AS SUBMITTED. The argv is walked in ORIGINAL case so ``-O``
    is distinguished from ``-o``; ``text_lower`` is used only for the cheap can-fire gate,
    because the deny gate passes already-lower-cased text that has lost that distinction.

    Liveness floor, not a security boundary. It stops the accidental pager forms that
    hang a non-interactive session; it does not try to stop deliberate evasion (a shell
    redirection, running a pager binary directly, or a program name assembled across a
    command separator). Those are out of scope: the floor does not add machinery to chase
    them, and stays linear in the length of the command it reads.
    """
    source = raw_text if raw_text is not None else text_lower
    # A ``#`` at a word boundary outside quotes begins a comment that the shell drops
    # to end of line, so its text is NOT a command. An annotated no-op -- the very form
    # the product's liveness advice invites (``# core.pager=less would hang; use cat``
    # then ``git -c core.pager=cat log``), or a trailing ``git grep foo # -O`` -- must not
    # have the comment's words read as a pager route. Strip line comments before the walk;
    # a comment can only START outside quotes, so the state machine is re-entered fresh at
    # each one and a quote inside the dropped text cannot leak past it.
    walk_source = _strip_line_comments(_strip_heredoc_bodies(source))
    # Perf + robustness gate: a git pager route always carries the substring
    # ``git`` -- the program basename git pages through, or a ``GIT_PAGER`` /
    # ``GIT_CONFIG_*`` env word. When neither the raw text nor its quote-stripped
    # form contains it AND no shell machinery could synthesise it, the floor
    # provably cannot fire and the recursive payload walk is skipped. The
    # machinery clause is what the earlier substring-only gate missed: a
    # substitution/brace/escape spelling such as ``gi$()t grep -O./x`` carries no
    # literal ``git`` yet resolves to it in the walk, so a ``$``/backtick/glob/
    # escape present means "maybe" and runs the full scan (the safe direction,
    # mirroring ``_self_floor_can_fire``).
    joined = _shell_join_continuations(text_lower)
    if (
        "git" not in text_lower
        and "git" not in _SELF_FLOOR_QUOTE_JUNK_RE.sub("", joined)
        and not _SELF_FLOOR_MACHINERY_RE.search(joined)
    ):
        return False

    def _carried_env(cmd: "list[str]") -> "list[str] | None":
        """The env a command EXPORTS to later commands in its frame, or None.

        A bare ``VAR=value ...`` command exports only when every word is an
        assignment (``VAR=value`` alone sets the shell variable; ``VAR=value cmd``
        sets it for ``cmd`` only -- caught by that command's own env prefix, not
        here). ``export VAR=value`` exports its assignment words.
        """
        words = [t for t in cmd if not _ends_argv(t)]
        if not words:
            return None
        if _shell_normalizer.ENV_ASSIGNMENT_RE.match(words[0]):
            if all(_shell_normalizer.ENV_ASSIGNMENT_RE.match(w) for w in words):
                return list(words)
            return None
        if _shell_normalizer._program_basename(words[0]).lower() == "export":
            return [w for w in words[1:] if _shell_normalizer.ENV_ASSIGNMENT_RE.match(w)]
        return None

    # The gate must return a DECISION, never propagate. Unlike the self-name
    # floors (which _self_floor_can_fire keeps away from the payload walk for
    # non-self commands), this floor walks for every git command, so a raising
    # walk would otherwise escape is_denied -- the rm floor guards the same way.
    # Abstaining (False) is sound: a real pager route does not depend on a
    # raising walk, and the regex/other tiers still decide.
    def _opener_at_program_position(tok: str) -> bool:
        """True if *tok*, seen where a PROGRAM word is expected (command so far empty),
        is a subshell / group / function-definition opener or a ``#`` comment -- a real
        command boundary. Judged by POSITION, not spelling: the SAME ``(`` / ``#`` AFTER
        the program is an operand (``git grep -e '(' -O./p``, ``git grep -e '#' …``), and a
        quoted one dequotes to the bare character, so only its program-position occurrence
        may split. ``;`` / ``|`` are never judged here -- the quote-aware segment split
        already cut the real ones, and any left is a quoted operand.
        """
        if tok.startswith("#"):
            return True
        stripped = tok.rstrip("{")
        return stripped in {"", "("} or stripped.endswith("()")

    try:
        for frame_source, frame_tokens in _shell_payload_walk(walk_source):
            # Pass (a): command boundaries taken BEFORE operands are dequoted, by
            # splitting the frame SOURCE with the quote-aware segment splitter; the
            # tokenised argv is used only WITHIN a segment, so a quoted separator in a
            # grep pattern or a ``-c`` value (``git grep -e ';' -O``, ``-e '&'``) stays
            # part of its command. Each segment is tokenised ONCE, on its own: the floor
            # does NOT carry an assembled program across a separator (``g=git; $g grep
            # -e ';' -O``), which is a constructed-command evasion the liveness floor
            # leaves out of scope -- carrying it would re-tokenise a growing prefix per
            # segment and make the walk quadratic. A same-line ``export`` is still caught
            # by pass (b), which threads its env through ``extra_env`` linearly.
            for segment in _shell_normalizer._split_push_command_segments(
                _join_quoted_newlines(frame_source)
            ):
                command: list[str] = []
                for token in _shell_normalizer._self_tokens(segment):
                    # Only an opener at PROGRAM position ends the command (a subshell /
                    # group / function body / ``#`` comment); a ``;`` / ``&`` / ``(`` /
                    # ``#`` sitting in an operand dequotes to the same bare token and must
                    # NOT split.
                    if not command and _opener_at_program_position(token):
                        command = []
                        continue
                    command.append(token)
                if command and _git_command_sets_pager(command):
                    return True
            # Pass (b): the WHOLE-frame tokens, which resolve a variable-assembled program
            # (``g=git; $g grep -O``) that per-segment re-tokenising cannot, split on
            # ``_ends_argv``. Quoted separators here only UNDER-split (they can miss, never
            # over-refuse), so the union of (a) and (b) adds detection, never a false
            # refusal, and a real background ``&`` separating two commands is caught here.
            env_b: list[str] = []
            command = []
            for token in frame_tokens:
                if _ends_argv(token):
                    # Keep a glued prefix (``cat;`` -> ``cat``) as this command's last arg
                    # but DROP the separator itself, so a trailing ``;`` / ``||`` glued to
                    # a value is not read as the config VALUE (that was a false refuse of
                    # ``git config core.pager cat; …`` / ``… || true``). Pass (a) owns the
                    # quote-aware boundary; this pass only ADDS the variable-resolved
                    # program (``g=git; $g grep -O``) and a real background ``&`` split.
                    stripped = token.rstrip(";|&")
                    if stripped and stripped != token:
                        command.append(stripped)
                    if command and _git_command_sets_pager(command, extra_env=env_b):
                        return True
                    carried = _carried_env(command)
                    if carried:
                        env_b = env_b + carried
                    command = []
                else:
                    command.append(token)
            if command and _git_command_sets_pager(command, extra_env=env_b):
                return True
    except Exception:
        return False
    return False


def _git_command_sets_pager(command: "list[str]", *, extra_env: "list[str] | None" = None) -> bool:
    """True if one argv *command* (program plus args) is a git pager route.

    *command* carries ORIGINAL-case tokens so ``-O`` is told from ``-o``. The program is
    found with :func:`argv_rules._program_args`, which steps over the leading
    ``NAME=value`` env words and every execution-prefix wrapper in the shared wrapper
    table (``command`` / ``env`` / ``sudo`` / ``doas`` / ``pkexec`` / ``runuser`` /
    ``chroot`` / ``flock`` / ``xargs`` / ...), so a wrapped ``git`` is still found and a
    pager reaching git through any known wrapper is not a bypass. ``_program_args`` drops
    the env words it steps over, so this function re-collects every ``NAME=value`` before
    the git program (the leading ones and those an ``env`` / ``sudo`` carries), and
    *extra_env* adds env exported by an earlier command on the same line
    (``export GIT_PAGER=...; git ...``). The program is matched case-insensitively
    against ``git``.

    ``git grep -O``, a program-naming ``-c`` / ``--config-env`` config, and a
    ``git config`` writer (legacy or the git 2.46+ ``config set`` grammar) are all decided
    by :func:`_git_args_set_pager`, which locates the subcommand AFTER git-global option
    parsing so a leading ``-C`` / ``--no-pager`` does not hide the ``config`` writer. An
    env pager (``GIT_PAGER`` / ``PAGER`` / the ``GIT_CONFIG`` trio) is refused here too,
    UNLESS a git-global ``--no-pager`` / ``-P`` cancels it; each refusal is skipped for the
    no-op forms (empty / ``cat`` pager, a git-false ``pager.<cmd>``).
    """
    # Imported here, not at module top, for the same reason rm_floor does: a top-level
    # binding would make `_argv_rules` a name the argv_floor facade must declare, and the
    # lazy form sidesteps that while staying cycle-free (argv_rules does not import this
    # module). argv_rules is not an argv-floor owner, so the layer rule does not apply.
    from . import argv_rules as _argv_rules

    env: list[str] = list(extra_env or [])
    # Find the program, stepping over leading NAME=value env words and EVERY
    # execution-prefix wrapper, by REUSING argv_rules._program_args -- the shared
    # wrapper table (command / env / sudo / doas / pkexec / runuser / chroot / flock /
    # xargs / ...) the rest of the argv floor already uses. A wrapper it knows is then
    # not a bypass here; a second, divergent inline copy had let `doas git grep -O`
    # through because it listed only some of those names.
    parsed = _argv_rules._program_args(command)
    if parsed is None:
        return False
    program, args = parsed
    if program.lower() != "git":
        return False
    # _program_args drops the NAME=value words it steps over, but an env pager
    # (GIT_PAGER / PAGER / the GIT_CONFIG trio -- leading, or carried by env / sudo) is
    # a route this floor must still see, so collect every assignment that precedes the
    # git program. `args` is the tail after git, so git sits at this index.
    git_index = len(command) - len(args) - 1
    for token in command[:git_index]:
        if _shell_normalizer.ENV_ASSIGNMENT_RE.match(token):
            env.append(token)

    # ``git config`` writers (legacy and the git 2.46+ ``config set`` grammar) and
    # ``git grep -O`` are both decided inside _git_args_set_pager now -- it locates the
    # subcommand AFTER git-global option parsing, so a ``git -C . config ...`` or
    # ``git --no-pager config ...`` writer is not hidden by the leading globals.
    route_refused, disables_pager, sets_core_pager = _git_args_set_pager(args)
    if route_refused:
        return True
    if disables_pager:
        return False
    # Environment pager: the LAST assignment of each env-var NAME wins, so a superseded
    # ``GIT_PAGER=cat`` after ``GIT_PAGER=less`` is the effective (allowed) value.
    git_false = frozenset(("false", "no", "off", "0"))
    effective: "dict[str, str]" = {}
    for word in env:
        name, eq, _value = word.partition("=")
        if eq:
            effective[name.lower()] = word
    # The GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n env trio sets config
    # like ``-c``: PAIR each KEY_n with its VALUE_n and refuse a pager key only when the
    # paired value runs a program, so the product's own no-op pin
    # (``GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0=cat``, or a git-false
    # ``pager.<cmd>``) is allowed. A pager KEY_n with no paired VALUE_n is refused closed.
    config_values = {
        name[len("git_config_value_") :]: word.partition("=")[2]
        for name, word in effective.items()
        if name.startswith("git_config_value_")
    }
    for name, word in effective.items():
        if not name.startswith("git_config_key_"):
            continue
        key_l = word.partition("=")[2].lower()
        if not _GIT_PAGER_CONFIG_KEY_RE.match(key_l):
            continue
        paired = config_values.get(name[len("git_config_key_") :])
        if paired is None:
            return True  # a pager key whose value is unknowable -- refuse closed
        if key_l == "core.pager":
            sets_core_pager = True
            if not _is_noop_pager_value(paired):
                return True
        elif not _is_noop_pager_value(paired) and paired.lower() not in git_false:
            return True
    # git picks its pager as GIT_PAGER, else core.pager, else PAGER, so PAGER is only
    # the last fallback: a session that keeps ``PAGER=less`` for man pages and pins
    # ``GIT_PAGER=cat`` or ``-c core.pager=cat`` runs no pager program in git.
    pager_superseded = "git_pager" in effective or sets_core_pager
    # The non-trio env pagers (GIT_PAGER / PAGER / GIT_CONFIG_PARAMETERS) keep the
    # simple per-word rule.
    return any(
        _env_sets_git_pager(word)
        for name, word in effective.items()
        if not name.startswith(("git_config_key_", "git_config_value_"))
        and name != "git_config_count"
        and not (name == "pager" and pager_superseded)
    )


_DEV_MODE_CONFIRM_FLAG = "--confirm-out-of-install-root"


def _is_dev_mode_out_of_root_confirm(text_lower: str) -> bool:
    """True if the operator's out-of-install confirm flag materializes after de-escaping.

    The regex tier matches the flag in RAW text, so quote-splitting inside the
    token (``--confirm-out-of-install-'root'``) reaches argparse as the accepted
    flag while the raw command never contains the literal.  This floor closes
    that class two ways: the whole string with quote/backslash glue removed
    (covers every quoting spelling in one O(n) pass), and every tokenized argv
    frame — the same descent the other floors use — whose payload walk also
    decodes printf/``$'…'`` escapes the glue-strip cannot see.

    Unlike the subcommand floors this predicate keys on the FLAG token, not on
    the product CLI being the argv program: the rule is deliberately broad (see
    its catalog comment), so a mention inside any command is a deny.  It matches
    the flag only as a token PREFIX boundary — ``--confirm-out-of-install-root``
    itself or with an attached ``=…``/wrapper — never as a substring of prose,
    because the leading ``--`` and full spelling make accidental prose hits
    implausible and the regex tier already denies them anyway.
    """
    # Cheap necessary condition: the flag cannot materialize from text that,
    # after glue removal, carries neither of its distinctive words unless an
    # escape encoding (backslash / ANSI-C quoting) could synthesize them.
    stripped = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", text_lower)
    if _DEV_MODE_CONFIRM_FLAG in stripped:
        return True
    if "confirm" not in stripped and "install" not in stripped and "\\" not in text_lower:
        return False
    for tokens in _self_token_frames(text_lower):
        for token in tokens:
            if _DEV_MODE_CONFIRM_FLAG in _SELF_FLOOR_QUOTE_JUNK_RE.sub(
                "", _shell_normalizer._normalize_operand(token)
            ):
                return True
    return False
