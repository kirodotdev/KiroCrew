"""Which argument of a harness's read tool names the file the call reads.

A tool row previews the images a read looked at (the dashboard's thumbnail strip).
kiro-cli's read names them in ``operations[].image_paths``, a shape the strip reads
on any row. Claude Code's Read names its one file in ``file_path``, the same argument
its Write and Edit carry, so the shape alone does not say the file was READ: the
row needs the backend to say so. :func:`read_path_arg` is that statement, made on
``AcpClient`` from the harness id and the call's ACP kind, carried on
``AcpEvent.read_path_arg`` and kept on the row as ``meta.read_path_arg`` until the
call ends without completing, when the dashboard clears it: a failed read read no file.

The answer is positive only: a member of ``ACP_BACKENDS_READ_PATH_ARG`` and a call
whose kind is ``read``. Any other harness, any other kind, and a value of the wrong
type answer ``""``, which the row reads as "no file named", so a stranger never has
an argument of its own taken for the file it read.
"""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_READ_PATH_ARG

#: The argument Claude Code's Read names its one file in (claude-agent-acp 0.107.0
#: ``tool-calls/reporters/read.js``).
READ_PATH_ARG = "file_path"

#: The ACP tool kind of a call that reads a file.
_READ_KIND = "read"


def read_path_arg(backend: object, kind: object) -> str:
    """The argument naming a read call's file, or ``""`` when no file is named.

    *backend* is the session's ``acp_backend`` id and *kind* the ACP ``kind`` of the
    ``tool_call`` frame.
    """
    if not isinstance(backend, str) or backend not in ACP_BACKENDS_READ_PATH_ARG:
        return ""
    return READ_PATH_ARG if kind == _READ_KIND else ""
