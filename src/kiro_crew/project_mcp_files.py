"""A project's own MCP files: which names they are, and where they sit in a folder.

kiro-cli and KAS read ``<folder>/.kiro/settings/mcp.json`` when a session starts in
that folder on a spec with ``includeMcpJson``; Claude Code reads
``<folder>/.mcp.json``. A server an agent plants in either file starts in the next
such session there, so an agent asks with ``request_server`` instead (the "agents
ask before changing gateway-trusted files" entries in ``docs/decisions/``):

* the file-edit tool is refused on either name in ANY folder
  (:func:`project_mcp_write`, read by ``security.paths.sensitive_write_path_refusal``),
  because any folder can be the one a later session starts in;
* the sandbox seals the existing ones in the spawn's own workspace read-only
  (:func:`project_mcp_targets`, read by ``sandbox_argv._live_plan_host``).

Matched by NAME at the end of every candidate spelling, never by a home-relative
row: these files live in the person's own folders, wherever those are.
"""

from __future__ import annotations

import logging
import os
import stat

logger = logging.getLogger(__name__)

#: A project's MCP files, relative to the project folder, in the order a card picks.
KIRO_PROJECT_MCP_FILE = os.path.join(".kiro", "settings", "mcp.json")
CLAUDE_PROJECT_MCP_FILE = ".mcp.json"
PROJECT_MCP_FILES = (KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE)

#: The same names as path tails, ``/``-separated and casefolded for the name match.
_TAILS = tuple("/" + name.replace(os.sep, "/").casefold() for name in PROJECT_MCP_FILES)


def _is_project_mcp_name(path: str) -> bool:
    folded = path.replace("\\", "/").casefold()
    return any(folded.endswith(tail) for tail in _TAILS)


def project_mcp_write(path_str: str, base_dir: str | None = None) -> bool:
    """Whether *path_str* names a project MCP file, in any spelling of it.

    Every candidate the write gate compares (resolved, lexical, raw) is checked, so
    a link to one of these files, or a ``..`` that lands on one, is caught. A
    resolver stall raises ``PathResolutionStalled``, which the gate turns into a
    refusal.
    """
    if not path_str:
        return False
    # Call-time import: the security package imports this module at load.
    from kiro_crew.security.paths_targets import _candidate_forms

    return any(_is_project_mcp_name(c) for c in _candidate_forms(path_str, base_dir))


def project_mcp_targets(folder: str) -> tuple[str, ...]:
    """The project MCP files of *folder* the sandbox seals read-only, every spelling.

    Each name is listed under the folder as given and under its resolved path,
    whether or not the file exists: Seatbelt matches the resolved path, so a
    folder reached through a link (``/tmp`` on macOS, a cloud-drive folder) needs
    that spelling to deny an absent name, and the Linux launcher skips an absent
    one (it can only bind a file that exists, and Crew never creates a file in the
    person's folder). Each name's own resolved path is listed too, existing or not,
    so a name behind a linked folder or a link is sealed where it points. A name
    the seal cannot hold -- one that resolves to a folder or another non-file, loops,
    runs through a file, or cannot be read -- is skipped with a log line: no harness
    reads it as an MCP file, and sealing it would make a folder read-only or stop
    the chat starting, which a seal never does. Empty for no folder, a relative
    one, or a folder that is not a directory.
    """
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return ()
    out: list[str] = []
    for base in dict.fromkeys((os.path.normpath(folder), os.path.realpath(folder))):
        for name in PROJECT_MCP_FILES:
            lexical = os.path.normpath(os.path.join(base, name))
            if not _sealable(lexical):
                continue
            out.append(lexical)
            # Resolved whether or not the file exists: a linked ``.kiro`` or
            # ``settings`` folder (a dotfiles checkout) moves the absent name too.
            resolved = os.path.realpath(lexical)
            if resolved != lexical and _sealable(resolved):
                out.append(resolved)
    return tuple(dict.fromkeys(out))


def _sealable(path: str) -> bool:
    """Whether *path* is a regular file or a missing one: the two the seal can hold."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.info("project MCP seal: skipping %s (%s)", path, exc.strerror or exc)
        return False
    if stat.S_ISREG(info.st_mode):
        return True
    logger.info("project MCP seal: skipping %s (not a regular file)", path)
    return False
