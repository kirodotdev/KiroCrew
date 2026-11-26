"""ssh's own refusal of a sign-in, ``Permission denied (<methods>)``.

The one matcher for it, shared by the App Registry's clone-failure classes
(``apps/registry_pipeline/git_targets.py``, which keeps its names bound for the
registry facade) and the remote crew token mint (``instances/token_mint.py``).
Standard library only, so neither caller loads the other to use it.
"""

from __future__ import annotations

# SSH credential refusal ONLY. The bare token "permission denied" also appears
# in a LOCAL filesystem error — an unwritable clone destination emits
# `fatal: could not create work tree dir '…': Permission denied` — so matching
# it would mislabel a disk-permission failure as withheld remote credentials
# and show the "move the repo inside the registry" hint on an error that has
# nothing to do with credentials. SSH's auth-refusal wording always carries a
# parenthesized, comma-separated list of the server's auth methods, in the
# server's own order: `git@host: Permission denied (publickey).`,
# `(publickey,password)`, `(publickey,keyboard-interactive)` and
# `(keyboard-interactive,publickey)` all occur. A local errno `Permission
# denied` never has that list, so the check requires one: method names that
# start with a letter and contain no spaces, comma-separated, closed by `)`.
# Matched against lowercased text.
_SSH_REFUSAL_PREFIX = "permission denied ("
_SSH_METHOD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789@._+-")


def _ssh_method_list_closes(low: str, pos: int) -> bool:
    """Whether a method list starting at *pos* in *low* is closed by ``)``.

    Reads one character at a time and stops at the first one that cannot
    continue the list: each name starts with ``a``-``z`` and uses only
    :data:`_SSH_METHOD_CHARS`, names are separated by single commas, and the
    list ends at ``)``.
    """
    name_start = True
    for i in range(pos, len(low)):
        ch = low[i]
        if name_start:
            if not ("a" <= ch <= "z"):
                return False
            name_start = False
        elif ch == ")":
            return True
        elif ch == ",":
            name_start = True
        elif ch not in _SSH_METHOD_CHARS:
            return False
    return False


def is_ssh_auth_refusal(low: str) -> bool:
    """Whether lowercased *low* holds an SSH ``Permission denied (<methods>)``.

    Each ``permission denied (`` occurrence is checked in turn. The next
    search starts right after the current prefix, so an occurrence inside a
    rejected list is still checked. A list stops at its first space or
    ``(``, and the prefix holds both, so text is read a bounded number of
    times and the check stays linear in the length of *low*.
    """
    start = low.find(_SSH_REFUSAL_PREFIX)
    while start >= 0:
        body_start = start + len(_SSH_REFUSAL_PREFIX)
        if _ssh_method_list_closes(low, body_start):
            return True
        start = low.find(_SSH_REFUSAL_PREFIX, body_start)
    return False
