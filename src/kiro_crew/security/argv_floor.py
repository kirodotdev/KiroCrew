"""The argv-structural self-protection floor: a floor that reads argv, not text.

Always on, and deliberately not a regex tier. Every predicate here answers a
question about a command's STRUCTURE -- which token is the program, which token
is a subcommand, which token is a redirection target, which payload a nested
interpreter would actually run -- so the product's own name appearing somewhere
in a path, a search pattern or a commit message is not on its own an answer.
That distinction is the whole reason the floor exists beside the catalog: the
catalog matches text and can be pinned or opted out of, while these predicates
are the last word on the handful of actions that must never succeed no matter
what the settings say.

Three families live here:

* Release detection, which recognises a publish invocation by its subcommand
  position rather than by the verb appearing anywhere, then decides whether the
  refspec it carries names a protected branch. It fails CLOSED: an invocation
  whose target cannot be read cleanly is refused rather than guessed at.
* Termination, restart, update and destructive-cloud subcommand floors, which
  recognise the product as the argv's own program and the action as its own
  subcommand.
* The credential-mint predicates, which follow a payload through the carriers
  that can hand it to an interpreter -- an inline program, a here-document, a
  standard-input redirection, a nested shell -- and ask whether the product's
  package is imported in it.

Writes INTO the governance data home are not a floor here: the OS sandbox mounts
the keystone read-only in every mode, so an archive or copy destination flag
aimed at it is refused by the kernel rather than by a matcher over the command's
text.

Layer. This module sits above the shell reader and reads its word layer, the
shared name vocabulary below it, and one sentinel the rule catalog owns for a
floor denial that no catalog pattern matches. It imports nothing from the facade,
so the dependency runs one way.

The audit emitter for an allowed feature-branch push is NOT here. It belongs to
the tier that decides to allow, which is the evaluator, and it redacts the
command it records through the output-redaction side; keeping it beside the
evaluator is what keeps this module free of that edge.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, NamedTuple

# The reader names a test instruments on the facade are reached as attributes of
# the reader module rather than bound here at import: the facade mirrors a patch
# onto the owning module's namespace, so a name bound into THIS namespace would
# keep resolving the unpatched object and the instrument would count nothing.
from . import shell_normalizer as _shell_normalizer
from .denied_rules import _GIT_PUBLISH_UNGATED
from .shell_normalizer import (
    _AMBIGUOUS_EXPANSION_RE,
    _PROCESS_SUBSTITUTION_OPENERS,
    _PYTHON_INLINE_PROGRAM_FLAGS,
    _PYTHON_OPERAND_FLAGS,
    _PYTHON_PROGRAM_RE,
    _REDIRECT_START_RE,
    _SHELL_WRAPPER_CHARS,
    _argv_programs,
    _cut_at_operator,
    _data_consumer_exempt,
    _debracket,
    _decode_printf_escapes,
    _dequote_token,
    _ends_argv,
    _glob_could_expand_to,
    _here_string_payload,
    _heredoc_marker,
    _is_mint_verb,
    _is_self_program,
    _iter_shell_chars,
    _matching_close_paren,
    _nested_shell_payloads,
    _operand_span_end,
    _push_option_matches,
    _push_token_redirection,
    _push_token_shell_read,
    _redirect_consumes_next,
    _redirect_glue_point,
    _resolve_param_defaults,
    _shell_join_continuations,
    _shell_payload_walk,
    _shell_quote_walk,
    _split_push_command_segments,
    _split_shell_words,
    _substitution_bodies,
    _substitution_depth_delta,
)
from .vocabulary import _KILL_BY_NAME_PROGRAMS, _SELF_NAME_RE

if TYPE_CHECKING:
    from collections.abc import Iterator


# ── Git publish detection (verb-anchored) ──
# ``git push`` must be blocked, but ``push`` appearing anywhere in arbitrary
# command text (a commit message, a branch name, a grep pattern, an ssh remote
# payload) must NOT trip the deny.  We therefore require ``push`` to be the git
# *subcommand* — i.e. the first non-flag/non-option token after ``git`` — rather
# than a substring.  Mirrors the anchored regex in
# ``config/defaults.json`` deniedCommands.
#
# ``git [<-c k=v>...] [<-C path>...] push ...`` is a publish.  Intervening
# tokens may only be options (``-x``) or option-with-value pairs
# (``-C /path``, ``-c core.x=y``) — a bare non-flag token before ``push``
# (e.g. ``stash``) means ``push`` is NOT the subcommand, so ``git stash push``
# is correctly allowed.  Anchored to a segment start (optionally preceded by a
# command separator) so ``git log --grep push`` is not matched.
#
# The trailing terminator is a lookahead that accepts whitespace, end-of-string,
# OR a shell metacharacter that closes/terminates the segment — so a bare
# ``git push`` (no remote/branch, valid: pushes current branch to the default
# remote) is still caught inside ``$(git push)``, `` `git push` ``, ``git push|cat``,
# ``git push&``, etc., not just when followed by a space.
_GIT_PUBLISH_RE = re.compile(
    # ``[^-\s]`` (not ``[^-]``): the optional non-flag arg after a flag must
    # NOT start with whitespace, otherwise inter-token whitespace could be
    # matched either by the preceding ``\s+`` or by this group's leading char —
    # an ambiguity that backtracks exponentially (ReDoS) on whitespace-laden
    # flag runs when the trailing ``push`` is absent.
    # ``(`` is in the leading class because bash treats it as an operator, so
    # ``(git push`` runs git exactly as ``; git push`` does -- without it the
    # glued subshell form ``(git push origin main)`` matched no branch and the
    # only enforcement for git-publish (this floor) never fired.
    r"(?:^|[;&|`\n(]|\$\()\s*git\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*push(?=\s|[)`;&|]|$)"
)

# Glue-evasion guard: bash command-substitution / quoting tricks that evaluate
# to ``git push`` but break the token sequence above, e.g.
# ``git$(echo ' ')push``, ``git`echo`push``, ``git$()push``.  After stripping
# empty substitutions/backticks the residue is ``gitpush``; we also match a
# literal ``git_push``, a form kiro-cli denies too.
_GIT_PUBLISH_GLUE_RE = re.compile(r"git(?:\$\([^)]*\)|`[^`]*`)+push|git_push")

# Program NAME produced by an expansion the shell resolves to the git binary
# BEFORE exec, so the literal ``git`` token never appears in the source text and
# neither the regex above nor the normalizer (which does not expand arbitrary
# vars) sees it:
#   ``$(echo git) push``, `` `echo git` push ``, ``${GIT} push``, ``$GIT push``
# (where e.g. ``GIT=/usr/bin/git``).  We cannot execute the expansion to recover
# the program, so a ``push`` subcommand immediately following an unresolvable
# program token is treated as a publish (FAIL CLOSED); ``_is_push_to_protected_branch``
# then reads the push target and denies a protected / bare / ambiguous one while
# still allowing an explicit feature-branch target.  Ported from the upstream
# project.
_GIT_PUBLISH_SUBST_PROGRAM_RE = re.compile(
    r"(?:^|[;&|`\n])\s*"
    r"(?:\$\([^)]*\)|`[^`]*`|\$\{[^}]*\}|\$[A-Za-z_]\w*)"
    r"\s+push(?=\s|$|[)`;&|])"
)

# Human-readable label recorded in the denial reason + SEL audit event when
# a git-publish invocation is blocked (the regexes above are the mechanism).
_GIT_PUBLISH_DENY_LABEL = "git push"


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


#: The import name as it appears INSIDE a ``-c`` payload. A payload that both names the package
#: and calls something is the module form written longhand; matching the bare package name is
#: enough, because reaching the CLI at all requires importing it under one of these spellings —
#: PROVIDED the name is written literally, which the split/base64 forms below deliberately avoid.
_SELF_IMPORT_RE = re.compile(r"\bkiro_crew\b")

#: Dynamic-execution primitives that let an inline Python payload REACH the CLI without the
#: package name ever appearing as a literal token: string-concatenated imports
#: (``__import__('kiro'+'_crew')``), name-computed imports (``importlib.import_module(...)``),
#: and second-stage decode/eval (``exec(base64.b64decode(...))``). ``_SELF_IMPORT_RE`` cannot
#: see through any of these, so a payload combining an inline-program interpreter with one of
#: them is treated as opaque and DENIED — the same fail-closed reading as a literal import,
#: because "I cannot tell what this imports" is not "it is safe". Kept as a NARROW list of the
#: dynamic-exec verbs, not a blanket deny on all inline Python: ``python -c "print(1)"`` and
#: routine one-liners stay allowed, and the residual — arbitrary code that avoids even these
#: (``perl``, a written-then-run script, a renamed interpreter) — is out of a string matcher's
#: reach and is documented as such rather than papered over.
_INLINE_DYNAMIC_EXEC_RE = re.compile(
    r"\b__import__\s*\(|\bimportlib\b|\bimport_module\b|\bexec\s*\(|\beval\s*\(|"
    r"\bcompile\s*\(|\bb64decode\b|\bmarshal\b|\bgetattr\s*\("
)


def _inline_payload_reaches_cli(payload: str) -> bool:
    """True if an inline-program payload could import this package, LITERALLY or opaquely.

    Two ways: it names ``kiro_crew`` outright, or it uses a dynamic-execution primitive that
    could construct that import from pieces a static matcher cannot follow. The second is a
    deliberate over-match — a payload doing ``exec(...)`` or ``__import__(...)`` might import
    something else entirely — but on the credential-mint path "I cannot tell what this runs" is
    the fail-closed answer, and the cost is refusing an inline one-liner that happens to use
    ``exec``/``eval``, which is not a shape ordinary tooling relies on.
    """
    return bool(_SELF_IMPORT_RE.search(payload) or _INLINE_DYNAMIC_EXEC_RE.search(payload))


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
# Control operators that end one command and begin another.  Used to find the
# An EMPTY substitution expands to nothing, so ``p$()kill`` runs ``pkill`` -- the
# An OUTPUT redirect. Two small sets, enumerated from the shells' own grammars rather
# The same descriptor vocabulary, widened to INPUT redirects and process
# ``X=kirocrew; $X token`` assigns the program name to a variable and invokes it


def _self_token_frames(text_lower: str) -> "list[list[str]]":
    """The command's own argv plus the argv of every nested shell payload."""
    return [tokens for _source, tokens in _shell_payload_walk(text_lower)]


def _shell_payload_sources(text_lower: str) -> "list[str]":
    """*text_lower* plus the source text of every nested shell payload in it."""
    return [source for source, _tokens in _shell_payload_walk(text_lower)]


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


def _has_self_importing_inline_program(tokens: list[str], i: int) -> bool:
    """True if ``tokens[i]`` is an interpreter given a ``-c`` payload that imports this package.

    Separate from ``_is_self_module_invocation`` because the two answer different questions.
    That one asks "does this argv run our code?", which admits ``-m`` and ``-c`` alike and is
    the right input to a verb-gated decision. This one asks "is the code inline?", which is the
    case where the verb gate cannot hold: an inline payload can append to ``sys.argv``, call
    ``main(['token'])``, or reach the token-minting function directly, so no argv word has to
    say ``token``.

    Only the interpreter's own inline-program operand counts — the separate (``-c PAYLOAD``)
    and attached (``-cPAYLOAD``) spellings. A later positional that happens to mention the
    import name is data for whatever the payload does with it, not code we are about to run.

    The STDIN forms are the same escape without an operand: ``python -`` (and a bare ``python``
    with no script) read the program from stdin, so a ``python - <<'PY' … PY`` heredoc or an
    ``echo '…' | python -`` pipe reaches the CLI with the payload nowhere in argv. When that
    program text is visible on the command line, matching the import is the same fail-closed
    decision as for ``-c`` — but it is matched only in the tokens that actually CARRY that
    program (see :func:`_stdin_program_text`), not anywhere in the frame. When it is NOT
    visible (a bare ``python -`` fed by an unseen producer) there is nothing to match and the
    gate cannot see it; that residual is noted, not silently claimed as covered.
    """
    if not _PYTHON_PROGRAM_RE.match(_shell_normalizer._program_basename(tokens[i])):
        return False
    later_tokens = tokens[i + 1 :]
    glued = tokens[i].strip(_SHELL_WRAPPER_CHARS)
    if "<" in glued:
        # A redirect GLUED to the program name is still this command's redirect, and the
        # detector only ever saw the tokens AFTER the interpreter -- so `python<<EOF … EOF`
        # had no marker in view and its body read as a script path. Hand the suffix over as
        # its own token.
        later_tokens = [glued[glued.index("<") :], *later_tokens]
    # STDIN program: the text is not an operand of this interpreter — the shell fills stdin from
    # a heredoc body, a redirected file, or a pipe producer — so the search space is those
    # carriers rather than this position's operands. `_python_reads_stdin` is precise so this
    # does not fire for `python script.py`, `python -c …`, or `python -m …`.
    if _python_reads_stdin(later_tokens) and any(
        _inline_payload_reaches_cli(t.strip(_SHELL_WRAPPER_CHARS))
        for t in _stdin_program_text(tokens, i)
    ):
        return True
    expect_payload = False
    skip_next = False
    for later in later_tokens:
        # The PAYLOAD is matched RAW, not through `_normalize_operand`. That helper truncates at
        # the first control operator, which is correct for an operand the shell will split — but
        # a `-c` payload is a quoted program, so its `;` is Python, not a command separator.
        # Normalising `"import sys; ...; from kiro_crew.cli import main; main()"` down to
        # `import sys` hid the import entirely and let the bypass through.
        raw = later.strip(_SHELL_WRAPPER_CHARS)
        if expect_payload:
            if _inline_payload_reaches_cli(raw):
                return True
            expect_payload = False
            continue
        # The FLAG itself is a plain token, so it is safe (and more accurate) to normalise.
        stripped = _shell_normalizer._normalize_operand(later).strip("\"'")
        if skip_next:
            skip_next = False
            continue  # value consumed by an operand-taking flag (`-X dev`)
        if stripped in _PYTHON_INLINE_PROGRAM_FLAGS:
            expect_payload = True
            continue
        if len(raw) > 2 and raw[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
            if _inline_payload_reaches_cli(raw):
                return True
        if stripped in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        if len(stripped) > 2 and stripped[:2] in _PYTHON_OPERAND_FLAGS:
            continue  # attached operand, e.g. `-Xdev`
        # Only interpreter flags precede a `-c` operand. The first token that is neither a flag
        # nor a flag's operand is the interpreter's own positional (a script path or `-`), and
        # nothing after it is a `-c` payload — so stop, rather than scan the rest of the frame.
        # Without this bail the loop was O(tokens) for EACH python token, i.e. O(n²) on a
        # `python open python open …` spam input, which the ReDoS-resistance test caught.
        if not stripped.startswith("-"):
            break
    return False


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
        redirect = _shell_normalizer._output_redirect_scan(redirect_word)
        if redirect is not None:
            # An OUTPUT redirect and its target are not this command's arguments, and
            # neither says anything about where the program comes from -- so the walk has
            # to step over both and keep looking, exactly as it does for a stdin
            # redirect. Falling through instead read the leftover descriptor digits of
            # `2>&1` as a script path and answered False, so `python 2>&1 <<< '<program>'`
            # had its stdin program go unscanned. Bash runs every one of these.
            redirect_target, position = redirect
            # A chain of output redirects glued into ONE word (`>a>a>a...`) is walked
            # here, in place. Re-injecting each remainder into the token stream instead
            # re-sliced the word per operator, which is quadratic in its length on a
            # floor that runs for every command.
            while position < len(redirect_word):
                further = _shell_normalizer._output_redirect_scan(redirect_word, position)
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
        norm = _shell_normalizer._normalize_operand(tok).strip("\"'")
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
#   * the literal name after stripping quotes/backslashes (`k""iro""crew`,
#     `ki\rocrew` — shlex removes those before the predicates compare).
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
    stripped = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", text_lower)
    if _SELF_FLOOR_NAME_HINT_RE.search(stripped):
        return True
    return bool(_INLINE_DYNAMIC_EXEC_RE.search(stripped))


def _is_credential_mint(text_lower: str) -> bool:
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
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        for i, token in enumerate(tokens):
            # AN INLINE PROGRAM THAT IMPORTS OUR CLI IS DENIED WITHOUT NEEDING THE VERB, and
            # this is checked FIRST because it does not depend on the self-program/module gate
            # below. Everywhere else the verb is the trigger, because ``kirocrew doctor`` is
            # legitimate and only ``kirocrew token`` mints. That reasoning does not survive an
            # inline program: ``-c`` and stdin (``python -``) both run arbitrary Python with
            # the interpreter's full authority, so it can BUILD the verb rather than pass it —
            # ``python -c "import sys; sys.argv.append('token'); from kiro_crew.cli import main;
            # main()"`` names no ``token`` argv word, and ``python - <<'PY' … PY`` puts the
            # program on stdin, off argv entirely. The honest gate is the import. Scoped to
            # ``_SELF_IMPORT_RE``, so ``python -c "print(1)"`` and a bare ``python -`` running
            # unrelated code are untouched.
            if _has_self_importing_inline_program(tokens, i):
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
            if _data_consumer_exempt(i, token, programs, tokens):
                continue
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
                    # bodies in the segment no longer share this one's index.
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


def _backtick_closer(source: str, start: int) -> int:
    """Index of the backtick that CLOSES a substitution opened before *start*.

    Within backticks bash strips a backslash before ``$``, ``\\``` and ``\\\\``,
    so an escaped backtick is data and must not be taken as the closer --
    ``str.find`` did, and it truncated ``kill `printf '\\`' ; pgrep -f <name>```
    one clause short of the target's name (found in pre-push review, bash-
    measured: the inner command past the escaped backtick runs).  Quotes do NOT
    protect a backtick from closing, so this scan honours backslashes only.

    -1 when no unescaped closer exists before the text ends.
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


def _is_self_kill(text_lower: str) -> bool:
    """True if *text_lower* terminates a Kiro Crew process.

    Two shapes, matched separately because the two kill families take different
    kinds of target:

    * ``pkill``/``killall`` select processes BY NAME, so the product name in any
      argument IS the target -- including inside a quoted pattern such as
      ``pkill -f '[;]*kirocrew'``, where a raw-string regex mis-reads the quoted
      ``;`` as a command separator and stops scanning short of the name.
    * bare ``kill`` takes PIDs, so it can only aim at the product through a
      command substitution that resolves the name to one (``kill $(pgrep -f
      kirocrew)``, ``kill $(pidof kirocrew)``, ``kill $(cat /run/kirocrew.pid)``,
      backticks).  A ``kill <pid>`` alongside a command that merely mentions a
      product path is NOT a self-kill -- that is the false positive this
      structural check avoids.
    """
    # Perf short-circuit: both loops below re-run the payload descent.
    # A kill can only target the product if the gate's necessary condition
    # holds, so a miss skips both descents.
    if not _self_floor_can_fire(text_lower):
        return False
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        for i, token in enumerate(tokens):
            if not _is_kill_by_name_program(token):
                continue
            # ``echo pkill kirocrew`` prints two words; it does not kill anything.
            if _data_consumer_exempt(i, token, programs, tokens):
                continue
            # Check each argument for the target BEFORE testing whether it ends the
            # argv, then stop.  Order matters: the target is often a quoted pattern
            # whose own characters look like separators (``pkill -f '[;]*kirocrew'``),
            # so testing the boundary first would discard the very argument that
            # names the target.  Stopping after it keeps an unrelated later command
            # out of the match (``pkill other; echo kirocrew`` is not a self-kill).
            depth = 0
            for arg in tokens[i + 1 :]:
                # Search the raw arg AND its normalized form.  Normalizing alone is
                # not enough: a pkill pattern is an ERE, so a ``>`` inside it is part
                # of the TARGET (``pkill -f '>kirocrew'``) and stripping it as a
                # redirect would discard the name.  Raw alone is not enough either --
                # an empty substitution (``kiro$()crew``) only reads as the name once
                # removed.  Either match is a hit.
                if _SELF_NAME_RE.search(_debracket(arg)) or _SELF_NAME_RE.search(
                    _shell_normalizer._normalize_operand(arg)
                ):
                    return True
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
    for source, frame in _shell_payload_walk(text_lower):
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
                    return True
            for body in _substitution_bodies(" ".join(own)):
                # ``kill $(pgrep -f kiro${x:-crew})`` hides the name behind an
                # expansion whose literal branch the shell substitutes back in, so
                # resolve those defaults before searching.
                if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                    _resolve_param_defaults(body)
                ):
                    return True
        # The window above is bounded by ``_substitution_depth_delta`` on
        # DE-QUOTED tokens, so a quoted close-paren reads as a real closer and
        # closes the window early, dropping the clause that names the target
        # (``kill $(printf ')' ; pgrep -f kirocrew)``).  Re-derive the same
        # window from the RAW text, where the quotes still exist.
        for body in _bare_kill_raw_bodies(source):
            if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                _resolve_param_defaults(body)
            ):
                return True
    return False


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


def _self_cli_operands(tokens: "list[str]", i: int) -> "list[str]":
    """Non-flag operand words the product CLI at program index *i* receives, in order.

    A token that stays a ``-``/``--`` word after quote/redirect normalization is a
    global flag and is skipped -- the self-protection top-level flags are all
    valueless (``-v``/``--verbose`` count, ``--no-jail`` bool), so a skipped flag
    never hides an operand behind it. A shell redirection (and its separate
    target, if any) is removed from argv by the shell and is skipped too, so
    ``kirocrew 2>/tmp/x restart`` still reads ``restart`` as the leading operand.
    Quoting is resolved by ``_normalize_operand``; the walk stops at the argv
    boundary so a chained later command's words are not attributed here.
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
            continue
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
        # Once per FRAME, not once per token: this is the loop whose per-token scan
        # made the floor quadratic.
        scan = _self_module_flag_scan(tokens)
        for i in range(len(tokens)):
            prog_idx = _self_program_index(tokens, i, scan)
            if prog_idx is None:
                continue
            # ``echo kirocrew restart`` / ``echo python -m kiro_crew restart`` print words.
            if _data_consumer_exempt(prog_idx, tokens[prog_idx], programs, tokens):
                continue
            if _operands_lead_with(_self_cli_operands(tokens, prog_idx), spec):
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


def _is_git_publish(text_lower: str) -> bool:
    """Return True if *text_lower* invokes ``git push`` (verb-anchored).

    Uses a two-pass approach:

    1. **Fast first-pass (regex):** ``_GIT_PUBLISH_RE`` and
       ``_GIT_PUBLISH_GLUE_RE`` catch normal ``git push`` invocations and
       command-substitution glue-evasion (e.g. ``git$(echo ' ')push``);
       ``_GIT_PUBLISH_SUBST_PROGRAM_RE`` catches expansion-produced program
       names (``$(echo git) push``, ``${GIT} push``, ``$GIT push``).
    2. **Normalizer second-pass:** ``normalize_shell_command`` strips quotes
       and empty-string concatenation so evasions like ``"git" push``,
       ``g""it push``, or ``'g'it push`` are resolved to their true tokens.

    Does NOT match ``git stash push``, ``git commit -m '...push...'``,
    ``git log --grep push``, etc.

    Operates on an already-lowercased string.
    """
    # Pass 1: regex fast-path
    if (
        _GIT_PUBLISH_RE.search(text_lower)
        or _GIT_PUBLISH_GLUE_RE.search(text_lower)
        or _GIT_PUBLISH_SUBST_PROGRAM_RE.search(text_lower)
    ):
        return True

    # Pass 2: normalizer-based detection (catches quote evasions like
    # "git" push, g""it push, 'g'it push)
    return _is_git_push_via_normalizer(text_lower)


# Git global flags that consume a separate argument token (appear between
# `git` and the subcommand).
_GIT_ARG_FLAGS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})


def _is_git_push_via_normalizer(text_lower: str) -> bool:
    """Normalizer-based git push detection (second pass).

    Tokenizes the command via ``normalize_shell_command``, then checks if
    any token sequence resolves to ``git`` followed by ``push`` as the
    subcommand (skipping flags and their arguments, and skipping empty or
    whitespace-only words in the subcommand seek, which git never resolves
    a command name from.)

    Avoids false positives on ``git stash push`` by requiring ``push`` to
    be the FIRST non-flag token after ``git`` (the subcommand position).
    """
    try:
        tokens = _shell_normalizer.normalize_shell_command(text_lower)
    except Exception:
        return False

    if not tokens:
        return False

    # Glued operators are not part of the word: ``(git`` is the git program and
    # ``push)`` is the push subcommand. But these tokens come from
    # ``normalize_shell_command``, which has ALREADY tokenized and dequoted, so
    # punctuation surviving inside a token is part of the WORD -- and cutting
    # there truncated a legal executable path (``/opt/my(dir)/git`` ->
    # ``/opt/my``, whose basename is not ``git``), which NARROWED detection and
    # let a protected push through. Replacing the token was therefore not the
    # widen-only step its previous comment claimed.
    #
    # Both spellings are consulted instead, so the claim actually holds: a token
    # counts when EITHER its raw form or its operator-cut form resolves to the
    # word. That is a superset of both readings, and detection can only ever
    # grow -- the allow/deny decision still rests with
    # ``_is_push_to_protected_branch``.
    def _resolves_to(token: str, word: str) -> bool:
        for candidate in (token, _cut_at_operator(token)):
            if candidate == word or os.path.basename(candidate) == word:
                return True
        return False

    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Check if this token resolves to "git"
        if _resolves_to(token, "git"):
            # Skip global flags and their arguments to find the subcommand.
            #
            # A zero-width or whitespace-only word is also skipped.  It is a
            # real argv element the shell hands over, and git does NOT ignore
            # it -- git takes it as its command name and
            # exits.  Skipping it is deliberate fail-closed OVER-detection: it
            # widens only DETECTION, and a spelling it newly reaches either
            # fails to run at all (git rejects the zero-width command name) or
            # was already reached in its adjacent spelling, so no runnable
            # push gains an escape.  What the floor DOES with a newly-detected
            # spelling is the ungated anti-obfuscation branch, not the
            # protected-branch rule: ``_git_push_args`` anchors on the raw
            # split and does not skip the empty word, so the parse fails and
            # ``_git_publish_floor_tags`` denies unconditionally
            # (``_GIT_PUBLISH_UNGATED``) -- the right treatment for a spelling
            # git itself cannot run.  ``str.strip()``'s whitespace set is
            # wider than POSIX IFS (NBSP, U+2000..200A, ...) and deliberately
            # so: every extra character it treats as skippable is still a word
            # git takes as its command name and rejects, and a skipped token
            # can never be the subcommand token, so the breadth only ever ADDS
            # detection -- do not narrow it to a literal space/tab set.  No
            # matching guard is needed in program position: a zero-width word
            # never resolves to the program word (``_resolves_to`` cannot
            # yield ``git`` from it), so the outer loop already steps past it.
            j = i + 1
            while j < len(tokens):
                if not tokens[j].strip():
                    j += 1  # zero-width/whitespace-only word
                elif tokens[j] in _GIT_ARG_FLAGS:
                    j += 2  # skip flag + its argument
                elif tokens[j].startswith("-"):
                    j += 1  # skip simple flag
                else:
                    break
            if j < len(tokens) and _resolves_to(tokens[j], "push"):
                return True
        i += 1
    return False


_PROTECTED_BRANCHES = {"main", "mainline", "master"}  # wokeignore:rule=master

# Push flags that push EVERY local branch (protected ones included) regardless
# of any explicit refspec, so a per-branch target check cannot vouch for them.
# Presence of any of these denies the push outright (kept in lockstep with the
# ``--(mirror|all)`` regex in config/defaults.json).
_PUSH_ALL_BRANCHES_OPTS = frozenset({"mirror", "all", "branches"})

#: Flags that CARRY the repository as their own value, so the repository is not
#: among the positional tokens. Git accepts ``--repo=<x>`` (and the separated
#: ``--repo <x>``), and both spellings start with ``-`` — so a naive "strip the
#: flags, the first positional is the remote" read treats the sole remaining
#: token as the REMOTE when it is really the refspec. That mis-parse routes
#: ``git push --repo=origin main`` to the single-arg rule instead of the
#: protected-branch rule. The rules are individually disableable, so with the
#: single-arg rule switched off that mis-parse publishes to ``main``.
_PUSH_REPO_OPTS = frozenset({"repo"})

#: The ARITY table: push options that take a REQUIRED value git also
#: accepts as a SEPARATED token (``--push-option ci.skip``). The token scan
#: must consume that value or it leaks into the positional list, where it is
#: read as a remote/refspec — and because an option value like ``ci.skip``
#: normalizes to a non-protected name, the tag set for an otherwise-bare
#: publish comes back EMPTY. An empty tag set IS the allow decision, so one
#: extra flag switches the protected-branch floor off. Same shape as
#: ``_PUSH_REPO_OPTS`` (which stays separate because its value being the
#: REMOTE also shifts the positional split), resolved through
#: ``_push_option_matches`` so abbreviations keep working.
#: Attached forms (``--push-option=x``) bind the value inside the token and
#: never disturb the split, so they need no entry here. (``repo`` itself is
#: deliberately NOT unioned in: the dedicated ``_PUSH_REPO_OPTS`` branch runs
#: first and would make the member unreachable.)
_PUSH_VALUE_OPTS = frozenset({"push-option", "receive-pack", "exec"})

#: Long push options that never consume the NEXT token: booleans, plus the
#: optional-value options (``--signed``, ``--force-with-lease``) whose value
#: git binds in ATTACHED form only. ``--no-*`` negations are recognised
#: structurally (git's negation never takes a separate value), so they are not
#: enumerated. ``recurse-submodules`` is deliberately ABSENT: listing an
#: option here vouches that its separated neighbour is a positional, and being
#: wrong about that is exactly the erasure the ARITY table prevents — so an
#: option whose arity is not modelled with confidence falls to the protective
#: fallback instead.
_PUSH_NO_VALUE_OPTS = frozenset(
    {
        "atomic",
        "delete",
        "dry-run",
        "follow-tags",
        "force",
        "force-if-includes",
        "force-with-lease",
        "ipv4",
        "ipv6",
        "porcelain",
        "progress",
        "prune",
        "quiet",
        "set-upstream",
        "signed",
        "tags",
        "thin",
        "verbose",
        "verify",
    }
)

#: Short-option arity, resolved the way git resolves a bundle: booleans may
#: stack (``-fq``), and the first value-taking short consumes the REST of the
#: token as its attached value (``-oci.skip``) or, when the rest is empty, the
#: NEXT token (``-o ci.skip`` — or ``-fo ci.skip``, which is ``-f -o ci.skip``).
_PUSH_VALUE_SHORTS = frozenset({"o"})
_PUSH_NO_VALUE_SHORTS = frozenset({"f", "n", "q", "v", "u", "d", "4", "6"})


# Symbolic refs that resolve at runtime — cannot statically verify safety.
# If the agent is on main and pushes HEAD, it pushes to main on the remote.
_AMBIGUOUS_REFS = {"head", "@", "fetch_head"}

# Refspec spellings that resolve only at runtime: ``@{upstream}`` / ``@{u}``
# git-revision syntax. (No ``$``/backtick branch here: the per-token ``$``
# check and the segment-level expansion ungate both run before any refspec
# reaches this, so such a branch would be a shadowed duplicate.)
_AMBIGUOUS_REFSPEC_RE = re.compile(r"@\{")


def _git_push_args(segment: str) -> list[str] | None:
    """Return the tokens AFTER the ``push`` subcommand if *segment* is a git push.

    Pure-Python (no regex backtracking — CodeQL ReDoS-safe) replacement for a
    ``\\bpush\\b`` scan. It anchors ``push`` as the git subcommand — the first
    non-flag token after ``git`` — so a segment that merely contains the word
    "push" (e.g. ``echo remember-to-push``) is NOT treated as a push and
    returns None. Skips leading flags, and a single non-flag value that a flag
    may take (e.g. ``-C <path>``) — but never swallows ``push`` itself.
    """
    # Strip glued shell operators for the same reason as ``_dequote_token``:
    # ``(git`` IS the git program to bash, and ``main)&`` IS the ref ``main``.
    raw_tokens = _split_shell_words(segment)
    tokens = [_cut_at_operator(t) for t in raw_tokens]
    # Anchoring compares against a DEQUOTED view, because a quoted ``"git"`` is
    # still the git program to bash. Matching the raw token missed it and
    # anchored on a LATER unquoted ``git push`` instead, returning only that
    # push's arguments -- so appending a benign second push hid the first one's
    # protected ref entirely and turned a fail-closed segment into an allow.
    #
    # The view is separate on purpose: the RETURNED tokens keep their quoting,
    # because callers dequote them once more, and dequoting twice would read a
    # literal ``'(main)'`` ref as the operators ``(``/``)`` around ``main`` and
    # deny a branch that is legitimately pushable.
    anchors = [_dequote_token(t) for t in tokens]

    # Resolution mirrors the publish floor's ``_resolves_to``: a token IS git
    # when either its raw or its operator-cut spelling equals the word or has it
    # as a basename. An exact ``== "git"`` test skipped a path-qualified
    # ``/usr/bin/git`` and anchored on a NESTED ``>(git push origin
    # my-feature)`` instead, so the feature branch that process substitution
    # pushes vouched for the protected push in front of it. Selecting the FIRST
    # resolving anchor can only move the anchor earlier than the exact test did,
    # which is the fail-closed direction: the push that must be judged is the
    # leading one.
    def _anchor_is_git(index: int) -> bool:
        # The untouched spelling is consulted as well: both ``_cut_at_operator``
        # and ``_dequote_token`` truncate at an operator that lives INSIDE a path
        # component (``/opt/my(dir)/git`` -> ``/opt/my``), which is exactly the
        # narrowing already fixed in the publish floor. Quotes are stripped
        # without cutting so a quoted absolute path still resolves.
        raw = raw_tokens[index]
        for candidate in (anchors[index], raw, raw.strip("'\"")):
            if candidate == "git" or os.path.basename(candidate) == "git":
                return True
        return False

    start = next((k for k in range(len(anchors)) if _anchor_is_git(k)), None)
    if start is None:
        return None
    i = start + 1
    while i < len(anchors) and anchors[i].startswith("-"):
        i += 1  # skip the flag
        # A flag may take one separate non-flag value (e.g. ``-C <path>``);
        # never consume the ``push`` subcommand as a flag value.
        if i < len(anchors) and not anchors[i].startswith("-") and anchors[i] != "push":
            i += 1
    if i < len(anchors) and anchors[i] == "push":
        # A redirection is SKIPPED, not treated as the end of the argument list.
        #
        # Its words are not refspecs -- stripping glued operators made
        # ``>(git push origin my-feature)`` read as ordinary refspecs, so a bare
        # ``git push``, which must fail closed, inherited a branch it never named
        # -- but the words AFTER it are. Truncating there dropped them, and bash
        # keeps them: ``git push origin feature 2>/dev/null main`` really runs
        # ``git push origin feature main``, so a trailing protected ref left the
        # gate while still reaching the server.
        #
        # Boundaries are read off the RAW spelling, because that is where the
        # redirection character still exists. A file target is one word, glued
        # (``2>/dev/null``) or spaced (``> out``); a PROCESS SUBSTITUTION target
        # is a whole command line, so it is skipped to its matching ``)`` rather
        # than by one word.
        #
        # A token that OPENS with ``<(`` / ``>(`` is process substitution, which
        # bash reads as a WORD, not a redirection -- so it is returned rather than
        # skipped. Skipping it consumes an option's value (``-o <(echo)``) and
        # shifts the positional split onto the remote, downgrading a push of a
        # protected branch to the disableable single-arg row.
        #
        # Redirection arity comes from ``_push_token_redirection``, the model the
        # argument scan itself uses, rather than a second reading of the same
        # grammar here. A local reading that treats the ``-`` of ``<<-`` as an
        # ATTACHED target leaves the tab-stripping heredoc's separated delimiter
        # word standing as a phantom refspec and erases the tag -- a shape already
        # closed one layer down. One model, one place.
        #
        # The tokens are returned in their RAW spelling. The caller's scan is
        # defined over raw words -- it splits each one at its own unquoted
        # operators and models redirection arity itself -- so handing it
        # operator-CUT words erases the shapes it classifies by (``origin>``
        # reading as a plain remote, ``@(main)`` as the ambiguous ref ``@``, a
        # lone ``&`` as an empty token).
        args: list[str] = []
        raw_args = raw_tokens[i + 1 :]
        k = 0
        while k < len(raw_args):
            if raw_args[k].startswith(_PROCESS_SUBSTITUTION_OPENERS):
                args.append(raw_args[k])
                k += 1
                continue
            is_redirection, consumes_next = _push_token_redirection(raw_args[k])
            if not is_redirection:
                args.append(raw_args[k])
                k += 1
                continue
            if not consumes_next and "(" in raw_args[k]:
                # A redirection whose ATTACHED target opens a paren
                # (``2>(cat ... )``): not the bare process-substitution word
                # (that is caught above) and not a file target -- the parens
                # span later words, and skipping this one as a self-contained
                # redirection leaves the body's remainder (``>/dev/null # fake
                # )``) to be read as argv, where the ``#`` truncates the real
                # refspecs. Whatever bash makes of
                # the spelling, this gate cannot read it: fail CLOSED.
                return None
            k += 1
            if not consumes_next or k >= len(raw_args):
                continue
            following = _REDIRECT_START_RE.match(raw_args[k])
            target = (raw_args[k][following.end() :] if following else raw_args[k]) or raw_args[k]
            if not target.startswith("("):
                k += 1  # ordinary file target -- one word
                continue
            # PROCESS-SUBSTITUTION BOUNDARY, walked QUOTE-AWARELY and proven.
            #
            # The target is a whole command line, so it ends at its matching
            # unquoted ``)``. Counting the parens per word with str.count is
            # quote-UNAWARE, and that is a live bypass: in
            # ``git push origin feature > >(echo '(' ) main`` the QUOTED ``(``
            # inflates the depth to 2, the real ``)`` only returns it to 1, and
            # the trailing ``main`` is swallowed into the substitution -- so a
            # protected-branch push comes back with the remaining words alone and
            # is allowed. ``> >(printf "(") main`` is the same shape with double
            # quotes. The shared state machine ignores quoted parens, so the
            # boundary lands where bash puts it.
            #
            # FAIL CLOSED when the boundary cannot be PROVEN complete -- the
            # words ran out with the substitution still open (``>(echo main``),
            # or a quote is still open at the end. Silently swallowing the rest
            # of the segment is precisely how an unterminated construct hides a
            # refspec. Returning None routes the segment to the caller's
            # unparseable branch, which emits the non-opt-out-able ambiguity
            # sentinel. A PROVEN boundary is a word; an UNPROVABLE one is
            # ambiguous.
            #
            # PROVEN is also refused for a body word the quote walk cannot READ
            # (``_process_substitution_word_is_opaque``): the paren count models
            # quoting, and nothing else -- so a construct outside quoting moves
            # the real closer or hides the program without the count noticing. A
            # word-initial ``#`` comments out the ``)`` after it (``>(cat
            # >/dev/null # fake )`` + newline + ``) main`` pushes main); a
            # reserved word makes the body a compound command whose ``)`` is
            # SYNTAX (``>(case x in x) git push;; esac)`` runs the bare push); an
            # unquoted glob or expansion in a body word means the program the
            # payload walk judges the skipped body by resolves only at run time
            # (``>(/usr/bin/g?t push origin main)`` closes cleanly and the walk
            # sees no ``git``). Modelling them one at a time is unbounded, so the
            # rule is the class: a body word the walk cannot read is not a
            # redirection the gate may skip.
            depth = 0
            state = 0
            ansi = False
            proven = False
            opener = k
            while k < len(raw_args):
                if _process_substitution_word_is_opaque(
                    raw_args[k], state, ansi, first=k == opener
                ):
                    return None
                walk = _shell_quote_walk(raw_args[k], state=state, ansi=ansi)
                depth += walk.paren_delta
                state, ansi = walk.end_state, walk.end_ansi
                k += 1
                if state == 0 and depth <= 0:
                    proven = True
                    break
            if not proven:
                return None
        return args
    return None


#: bash reserved words. Any of them as an UNQUOTED word inside a
#: process-substitution body means the body is a compound command whose ``)``
#: may be SYNTAX (``case x in x)``) rather than the closer.
_SHELL_RESERVED_WORDS = frozenset(
    {
        "!",
        "[[",
        "]]",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
        "{",
        "}",
    }
)

#: The characters an UNQUOTED process-substitution body word may consist of and
#: still be one the redirect skip may step over: the alphabet of an ordinary
#: program invocation -- letters, digits, and ``/ - _ . = :`` (paths, flags,
#: ``KEY=value``, ``host:port``). Everything else is refused as OPAQUE. This is
#: an ALLOWLIST on purpose: a denylist (``*?[``, extglob ``(``, ``#``,
#: ``& ; |``) grows by one shell metacharacter at a time, and an enumeration of
#: what bash can do with a character is never finished. Quoted text is not
#: judged here at all -- the quote walk owns
#: it -- and ``$'`` (the ANSI-C quote that walk models) is the one active ``$``
#: admitted, so ``> >(echo '(' ) main`` and ``> >(echo $'a\'b') main`` keep
#: their precise reading while a glob, an extglob or nested paren, a comment, a
#: control operator, a tilde, a history ``!``, a brace or an expansion in a
#: body word all fail closed: with such a word the shell either moves the real
#: closer or resolves the program only at run time, and either way the payload
#: walk that judges the skipped body cannot see what bash runs.
_PROCESS_SUBSTITUTION_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_.=:"
)


def _process_substitution_word_is_opaque(word: str, state: int, ansi: bool, *, first: bool) -> bool:
    """True when *word*, read from quote state ``(state, ansi)`` inside a
    process-substitution body, is NOT plainly readable: an unquoted reserved
    word, or any unquoted character outside
    :data:`_PROCESS_SUBSTITUTION_SAFE_CHARS` other than the ``$`` of an ANSI-C
    ``$'...'`` and a ``)`` that ends the word (the construct's closer, which
    the caller's depth walk accounts for). *first* marks the opener word, whose
    text up to and including the ``(`` is the ``>(`` / ``2>(`` opener rather
    than body.
    """
    body = word[word.index("(") + 1 :] if first else word
    if state == 0 and body.rstrip(")") in _SHELL_RESERVED_WORDS:
        return True
    steps = list(_iter_shell_chars(body, state, ansi))
    for index, step in enumerate(steps):
        if not step.active and step.state == 0 and step.text.startswith("\\"):
            # An UNQUOTED escape pair: ``\g\i\t`` reaches the program as
            # ``git`` while no scanner word spells it.  Inside quotes the walk
            # owns the backslash; outside them it is a spelling the allowlist
            # must not see through.
            return True
        if not step.active and step.state == 2 and step.text == step.char and step.char in "$`":
            # Double quotes do NOT suspend expansion: ``"$GIT" push origin
            # main`` runs whatever ``$GIT`` names.
            # An unescaped ``$`` or backtick inside double quotes is an
            # expansion the scan cannot resolve, so the word is opaque; an
            # escaped ``\$`` (text ``\$``) stays data.
            return True
        if not step.active or step.char in _PROCESS_SUBSTITUTION_SAFE_CHARS:
            continue
        if step.char in "'\"":
            continue  # a quote DELIMITER: the quote walk owns what it encloses
        if step.char == ")" and index == len(steps) - 1:
            continue  # the closer, accounted for by the caller's depth walk
        if step.char == "$":
            following = steps[index + 1] if index + 1 < len(steps) else None
            if following is not None and following.char == "'" and following.ansi:
                continue  # ``$'...'`` -- the ANSI-C quote, modelled by the walk
        return True
    return False


def _normalize_ref(ref: str) -> str:
    """Reduce a push destination ref to the bare branch name git resolves it to.

    Git accepts several destination-side spellings that all resolve to the same
    branch server-side: ``main``, ``heads/main``, ``refs/heads/main``,
    ``remotes/<remote>/main``, ``refs/remotes/<remote>/main``. Stripping only
    ``refs/heads/`` let ``heads/main`` and the ``remotes/`` forms dodge the
    protected-name check (they still resolve to a protected branch on the
    server). Normalize every spelling to the bare name so the comparison cannot
    be evaded by ref-path spelling.
    """
    ref = ref.removeprefix("refs/")
    if ref.startswith("remotes/"):
        parts = ref.split("/", 2)  # remotes/<remote>/<branch>
        if len(parts) == 3:
            return parts[2]
    return ref.removeprefix("heads/")


def _push_segment_targets_protected(arg_tokens: list[str]) -> frozenset[str]:
    """Return the git-publish rule tags a single push's argument tokens trip.

    *arg_tokens* are the tokens following the ``push`` subcommand within ONE
    shell segment (separators already removed).  An EMPTY result means this
    segment is an explicit feature-branch push and is allowed.

    Each returned tag is either a ``git-publish`` catalog rule id (the caller
    denies only while that rule is still enabled, so an operator opt-out is
    honoured) or :data:`_GIT_PUBLISH_UNGATED` for the anti-obfuscation branches,
    which are NOT opt-out-able: they are what makes the gated tags
    non-bypassable, since a refspec the shell fuses together cannot be checked
    against a branch name at all.

    ALL refspecs are collected rather than short-circuiting on the first hit: a
    refspec that trips a DISABLED rule must not allow the push when a sibling
    refspec trips an enabled one.

    A bare push (no explicit branch) is reported because the current branch
    might be a protected one.  Force flags (``--force``/``-f``/
    ``--force-with-lease``) do NOT by themselves make a feature-branch push
    protected — force-push to a feature branch is a normal PR/rebase workflow —
    but a force-push to a protected branch is still reported, because the target
    check below fires regardless of any flags (force flags are stripped first).
    """
    tags: set[str] = set()
    tokens = [_dequote_token(t) for t in arg_tokens]
    # Flags that push ALL local branches (protected ones included) bypass any
    # per-branch target check.  Detected BEFORE stripping flags, and resolved the
    # way GIT resolves them, so an abbreviation (``--mirr``) counts.
    if any(_push_option_matches(tok, _PUSH_ALL_BRANCHES_OPTS) for tok in tokens):
        tags.add("git-publish-push-mirror-all")
    # Skip flags (tokens starting with -); non_flags[0] is the remote and
    # non_flags[1:] are the refspecs/branches. Option ARITY is modelled
    # explicitly: a flag that CARRIES the repository (``--repo=x`` /
    # ``--repo x``) means the remote is NOT positional, a value-taking option's
    # SEPARATED value is consumed so it is never read as a remote/refspec, and
    # any option the scan does not recognise poisons the positional split
    # entirely (see the fail-protective fallback below) — because trusting a
    # split that may contain a leaked option value is how the floor tag was
    # erased. A bare ``--`` ends option parsing, exactly as git reads it.
    repo_in_flag = False
    positional_only = False
    non_flags: list[str] = []
    skip_next = False
    # One shared quote/escape walk per raw token yields both shell signals:
    # operator PIECES (unquoted < > & split the word) and OPEN STATE (an
    # unterminated quote or trailing escape means the shell fused a
    # whitespace-spanning word this whitespace tokenizer split apart). Either
    # signal means no per-token reading of the split can be trusted.
    shell_reads = [_push_token_shell_read(t) for t in arg_tokens]
    # ``#`` at the start of a WORD comments out the REST of the segment, so
    # the shell never passes those tokens to git: truncate before any other
    # reading, or ``git push origin #main`` scans a phantom refspec while the
    # shell runs a remote-only push. A ``#`` is word-initial only when the
    # whitespace before it was a REAL separator: if ANY earlier token leaves
    # the shell state open (trailing escape / unterminated quote fuses across
    # the split), the ``#`` may be mid-word — truncating there discards a
    # real trailing refspec (an escaped-space option value fused into ``#x``
    # drops ``main`` from the scan, leaving only the disableable bare tag).
    # With an open token seen, truncation is skipped entirely: the open state
    # already poisons the
    # split protectively and the superset scan keeps every later positional
    # visible.
    _open_seen = False
    for _idx, _raw in enumerate(arg_tokens):
        if _raw.startswith("#") and not _open_seen:
            arg_tokens = arg_tokens[:_idx]
            tokens = tokens[:_idx]
            shell_reads = shell_reads[:_idx]
            break
        _open_seen = _open_seen or shell_reads[_idx][1]
    unrecognised_option = any(open_state for _pieces, open_state in shell_reads)
    # A segment whose CUMULATIVE quote/escape state is still open at its end
    # continues into the NEXT line: bash line continuation (backslash-newline
    # vanishes entirely) and quoted newlines splice words ACROSS the segment
    # split, so the real refspec may be assembled from pieces this segment
    # cannot see — ``origin ma\`` + newline + ``in`` pushes MAIN while no
    # token here spells it. An
    # unreconstructable name gets the same posture as ``ma$in``: the ungated
    # sentinel, which no catalog row can switch off. Deliberately NARROWER
    # than ungating on any per-token open state: a MID-segment open (a quoted
    # value containing a space, whose quote closes before segment end) stays
    # on the DISABLEABLE fallback, because joining within one segment can
    # only fuse whitespace into the word — never a valid refname — and every
    # piece stays visible to the superset scan below. The cumulative state is
    # the per-token walk run over the joined segment (whitespace is inert to
    # the state machine).
    if arg_tokens and _push_token_shell_read(" ".join(arg_tokens))[1]:
        tags.add(_GIT_PUBLISH_UNGATED)

    def _classify_word(word: str) -> None:
        """Read ONE argv word exactly as git's option parser would.

        The single place a word becomes either an option (with its arity) or a
        positional. Stripping shell punctuation off a word changes WHERE the
        word came from, never WHAT it is, so every branch that recovers a word
        from operator glue routes it through here instead of appending it to
        ``non_flags`` directly. Appending unconditionally is how ``(git push
        --repo=origin -f)`` erases the floor: the ``)`` is stripped, ``-f`` is
        filed as a refspec, it matches no protected name, and the segment comes
        back with NO tags at all — a force push to a possibly-protected current
        branch, admitted by adding one parenthesis. The same spelling without
        parens is correctly bare.
        """
        nonlocal skip_next, positional_only, repo_in_flag, unrecognised_option
        if skip_next:
            # The separated value of a value-taking option: consumed, so it is
            # never read as a remote or a refspec.
            skip_next = False
            return
        if not word:
            return
        if positional_only or word == "-" or not word.startswith("-"):
            # A lone ``-`` is an OPERAND to git's option parser (a repository
            # spelled ``./-`` is addressable) — skipping it as a flag shifts
            # the real refspec into the remote slot and downgrades the row.
            non_flags.append(word)
            return
        if word == "--":
            positional_only = True
            return
        if _push_option_matches(word, _PUSH_REPO_OPTS):
            repo_in_flag = True
            skip_next = "=" not in word
            return
        if "=" in word:
            # An attached value binds inside the token — whatever the option is,
            # it cannot disturb the positional split.
            return
        if word.startswith("--"):
            if _push_option_matches(word, _PUSH_VALUE_OPTS):
                skip_next = True
            elif not (
                word.startswith("--no-")
                or _push_option_matches(word, _PUSH_NO_VALUE_OPTS)
                or _push_option_matches(word, _PUSH_ALL_BRANCHES_OPTS)
            ):
                unrecognised_option = True
            return
        # Short-option token: resolve the bundle char by char like git does.
        for i, ch in enumerate(word[1:]):
            if ch in _PUSH_VALUE_SHORTS:
                # Rest of the token is the attached value; consume the NEXT
                # token only when there is no rest.
                skip_next = i == len(word) - 2
                break
            if ch not in _PUSH_NO_VALUE_SHORTS:
                unrecognised_option = True
                break

    pending_redirection_target = False
    for raw, tok, (operator_pieces, _open) in zip(arg_tokens, tokens, shell_reads):
        if tok:
            # Word-producing shell syntax makes ANY token unverifiable, no
            # matter which slot the split assigns it: ``V='ci.skip main'; git
            # push --repo=origin --push-option $V`` expands and word-splits
            # AFTER this scan, handing git a ``main`` refspec the split never
            # saw — and consuming the literal ``$V`` would drop that case from
            # the ungated posture (where the leaked value hits the refspec
            # ambiguity check) to the disableable bare rule. A ``$`` anywhere
            # therefore lands on the ungated branch, the same posture as
            # ``ma$in``; ``$(``/``${``/backticks never reach here because the
            # caller's expansion regex already ungated the whole segment.
            # Glob characters (``* ? [``) are pathname expansion — a file
            # named ``main`` makes ``ma[i]n`` push main — and none of them is
            # legal in a refname, so they take the wildcard-refspec identity,
            # at zero cost to real commands.
            if "$" in tok or tok.startswith("~"):
                # Tilde expansion is env-driven text, not path syntax: bare
                # ``~`` IS ``$HOME`` (``HOME=main`` publishes main), ``~±``
                # and ``~N`` read PWD/OLDPWD/DIRSTACK, and even ``~/main``
                # resolves to ``refs/heads/main`` under a crafted
                # ``HOME=refs/heads`` — so a leading unquoted ``~`` is as
                # unverifiable as ``$``. Mid-word ``~`` is literal in an argv
                # word and stays data.
                tags.add(_GIT_PUBLISH_UNGATED)
            # Extglob patterns (``@( +( !(`` — and ``?( *(``, already covered
            # by their leading glob char) are pathname expansion too when the
            # shell has extglob on, so they take the same wildcard identity:
            # like a glob, they can only ever match existing FILE names
            # (``@(main)`` beside a file named ``main`` expands to a push of
            # main with no tag at all).
            if any(ch in tok for ch in "*?[") or any(op in tok for op in ("@(", "+(", "!(")):
                tags.add("git-publish-push-wildcard-refspec")
            # Process substitution that SURVIVED redirection removal is an argv
            # word the shell replaces with a ``/dev/fd`` path, so the split
            # cannot model it — the ungated posture, like ``ma$in``. Tested here
            # rather than on the whole segment because a process substitution the
            # shell REMOVES (the target of ``> >(tee log.txt)``) never reaches
            # git's argv and must keep the precise reading.
            if any(op in tok for op in _PROCESS_SUBSTITUTION_OPENERS):
                tags.add(_GIT_PUBLISH_UNGATED)
        # Shell operators are consumed by the SHELL, so they are handled
        # before every argv-level reading — including after ``--``, which is
        # git's end-of-options, not the shell's.
        if pending_redirection_target:
            # The word a bare redirection operator takes as its target; the
            # shell removes it from argv.
            pending_redirection_target = False
            continue
        is_redirection, consumes_next = _push_token_redirection(raw)
        if is_redirection:
            # Modelled with the shell's own arity so ``2>&1`` keeps a feature
            # push allowed while ``origin </dev/null`` reads as the precise
            # remote-only shape instead of scanning a phantom refspec.
            pending_redirection_target = consumes_next
            continue
        if operator_pieces is not None:
            # A word GLUED to its redirection: bash reads ``origin>/dev/null``
            # as the word ``origin`` plus a redirection, i.e. a remote-only
            # push whose true row is SINGLE-ARG — the protective fallback
            # emits BARE for it, and a wrong identity is itself a hazard
            # under per-rule opt-out. When the token decomposes cleanly — a
            # non-flag word, then a well-formed redirection (no risky ``&``
            # beyond an
            # fd-dup) — keep the word positional and consume the redirection
            # exactly as the shell does, with no fallback. Anything murkier
            # (a bare ``&`` command boundary, a flag-shaped prefix, quotes
            # inside the redirection) keeps the protective fallback below.
            prefix = operator_pieces[0] if operator_pieces else ""
            rest = raw[len(prefix) :] if prefix and raw.startswith(prefix) else ""
            dequoted_prefix = _dequote_token(prefix)
            # A flag GLUED to a redirection: the flag identity must not be
            # lost to the fallback — ``--all>/dev/null`` is an all-branches
            # push, and emitting only the disableable no-refspec rows lets an
            # operator who disabled those admit it while mirror-all stays
            # enabled. The all-branches check is the one whose MISSED identity
            # is a
            # bypass; other flag prefixes stay on the fallback, which only
            # ever over-protects.
            if _push_option_matches(dequoted_prefix, _PUSH_ALL_BRANCHES_OPTS):
                tags.add("git-publish-push-mirror-all")
            if (
                (rest[:1] in ("<", ">") or rest.startswith(("&>", "&>>")))
                and dequoted_prefix
                and not dequoted_prefix.startswith("-")
                and _push_token_redirection(rest)[0]
                and (
                    "&" not in rest
                    # Glued all-output redirection: the & is the operator head.
                    or rest.startswith("&")
                    # Glued fd-dup / fd-close / fd-move: >&2, >&-, >&1-.
                    or re.fullmatch(r"[<>]{1,2}&([0-9]+-?|-)", rest)
                )
            ):
                # The glued WORD is exactly what the shell hands git as the
                # argv word, so it must flow wherever a plain word would: a
                # pending option value first (appending it as a positional
                # while ``skip_next`` stays armed lets the NEXT real word be
                # eaten as the "value" and erases the
                # tags), else through the ordinary option-vs-positional
                # reading. The guard above already keeps a flag-shaped prefix
                # off this branch; classifying rather than appending means a
                # future loosening of that guard cannot turn a flag into a
                # refspec behind the gate's back.
                _classify_word(dequoted_prefix)
                pending_redirection_target = _push_token_redirection(rest)[1]
                continue
            # A word carrying only SUBSHELL PUNCTUATION: ``(cd /tmp; git push
            # origin my-feature)`` hands the ref token ``my-feature)``, whose
            # ``)`` merely closes the subshell. It removes nothing from argv and
            # adds nothing to the word, so the word keeps its exact identity and
            # the split stays trusted — routing it to the fallback below would
            # deny every legitimate refname pushed inside a subshell. The word itself
            # is still read as a refspec candidate, so a protected name inside the
            # parens is caught exactly as it is without them. A leading paren
            # leaves no prefix and keeps the protective fallback.
            #
            # Read through the SAME classifier a bare word takes: stripping the
            # paren must not change what the word IS. Appending it as a
            # positional turned ``(git push --repo=origin -f)`` into a push of a
            # refspec named ``-f`` — no tags at all, so a force push to a
            # possibly-protected current branch was admitted by one parenthesis,
            # while ``(git push -f)`` reported the wrong row (remote-only instead
            # of bare).
            if rest and dequoted_prefix and all(ch in "()" for ch in rest):
                _classify_word(dequoted_prefix)
                continue
            # A bare control operator (``&`` — a single ampersand is NOT a
            # segment separator upstream, only ``&&`` is) or operator glue
            # mid-word (``main>log`` = the word ``main`` plus a redirection:
            # it pushes main). The split is untrusted, and the operator-
            # delimited pieces are scanned as refspec candidates so a
            # protected name cannot hide behind the glue.
            #
            # This branch appends the pieces DIRECTLY, deliberately: it has
            # already set ``unrecognised_option``, so the fallback below reads
            # the whole segment protectively (both no-refspec rows fire) and
            # treats every positional as a refspec candidate. A flag landing in
            # that candidate list can only ADD a tag, never remove one.
            unrecognised_option = True
            non_flags.extend(p for p in (_dequote_token(pc) for pc in operator_pieces) if p)
            continue
        _classify_word(tok)
    if unrecognised_option:
        # Fail-protective invariant: an option this scan does
        # not model might take a separated value, so the positional split
        # cannot be trusted — the "remote" it would drop may really be a
        # leaked option value. Read the segment protectively instead: the
        # current branch might be protected (the bare tag — unless an
        # all-branches flag already names the target set exhaustively, in which
        # case mirror-all covers a superset of bare), and EVERY positional is
        # scanned as a refspec candidate so an
        # actual protected name still reports its own precise catalog row. A
        # mis-parse can therefore only ever OVER-protect: a future value-taking
        # push option cannot silently reopen the erasure class.
        if "git-publish-push-mirror-all" not in tags:
            tags.add("git-publish-push-bare")
            if non_flags:
                # An untrusted split cannot distinguish the bare shape from
                # the remote-only shape — the visible positionals may all be
                # option values, or one may be the remote. Naming a single row
                # turns that ambiguity into a bypass — disabling whichever row
                # the fallback happened to emit admits the spelling — so the
                # fallback names BOTH no-refspec rows: admitting an unparseable
                # spelling takes
                # disabling both. (With no positionals at all the remote-only
                # shape is impossible and bare stands alone; an all-branches
                # flag still suppresses both, since mirror-all covers a
                # superset.)
                tags.add("git-publish-push-single-arg")
        refspecs = non_flags
    else:
        # With the repository supplied by a flag there is no positional remote
        # to drop, so the refspecs start at index 0.
        refspecs = non_flags if repo_in_flag else non_flags[1:]
    if not refspecs and "git-publish-push-mirror-all" not in tags:
        # Bare ``push`` or ``push <remote>`` with no explicit branch — the
        # current branch might be protected.  The two spellings are separate
        # catalog rules, so report them separately. ``--repo=x`` with no refspec
        # is the bare form: the flag named the remote, nothing named a branch.
        #
        # Skipped when an all-branches flag is present, because then the absence
        # of a refspec is not the "which branch is this?" shape at all — the flag
        # already names the target set exhaustively. Tagging both meant
        # ``push --all origin`` also carried the single-arg tag, so disabling
        # mirror-all left the command blocked by its sibling and the toggle read
        # as enabled-and-off while enforcement never changed.
        tags.add("git-publish-push-bare" if not non_flags else "git-publish-push-single-arg")
        return frozenset(tags)
    if not refspecs:
        return frozenset(tags)
    for refspec in refspecs:
        # Refspecs with shell expansion ($, `) or git-revision syntax
        # (@{upstream}, @{u}) cannot be statically verified — never opt-out-able.
        if _AMBIGUOUS_REFSPEC_RE.search(refspec):
            tags.add(_GIT_PUBLISH_UNGATED)
            continue
        clean = refspec.lstrip("+")  # strip force-push '+' ref prefix
        # Wildcard refspec (refs/heads/*:refs/heads/*, *:*, feat*) expands to
        # MANY refs — like --mirror/--all it can include a protected branch and
        # cannot be statically verified.
        if "*" in clean:
            tags.add("git-publish-push-wildcard-refspec")
            continue
        # Handle "local:remote" refspec format — the remote side is the target.
        target_branch = clean.split(":")[-1] if ":" in clean else clean
        # Normalize every ref spelling git resolves server-side (heads/main,
        # remotes/<remote>/main, refs/... ) to the bare name so the path form
        # cannot dodge the protected-name check.
        normalized = _normalize_ref(target_branch)
        if normalized in _AMBIGUOUS_REFS:
            tags.add("git-publish-push-ambiguous-ref")
        elif normalized in _PROTECTED_BRANCHES:
            # Distinguish the bare-name spelling from the ref-PATH spelling:
            # they are separate catalog rules, and reporting the wrong one would
            # let an operator disable a row that is not what fired.
            tags.add(
                "git-publish-push-protected-ref-path"
                if normalized != target_branch
                else "git-publish-push-protected-branch-name"
            )
    return frozenset(tags)


def _git_publish_floor_tags(text_lower: str) -> frozenset[str]:
    """Return the git-publish rule tags a command trips, EMPTY if it is allowed.

    Same analysis as :func:`_is_push_to_protected_branch` (which is now a thin
    boolean view of this), but it reports WHICH rule each denial belongs to so
    the enforcement site can honour an operator opt-out per rule. A tag is
    either a ``git-publish`` catalog rule id or :data:`_GIT_PUBLISH_UNGATED`.

    Three branches emit the ungated tag, deliberately: substitution / expansion
    glue in a push command, a segment detected as a push that does not parse
    cleanly, and a push detected on the whole string with no clean push segment
    surviving the split. None of them is a user-facing rule — they are the
    anti-obfuscation backstop, and gating them would let ``git push origin
    ma$(echo)in`` be allowed by disabling ONE row, defeating the protected-branch
    rule without disabling it.

    Iterates the command's TRUE shell segments (split only on ``;`` / ``&&`` /
    ``||`` / ``|`` / newline — NOT on ``$(`` / backtick, which are glued into a
    single word by the shell), and collects across ALL of them: a benign feature
    push cannot vouch for a sibling protected one.
    """
    tags: set[str] = set()
    saw_push = False
    for command in _split_push_command_segments(text_lower):
        # ``_is_git_publish`` (not ``_git_push_args``) gates the checks so that
        # glue-evasion forms — which do NOT tokenize to a clean ``git`` token —
        # are still recognized as pushes and cannot slip past the ambiguity /
        # fail-closed guards below.
        if not _is_git_publish(command):
            continue
        saw_push = True
        # Substitution / expansion glue anywhere in a push command makes it
        # unverifiable (the shell fuses it into the verb or the target word).
        # This is also what covers brace expansion, which is why
        # ``git-publish-push-brace-expansion-refspec`` stays floor-enforced.
        if _AMBIGUOUS_EXPANSION_RE.search(command):
            tags.add(_GIT_PUBLISH_UNGATED)
            continue
        args = _git_push_args(command)
        if args is None:
            # Detected as a push but not cleanly parseable. That normally means
            # OBFUSCATION (``git$(echo ' ')push``) -> ungated deny.
            #
            # One exception: a shell WRAPPER carrying the push inside a quoted
            # argument. Admitting ``(`` as a leading separator makes the outer
            # line match the detector, because the ``(`` sits right after the
            # wrapper's quote -- but the outer line is not itself a push, so
            # there is no ``git`` token here to parse and this is not evasion.
            # Denying it blocked ordinary work: a FEATURE-branch push inside a
            # subshell inside ``bash -c`` was refused along with a protected one.
            #
            # The caller evaluates every nested payload source on its own, so
            # defer to that reading rather than guessing from a line that cannot
            # carry the answer.
            #
            # Defer only when a payload is ITSELF a publish, because that is the
            # source the caller will actually judge. Asking merely whether a
            # payload EXISTS is a bypass: an ARGUMENT that happens to share a
            # name with a shell verb (a remote or refspec called ``eval``) makes
            # the walk report a payload, and quoting the program defeats the
            # ``git`` anchor so the args come back None -- together those admit
            # a protected-branch publish that nothing downstream ever judges. A
            # payload that is not a publish answers nothing, so it buys no pass,
            # and with no payload at all there is nothing to wait for.
            #
            # Guarded because this runs inside the PreToolUse gate, which must
            # return a security DECISION and never raise. Failing CLOSED is the
            # only sound answer here: an exception means we cannot tell whether a
            # payload reading exists to defer to.
            try:
                defer_to_payload = any(
                    _is_git_publish(payload)
                    for payload in _nested_shell_payloads(
                        _shell_normalizer.normalize_shell_command(command)
                    )
                )
            except Exception:
                tags.add(_GIT_PUBLISH_UNGATED)
                continue
            if not defer_to_payload:
                tags.add(_GIT_PUBLISH_UNGATED)
            continue
        tags |= _push_segment_targets_protected(args)
    if not saw_push:
        # A push was detected upstream (e.g. glue-evasion ``git_push``) but no
        # clean ``push`` segment survived splitting — deny to be safe.
        tags.add(_GIT_PUBLISH_UNGATED)
    return frozenset(tags)


def _is_push_to_protected_branch(text_lower: str) -> bool:
    """Return True if ANY ``git push`` in the command targets a protected branch.

    A bare ``git push`` (no explicit branch) is BLOCKED because the current
    branch might be main/mainline. Only explicit non-protected branch targets
    are allowed. ALL refspecs of ALL push sub-invocations are checked: git
    accepts multiple refspecs, and a shell command can chain multiple pushes
    (``push origin feat && push origin main``). Force pushes to feature
    branches are allowed (normal PR workflow); force pushes to protected
    branches are blocked by the target check.

    Iterates the command's TRUE shell segments (split only on ``;`` / ``&&`` /
    ``||`` / ``|`` / newline — NOT on ``$(`` / backtick, which are glued into a
    single word by the shell). Each segment that is a git-publish (detected via
    ``_is_git_publish``, so glue-evasion like ``git$(echo ' ')push`` is seen) is
    validated and FAILS CLOSED:

    * any command-substitution / brace-expansion / backtick glue in the segment
      — in the verb OR the target (``origin ma$(echo)in`` -> ``main``) — is
      unverifiable -> deny;
    * a segment that ``_is_git_publish`` flags as a push but ``_git_push_args``
      cannot cleanly parse (obfuscated) -> deny;
    * a bare push, ambiguous ref, or explicit protected target -> deny.

    Only an explicit non-protected branch target is allowed. EVERY push segment
    is checked (a benign feature push cannot vouch for a sibling protected one).
    Force pushes to feature branches stay allowed (normal PR workflow). If a
    push was detected upstream but no segment here parses as one, denies.

    FLOOR SEMANTICS: this ignores opt-out state, so it answers "would the floor
    deny this at all". Enforcement in :func:`is_denied` uses
    :func:`_git_publish_floor_tags` instead, which reports WHICH rule fired so a
    disabled rule stays disabled.
    """
    return bool(_git_publish_floor_tags(text_lower))
