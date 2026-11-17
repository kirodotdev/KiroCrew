"""Crew's own floors on the files a granted shell command's redirects open.

A spec's ``allowedCommands`` grants a command with its plain redirects taken off
(:func:`kiro_crew.platform.allowed_commands.split_redirects`), as kiro-cli's v3
engine does. The gate's ``spec-builtin-grant`` row asks :func:`redirects_clear`
before it grants such a command, so a redirect never opens a file the gate would
refuse to the file tools.
"""

from __future__ import annotations

import os

from kiro_crew.platform import allowed_commands
from kiro_crew.security.paths import sensitive_path_refusal, sensitive_write_path_refusal


def redirects_clear(command: str | None, cwd: str) -> bool:
    """Whether every file *command*'s plain redirects open is one Crew's floors
    leave alone: a write target is not sensitive or write-protected
    (:func:`sensitive_write_path_refusal`), a ``<`` source is not sensitive
    (:func:`sensitive_path_refusal`). A command with no redirect is clear.

    A relative target is judged under *cwd*, the session's directory. With no
    absolute *cwd*, or inside a chain, where a ``cd`` before it moves the
    directory the shell opens it in, a relative target is not clear. Not clear
    only withholds the grant: the call keeps its prompt.
    """
    split = allowed_commands.split_redirects(command)
    if split is None:
        return False
    rest, redirects = split
    if not redirects:
        return True
    chained = len(allowed_commands.chain_commands(rest) or ()) != 1
    for redirect in redirects:
        if not os.path.isabs(redirect.target) and (chained or not os.path.isabs(cwd)):
            return False
        refusal = sensitive_write_path_refusal if redirect.write else sensitive_path_refusal
        if refusal(redirect.target, cwd or None) is not None:
            return False
    return True
