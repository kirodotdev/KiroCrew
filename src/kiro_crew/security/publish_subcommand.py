"""The normalizer pass of the git-publish detector.

``_is_git_publish`` in ``argv_floor`` asks this pass whether a command whose raw
text hides the program or the subcommand behind quoting still runs ``git`` with
``push`` as its subcommand, reading the words ``normalize_shell_command`` hands
back and skipping git's global flags (``_GIT_ARG_FLAGS``) on the way. Both live
here so ``argv_floor`` stays under the package's per-module cap; ``argv_floor``
re-binds them, so the facade and the floor read the same objects. This module
imports only ``shell_normalizer``.
"""

from __future__ import annotations

import os

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import _cut_at_operator

# Git global flags that consume a separate argument token (appear between
# `git` and the subcommand).
_GIT_ARG_FLAGS = frozenset(
    "-c -C --attr-source --config-env --git-dir --namespace --super-prefix --work-tree".split()
)


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
