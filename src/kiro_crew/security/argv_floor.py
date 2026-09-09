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

import fnmatch
import ipaddress
import os
import re
import socket
import sys
import threading
import time
from typing import TYPE_CHECKING, NamedTuple

# fcntl/struct drive the Linux per-interface address sweep in
# _resolve_own_host_names_into_cache.  They are imported here at module scope to
# satisfy the top-level-imports rule; both are stdlib on Linux, and fcntl is
# absent on Windows, so the import is guarded and the sweep is gated on
# sys.platform == "linux" regardless (round-8).
try:
    import fcntl as _fcntl
    import struct as _struct
except ImportError:  # pragma: no cover - non-Linux platforms lack fcntl
    _fcntl = None  # type: ignore[assignment]
    _struct = None  # type: ignore[assignment]

# The reader names a test instruments on the facade are reached as attributes of
# the reader module rather than bound here at import: the facade mirrors a patch
# onto the owning module's namespace, so a name bound into THIS namespace would
# keep resolving the unpatched object and the instrument would count nothing.
from . import shell_normalizer as _shell_normalizer
from .denied_rules import _GIT_PUBLISH_UNGATED
from .host_addresses import (  # noqa: F401  (parser re-imported as a test entry point)
    _darwin_interface_addresses,
    _linux_netlink_addresses,
    _parse_netlink_addr_dump,
    _windows_interface_addresses,
)
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
    _decode_shell_quoted_literals,
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
    _program_basename,
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
    _strip_redirect,
    _substitution_bodies,
    _substitution_depth_delta,
    _xargs_here_string_rebuild,
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


# ── Sandbox-escape floor (ssh back into this same host) ──
# The agent's shell runs inside a sandbox; sshd does not.  A connection whose
# TARGET is this same machine re-enters it outside every control in this
# module, so the floor resolves the target in OPERAND POSITION — behind
# options (``ssh -p 22 localhost``), redirects, a ``user@`` prefix, URI
# schemes, bracketed/bare IPv6 and numeric IPv4 literals, and quoting the
# raw-text regex cannot see through — and refuses it when it names this host.
#
# Named residuals, fail-open BY DESIGN (failing closed on any of these would
# deny every legitimate remote ssh, which this box's workflows depend on):
# config-level routing that needs ssh's own config resolution (``-F`` files,
# ssh_config Host aliases), and a substitution that only resolves to this
# host at run time (``$(some-command)``).  The literal
# ``hostname``-substitution spellings and ``-o hostname=``/``-o proxyjump=``
# values ARE checked below, as text, and a ``-o proxycommand=`` value is
# checked twice (round-26): the floor recurses on it as a command line AND
# scans it for a LITERAL self endpoint (``nc 127.0.0.1 22`` relays the
# session to the local sshd with no ssh verb for the recursion to see) --
# a resolvable ALIAS inside such a transport stays in the residual class.
# The alias fail-open above is scoped: a DOTLESS alias is answered from the
# hosts file same-call (open) with async revalidation; a DOTTED alias that
# resolves is classified by its addresses; a DOTTED name whose lookup FAILS
# is refused per call, deliberately -- answering a failed lookup open would
# let whoever controls resolution mint an allow by suppressing it, the
# same class the verdict cache refuses to latch.
#
# The strictly larger residual is OUT OF GATE SCOPE by the module's own
# doctrine (a script body is never a gate subject): interpreter/script-file
# indirection (``bash escape.sh``, ``python -c`` + subprocess or paramiko)
# and non-ssh-family clients (``git clone ssh://localhost/…``, autossh) reach
# the unsandboxed sshd without an ssh-family command line ever crossing this
# floor.  The sandbox has no network namespace, so loopback:22 stays
# reachable from inside; this floor is the interim tier, and the fix of
# record is an OS-level network fence in the sandbox (tracked follow-up:
# see the sandbox-escape residuals note in docs/system-specs/modules/security.md).

# Names every machine answers to on loopback, plus the two spellings that
# reach the wildcard/unspecified address (both connect to loopback in
# practice).
_LOOPBACK_HOST_NAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "localhost4",
        "localhost4.localdomain4",
        "localhost6",
        "localhost6.localdomain6",
        "ip6-localhost",
        "ip6-loopback",
        "0",
        "0.0.0.0",
        "::",
        "::1",
    }
)

# Operand spellings that resolve to this host's name at run time, matched as
# TEXT inside a single argv token.  The substitution forms are substring
# matches ("$(hostname -f)" still hits); the variable forms are exact or
# dot-suffixed ("$hostname", "$hostname.example") so an unrelated variable
# like "$hostname_backup" is NOT read as this host.
_HOSTNAME_SUBSTITUTION_HINTS: tuple[str, ...] = ("$(hostname", "`hostname")
_HOSTNAME_VARIABLE_FORMS: tuple[str, ...] = ("$hostname", "${hostname}")

# A FLAT command substitution -- no nested substitution characters in the
# body -- is the only shape ``_static_substitution_output`` can decide
# statically.  Both spellings are matched; group 1 xor group 2 carries the
# body.
_FLAT_SUBSTITUTION_RE = re.compile(r"\$\(([^()`$]*)\)|`([^`()$]*)`")

# bash pathname-expands an unquoted glob against the filesystem before exec,
# so a word carrying one of these can BECOME an ssh-family program or a
# self-host by matching a file that exists (``/usr/bin/s?h`` matches the
# installed client; ``localho?t`` matches a file the agent creates).  The
# deny floor cannot consult the filesystem, so a pattern that CAN match is
# treated as matching -- an over-approximation toward deny.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")


def _glob_can_name_ssh_verb(probe: str) -> bool:
    """True when a glob word in *probe* can expand to an ssh-family program."""
    for word in probe.split():
        if not _GLOB_CHARS.intersection(word):
            continue
        base = word.rsplit("/", 1)[-1]
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return True
    return False


# A hostname the textual layers cannot classify may still resolve to a
# loopback or local address (a DNS alias pointed at 127.0.0.1).  Resolution
# is a blocking network call, so it runs in a worker off the event loop:
# the decision FAILS CLOSED (denied) until the worker publishes a verdict,
# and the verdict is cached per host.  Only dotted, lettered hostname
# shapes reach this layer: IP-literal spellings are classified above it,
# and a dotless word on an ssh command line is ordinarily the remote
# command, not a destination.
# Hostname shape eligible for the DNS-alias verdict layer.  The dotted-part
# is OPTIONAL (round-18): a dotless name in a confirmed HOST position may be
# an /etc/hosts loopback alias, and since round-17 the layer only runs in
# host position, so position -- not punctuation -- keeps junk words out.
_DNS_CANDIDATE_RE = re.compile(r"(?=.*[a-z])[a-z0-9_-]+(\.[a-z0-9_-]+)*\Z")
_HOST_VERDICT_CACHE: "dict[str, bool]" = {}
_HOST_VERDICT_PENDING: "set[str]" = set()
_HOST_VERDICT_LOCK = threading.Lock()
# ponytail: unbounded per-host growth is capped by evicting the oldest entry;
# an LRU adds bookkeeping the floor does not need.
_HOST_VERDICT_CACHE_CAP = 4096
# round-18: an ALLOW verdict is not reused unbounded -- DNS rebinding could
# repoint a once-public name at loopback after the first check.  A negative
# entry older than this is served stale ONCE while a single-flight worker
# revalidates it (no recurring first-contact refusal), so the rebinding
# window is bounded by the TTL.  DENY verdicts stay permanent: over-blocking
# is this floor's safe direction.
_HOST_VERDICT_ALLOW_TTL = 300.0
_HOST_VERDICT_STAMP: "dict[str, float]" = {}


def _resolve_host_verdict_into_cache(host: str) -> None:
    """Worker: resolve *host* off-loop and record whether it is local.

    Any resolved address that is loopback/unspecified, or a member of the
    own-address set, marks the host self.  A name that RESOLVES to no local
    address is not self.  A resolution FAILURE caches nothing (round-25): a
    transient DNS error latched as an allow would serve a recovered loopback
    alias for the whole allow-TTL, so the failure only clears the pending
    latch and the next decision retries.
    """
    verdict = False
    resolved_ok = True
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        own = _own_host_names()
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0].lower()
            try:
                ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
            except ValueError:
                continue
            mapped = getattr(ip, "ipv4_mapped", None)
            if mapped is not None:
                ip = mapped
            if ip.is_loopback or ip.is_unspecified or str(ip).lower() in own:
                verdict = True
                break
    except Exception:
        resolved_ok = False
    with _HOST_VERDICT_LOCK:
        if resolved_ok:
            if len(_HOST_VERDICT_CACHE) >= _HOST_VERDICT_CACHE_CAP:
                evicted = next(iter(_HOST_VERDICT_CACHE))
                _HOST_VERDICT_CACHE.pop(evicted)
                _HOST_VERDICT_STAMP.pop(evicted, None)
            _HOST_VERDICT_CACHE[host] = verdict
            _HOST_VERDICT_STAMP[host] = time.monotonic()
        _HOST_VERDICT_PENDING.discard(host)


def _hosts_file_paths() -> "tuple[str, ...]":
    """Platform hosts-file path(s); a seam for tests."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return (os.path.join(root, "System32", "drivers", "etc", "hosts"),)
    # Component-assembled per the portability gate's remedy: this branch is
    # POSIX-only by the ``os.name`` guard above, so the path never reaches a
    # Windows process.
    return (os.path.join("/etc", "hosts"),)


# Read cap for a hosts file: covers even the multi-megabyte ad-block variants;
# a file larger than this is read truncated, and a name past the cap simply
# falls through to the async DNS revalidation path, whose resolver reads the
# real (untruncated) hosts database.
_HOSTS_FILE_READ_CAP = 4 * 1024 * 1024


# path -> ((mtime, size, published), {name -> maps-to-local}) — reparsed when
# the file changes or netlink publication flips.  Unlocked by design: a racing
# double-parse writes the same value; the dict swap is atomic under the GIL.
_HOSTS_FILE_CACHE: "dict[str, tuple[tuple[float, int, bool], dict[str, bool]]]" = {}


def _hosts_file_verdict(host: str) -> "bool | None":
    """Hosts-file verdict: True local, False remote, None absent/deferred.

    This is the round-18 attack vector itself — a hosts-file alias for a
    loopback/local address — answered by a LOCAL file read: no DNS, no
    resolver thread, no event-loop concern, and a same-call verdict where
    the async layer can only fail closed or revalidate later.  A name on
    several lines is local if ANY of them maps local (deny-floor direction).
    """
    for path in _hosts_file_paths():
        try:
            stat = os.stat(path)
            key = (stat.st_mtime, stat.st_size, _NETLINK_ADDRS_PUBLISHED)
            cached = _HOSTS_FILE_CACHE.get(path)
            if cached is None or cached[0] != key:
                table: "dict[str, bool]" = {}
                with open(path, encoding="utf-8", errors="replace") as fh:
                    # Bounded read (the repo-wide handle-iteration guard, and
                    # a real cap): see _HOSTS_FILE_READ_CAP for the overflow
                    # degradation path.
                    for line in fh.read(_HOSTS_FILE_READ_CAP).splitlines():
                        fields = line.partition("#")[0].split()
                        if len(fields) < 2:
                            continue
                        addr = fields[0].split("%", 1)[0]
                        try:
                            ip: ipaddress.IPv4Address | ipaddress.IPv6Address = (
                                ipaddress.ip_address(addr)
                            )
                        except ValueError:
                            continue
                        mapped = getattr(ip, "ipv4_mapped", None)
                        if mapped is not None:
                            ip = mapped
                        local = (
                            ip.is_loopback
                            or ip.is_unspecified
                            or str(ip).lower() in _own_host_names()
                        )
                        for name in fields[1:]:
                            lowered = name.lower()
                            table[lowered] = table.get(lowered, False) or local
                cached = (key, table)
                _HOSTS_FILE_CACHE[path] = cached
            verdict = cached[1].get(host)
            if verdict is False and not _NETLINK_ADDRS_PUBLISHED:
                # Not-local is untrustworthy while the own-address set
                # is incomplete: defer to the async verdict layer.
                verdict = None
            if verdict is not None:
                return verdict
        except OSError:
            continue
    return None


def _resolved_host_verdict(host: str, *, fail_closed: bool = True) -> bool:
    """Cached is-self verdict for *host*; *fail_closed* answers a miss.

    A cache hit answers directly.  A miss schedules one single-flight
    resolver worker and answers *fail_closed* for this call — True (denied)
    for dotted names, False (allowed) for dotless names the hosts file does
    not know (round-21: refusing every dotless first contact broke the
    everyday ``ssh dev-dsk <cmd>`` shape) — and a later call reads the
    published verdict.  A worker that cannot start keeps this call's answer
    and clears the latch so a later call retries.
    """
    with _HOST_VERDICT_LOCK:
        if host in _HOST_VERDICT_CACHE:
            verdict = _HOST_VERDICT_CACHE[host]
            # round-18: an aged ALLOW is served stale exactly while ONE
            # revalidation worker runs -- rebinding to loopback is caught at
            # the next publish, without a recurring first-contact refusal.
            if (
                not verdict
                and host not in _HOST_VERDICT_PENDING
                and time.monotonic() - _HOST_VERDICT_STAMP.get(host, 0.0) > _HOST_VERDICT_ALLOW_TTL
            ):
                _HOST_VERDICT_PENDING.add(host)
                try:
                    threading.Thread(
                        target=_resolve_host_verdict_into_cache,
                        args=(host,),
                        name="kirocrew-host-verdict",
                        daemon=True,
                    ).start()
                except Exception:
                    _HOST_VERDICT_PENDING.discard(host)
            return verdict
        if host not in _HOST_VERDICT_PENDING:
            _HOST_VERDICT_PENDING.add(host)
            try:
                threading.Thread(
                    target=_resolve_host_verdict_into_cache,
                    args=(host,),
                    name="kirocrew-host-verdict",
                    daemon=True,
                ).start()
            except Exception:
                _HOST_VERDICT_PENDING.discard(host)
    return fail_closed


# ``${VAR:-word}`` / ``${VAR:=word}`` / ``${VAR:+word}`` — bash substitutes the
# embedded WORD before exec, so a self-host hiding in the default IS the
# destination when the variable is unset (``ssh "${TARGET:-localhost}"``).
# The word is statically visible, so it is checked; a bare ``$VAR`` whose
# value only exists at run time remains the documented fail-open residual.
_EXPANSION_DEFAULT_RE = re.compile(r"\$\{[^{}:]*:[-=+?]([^{}]+)\}")

_SSH_FAMILY_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp", "rsync"})

# Verbs that place a copy (or a link) of their SOURCE operand at their
# DESTINATION operand.  A destination whose source is an ssh-family binary
# IS that binary under a new name, so the walk binds it as the verb for the
# rest of the line (round-20).
_BINARY_COPY_VERBS: frozenset[str] = frozenset({"cp", "mv", "ln", "install"})

# A leading environment-assignment word (``FOO=bar cmd``) keeps the NEXT
# token in program position.  The walk reads lowercased text.
_ENV_ASSIGN_PREFIX_RE = re.compile(r"[a-z_][a-z0-9_]*=")

# rsync execs TWO env-var command values FROM HERE: ``RSYNC_RSH`` (its remote
# shell, exactly as ``-e``/``--rsh``) and ``RSYNC_CONNECT_PROG`` (its
# daemon-mode proxy for reaching ``rsync://`` / ``host::module``
# destinations).  A LEADING assignment (``RSYNC_RSH='ssh localhost' rsync …``)
# rides BEFORE the verb, where the operand walk in ``_is_ssh_to_self`` never
# sees it.  The captured value is a command line rsync execs FROM HERE, so it
# recurses the floor.  Tokens reach the walk lowercased; IGNORECASE keeps the
# match robust regardless.  The remote-shell env vars of OTHER verbs
# (``GIT_SSH_COMMAND`` and the like) stay a documented residual, deliberately
# out of this floor's scope.
_RSYNC_RSH_ASSIGN_RE = re.compile(r"^(rsync_rsh|rsync_connect_prog)=(.+)$", re.IGNORECASE)

# Sentinel bytes standing in for an ``_ends_argv`` boundary character that was
# QUOTED or backslash-escaped in the raw source, so it is literal DATA rather
# than a command separator.  ``_self_tokens`` runs shlex, which resolves quotes
# BEFORE the operand walk in ``_is_ssh_to_self`` runs, so by token time a quoted
# ``a;b`` is indistinguishable from a glued unquoted operator ``a;b`` -- yet the
# first is one operand and the second is a command boundary.  The distinguishing
# information exists only in the SOURCE, before tokenization, so the raw text is
# scanned there and every data separator is rewritten to a sentinel that
# ``_ends_argv`` does not treat as a boundary.  A real, unquoted separator keeps
# its character and still ends the walk.  The table covers EVERY character that
# can make ``_ends_argv`` end the walk (``;`` ``|`` ``&`` newline ``#`` ``(``
# ``{``) -- covering only a subset would let a quoted spelling of the missing
# one hide a self-host operand behind a fake boundary (``scp '&' localhost:/x``
# stops the walk at ``&`` with the target unexamined).  Control bytes are used
# because a real shell command never contains them.
_QUOTED_SEP_SENTINELS = {
    ";": "\x00",
    "|": "\x01",
    "&": "\x02",
    "\n": "\x03",
    "#": "\x04",
    "(": "\x05",
    "{": "\x06",
}


def _mask_quoted_separators(text: str) -> str:
    """Rewrite quoted/escaped ``_ends_argv`` boundary chars in *text* to sentinels.

    Single-pass quote-state scan over the RAW source:

    * inside single quotes -- boundary chars are literal data, so they become
      sentinels; a single quote takes no escapes, so a backslash inside is an
      ordinary character.
    * inside double quotes -- boundary chars are literal data, so they become
      sentinels; a backslash does not change a separator's literalness, so each
      character is handled on its own.
    * outside quotes -- a backslash escapes the next character, so an escaped
      boundary char is data and becomes a sentinel (the backslash is kept so
      shlex still de-escapes downstream, and an escaped quote never opens a
      context); the one exception is backslash-newline, a line continuation,
      handled at the escape branch.
    * every other character is copied verbatim; an unterminated quote leaves the
      rest of the string quoted, matching how bash reads it.

    Idempotent for this floor's purposes: a sentinel byte already present in
    hostile input is left in place, and ``_unmask_separators`` later turns it
    into a real separator -- which only ends the walk EARLIER, widening the deny
    (the safe direction).  The mask feeds ``_self_tokens`` (shlex) and recursive
    floor calls, both of which discard quotes anyway, so it never changes the
    resolved tokens beyond neutralizing the boundary ambiguity it exists to
    close.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                # Escaped char: an escaped separator is data (mask it); keep the
                # backslash so shlex de-escapes downstream, and consume the next
                # char here so an escaped quote never opens a quote context.
                # EXCEPTION: backslash-newline is a LINE CONTINUATION -- bash
                # (and posix shlex) glue the surrounding text into one token
                # (``local\<newline>host`` resolves to ``localhost``), so it is
                # neither data nor a separator; masking it would corrupt the
                # glued token and un-match a continued self-host spelling.
                nxt = text[i + 1]
                out.append(ch)
                if nxt == "\n":
                    out.append(nxt)
                else:
                    out.append(_QUOTED_SEP_SENTINELS.get(nxt, nxt))
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
            out.append(ch)
        elif quote == '"' and ch == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
            # Inside DOUBLE quotes bash honors ``\`` only before ``"  \  $  ` ``
            # (round-8): ``\"`` is a literal quote, NOT a close.  Copy both bytes
            # verbatim and skip the next char so the escaped quote never ends the
            # quote context -- otherwise ``scp "a\";b" localhost:/x`` would exit
            # the quote at ``\"`` and read the following ``;`` as a real
            # separator, ending the operand walk before the self-host target.
            # Single quotes honor no escape, so this is scoped to ``"``.
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        elif ch == quote:
            quote = None
            out.append(ch)
        else:
            # Inside a quote: a separator is literal data; everything else
            # (backslash included) is copied verbatim.
            out.append(_QUOTED_SEP_SENTINELS.get(ch, ch))
        i += 1
    return "".join(out)


def _unmask_separators(text: str) -> str:
    """Reverse :func:`_mask_quoted_separators` -- restore the boundary chars."""
    for sep, sentinel in _QUOTED_SEP_SENTINELS.items():
        text = text.replace(sentinel, sep)
    return text


# An EMPTY command substitution expands to nothing, so ``s$()sh`` runs ``ssh``
# and ``local$()host`` resolves to ``localhost`` -- the same glue-evasion the
# self-kill floor names for ``p$()kill``, but aimed at the verb gate and at the
# operand.  Collapsing these in the SOURCE text (before the gate and before
# tokenization) makes both the substring gate and every resolved operand read
# the spliced-out spelling.  Only ``$()``/backticks are matched: an empty
# ``${}`` is a bash syntax error, not an expansion, so it never splices.
_EMPTY_EXPANSION_RE = re.compile(r"\$\(\s*\)|`\s*`")

# ssh options whose VALUE is a forward/bind spec or a login name, not a
# destination this process connects to from here (lowered text collapses
# ``-L``/``-l`` and ``-W``/``-w``): ``-L``/``-R``/``-D``/``-W`` forward specs
# name listen addresses and far-side hops, ``-b`` a local bind address, and
# ``-l`` a login name.  Every value except ``-R``'s is exempt from the
# fail-closed operand check — ``ssh -L 127.0.0.1:8080:db:5432 far-host`` is
# the recommended way to reach a remote service and must stay allowed.  The
# ``-R`` destination is dialed FROM HERE, so its spec IS checked (round-27,
# ``_remote_forward_value_targets_self``); ``-J`` deliberately stays checked
# too: the jump connection originates from THIS machine.
_SSH_FORWARD_OPT_LETTERS: frozenset[str] = frozenset("lrdwb")

# This machine's own names/addresses, lowered.  Seeded SYNCHRONOUSLY from
# ``socket.gethostname()`` on first use — a local syscall (uname), not DNS, so
# it is safe on the event loop and closes the resolution race where the first
# ``ssh <own-hostname>`` arrived before any name was known.  The DNS-backed
# enrichment (``socket.getfqdn``/``socket.getaddrinfo``) runs in a BACKGROUND
# thread: those are synchronous network calls, and ``is_denied`` runs inline
# on the gateway's event loop (the PreToolUse gate), where a hung resolve
# would freeze every session — the AUTOSDE no-blocking-call-on-the-loop rule
# names them.  Until enrichment lands the own-name half covers only the
# machine's own reported hostname; the hard-coded loopback half above is
# fully synchronous and never depends on any of this.  A failed enrichment
# retries on a later miss after a backoff rather than caching the failure for
# the process lifetime.
_OWN_HOST_NAMES_CACHE: "frozenset[str] | None" = None
_OWN_HOST_RESOLVE_DONE = False
_OWN_HOST_RESOLVE_LOCK = threading.Lock()
_OWN_HOST_RESOLVE_NEXT_TRY: float = 0.0
_OWN_HOST_RESOLVE_BACKOFF_SECS = 60.0
# When the last COMPLETE resolve published (time.monotonic()).  A complete set
# goes stale after ``_OWN_HOST_REFRESH_SECS`` so an interface address the host
# gains later (VPN attach, DHCP renewal) becomes a self address on a
# long-lived gateway: the stale set keeps being served (never blocks, never
# shrinks) while the single-flight worker re-enumerates and merges.
_OWN_HOST_RESOLVE_STAMP: float = 0.0
_OWN_HOST_REFRESH_SECS = 300.0
# single-flight latch — True while an enrichment worker is alive; gates the
# spawn in ``_own_host_names`` and is cleared in the worker's finally.
_OWN_HOST_RESOLVE_IN_FLIGHT = False


def _own_host_seed() -> "frozenset[str]":
    """The synchronously-knowable own names: gethostname forms + interface IPs.

    Interface addresses MUST be in the seed, not only in the async DNS
    enrichment: the enrichment publishes after the first command is judged, so
    a first ``ssh <own-interface-IP>`` would otherwise be admitted before the
    worker finishes -- a deterministic first-command escape, not a race.  The
    enumeration is local and packet-less (no name resolution of any kind: this
    runs on the event-loop ``is_denied`` path, where a slow resolver would
    stall the gateway) and runs once per process, so the first ssh-family
    command absorbs its cost and every later call reads the cache.
    """
    names: set[str] = set()
    try:
        short = socket.gethostname().strip().lower()
        if short:
            names.add(short)
            names.add(short.split(".", 1)[0])
    except Exception:  # pragma: no cover - hostname lookup is best-effort
        pass
    names |= _own_interface_addresses()
    return frozenset(n for n in names if n)


def _own_interface_addresses() -> "set[str]":
    """This machine's local interface addresses (lowered), best-effort.

    DNS enrichment misses an interface IP that has no DNS record -- a DHCP
    lease, a secondary NIC -- yet ``ssh <that-IP>`` still re-enters THIS host,
    so the own-name set needs them too.  Enumerated from the stdlib in layers,
    each wrapped on its own so a platform missing a facility contributes nothing
    and raises nothing.
    """
    addrs: set[str] = set()
    # This runs inside the synchronous seed on the event-loop ``is_denied``
    # path, so it must never resolve names: every layer below is packet-less
    # local enumeration.  DNS-derived names (getfqdn/getaddrinfo forms) come
    # from the async enrichment worker, off the loop.
    # 1. UDP-connect trick per family: a datagram ``connect`` sends no packet
    #    but binds the primary outbound address for that family, even when it
    #    has no DNS record.  The peers are TEST-NET-2 / documentation addresses,
    #    so nothing is ever contacted.
    for _family, _probe in (
        (socket.AF_INET, ("198.51.100.1", 53)),
        (socket.AF_INET6, ("2001:db8::1", 53)),
    ):
        try:
            with socket.socket(_family, socket.SOCK_DGRAM) as _sock:
                _sock.connect(_probe)
                addrs.add(_sock.getsockname()[0])
        except Exception:
            pass
    # 2. Per-interface sweep for addresses the probe above misses.  fcntl/struct
    #    are module-level optional imports (Windows has no fcntl); the sweep is
    #    gated on the linux guard AND on them being present so it never runs off
    #    Linux regardless.  SIOCGIFADDR's value is Linux-specific; macOS has its
    #    own getifaddrs sweep below.
    if sys.platform.startswith("linux") and _fcntl is not None and _struct is not None:
        try:
            for _idx, _ifname in socket.if_nameindex():
                # SIOCGIFADDR (0x8915): an interface with no IPv4 address raises
                # OSError -- skip it.
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _sock:
                        _packed = _struct.pack("256s", _ifname.encode()[:15])
                        _info = _fcntl.ioctl(_sock.fileno(), 0x8915, _packed)
                        addrs.add(socket.inet_ntoa(_info[20:24]))
                except Exception:
                    pass
            # IPv6 has no ioctl equivalent; /proc/net/if_inet6 lists them, the
            # first whitespace field being 32 hex chars.
            try:
                with open("/proc/net/if_inet6") as _fh:
                    for _line in _fh:
                        try:
                            addrs.add(str(ipaddress.IPv6Address(int(_line.split()[0], 16))))
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            pass
    # 3. Windows per-adapter sweep, the sibling of the Linux one:
    #    GetAdaptersAddresses reads the local adapter table (no resolver, no
    #    packet), covering the secondary/VPN addresses the route-selected
    #    probes miss.  The helper self-gates: off Windows it returns empty.
    addrs |= _windows_interface_addresses()
    # 4. macOS per-interface sweep, the getifaddrs sibling of the two above:
    #    a pure local-table read (no resolver, no packet) covering the
    #    secondary/VPN addresses the route-selected probes miss.  The helper
    #    self-gates: off macOS it returns empty.
    addrs |= _darwin_interface_addresses()
    # Drop scope ids (``fe80::1%eth0``) so every result parses as a bare
    # address, and lower/skip empties.
    out: set[str] = set()
    for _addr in addrs:
        _norm = str(_addr).split("%", 1)[0].strip().lower()
        if _norm:
            out.add(_norm)
    return out


# Whether the netlink RTM_GETADDR layer has published (round-33).  The dump
# is a blocking socket read, so it runs in the enrichment WORKER, never the
# synchronous seed -- and until its addresses land, a non-own IP literal in
# host position could be an unlisted secondary of this very machine, so
# ``_host_is_self`` denies those (fail closed).  Off Linux there is no
# netlink layer to wait for, so the window starts closed.
_NETLINK_ADDRS_PUBLISHED: bool = not (
    sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK")
)


def _resolve_own_host_names() -> "tuple[frozenset[str], bool]":
    """Resolve this machine's own hostname/FQDN/addresses (lowered).

    Blocking (DNS) — call from a worker thread, never on the event loop.
    Returns ``(resolved, complete)``: *resolved* is the set of names/addresses
    found (empty when nothing could be resolved), and *complete* is False when
    the FQDN lookup or ANY per-name address lookup raised — so a pass that
    enriched only some names is not mistaken for a full one.  The set content is
    unchanged by *complete*; a caller publishes the partial set but withholds the
    resolved-once latch when it is False.
    """
    names: set[str] = set(_own_host_seed())
    complete = True
    try:
        fqdn = socket.getfqdn().strip().lower()
        if fqdn and fqdn != "localhost":
            names.add(fqdn)
    except Exception:  # pragma: no cover - fqdn lookup is best-effort
        complete = False
    for name in sorted(names):
        try:
            for info in socket.getaddrinfo(name, None):
                # Drop an IPv6 zone id (round-8): ``getaddrinfo`` can return a
                # scoped spelling (``fe80::1%eth0``) for a link-local address,
                # which would never equal the bare ``fe80::1`` a command names,
                # so strip it before caching -- the same normalization the
                # operand side does in ``_host_is_self``.
                addr = str(info[4][0]).split("%", 1)[0].strip().lower()
                if addr:
                    names.add(addr)
        except Exception:
            complete = False
            continue
    # Interface addresses DNS does not know are merged in here.  A miss inside
    # the enumeration is NOT an incomplete resolve -- the retry latch is for DNS
    # enrichment, and a host with no IPv6 route is not a partial pass -- so this
    # never touches ``complete`` (and the helper is best-effort, never raising).
    names |= _own_interface_addresses()
    # The netlink RTM_GETADDR dump lists EVERY assigned address (secondary
    # IPv4s the SIOCGIFADDR sweep cannot see).  Its recv blocks, so it lives
    # here in the worker.  Unlike the sweeps above it is LOAD-BEARING: the
    # IP-literal window stays closed until it publishes, so an empty pass on
    # a netlink-capable host keeps ``complete`` False and the backoff retry
    # alive rather than caching a table-less process for its lifetime.
    global _NETLINK_ADDRS_PUBLISHED
    nl = _linux_netlink_addresses()
    if nl:
        names |= nl
        _NETLINK_ADDRS_PUBLISHED = True
    elif sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK"):
        complete = False
    return frozenset(n for n in names if n), complete


def _resolve_own_host_names_into_cache() -> None:
    """Worker-thread body: publish resolved names, latch DONE only when complete.

    Merges into the existing cache (a later partial pass never SHRINKS it) and
    publishes whenever anything resolved, so partial results still protect.  The
    resolved-once latch (``_OWN_HOST_RESOLVE_DONE``) is set ONLY on a complete
    resolve: an incomplete one leaves it False so ``_own_host_names`` keeps
    scheduling the backoff retry for the names that failed to enrich, instead of
    caching a partial set for the process lifetime.  The worker clears the
    single-flight latch on exit (success, partial, or raise) so the backoff
    retry can spawn again.
    """
    global _OWN_HOST_NAMES_CACHE, _OWN_HOST_RESOLVE_DONE, _OWN_HOST_RESOLVE_IN_FLIGHT
    global _OWN_HOST_RESOLVE_STAMP
    try:
        resolved, complete = _resolve_own_host_names()
        if resolved:
            existing = _OWN_HOST_NAMES_CACHE or frozenset()
            _OWN_HOST_NAMES_CACHE = existing | resolved
        if complete:
            _OWN_HOST_RESOLVE_DONE = True
            _OWN_HOST_RESOLVE_STAMP = time.monotonic()
    finally:
        with _OWN_HOST_RESOLVE_LOCK:
            _OWN_HOST_RESOLVE_IN_FLIGHT = False


def _own_host_names() -> "frozenset[str]":
    """The own-name set: the synchronous seed at once, DNS enrichment later.

    Non-blocking: the first call publishes the gethostname seed inline (so an
    own-hostname target is denied from the very first command — no resolution
    race), then kicks the DNS enrichment off in a daemon thread and returns
    whatever is published.  The enrichment is single-flight: at most one worker
    at a time, retried after the backoff while it keeps failing.
    """
    global _OWN_HOST_NAMES_CACHE, _OWN_HOST_RESOLVE_NEXT_TRY, _OWN_HOST_RESOLVE_IN_FLIGHT
    if (
        _OWN_HOST_RESOLVE_DONE
        and time.monotonic() - _OWN_HOST_RESOLVE_STAMP < _OWN_HOST_REFRESH_SECS
    ):
        cached = _OWN_HOST_NAMES_CACHE
        return cached if cached is not None else frozenset()
    with _OWN_HOST_RESOLVE_LOCK:
        if _OWN_HOST_NAMES_CACHE is None:
            _OWN_HOST_NAMES_CACHE = _own_host_seed()
        now = time.monotonic()
        if now >= _OWN_HOST_RESOLVE_NEXT_TRY and not _OWN_HOST_RESOLVE_IN_FLIGHT:
            _OWN_HOST_RESOLVE_NEXT_TRY = now + _OWN_HOST_RESOLVE_BACKOFF_SECS
            _OWN_HOST_RESOLVE_IN_FLIGHT = True
            try:
                threading.Thread(
                    target=_resolve_own_host_names_into_cache,
                    name="kirocrew-own-host-resolve",
                    daemon=True,
                ).start()
            except Exception:
                # A thread that cannot start (resource exhaustion) must not
                # abort the permission decision: clear the latch so a later
                # call retries the worker, and answer from the synchronous
                # seed already cached above.
                _OWN_HOST_RESOLVE_IN_FLIGHT = False
        return _OWN_HOST_NAMES_CACHE


def _host_is_self(host: str, *, dns_fallback: bool = True) -> bool:
    """True if *host* (already isolated) names this machine.

    Checks the hard-coded loopback names, IP-literal forms — bare IPv6
    (``::1``, ``::ffff:127.0.0.1``), and the numeric IPv4 spellings
    ``inet_aton`` accepts (``2130706433``, ``0x7f000001``, ``0177.0.0.1``) —
    and finally the resolved own-name set.  The DNS-alias verdict layer at the
    end runs only with ``dns_fallback`` (round-17): callers set it for tokens
    in HOST POSITION, so a dotted local FILENAME (``backup.tar.gz`` as an scp
    source) is never refused as an unresolved first-contact hostname.
    """
    host = host.rstrip(".")
    if not host:
        return False
    # Strip an IPv6 zone id (``fe80::1%eth0`` -> ``fe80::1``) before any
    # address compare (round-8): the scope suffix never changes which address
    # this is, and Python hands back both spellings, so the token and the
    # cached own-address must reduce to the same bare form.  Scoped to
    # IPv6-ish tokens (a ``:`` is present) so an ordinary hostname carrying a
    # ``%`` is left untouched.
    if ":" in host and "%" in host:
        host = host.split("%", 1)[0]
    if _GLOB_CHARS.intersection(host):
        # A glob operand expands against the filesystem before exec, so a
        # pattern that CAN match a self name is treated as one (see
        # ``_GLOB_CHARS``); a pattern that cannot match any self name is
        # not a self target, and no later layer parses a glob.
        candidates = set(_LOOPBACK_HOST_NAMES) | {"127.0.0.1", "::1"} | _own_host_names()
        return any(fnmatch.fnmatchcase(cand, host) for cand in candidates)
    if host in _LOOPBACK_HOST_NAMES:
        return True
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
        # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) explicitly: is_loopback
        # only delegates to the mapped address from Python 3.12.4 on, and
        # requires-python admits older 3.12 micros where it reads False.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_loopback or ip.is_unspecified:
            return True
        if str(ip).lower() in _own_host_names():
            return True
        if dns_fallback and not _NETLINK_ADDRS_PUBLISHED:
            # Same unread-table window as the ``inet_aton`` branch below --
            # this branch is the one IPv6 literals take (round-33).
            return True
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
        ip4 = ipaddress.IPv4Address(packed)
        if ip4.is_loopback or ip4.is_unspecified:
            return True
        if str(ip4) in _own_host_names():
            return True
        if dns_fallback and not _NETLINK_ADDRS_PUBLISHED:
            # The kernel address table is unread; this literal could be an
            # unlisted secondary of this machine.  Deny until the worker
            # publishes (round-33) -- host position only.
            return True
    except OSError:
        pass
    if host in _own_host_names():
        return True
    # A hostname that survives every textual layer may still be a DNS alias
    # for a loopback/local address; the off-loop verdict layer classifies
    # it and the decision fails closed until the verdict is published.
    # Consulted only in host position (round-17, ``dns_fallback``).
    if dns_fallback and _DNS_CANDIDATE_RE.fullmatch(host) is not None:
        if "." not in host:
            # round-21: a DOTLESS name is the hosts-file alias class the
            # round-18 finding named -- and the hosts file is a local read
            # that answers that vector SAME-CALL.  Absent an entry, answer
            # OPEN while one async worker revalidates through DNS: the
            # fail-closed first contact broke the everyday ``ssh dev-dsk``
            # shape (CI allow pin), and a dotless loopback alias that lives
            # only in DNS search domains is caught at the next decision
            # (documented residual).  Dotted names keep fail-closed.
            hosts_verdict = _hosts_file_verdict(host)
            if hosts_verdict is not None:
                return hosts_verdict
            return _resolved_host_verdict(host, fail_closed=False)
        return _resolved_host_verdict(host)
    return False


# round-10 static resolutions for ``_operand_targets_self``.  A ``$((…))``
# carrying ONE integer literal is normalized to the decimal spelling bash
# prints (hex and bash-octal included); anything else inside ``$((…))`` is
# left alone — an in-floor expression evaluator is itself attack surface, and
# a variable-carrying expression is runtime state (documented residual).
_ARITH_INT_LITERAL_RE = re.compile(r"\$\(\(\s*(-?(?:0[xX][0-9a-fA-F]+|[0-9]+))\s*\)\)")

# A brace group with a comma alternation or a ``..`` range is a brace
# expansion; ``{single}`` is literal in bash and stays untouched, and the
# lookbehind keeps ``${…}`` parameter expansions out.
_BRACE_GROUP_RE = re.compile(r"(?<!\$)\{([^{}]*)\}")
_BRACE_RANGE_RE = re.compile(r"\A(-?[0-9]+|[A-Za-z])\.\.(-?[0-9]+|[A-Za-z])(?:\.\.(-?[0-9]+))?\Z")
# ponytail: fan-out ceiling.  Expansion stops at 256 generated words and the
# caller DENIES (fail-closed) — a wider product in a connection operand is
# pathological, and enumerating it here would be its own DoS.
_BRACE_EXPANSION_CAP = 256


def _arith_int_literal_repl(match: "re.Match[str]") -> str:
    """The decimal spelling bash prints for one arithmetic integer literal."""
    lit = match.group(1)
    magnitude = lit.lstrip("-")
    try:
        if magnitude.lower().startswith("0x"):
            value = int(lit, 16)
        elif magnitude.startswith("0") and len(magnitude) > 1:
            # bash reads a leading zero as octal; an invalid octal digit is a
            # bash error (no command runs), so the spelling is left alone.
            value = int(lit, 8)
        else:
            value = int(lit, 10)
        # ``str`` stays INSIDE the try (round-18): hex/octal ``int`` uses a
        # power-of-two base exempt from the interpreter's digit cap, so a
        # ~3600-digit hex literal converts -- but its decimal ``str`` IS
        # capped and would raise straight through ``is_denied``, aborting
        # the evaluation.  Keeping the original spelling instead composes
        # with the target-position rule into a fail-closed deny.
        return str(value)
    except ValueError:
        return match.group(0)


def _brace_alternatives(body: str) -> "list[str] | None":
    """The words one brace-group *body* expands to, or None when literal.

    A comma body is an alternation (empty alternatives included, as in
    ``{h,}``); a ``a..b`` / ``a..b..step`` body is a numeric or single-char
    range.  Anything else — ``{single}``, an empty body — is not a brace
    expansion in bash and expands to nothing here.
    """
    range_match = _BRACE_RANGE_RE.match(body)
    if range_match is not None:
        start_s, end_s, step_s = range_match.groups()
        if start_s.isalpha() != end_s.isalpha():
            return None
        try:
            step = abs(int(step_s)) if step_s else 1
            if step == 0:
                step = 1
            if start_s.isalpha():
                start, end = ord(start_s), ord(end_s)
                render = chr
            else:
                start, end = int(start_s), int(end_s)
                render = str  # type: ignore[assignment]
        except ValueError:
            # ``int()`` refuses digit strings past the interpreter's
            # conversion cap (~4300 digits); uncaught, that crashes the
            # gate.  An endpoint or step that large cannot name a single
            # legitimate target, so signal overflow exactly like the cap
            # check below -- the caller converts it to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        if abs(end - start) // step + 1 > _BRACE_EXPANSION_CAP:
            # Signal overflow with an over-long list the caller's cap check
            # converts to a fail-closed deny.
            return [""] * (_BRACE_EXPANSION_CAP + 1)
        direction = 1 if start <= end else -1
        return [render(v) for v in range(start, end + direction, direction * step)]
    if "," in body:
        return body.split(",")
    return None


def _brace_expansions(token: str) -> "list[str] | None":
    """Every word bash brace expansion produces for *token*; None on overflow.

    Groups are expanded left-to-right to a fixpoint (each rewrite removes one
    brace pair, so this terminates); the returned words carry no expandable
    group.  A product beyond ``_BRACE_EXPANSION_CAP`` returns None and the
    caller fails closed.
    """
    words = [token]
    changed = True
    while changed:
        changed = False
        next_words: list[str] = []
        for word in words:
            expanded = False
            for group in _BRACE_GROUP_RE.finditer(word):
                alternatives = _brace_alternatives(group.group(1))
                if alternatives is None:
                    continue
                head, tail = word[: group.start()], word[group.end() :]
                next_words.extend(head + alt + tail for alt in alternatives)
                expanded = True
                changed = True
                break
            if not expanded:
                next_words.append(word)
            if len(next_words) > _BRACE_EXPANSION_CAP:
                return None
        words = next_words
    return words


def _operand_targets_self(operand: str, *, host_position: bool = True) -> bool:
    """True if *operand* names THIS machine as a connection target.

    Resolves the host part the way the ssh family reads an operand: an
    optional ``ssh://``/``scp://``/``sftp://``/``rsync://`` scheme (authority
    isolated before any path), an optional ``user@`` prefix stripped from the
    HOST part only (an ``@`` in a remote PATH is not userinfo), a bracketed
    IPv6 literal, a bare IPv6/numeric IP literal, and a ``host:path`` colon
    form.  A bare word equal to a self-host name also counts: for ssh/sftp it
    IS the positional target, and for scp/rsync a local file named exactly
    ``localhost`` is not worth carving an allowance for (over-blocking is the
    safer direction, and the catalog pattern must stay a SUBSET of this
    predicate — see ``test_retained_pattern_is_a_subset_of_its_predicate``).

    Every statically-derivable expansion/gluing form is resolved over the
    WHOLE operand and the resolved spelling re-checked (round-10 invariant):
    parameter defaults (``local${U:-host}`` -> ``localhost``), brace
    expansion (``local{h,}ost``), and single-integer arithmetic literals
    (``$((0x7f)).0.0.1``).  Each resolution only WIDENS the deny.  Forms
    whose produced text depends on runtime state — a bare ``$VAR`` value,
    command substitution with output (``$(get-host)``), arithmetic beyond
    one literal (``$((i+1))``) — are the documented run-time residual class:
    emulating them needs an in-floor evaluator that is itself attack
    surface, and their statically-visible parts are already checked (the
    embedded defaults here, the ``$(hostname`` hints, the empty-substitution
    collapse in ``_is_ssh_to_self``).
    """
    t = operand.strip().strip("\"'")
    if not t or t.startswith("-"):
        return False
    # A glued shell operator (``ssh localhost;true`` hands the operand
    # ``localhost;true``) is never part of a hostname, so consult the
    # operator-cut spelling too -- the same ``(token, _cut_at_operator(token))``
    # idiom the git-push floor uses.  This only WIDENS the deny: the cut token
    # has no operator left, so the recursive call cuts nothing and does not
    # recurse again, and a bare ``$VAR`` (no operator to cut) is unaffected.
    cut = _cut_at_operator(t)
    if cut != t and _operand_targets_self(cut, host_position=host_position):
        return True
    for hint in _HOSTNAME_SUBSTITUTION_HINTS:
        if hint in t:
            return True
    for var in _HOSTNAME_VARIABLE_FORMS:
        if t == var or t.startswith(var + ".") or t.startswith(var + ":"):
            return True
    # An expansion's embedded default is the destination when the variable is
    # unset — check every ``${…:-word}``-style word (recursion terminates:
    # the word is strictly shorter than the operand).  Kept alongside the
    # whole-operand resolution below because it is WIDER on mangled glue
    # (``foo${X:-localhost}bar``): the embedded word alone is self even when
    # the resolved whole is not.
    for match in _EXPANSION_DEFAULT_RE.finditer(t):
        if _operand_targets_self(match.group(1), host_position=host_position):
            return True
    # round-10: bash substitutes a parameter default INTO the surrounding
    # word (``local${KC_UNSET:-host}`` -> ``localhost``), so the whole
    # operand is resolved and re-checked — checking each default word in
    # isolation misses the reconstruction.  ``_resolve_param_defaults``
    # substitutes every ``${VAR<op>literal}`` with its literal to a fixpoint
    # (nesting and colon-less operators included); the recursive call sees a
    # string with no such form left, so it does not recurse here again.
    resolved = _resolve_param_defaults(t)
    if resolved != t and _operand_targets_self(resolved, host_position=host_position):
        return True
    # round-10: a single integer literal inside arithmetic expansion is
    # printed decimally by bash (``$((0x7f))`` -> ``127``), gluing into the
    # surrounding word.  Only the literal spellings are normalized — an
    # expression or a variable stays unresolved (run-time residual, see the
    # docstring) — and the substituted string carries no ``$((…))`` literal,
    # so the recursion is a single level.
    normalized = _ARITH_INT_LITERAL_RE.sub(_arith_int_literal_repl, t)
    if normalized != t and _operand_targets_self(normalized, host_position=host_position):
        return True
    # round-17: an arithmetic expansion that SURVIVED normalization is an
    # expression (``$((0+1))``) whose produced text only the shell knows.
    # In a connection-target position that unknown glues into the host
    # (``127.0.0.$((0+1))`` -> ``127.0.0.1``), so fail closed.  For the
    # ``host:path`` colon form only the PRE-COLON host part can name this
    # machine (round-25): arithmetic after the colon glues into a remote
    # filename (``far:/backups/part$((i)).tar``), so it stays allowed.
    if "$((" in normalized and (
        host_position or (":" in normalized and "$((" in normalized.split(":", 1)[0])
    ):
        return True
    # round-10: brace expansion splices each alternative into the
    # surrounding word BEFORE every other expansion (``local{h,}ost`` ->
    # ``localhost``), so each choice is re-checked.  ``None`` means the
    # fan-out exceeded the cap: fail closed — no legitimate single-target
    # connection operand needs a >256-way product, and over-blocking is this
    # floor's safe direction.
    choices = _brace_expansions(t)
    if choices is None:
        return True
    for choice in choices:
        if choice != t and _operand_targets_self(choice, host_position=host_position):
            return True
    scheme_seen = False
    for scheme in ("ssh://", "scp://", "sftp://", "rsync://"):
        if t.startswith(scheme):
            # Isolate the URI authority before any path, so an ``@`` or ``:``
            # in the remote path cannot masquerade as userinfo or a port.
            t = t[len(scheme) :].split("/", 1)[0]
            scheme_seen = True
            break
    # A bare loopback/IP literal is a host even when it CONTAINS colons
    # (``::1``, ``::ffff:127.0.0.1``): check the whole token before
    # colon-splitting, which would read an IPv6 literal as an empty host.
    # The DNS layer applies to the WHOLE token only when the token sits in
    # host position (or a scheme marked it as an authority) -- a plain word
    # here may be a local filename (round-17).
    if _host_is_self(t, dns_fallback=host_position or scheme_seen):
        return True
    if t.startswith("[") or "@[" in t:
        # Bracketed IPv6, optionally behind userinfo: user@[::1]:path
        host = t.split("[", 1)[1].partition("]")[0]
    else:
        # host[:path] first, THEN userinfo — in that order, so an ``@`` in
        # the path part (``localhost:/tmp/a@b``) is never taken as userinfo.
        pre_colon = t.partition(":")[0]
        if "@" in pre_colon:
            # An ``@`` BEFORE the first colon is real userinfo.  Check the
            # whole remainder after it as a bare host first: ``user@::1``
            # names ``::1``, which the plain colon split below would misread
            # as an empty host.  Userinfo marks the remainder as a host.
            if _host_is_self(t[len(pre_colon.rsplit("@", 1)[0]) + 1 :]):
                return True
        host = pre_colon
        if "@" in host:
            host = host.rsplit("@", 1)[1]
    # An isolated host part (a scheme authority, a bracketed literal, a
    # ``host:path`` prefix, a userinfo remainder) IS a host wherever the
    # operand sits, so the DNS layer applies; an operand isolation did not
    # shorten is a bare word -- a host only in host position (round-17).
    if "@" in t and (scheme_seen or "/" not in t):
        # Userinfo may CONTAIN a colon (``user:pass@host``): the colon-first
        # split above then reads the host as ``user``.  OpenSSH resolves the
        # destination AFTER the LAST ``@`` -- in a URI authority (its path is
        # already split off) and in the plain ``[user@]host`` form (which has
        # no path).  A token with a ``/`` outside a scheme keeps the round-17
        # order: its ``@`` is path data (``far:/backup/a@b``).
        tail = t.rsplit("@", 1)[1]
        if _host_is_self(tail, dns_fallback=host_position or scheme_seen):
            return True
        tail_host = tail.partition(":")[0]
        if tail_host != tail and _host_is_self(
            tail_host, dns_fallback=host_position or scheme_seen
        ):
            return True
    return _host_is_self(host, dns_fallback=host_position or scheme_seen or host != t)


def _ssh_family_verb(token: str) -> "str | None":
    """The ssh-family verb *token* invokes, or None.

    Path-qualified (``/usr/bin/ssh``) and Windows (``ssh.exe``) spellings
    resolve to the bare verb.  A glob basename that CAN expand to one
    (``s?h``, see ``_GLOB_CHARS``) resolves to the verb it can name.
    """
    base = _program_basename(_strip_redirect(token.strip("\"'")))
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    if base in _SSH_FAMILY_VERBS:
        return base
    if _GLOB_CHARS.intersection(base):
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return verb
    return None


# ssh_config keywords that SET the destination.  ``-o hostname=…`` rewrites
# the host the positional operand merely aliases, and ``-o proxyjump=…``
# opens a connection of its own — so a self value in either is a self
# connection regardless of the operand.  Generic ``opt=value`` spellings
# (rsync ``--exclude=localhost``) name data, not a destination.
_SSH_ROUTING_OPTION_KEYS: tuple[str, ...] = ("hostname", "proxyjump")

# ssh_config keywords whose VALUE is a command line ssh runs LOCALLY, not a
# host.  ``-o proxycommand="ssh localhost x"`` and ``-o localcommand=…`` both
# exec their value on THIS machine, so a value that itself opens a channel to
# this host is a self connection; ``-o knownhostscommand=…`` likewise runs its
# value locally (to print known_hosts lines), so it is scanned the same way.
# scp/sftp forward ``-o`` to ssh, so the same hole reaches them.  The value is
# checked by recursing the whole floor on it.
_SSH_COMMAND_OPTION_KEYS: tuple[str, ...] = ("proxycommand", "localcommand", "knownhostscommand")

# OpenSSH's valueless short flags, case-folded (the floor lowercases the whole
# command line once at entry).  getopt lets these BUNDLE in front of an option
# letter -- ``-voHostname=x`` is ``-v`` + ``-o Hostname=x`` -- so a glued
# ``o``/``j`` is still the option letter after this prefix.  Value-taking
# letters (``-l``, ``-p``, ...) are NOT here: they consume the rest of the
# token as their argument.  A folded twin whose uppercase takes a value
# (``-Q``, ``-M``) can only ADD a denial of a spelling ssh itself rejects,
# never an allow.
_SSH_VALUELESS_SHORT_FLAGS = "1246acfgkmnqstvxy"

# Case-folded option letters that CONSUME the next token as their value, PER
# VERB (round-19).  The walk itself stays table-free and fail-closed -- these
# sets only decide whether the token AFTER an option keeps HOST POSITION and
# whether it consumes the ssh/sftp positional slot.  The source is case-folded,
# so where upper and lower case disagree on valueness the letter is IN the
# set, i.e. treated as VALUE-TAKING: a value token mistaken for the positional
# CONSUMES the slot and the real host after it is never checked at all
# (``sftp -R 64 localhost``), while a host token mistaken for a value leaves
# the slot pending, so every later token still gets the full checks --
# over-checking is this floor's safe direction.  (This reverses the round-18
# collision rule, whose "the DNS layer still covers a host after it" bet did
# not survive the consumption path.)  scp/rsync have no positional slot in
# this walk, so they need no entry.
_VERB_VALUE_TAKING_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("bcdefijlmopqrsw"),
    "sftp": frozenset("bcdfijloprsx"),
}

# Folded letters whose two cases DISAGREE on valueness (round-31): the letter
# stays in the value set above (so the slot is not consumed by mistake), but
# the swallowed token ALSO keeps full host-position checks — both candidate
# destinations are over-checked, this floor's safe direction.  ssh: c/C, f/F,
# m/M, q/Q, s/S.  sftp: c/C, f/F, p/P, r/R.
_VERB_FOLD_AMBIGUOUS_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("cfmqs"),
    "sftp": frozenset("cfpr"),
}


def _proxyjump_value_targets_self(value: str) -> bool:
    """True if any hop in a ProxyJump chain names this host.

    A ProxyJump value is a COMMA-SEPARATED chain (``localhost,far``); ssh dials
    the first hop directly from HERE, so a self-host anywhere in the chain is a
    self dial.  Each hop is checked (fail-closed): over-blocking a later hop is
    the safe direction, and it keeps this simple.  The WHOLE value is checked
    too (round-10): a brace alternation's own comma (``local{h,}ost``) is torn
    by the hop split, and only the unsplit spelling resolves back to the word
    bash glues together.
    """
    if _operand_targets_self(value):
        return True
    return any(_operand_targets_self(hop) for hop in value.split(","))


def _remote_forward_value_targets_self(value: str) -> bool:
    """True if a remote-forward (``-R`` / RemoteForward) spec dials THIS host.

    ``-R`` is the one forward whose DESTINATION is dialed FROM HERE: the far
    sshd listens, and each accepted connection is handed back for THIS client
    to connect to ``host:hostport`` locally -- so ``-R 2222:localhost:22``
    gives remote users the local unsandboxed sshd.  Branch table for
    ``_SSH_FORWARD_OPT_LETTERS`` (the others stay exempt): ``-L``/``-D``
    destinations are dialed from the FAR side (its loopback is the far
    machine), ``-w`` names tun devices, ``-b`` a local source bind -- none
    dials a destination from here.  Spec shapes, colon-split OUTSIDE
    brackets: ``[bind:]port:host:hostport`` (3-4 fields) checks the host
    field as a connection target (DNS-classified like any host value);
    ``[bind:]port`` (1-2 fields) is reverse-SOCKS, where the REMOTE chooses
    every local dial destination -- loopback included -- so it fails closed.
    The config spelling ``RemoteForward listen dest`` is whitespace-joined
    onto ``:`` first, which reduces it to the same shape.
    """
    spec = ":".join(value.strip().strip("\"'").split())
    fields: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            fields.append(spec[start:i])
            start = i + 1
    fields.append(spec[start:])
    if len(fields) <= 2:
        return True
    dest = fields[-2].strip().strip("[]")
    return bool(dest) and _operand_targets_self(dest)


def _command_value_names_self_endpoint(value: str) -> bool:
    """True if a ProxyCommand-style value names a LITERAL self endpoint.

    The value is a local command line whose network endpoint decides where
    the outer session lands (round-26): ``nc 127.0.0.1 22`` relays the whole
    session to the local sshd with no ssh-family verb for the recursion to
    see.  Each whitespace word is checked as a connection operand, and its
    bracketed groups and separator-split fragments are checked LITERALLY --
    fragments never resolve DNS (``dns_fallback=False``), so option words
    like ``-connect`` or socat's ``TCP`` address prefix cannot fail closed,
    and a resolvable alias inside a transport (``nc myalias 22``) stays the
    documented alias residual the interim tier does not close.
    """
    for word in value.split():
        stripped = word.strip("\"'")
        if not stripped or stripped.startswith("-"):
            continue
        if _operand_targets_self(stripped, host_position=False):
            return True
        for group in re.findall(r"\[([^\]]*)\]", stripped):
            if _host_is_self(group, dns_fallback=False):
                return True
        without_brackets = re.sub(r"\[[^\]]*\]", "", stripped)
        for frag in re.split(r"[:=,]", without_brackets):
            if frag and _host_is_self(frag, dns_fallback=False):
                return True
    return False


def _routing_option_key_value_targets_self(key: str, value: str) -> bool:
    """True if ssh option *key* routes *value* to this host.

    ``-o`` may be GLUED to the keyword (``-ohostname=…``), so a leading ``o`` in
    front of a longer name is the option letter, not part of the keyword.
    ProxyJump is a comma-chain; ProxyCommand/LocalCommand values are local
    command lines that recurse the floor (the value is strictly shorter than the
    token, so the recursion terminates); Hostname is a single host.
    """
    if key.startswith("o") and len(key) > 1:
        key = key[1:]
    if key == "proxyjump":
        return _proxyjump_value_targets_self(value)
    if key == "remoteforward":
        return _remote_forward_value_targets_self(value)
    if key in _SSH_ROUTING_OPTION_KEYS:  # "hostname" (proxyjump handled above)
        return _operand_targets_self(value)
    if key in _SSH_COMMAND_OPTION_KEYS:
        return _is_ssh_to_self(value) or _command_value_names_self_endpoint(value)
    return False


def _routing_option_value_targets_self(token: str, *, value_slot: bool) -> bool:
    """True if *token* carries a routing option value naming this host.

    Covers the ``key=value`` spelling, the config-style WHITESPACE spelling
    OpenSSH equally accepts (``-o "Hostname localhost"`` resolves to the same
    routing -- verified against ``ssh -G``), and the attached jump-host form
    (``-Jlocalhost``, ``-Jlocalhost,far``, which opens a connection of its own).
    The whitespace form is only read in a VALUE SLOT (an option's argument, or
    attached to the option itself): in plain operand position a two-word token
    is remote command data (``ssh far 'hostname localhost'``), not routing.
    """
    head, eq, value = token.partition("=")
    if eq:
        key = head.lstrip("-")
        if token.startswith("-"):
            # getopt bundles valueless flags in FRONT of the option letter:
            # ``-voHostname=x`` is ``-v`` + ``-o Hostname=x``.
            key = key.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if _routing_option_key_value_targets_self(key, value):
            return True
    elif value_slot:
        parts = token.strip().split(None, 1)
        if len(parts) == 2 and _routing_option_key_value_targets_self(
            parts[0].lstrip("-"), parts[1]
        ):
            return True
    if token.startswith("-"):
        bare = token.lstrip("-").lstrip(_SSH_VALUELESS_SHORT_FLAGS)
        if len(bare) > 1 and bare[0] == "j" and _proxyjump_value_targets_self(bare[1:]):
            return True
    return False


# round-17: same-line literal shell assignments.  bash substitutes ``$a`` /
# ``${a}`` from an assignment earlier on the SAME line before exec, so
# ``a=s; ${a}sh localhost`` runs ``ssh``.  Only literal, separator-free values
# are modeled (optionally quoted); a value carrying ``$``, a backtick, spaces,
# or a command separator stays unresolved -- the documented run-time residual.
# Resolution is position-aware: a reference takes the LATEST assignment whose
# text precedes it, exactly like bash, so ``${a}sh; a=s`` does not resolve.
_LINE_ASSIGNMENT_RE = re.compile(
    r"(?:\A|[;&|`(\s])([a-z_][a-z0-9_]*)=([\"']?)([a-z0-9._:@/-]*)\2(?=\Z|[)\s;&|])"
)
_VAR_REFERENCE_RE = re.compile(r"\$\{([a-z_][a-z0-9_]*)\}|\$([a-z_][a-z0-9_]*)")


def _resolve_line_assignments(text: str) -> str:
    """Substitute ``$var``/``${var}`` from literal same-line assignments."""
    assignments: "list[tuple[int, str, str]]" = []
    for m in _LINE_ASSIGNMENT_RE.finditer(text):
        assignments.append((m.end(), m.group(1), m.group(3)))
    if not assignments:
        return text

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        value: "str | None" = None
        for end, assigned, assigned_value in assignments:
            if end <= match.start() and assigned == name:
                value = assigned_value
        return match.group(0) if value is None else value

    return _VAR_REFERENCE_RE.sub(_substitute, text)


# round-17: one-level function-call argument binding.  A same-line function
# definition whose body dials a positional parameter (``f(){ ssh "$1" id; };
# f localhost``) is an ordinary evasion: the literal call argument IS the
# destination.  Each (definition, later call) pair is bound and the bound body
# recursed through the floor.  One level only -- a function calling another
# function is the documented residual -- and both fan-outs are capped.
_FUNCTION_DEF_RE = re.compile(
    # ``name() { … }``, ``function name() { … }``, and bash's parenthesis-free
    # ``function name { … }`` keyword form (round-18) all define the same
    # function.  The body group matches BALANCED braces one level deep
    # (round-24): a non-greedy ``.*?`` stopped at the ``}`` inside ``${1}``,
    # truncating the body and losing the dial.  The two alternatives are
    # disjoint on their first character, so the group is backtracking-safe;
    # a body nesting braces two deep stays unmatched -- same one-level scope
    # as the binder itself (a function calling a function is the documented
    # residual).
    r"(?:function\s+([a-z_][a-z0-9_]*)\s*(?:\(\s*\))?|([a-z_][a-z0-9_]*)\s*\(\s*\))\s*"
    r"\{((?:[^{}]|\{[^{}]*\})*)\}",
    re.DOTALL,
)
_FUNCTION_BIND_CAP = 8

# round-24: a call still INVOKES the function behind leading assignment words
# (``x=1 f localhost``) and the invocation keywords that run their operand as
# a command (``time f``, ``! f``, ``if f; then``).  ``command``/``exec``/
# ``nohup`` are NOT here: ``command`` bypasses function lookup and the other
# two exec real binaries, so none of them reaches the function body.
_CALL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*\Z")
_CALL_WRAPPER_WORDS: frozenset[str] = frozenset(
    {"time", "!", "if", "elif", "while", "until", "then", "else", "do", "{", "("}
)


def _function_call_binds_self(text: str) -> bool:
    """True if a bound (def, call) pair opens an ssh channel to this host."""
    # The caller hands over masked text: a quoted ``{`` rides as a sentinel
    # byte while its closing ``}`` stays literal, which breaks the DEF
    # regex's balanced-brace body capture on ``"${10}"``.  Restore ONLY the
    # parameter-expansion opener -- ``${`` is an expansion, never a command
    # boundary -- so quoted braced positionals capture and bind (round-33).
    text = text.replace("$\x06", "${")
    for def_count, m in enumerate(_FUNCTION_DEF_RE.finditer(text)):
        if def_count >= _FUNCTION_BIND_CAP:
            # Past the cap the binder cannot resolve what a definition does,
            # so it fails CLOSED: a 9th definition is deniable padding, not a
            # command shape the floor can clear (round-30).
            return True
        name, body = m.group(1) or m.group(2), m.group(3)
        # The caller hands over masked text (quoted separators AND quoted
        # braces ride as mask bytes), so a quoted ``"${10}"`` reads as
        # ``"$\x0610}"`` here.  Unmask the captured body before binding or
        # the braced replacements never match (round-33).
        body = _unmask_separators(body)
        if "$" not in body:
            continue  # nothing to bind
        call_count = 0
        for segment in re.split(r"[;&|\n]", text[m.end() :]):
            words = segment.split()
            # round-24: skip leading assignment words and invocation keywords
            # -- both still invoke the function.  Any OTHER leading word means
            # the name is an argument (``echo f localhost``), not a call.
            idx = 0
            while idx < len(words) and (
                words[idx] in _CALL_WRAPPER_WORDS
                or _CALL_ASSIGNMENT_RE.fullmatch(words[idx]) is not None
            ):
                idx += 1
            if idx >= len(words) or words[idx] != name:
                continue
            call_count += 1
            if call_count > _FUNCTION_BIND_CAP:
                # Same fail-closed rule as the definition cap: the 9th call
                # of one name is unresolvable padding, so deny (round-30).
                return True
            args = [w.strip("\"'") for w in words[idx + 1 :]]
            bound = body
            joined = " ".join(args)
            for star in ('"$@"', "$@", '"$*"', "$*"):
                bound = bound.replace(star, joined)
            # Braced ``${N}`` has no single-digit ceiling in bash, so it
            # binds for EVERY supplied argument; unbraced ``$N`` reads one
            # digit (``$10`` is ``${1}0``), so it stays capped at nine
            # (round-33).
            for i, arg in enumerate(args, start=1):
                bound = bound.replace('"${%d}"' % i, arg)
                bound = bound.replace("${%d}" % i, arg)
                if i <= 9:
                    bound = bound.replace('"$%d"' % i, arg)
                    bound = bound.replace("$%d" % i, arg)
            if bound != body and _is_ssh_to_self(bound):
                return True
    return False


def _is_ssh_to_self(text_lower: str) -> bool:
    """True if *text_lower* opens an ssh/scp/sftp/rsync channel to THIS host.

    Structural for the same reason the other floors are: the target hides
    behind options, redirects, quoting, and prefixes.  The argv walk is
    FAIL-CLOSED about option grammar: EVERY token that could be an operand —
    including one sitting where an option's value would go — is checked
    against the self-host set.  The per-verb value-taking table
    (``_VERB_VALUE_TAKING_OPT_LETTERS``) never exempts a token from that
    check; it only decides whether the token after an option keeps HOST
    POSITION (the DNS-layer gate) and whether it consumes the ssh/sftp
    positional slot, with collisions resolved toward value-taking — the
    over-checking direction (a valueless-flag table exempting the target
    itself is the shape that mis-consumed ``scp -r localhost:…``).  A
    non-forwarding option value (a port, a cipher) never names this host, so
    the check costs nothing; a self-host hiding in value position is denied.
    The one carved-out value class is the forward/bind specs
    (``_SSH_FORWARD_OPT_LETTERS``): ``ssh -L 127.0.0.1:8080:db:5432
    far-host`` names a local LISTEN address, not a destination, and must
    stay allowed.
    ssh/sftp consume their FIRST unshadowed operand as the positional host
    (later operands are the remote command, where a word like "localhost" is
    data, not a destination); scp/rsync accept a target in any operand
    position.  ``opt=value`` tokens (``-ohostname=localhost``,
    ``-o proxyjump=localhost``) have their value checked too.  Redirections
    are stepped over the way bash removes them from argv.  Same argv-boundary
    discipline as the self-kill floor: the walk is substitution-aware and
    stops at this command's own separator.

    Two evasions are neutralized before the walk.  Empty command substitutions
    (``s$()sh``, ``local$()host``) expand to nothing, so they are collapsed out
    of the source text first -- otherwise they hide the verb from the substring
    gate and the self-host from an operand.  Quote and backslash splices
    (``ss""h``, ``s\\sh``) are rejoined by shlex in the tokens but still hide the
    verb from the raw-substring gate, so the gate probes a splice-stripped copy.
    Two value classes are command lines this host runs LOCALLY rather than
    hosts, and both recurse this floor on the value: ssh's
    ``-o proxycommand=``/``-o localcommand=`` (in
    ``_routing_option_value_targets_self``) and rsync's ``-e``/``--rsh``.
    rsync also honors a leading ``RSYNC_RSH=`` environment assignment as the
    same selector -- handled in the walk below; the wider remote-shell env-var
    family (``GIT_SSH_COMMAND`` and the like) is a documented residual, as are
    the shell-builtin export spellings (``declare -x``/``typeset -x``), which
    are not leading-assignment syntax and so are not modeled by the walk.
    """
    # Collapse empty expansions in the SOURCE so the gate and tokenization both
    # read the spliced-out spelling (``s$()sh localhost`` -> ``ssh localhost``).
    text_lower = _EMPTY_EXPANSION_RE.sub("", text_lower)

    # A flat command substitution whose output is statically decidable
    # (``$(printf localhost)``, backtick ``echo localhost``) IS its output by
    # the time the kernel sees the command line, so splice that output into
    # the source before the probe and the walk -- the resolved self-host is
    # then an ordinary operand, glued spellings (``local$(printf host)``)
    # included.  ``_static_substitution_output`` resolves only echo/printf
    # literals; every other body keeps its original text (the ``$(hostname``
    # hints below read it) and stays the documented run-time residual.  A
    # single-quoted spelling resolves too -- an over-approximation toward
    # deny, accepted for a deny floor.
    def _splice_static_substitution(match: "re.Match[str]") -> str:
        body = match.group(1) if match.group(1) is not None else match.group(2)
        out = _static_substitution_output(body)
        return match.group(0) if out == "\x00" else out

    text_lower = _FLAT_SUBSTITUTION_RE.sub(_splice_static_substitution, text_lower)
    # round-17: substitute ``$a``/``${a}`` from literal same-line assignments
    # (``a=s; ${a}sh localhost`` -> ``ssh localhost``) so neither the verb nor
    # the operand can be spliced from statically-known text.  Values are
    # separator-free by construction, so the substitution cannot fabricate a
    # command boundary the masking below would misread.
    text_lower = _resolve_line_assignments(text_lower)
    # Mask every ``;``/``|`` that is QUOTED or backslash-escaped in the source to
    # a sentinel BEFORE tokenization, so a quoted separator surviving into a
    # shlex-dequoted token (``scp 'a;b' localhost:/x``) is not read as a
    # command boundary by ``_ends_argv`` -- which would end the walk before the
    # real target and let the connection through.  It is restored only at the
    # faithful operand/routing checks below; recursion sites receive the masked
    # text unchanged (the mask is idempotent -- their quotes are already gone).
    text_lower = _mask_quoted_separators(text_lower)
    # Quote/backslash splices survive into the tokens (shlex rejoins them) but
    # defeat this raw-substring gate, so probe a copy with them stripped out.
    # round-20 (Opus): bash decodes ANSI-C quoting ($'\x73\x73\x68' -> ssh)
    # before exec, and the operand walk's tokenizer resolves it too -- so the
    # gate guarding that walk probes the DECODED text, or a verb spelling the
    # walk would deny slips past the gate the walk never gets to correct.
    probe = _decode_shell_quoted_literals(text_lower)
    probe = probe.replace('"', "").replace("'", "").replace("\\", "")
    # bash substitutes ``${VAR:-word}`` defaults before exec, so the verb can
    # be spliced from statically-known text (``s${U:-s}h`` -> ``ssh``).
    # Resolve the same forms the operand walk resolves; a bare ``$VAR`` whose
    # value exists only at run time stays the documented fail-open residual.
    probe = _resolve_param_defaults(probe)
    if not any(verb in probe for verb in _SSH_FAMILY_VERBS) and not _glob_can_name_ssh_verb(probe):
        return False
    # round-17: bind one level of function-call arguments (``f(){ ssh "$1"
    # id; }; f localhost``) and recurse on the bound body -- the literal call
    # argument is the destination the walk below cannot see through ``$1``.
    if _function_call_binds_self(text_lower):
        return True
    # round-8: a self-targeting ``RSYNC_RSH`` set in one frame is inherited by
    # LATER frames (an exported value, or a command-scoped prefix on a command
    # that spawns a nested ``sh -c`` payload), where the per-frame pending/export
    # walk below -- which re-initialises inside each frame -- cannot see it.
    # This function-scope latch carries that fact forward; it is set at the END
    # of a frame (so the round-7 same-frame ``;``-clear semantics are unchanged
    # for the frame that owns the assignment) and never cleared.
    # Over-approximation, accepted for a deny floor: once set, the latch also
    # covers a textually-later SIBLING nested payload; a contrived same-frame
    # ``RSYNC_RSH=self cmd; rsync other:/ .`` stays allowed by the round-7 clear
    # rule because the latch is consulted only in LATER frames.
    rsync_rsh_carried_self = False
    # round-20 (GPT): destination paths a same-line cp/mv/ln/install gave to
    # an ssh-family binary, bound to the verb they now carry.  Shared across
    # frames (a copy staged in a wrapper payload reaches its siblings); a
    # copy staged in an EARLIER command line is the documented residual.
    bound_program_verbs: "dict[str, str]" = {}
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        # A leading ``RSYNC_RSH=<cmd>`` environment assignment selects rsync's
        # remote shell exactly like ``-e``/``--rsh``, but rides BEFORE the verb
        # so the operand walk below never sees it.  When rsync runs live, that
        # value is a local command line that recurses the floor.  Bash applies a
        # leading assignment to the WHOLE simple command that follows -- the
        # command word and every child it execs, wrappers of any shape
        # (``env``/``command``/``timeout`` …) included -- so an ordinary word
        # does NOT drop the pending value; only a command separator ends that
        # simple command and clears it (tracked substitution-aware, so a ``;``
        # inside ``$( … )`` does not).  ``export`` makes the value persist for
        # the rest of the line, so an exported value is remembered separately
        # and survives separators.
        rsync_rsh_pending: dict[str, str] = {}
        rsync_rsh_exported: dict[str, str] = {}
        # Names declared for export BEFORE any value exists (the POSIX
        # ``export VAR; VAR=value`` order, round-31): a later assignment to a
        # marked name is exported the moment it lands.  Never cleared by
        # separators.
        rsync_rsh_export_marked: set[str] = set()
        # The last plain assignment seen in this frame, PER NAME (round-30:
        # the two rsync env vars are independent — one shared slot let a later
        # far assignment overwrite an earlier self value), wherever it
        # appeared: a shell variable persists for the rest of the line even
        # after the simple command it prefixed ends, so a later bare
        # ``export RSYNC_RSH`` (the POSIX ``VAR=value; export VAR`` two-step)
        # promotes THAT name's value into the environment.  Never cleared by
        # separators.
        rsync_rsh_assigned: dict[str, str] = {}
        prev_stripped_tok: "str | None" = None
        cmd_start = True  # the next token sits in program position
        outer_depth = 0
        xargs_prefix_index: "int | None" = None  # a bare ``xargs`` in this simple command
        for i, token in enumerate(tokens):
            verb = _ssh_family_verb(token)
            if verb is None and bound_program_verbs:
                stripped_prog = token.strip("\"'")
                verb = bound_program_verbs.get(stripped_prog) or bound_program_verbs.get(
                    _program_basename(stripped_prog)
                )
            if verb is None:
                stripped_tok = token.strip("\"'")
                assign = _RSYNC_RSH_ASSIGN_RE.match(stripped_tok)
                if assign is not None:
                    env_name = assign.group(1)
                    value = assign.group(2).strip("\"'")
                    rsync_rsh_pending[env_name] = value
                    rsync_rsh_assigned[env_name] = value
                    # A preceding ``export`` makes the assignment persist into
                    # every later segment's environment for real, so remember it
                    # where no separator clears it.
                    if prev_stripped_tok == "export" or env_name in rsync_rsh_export_marked:
                        rsync_rsh_exported[env_name] = value
                else:
                    exported_name = stripped_tok.rstrip(";&")
                    if prev_stripped_tok == "export" and exported_name in (
                        "rsync_rsh",
                        "rsync_connect_prog",
                    ):
                        # Bare ``export RSYNC_RSH`` (or ``RSYNC_CONNECT_PROG``)
                        # after an earlier plain assignment: THAT name's stored
                        # value enters the environment.  The walk reads lowered
                        # text, and a glued separator (``export RSYNC_RSH;``)
                        # rides on the name token.  Declared BEFORE any value
                        # (export-first), the name is marked so the later
                        # assignment exports itself (round-31).
                        rsync_rsh_export_marked.add(exported_name)
                        if exported_name in rsync_rsh_assigned:
                            rsync_rsh_exported[exported_name] = rsync_rsh_assigned[exported_name]
                    outer_depth += _substitution_depth_delta(token)
                    if outer_depth <= 0 and _ends_argv(token):
                        # A command separator ends the simple command the
                        # leading assignment prefixed; pending does not cross it.
                        rsync_rsh_pending.clear()
                        xargs_prefix_index = None
                    outer_depth = max(outer_depth, 0)
                # round-35 (GPT): xargs turns its stdin into argv for the
                # program it launches -- remember a bare ``xargs`` in this
                # simple command (any position: wrappers keep it off program
                # position), so a later verb + here-string is judged as the
                # command xargs actually runs.
                if _program_basename(stripped_tok) == "xargs":
                    xargs_prefix_index = i
                # round-20 (GPT): a program-position cp/mv/ln/install whose
                # source operand is an ssh-family binary binds its DESTINATION
                # operand as that verb for the rest of the line, so
                # ``cp /usr/bin/ssh /tmp/x && /tmp/x localhost`` cannot shed
                # the floor by shedding the basename.  Statically decidable,
                # exactly like the round-17 assignment/function binders.
                if cmd_start and _program_basename(stripped_tok) in _BINARY_COPY_VERBS:
                    operands: "list[str]" = []
                    for peek in tokens[i + 1 :]:
                        peeked = peek.strip("\"'")
                        if _ends_argv(peek):
                            # A separator can ride glued on the last operand
                            # (``/tmp/y;``) -- keep the de-glued word, then
                            # stop at the boundary.
                            deglued = peeked.rstrip(";&|\n")
                            if deglued and not deglued.startswith("-"):
                                operands.append(deglued)
                            break
                        if peeked.startswith("-"):
                            continue
                        operands.append(peeked)
                    if len(operands) >= 2:
                        for source in operands[:-1]:
                            bound_verb = _ssh_family_verb(source)
                            if bound_verb is not None:
                                dest = operands[-1]
                                bound_program_verbs[dest] = bound_verb
                                bound_program_verbs[_program_basename(dest)] = bound_verb
                                break
                cmd_start = _ends_argv(token) or (
                    cmd_start and _ENV_ASSIGN_PREFIX_RE.match(stripped_tok) is not None
                )
                prev_stripped_tok = stripped_tok
                continue
            # A verb starts a fresh simple command, so the ``export`` adjacency
            # run ends here.
            prev_stripped_tok = None
            # ``echo ssh localhost`` prints two words; it connects to nothing.
            if _data_consumer_exempt(i, token, programs, tokens):
                continue
            # round-35 (GPT): launched through xargs, the verb's REAL argv
            # arrives on stdin -- and a here-string puts that stdin in the
            # source text, so ``xargs ssh <<< localhost`` runs
            # ``ssh localhost``.  Rebuild that command and judge it like a
            # directly-typed one; the rebuild drops the xargs word and the
            # here-string pair, so the recursion walks strictly fewer tokens
            # and terminates.
            if xargs_prefix_index is not None and xargs_prefix_index < i:
                rebuilt = _xargs_here_string_rebuild(verb, tokens, xargs_prefix_index, i)
                if rebuilt is not None and _is_ssh_to_self(rebuilt):
                    return True
            # rsync execs a pending ``RSYNC_RSH`` / ``RSYNC_CONNECT_PROG``
            # value as its remote shell (or daemon proxy) FROM HERE, so a
            # value that opens a channel to this host is a self dial.
            # An unexported value applies only to this simple command; an
            # exported one persists, so fall back to it when nothing is pending.
            if verb == "rsync":
                for env_name in ("rsync_rsh", "rsync_connect_prog"):
                    env_val = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
                    if env_val is not None and _is_ssh_to_self(env_val):
                        return True
                # round-8: with neither a pending nor an exported value in THIS
                # frame, fall back to a self-targeting value carried from an
                # EARLIER frame (inherited into this nested payload).
                if not rsync_rsh_pending and not rsync_rsh_exported and rsync_rsh_carried_self:
                    return True
            positional_pending = verb in ("ssh", "sftp")
            option_shadow = False  # previous token was an option that may take a value
            value_shadow = False  # previous token was an option that CONSUMES a value
            value_shadow_ambiguous = False  # ...but its folded letter is case-ambiguous
            forward_value_pending = False  # previous token was a forward/bind option
            forward_value_letter = ""  # which forward letter armed it (``r`` is checked)
            redirect_target_pending = False  # previous token was a detached redirect op
            rsh_value_pending = False  # previous token was rsync -e/--rsh (value is a local cmd)
            proxyjump_value_pending = False  # previous token was a detached -J (value = hop chain)
            opts_terminated = False  # an exact ``--`` ended option parsing (POSIX)
            depth = 0
            for arg in tokens[i + 1 :]:
                stripped = arg.strip("\"'")
                # Classify BEFORE testing whether the token ends the argv
                # (same order as the self-kill floor): a quoted remote payload
                # may contain separator characters, and for scp/rsync a
                # target can legally follow it.
                if redirect_target_pending:
                    # The filename after a detached ``>``/``2>``/``<`` — bash
                    # removes both words from argv before exec.
                    redirect_target_pending = False
                elif rsh_value_pending:
                    # The detached value of rsync ``-e``/``--rsh``: a
                    # remote-shell command line rsync execs FROM HERE, so
                    # ``-e 'ssh localhost'`` opens a local ssh into this host.
                    # Recurse the floor on the value (strictly shorter than the
                    # whole command, so this terminates).  Checked first so the
                    # value is consumed whatever it looks like.
                    rsh_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _is_ssh_to_self(stripped):
                        return True
                elif proxyjump_value_pending:
                    # The detached value of ``-J`` (round-17): a ProxyJump hop
                    # chain whose FIRST hop is dialed from here -- comma-split
                    # it exactly like the attached ``-Jvalue`` spelling, which
                    # ``_routing_option_value_targets_self`` already covers.
                    proxyjump_value_pending = False
                    option_shadow = False
                    value_shadow = False
                    if _proxyjump_value_targets_self(_unmask_separators(stripped)):
                        return True
                elif ">" in stripped or "<" in stripped:
                    # A redirection construct.  The part before the operator is
                    # an ordinary word when non-numeric
                    # (``localhost>/dev/null``); a bare or fd-prefixed operator
                    # (``>``, ``2>``) also consumes the NEXT token as its
                    # target, unless the target is attached (``>/dev/null``,
                    # ``2>&1``).
                    remainder = _strip_redirect(stripped)
                    if remainder and not remainder.isdigit():
                        checkable = positional_pending or verb in ("scp", "rsync")
                        # Host position = the unshadowed ssh/sftp positional
                        # slot; an option value or an scp/rsync file operand
                        # is not one (its ``host:path`` colon form re-enables
                        # the DNS layer inside the check) -- round-17.
                        if checkable and _operand_targets_self(
                            _unmask_separators(remainder),
                            host_position=positional_pending and not value_shadow,
                        ):
                            return True
                        if not value_shadow:
                            # round-18: after a VALUELESS flag the token IS
                            # the positional destination -- consuming the slot
                            # keeps a later dotted remote-command argument
                            # out of host position.
                            positional_pending = False
                    elif stripped.endswith((">", "<")):
                        redirect_target_pending = True
                    option_shadow = False
                    value_shadow = False
                elif stripped == "--" and not opts_terminated:
                    # round-20 (GPT): exact ``--`` is the POSIX option
                    # TERMINATOR -- everything after it is an operand.
                    # Classified as a long option, ``value_shadow`` swallowed
                    # the NEXT token out of host position, so a DNS-classified
                    # self alias after ``--`` was never checked.
                    opts_terminated = True
                    option_shadow = False
                    value_shadow = False
                elif not opts_terminated and stripped.startswith("-") and len(stripped) > 1:
                    # An option.  Its attached ``=value`` is checked only for
                    # the ROUTING options ssh resolves a destination from
                    # (``-ohostname=localhost``, ``-oproxyjump=localhost``) —
                    # a generic ``--opt=value`` (rsync ``--exclude=localhost``)
                    # names data, not a destination.
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=True
                    ):
                        return True
                    # rsync runs its ``-e``/``--rsh`` value as the remote-shell
                    # command FROM HERE, so ``-e 'ssh localhost'`` execs a local
                    # ssh into this host: the value is a command line, not a
                    # host, and must recurse this floor.  It rides in-token for
                    # ``--rsh=…`` and for a single-dash bundle that reaches
                    # ``e`` with letters after it (``-e'ssh …'`` tokenizes to
                    # ``-essh …``); a bare ``--rsh`` or a bundle ENDING in ``e``
                    # (``-ave``) takes the NEXT token via ``rsh_value_pending``.
                    # Only exactly ``rsh`` among double-dash options consumes a
                    # value, so ``--exclude=localhost`` stays data.
                    if verb == "rsync":
                        bare_opt = stripped.lstrip("-")
                        if stripped == "--rsh":
                            rsh_value_pending = True
                        elif stripped.startswith("--rsh="):
                            if _is_ssh_to_self(stripped.partition("=")[2]):
                                return True
                        elif not stripped.startswith("--") and "e" in bare_opt:
                            attached = bare_opt.partition("e")[2]
                            if attached and _is_ssh_to_self(attached):
                                return True
                            if not attached:
                                rsh_value_pending = True
                    option_shadow = True
                    bare = stripped.lstrip("-")
                    # round-18/19: only an option whose (bundle-final) letter
                    # takes a value for THIS verb swallows the next token out
                    # of host position; ``ssh -v self.example`` keeps its
                    # destination DNS-checked, ``sftp -R 64 localhost`` keeps
                    # its positional slot pending past the consumed ``64``.
                    # Double-dash options keep the conservative value
                    # assumption.
                    value_shadow = stripped.startswith("--") or (
                        bool(bare)
                        and bare[-1] in _VERB_VALUE_TAKING_OPT_LETTERS.get(verb, frozenset())
                    )
                    # round-31: a folded letter whose two cases disagree on
                    # valueness keeps the swallowed token host-checkable.
                    value_shadow_ambiguous = (
                        not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _VERB_FOLD_AMBIGUOUS_OPT_LETTERS.get(verb, frozenset())
                    )
                    # Forward/bind exemption is ssh-ONLY: scp/rsync have no
                    # forward options, and their `-r`/`-l` are valueless
                    # flags -- mis-classifying them as value-taking would hide
                    # the target of `scp -r localhost:…`.  Bundle-final
                    # spellings arm like the ``-4J`` precedent below, and the
                    # LETTER is recorded because ``-R`` is not exempt like its
                    # siblings (see ``_remote_forward_value_targets_self``).
                    forward_value_pending = (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and bool(bare)
                        and bare[-1] in _SSH_FORWARD_OPT_LETTERS
                    )
                    forward_value_letter = bare[-1] if forward_value_pending else ""
                    # The GLUED remote-forward spelling carries its spec in the
                    # same token (``-R2222:localhost:22``, ``-R2222``); ssh has
                    # no valueless ``-r``, so within the ssh verb a leading
                    # ``r`` with a spec-shaped remainder is the forward.  A
                    # valueless bundle prefix (``-vR2222:…``) is stripped
                    # first, the round-28 ``-o``/``-J`` treatment (round-31).
                    fwd_bare = bare.lstrip(_SSH_VALUELESS_SHORT_FLAGS)
                    if (
                        verb == "ssh"
                        and not stripped.startswith("--")
                        and len(fwd_bare) > 1
                        and fwd_bare[0] == "r"
                        and (":" in fwd_bare[1:] or fwd_bare[1:].isdigit())
                        and _remote_forward_value_targets_self(fwd_bare[1:])
                    ):
                        return True
                    # round-17: a DETACHED ``-J`` -- or a flag bundle ending in
                    # the jump letter (``-4J``) -- takes the NEXT token as its
                    # ProxyJump hop chain.  The attached spelling (``-Jhost``)
                    # is handled by ``_routing_option_value_targets_self``.
                    if not stripped.startswith("--") and bare.endswith("j"):
                        proxyjump_value_pending = True
                        forward_value_pending = False
                        forward_value_letter = ""
                elif forward_value_pending:
                    # The value of a forward/bind option (see
                    # ``_SSH_FORWARD_OPT_LETTERS``) names a listen address or
                    # a far-side hop for every letter EXCEPT ``r``: a
                    # remote-forward destination is dialed FROM HERE, so its
                    # spec is checked instead of exempted (round-27; the
                    # branch table lives on ``_remote_forward_value_targets_self``).
                    if forward_value_letter == "r" and _remote_forward_value_targets_self(
                        _unmask_separators(stripped)
                    ):
                        return True
                    forward_value_pending = False
                    forward_value_letter = ""
                    option_shadow = False
                    value_shadow = False
                else:
                    # An operand, or the value of the preceding option.  Check
                    # it either way (fail-closed — see the docstring); only an
                    # UNSHADOWED operand consumes the ssh/sftp positional slot.
                    checkable = positional_pending or verb in ("scp", "rsync")
                    if checkable and _operand_targets_self(
                        _unmask_separators(stripped),
                        host_position=positional_pending
                        and (not value_shadow or value_shadow_ambiguous),
                    ):
                        return True
                    if _routing_option_value_targets_self(
                        _unmask_separators(stripped), value_slot=option_shadow
                    ):
                        return True
                    if not value_shadow:
                        # round-18: same consumption rule as the redirect
                        # branch above.
                        positional_pending = False
                    option_shadow = False
                    value_shadow = False
                    value_shadow_ambiguous = False
                depth += _substitution_depth_delta(arg)
                if depth <= 0 and _ends_argv(arg):
                    break
                depth = max(depth, 0)
        # End of frame: a leading RSYNC_RSH selector that SURVIVED to here was
        # consumed by the frame's command (which may spawn a nested payload),
        # and an exported one persists for the whole line -- either way, if it
        # is self-targeting, later frames inherit it.  Latch it now (never
        # cleared) so a nested ``sh -c 'rsync ...'`` frame denies.  Same
        # precedence as the in-walk rsync check: pending, else exported.
        for env_name in ("rsync_rsh", "rsync_connect_prog"):
            carried_now = rsync_rsh_pending.get(env_name, rsync_rsh_exported.get(env_name))
            if carried_now is not None and _is_ssh_to_self(carried_now):
                rsync_rsh_carried_self = True
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
