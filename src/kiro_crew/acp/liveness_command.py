"""A tool call's command text, as the liveness oracle retains it and reads it back.

:func:`bounded_command_text` cuts every command-derived string a tool-call row keeps.
The readers parse the retained text: :func:`match_fragment` and :func:`first_program`
recognise the shell child a command started, :func:`parse_wait_seconds` reads the wait
tool's declared duration, and :func:`launch_matches` recognises an MCP server by the argv
the session launched it with. A new reader of the cached command goes in this module;
:mod:`kiro_crew.acp.liveness` re-exports every name here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

from kiro_crew.acp.liveness_evidence import _GENERIC_PROGRAMS, _MIN_MATCH_FRAGMENT, _WAIT_SECONDS_RE

# ── MCP server attribution ──

#: ``{server name: (launch argv, ...)}`` -- how a session started each stdio MCP
#: server, one argv per source that names it (the agent spec, the session's
#: ``mcpServers`` array). A name with two sources keeps both, because which one
#: the host launched under a shared name is the host's call. Built and bounded by
#: ``session_handle.mcp_launch_roster``; the oracle only reads it.
LaunchRoster = Mapping[str, tuple[tuple[str, ...], ...]]


def launch_matches(cmdline: str, launch: tuple[str, ...]) -> bool:
    """Whether a process whose argv reads *cmdline* is *launch* as started.

    *cmdline* is argv joined by single spaces (the shape :func:`read_cmdline`
    and :class:`ProcessRow` hold), so both sides are compared as space-split
    tokens. Three shapes count, and nothing looser:

    - the launch exactly;
    - the same program reached by another path (a PATH lookup, a relative
      command), with exactly the declared args after it;
    - an interpreter running the command as its script (a ``#!`` line), with
      exactly the declared args after the script.

    A launcher that execs into a different argv is NOT matched. Missing such a
    server costs only the narrowing: the call keeps its full window.
    """
    if not cmdline or not launch:
        return False
    command = launch[0]
    want = " ".join(launch).split(" ")
    got = cmdline.split(" ")
    if got == want:
        return True
    if " " in command:
        # A command path with a space spans several tokens; only the exact
        # shape above can be read back reliably.
        return False
    args = want[1:]
    program = os.path.basename(command)
    if os.path.basename(got[0]) == program and got[1:] == args:
        return True
    return len(got) >= 2 and os.path.basename(got[1]) == program and got[2:] == args


# ── Command matching ──


#: The marker :func:`bounded_command_text` joins a cut command's head and tail
#: with. Defined beside :func:`match_fragment` because that reader must split on
#: it: a fragment spanning the cut is not a substring of any real cmdline.
_COMMAND_ELISION = " ...[elided]... "


def match_fragment(command: str) -> str:
    """Extract the most distinctive fragment of a cached tool command.

    The cached input is the redacted rendering of the shell tool's input —
    usually the command text itself (possibly JSON-wrapped). kiro-cli runs
    shell tools via ``bash -c <command>``, so the child's cmdline contains the
    command text near-verbatim; a long contiguous fragment is a strong match
    key. The fragment is cut from the decoded command text
    (:func:`_command_text`): in the JSON rendering every newline is the two
    characters ``\\n``, so a fragment cut there straddles the escape and is
    not a substring of the real cmdline, and a multi-line command would match
    only on its first line. Redaction markers and shell metacharacters split
    the text into fragments; the longest one wins. Returns "" when nothing
    distinctive survives (caller degrades to the weaker program-name match).
    """
    text = _command_text(command)
    # Split on redaction markers, the retention cut's elision marker (a
    # fragment spanning the cut can never match a real cmdline) and
    # quoting/control chars that differ between the cached rendering and the
    # real argv.
    fragments = re.split(
        r"\*{3,}|\[REDACTED[^\]]*\]|" + re.escape(_COMMAND_ELISION) + r"|[\"'\\\n\r]", text
    )
    best = max((f.strip() for f in fragments), key=len, default="")
    return best if len(best) >= _MIN_MATCH_FRAGMENT else ""


def first_program(command: str) -> str:
    """First non-generic program token of a command ("" when too generic)."""
    for token in re.split(r"[\s;|&]+", (command or "").strip()):
        base = token.rsplit("/", 1)[-1]
        if not base or "=" in base:
            continue  # env assignments
        if base in _GENERIC_PROGRAMS or base == "-c":
            continue
        return base if len(base) >= 3 else ""
    return ""


def parse_wait_seconds(command: str) -> int | None:
    """Parse the declared ``seconds`` from a cached MCP wait-tool input."""
    m = _WAIT_SECONDS_RE.search(command or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _command_text(command: str) -> str:
    """The shell text of a cached tool input (JSON ``command`` field or raw)."""
    text = command or ""
    m = re.search(r"[\"']command[\"']\s*:\s*\"((?:[^\"\\]|\\.)*)\"", text)
    if m:
        try:
            return str(json.loads(f'"{m.group(1)}"'))
        except ValueError:
            return m.group(1)
    return text


#: Longest ``title`` / ``command`` a :class:`ToolCallState` retains. Both are
#: backend-authored strings bounded on the wire only by the transport's frame
#: cap, and the handle keeps up to ``MAX_ACTIVE_TOOL_CALLS`` states for a turn.
#: ``title`` is display text, so its head is kept. ``command`` has readers at
#: both ends: ``parse_wait_seconds``, ``first_program`` and ``match_fragment``
#: read its head, and the stall-recovery nudge's log-redirect hint
#: (``extract_log_redirect_target``) reads the ``> file`` a long shell command
#: typically ends with -- so a command over the bound keeps its first
#: ``MAX_RETAINED_COMMAND_CHARS - MAX_RETAINED_COMMAND_TAIL_CHARS`` characters
#: and its last ``MAX_RETAINED_COMMAND_TAIL_CHARS``, joined by a marker, on
#: every harness alike. The residual is a redirect buried in the middle of a
#: command longer than four kilobytes.
MAX_RETAINED_TITLE_CHARS = 512
MAX_RETAINED_COMMAND_CHARS = 4096
MAX_RETAINED_COMMAND_TAIL_CHARS = 512
#: Longest ``tool_name`` / ``mcp_server_name`` a state retains -- the bound
#: Crew's own MCP tool surface places on a name, so a name the surface would
#: list is kept whole and a longer one is a head.
MAX_RETAINED_NAME_CHARS = 512


def bounded_command_text(text: str) -> str:
    """*text* cut to ``MAX_RETAINED_COMMAND_CHARS``, keeping its head and tail.

    The one shape every command-derived string a tool-call row retains is cut
    to: the state's ``command`` and the interactive classifier's editor hint,
    which is built from the command's own arguments and travels beside the
    state as the row's aux payload. Under the bound the text is returned as it
    is.
    """
    if len(text) <= MAX_RETAINED_COMMAND_CHARS:
        return text
    head = MAX_RETAINED_COMMAND_CHARS - MAX_RETAINED_COMMAND_TAIL_CHARS - len(_COMMAND_ELISION)
    return text[:head] + _COMMAND_ELISION + text[-MAX_RETAINED_COMMAND_TAIL_CHARS:]
