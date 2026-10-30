"""Read whether a command runs ``curl`` with an upload flag, one command at a time.

The ``data-exfil-curl-upload`` row used to search the WHOLE command line for
``curl`` and then anywhere later for ``--upload-file`` or ``-T``, case-folded. So a
``curl`` in one command and a ``-t``/``-T`` that belongs to another program in the
next (``curl -s URL | sort -t, -k2``, ``curl -o x URL && tar -T list -cf a.tar``)
read as one upload. This module answers the question the row means instead: does
some command in the line run ``curl`` with ``--upload-file`` (or
``--upload-file=``) or a short option cluster holding ``T``, which curl reads as
``--upload-file``. ``-T`` is case-sensitive, as curl reads it: ``-t`` is curl's
``--telnet-option`` and uploads nothing.

Commands come from the shared quote-aware reader
(:func:`shell_commands.read_commands`), so a separator inside quotes does not cut a
command and a quoted program word (``"curl"``, ``'/usr/bin/curl'``) is read as the
program it runs. Payloads a nested shell would run (``bash -c '…'``, ``eval``) and
the lines of a here-document body are read as commands too.

It only ever ADDS reach to the row's regex: the deny gate and the exfil gate both
call it beside the row's own pattern, never instead of it. A command the reader
cannot read is judged on its text the way the row used to be, so a reader failure
refuses rather than allows.
"""

from __future__ import annotations

import re

from . import shell_commands as _commands
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

#: The catalog row this reads for.
CURL_UPLOAD_RULE_ID = "data-exfil-curl-upload"
_CURL_UPLOAD_PATTERN: str = next(
    rule.pattern for rule in BUILTIN_DENIED_RULES if rule.id == CURL_UPLOAD_RULE_ID
)
_CURL_UPLOAD_NOTE = (
    "Matched on the curl upload flag in the curl command itself (`--upload-file` or `-T`), "
    "not the pattern text."
)

#: The flag spellings :func:`curl_upload_flag` reports.
LONG_FLAG = "--upload-file"
SHORT_FLAG = "-T"

#: curl short options that take a value. In a cluster (``-sTfile``, ``-HX-T``) the
#: letters after one of these are its value, not more options.
_VALUE_LETTERS = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
#: ``curl`` as a word in a program token: ``curl``, ``curl.exe``, ``$CURL``,
#: ``${CURL_BIN}``; not ``libcurl``.
_CURL_WORD_RE = re.compile(r"(?:^|[^a-z0-9])curl(?:[^a-z0-9]|$)")
#: How deep nested payloads are read; each level is shorter than its parent.
_MAX_DEPTH = 8
#: The cheap pre-check: no ``curl`` in the text means no command can run it.
_CAN_FIRE_RE = re.compile(r"curl", re.IGNORECASE)
#: The row's old whole-text reading, used only when the reader fails.
_FALLBACK_RE = re.compile(r"curl.*(?:--upload-file|(?:^|\s)-T\s*\S)", re.DOTALL)


def _can_fire(command: str) -> bool:
    return bool(_CAN_FIRE_RE.search(command))


def _is_curl(token: str) -> bool:
    return bool(_CURL_WORD_RE.search(_shell._program_basename(token).lower()))


def _short_cluster_uploads(token: str) -> bool:
    """Whether the short option cluster *token* (``-sT``, ``-Tfile``) holds ``-T``."""
    for letter in token[1:]:
        if letter == "T":
            return True
        if letter in _VALUE_LETTERS:
            return False  # the rest of the cluster is this option's value
    return False


def _upload_flag_in(tokens: "list[str]") -> "str | None":
    """The upload flag a ``curl`` in *tokens* carries, or None."""
    start = next((i for i, token in enumerate(tokens) if _is_curl(token)), None)
    if start is None:
        return None
    for token in tokens[start + 1 :]:
        if token == LONG_FLAG or token.startswith(LONG_FLAG + "="):
            return LONG_FLAG
        if token.startswith("-") and not token.startswith("--") and _short_cluster_uploads(token):
            return SHORT_FLAG
    return None


def _sources(command: str) -> "list[str]":
    """*command*, its line-continuation fold, and each of its lines when it has several.

    A here-document body is data to the reader, which stops at ``<<``, but a shell
    fed it runs every line, so each line is read as a command of its own.
    """
    folded = _shell._fold_line_continuations(command)
    out = [command, folded]
    if "\n" in folded:
        out.extend(folded.split("\n"))
    return list(dict.fromkeys(s for s in out if s.strip()))


def _read(command: str, depth: int, seen: "set[str]") -> "str | None":
    for source in _sources(command):
        for cmd in _commands.read_commands(source):
            argv = cmd.argv
            if not argv:
                continue
            tokens = _shell._shell_tokens(" ".join(word.raw for word in argv))
            flag = _upload_flag_in(tokens)
            if flag:
                return flag
            if depth >= _MAX_DEPTH:
                continue
            for payload in _shell._nested_shell_payloads(tokens, allow_join=False):
                if payload in seen or len(payload) >= len(source):
                    continue
                seen.add(payload)
                flag = _read(payload, depth + 1, seen)
                if flag:
                    return flag
    return None


def curl_upload_flag(command: str) -> "str | None":
    """The upload flag (``--upload-file`` or ``-T``) a ``curl`` command in *command* uses.

    None when no command in it runs ``curl`` with one. Fails closed: a command the
    reader cannot read is judged by the row's old whole-text reading.
    """
    if not _can_fire(command):
        return None
    try:
        return _read(command, 0, {command})
    except Exception:  # noqa: BLE001 - incl. RecursionError; fail closed below
        match = _FALLBACK_RE.search(command)
        if match is None:
            return None
        return LONG_FLAG if LONG_FLAG in match.group(0) else SHORT_FLAG
