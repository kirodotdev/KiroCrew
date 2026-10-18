"""Release detection: a ``git push`` recognised by its subcommand position, then judged by its target.

The verb-anchored regexes and the normalizer second pass decide that a command line
publishes, wherever ``push`` sits in it as text; the push-argument reader, its option
arity tables and the refspec rules decide what it publishes to, and a push to a
protected, bare or ambiguous target is refused. It fails CLOSED: a target that cannot be
read cleanly is refused rather than guessed at. A new git-publish spelling or push
option goes here.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the shell reader
and one sentinel the rule catalog owns, and imports no other part of the argv floor.
"""

from __future__ import annotations

import os
import re

# The reader names a test instruments on the facade are reached as attributes of
# the reader module rather than bound here at import: the facade mirrors a patch
# onto the owning module's namespace, so a name bound into THIS namespace would
# keep resolving the unpatched object and the instrument would count nothing.
from . import shell_normalizer as _shell_normalizer
from .denied_rules import _GIT_PUBLISH_UNGATED
from .shell_normalizer import (
    _AMBIGUOUS_EXPANSION_RE,
    _PROCESS_SUBSTITUTION_OPENERS,
    _REDIRECT_START_RE,
    _cut_at_operator,
    _dequote_token,
    _iter_shell_chars,
    _nested_shell_payloads,
    _push_option_matches,
    _push_token_redirection,
    _push_token_shell_read,
    _shell_quote_walk,
    _split_push_command_segments,
    _split_shell_words,
)

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
