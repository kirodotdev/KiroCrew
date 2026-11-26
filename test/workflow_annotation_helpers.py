"""Shared readers for the runtime annotation helpers a workflow step opens with.

A `run:` block that reports through a workflow command does not write the
`::error::`, `::warning::` or `::notice::` prefix as a literal. GitHub Actions
echoes a step's whole source into the log before it runs, so a literal inside
an arm that never fired would still print as a real annotation on a green run.
Each block instead defines one-line helpers at its top and calls them:

    err() { printf '::%s::%s\\n' error "$*"; }
    warn() { printf '::%s::%s\\n' warning "$*"; }
    notice() { printf '::%s::%s\\n' notice "$*"; }

Two kinds of test need to know that shape. A test that pins the SOURCE of a
step asserts on the helper call (``'err "the message'``), because the source
carries no literal prefix. A test that EXECUTES a slice of a step has to carry
the block's definitions along with the slice, or the slice dies with
``err: command not found`` (exit 127) instead of failing the way the step does.
Several test modules do one or both, so the readers live here, imported by
bare name like the other ``*_helpers`` modules.
"""

from __future__ import annotations

import re

ANNOTATION_HELPER_LINE = re.compile(
    r"^\s*(?P<name>err|warn|notice)\(\) \{ printf '::%s::%s\\n' "
    r"(?P<kind>error|warning|notice) \"\$\*\"; \}\s*$"
)

# `err "…"` as a command, not the tail of a longer word (`stderr "`) or a
# PowerShell variable (`$err`).
_CALL = {
    kind: re.compile(r"(?<![\w$-])" + name + r' "')
    for kind, name in (("error", "err"), ("warning", "warn"), ("notice", "notice"))
}


def annotation_helpers(script: str) -> str:
    """The helper definitions `script` opens with, one per line, newline-terminated.

    Empty when the block defines none, so prepending the result to a slice of a
    block that never annotates is a no-op.
    """
    lines = [line.strip() for line in script.splitlines() if ANNOTATION_HELPER_LINE.match(line)]
    return "".join(line + "\n" for line in lines)


def with_annotation_helpers(script: str, fragment: str) -> str:
    """`fragment`, a slice of `script`, made runnable by prepending the block's helpers."""
    return annotation_helpers(script) + fragment


def _is_call(line: str, kind: str) -> bool:
    # Neither a comment nor the definition itself (`… notice "$*"; }`) is a call.
    return (
        not line.lstrip().startswith("#")
        and not ANNOTATION_HELPER_LINE.match(line)
        and _CALL[kind].search(line) is not None
    )


def annotation_calls(script: str, kind: str) -> list[str]:
    """The lines of `script` that emit a `kind` annotation through its helper.

    `kind` is the workflow-command name: ``"error"``, ``"warning"`` or ``"notice"``.
    """
    return [line for line in script.splitlines() if _is_call(line, kind)]


def first_annotation_call(script: str, kind: str) -> int:
    """Offset of the first line of `script` that emits a `kind` annotation.

    Raises ``ValueError`` like ``str.index`` when there is none, so a positional
    assertion (`this arm annotates before that one`) reads the same as before.
    """
    offset = 0
    for line in script.splitlines(keepends=True):
        if _is_call(line, kind):
            return offset
        offset += len(line)
    raise ValueError(f"no {kind} annotation call in script")
