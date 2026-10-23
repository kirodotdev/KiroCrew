"""Render the Linux namespace launcher a sandboxed agent spawn runs first.

The program itself is :mod:`kiro_crew.sandbox_launcher_program`, a stdlib-only program:
it forks, unshares the user and mount namespaces, stages the private windows, seals the
read-only dirs, bind-masks the sensitive dirs and files, applies the write carve-outs,
scrubs the environment and execs the agent. :func:`render_namespace_launcher`
returns the program's source with exactly ONE substitution, the plan's data, so the file
the child runs is the program as the package holds it. ``kiro_crew.sandbox`` plans the
spawn, writes the text to ``<config_dir>/run`` and invokes it (``namespace_argv``).

That source spans ``sandbox_launcher_program.py`` and the modules of
:mod:`kiro_crew.sandbox_launcher_parts`. The facade carries a marker line where each part's
text goes and an end-of-program marker above its own module glue, and each part a marker
line above its text; the source is the facade with each part's text at its marker and
nothing past the end marker, the one program the child runs, byte for byte.

The program's source is read ONCE, when this module is imported, together with the plan
code it is rendered from. A package replaced on disk under a running gateway (an in-place
upgrade) therefore cannot pair the new program with the plan data the old code computes:
every launcher a process renders is the program it booted with. The facade and every part
are read in that one pass, so no new part can reach a render either, and a package whose
program cannot be read or assembled leaves none, so every Linux spawn refuses.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import TYPE_CHECKING

from kiro_crew.sandbox_plan import namespace_payload

if TYPE_CHECKING:
    from kiro_crew.sandbox_plan import ConfinementPlan

#: The one line of the program the renderer replaces, and what it becomes.
PLAN_PLACEHOLDER = "_PLAN = None  # the renderer substitutes the plan here\n"
_PLAN_LINE = "_PLAN = {}\n"

#: The program's parts, in the order their text runs in it, under ``sandbox_launcher_parts/``.
_PARTS = ("pinned_mounts.py", "mask_stages.py", "exec_lockdown.py")
#: The facade line a part's text replaces, spelled for each part in :data:`_PARTS`.
_PART_MARKER = "# -- the launcher program continues in sandbox_launcher_parts/{}.py --\n"
#: What every part marker starts with, so a marker naming no known part is caught too.
_PART_MARKER_PREFIX = "# -- the launcher program continues in "
#: The line in a part that its program text starts below.
_BODY_MARKER = "# -- the launcher program continues below this line --\n"
#: The facade line its program ends above; the module glue below it is never rendered.
_END_MARKER = "# -- the launcher program ends above this line --\n"


def _part_text(source: str) -> str | None:
    """The program text *source* carries below its one body marker, or ``None``."""
    lines = source.splitlines(keepends=True)
    if lines.count(_BODY_MARKER) != 1:
        return None
    return "".join(lines[lines.index(_BODY_MARKER) + 1 :])


def _assemble(facade: str, parts: dict[str, str]) -> str | None:
    """The program: *facade* above its end marker, each part's text at its marker.

    ``None`` unless the facade has exactly one end marker and, above it, exactly one marker
    for each of :data:`_PARTS`, in that order, and no other line that reads as a marker, and
    unless the program that results carries no marker text at all.
    """
    lines = facade.splitlines(keepends=True)
    if lines.count(_END_MARKER) != 1:
        return None
    program: list[str] = []
    placed: list[str] = []
    for line in lines[: lines.index(_END_MARKER)]:
        if not line.startswith(_PART_MARKER_PREFIX):
            program.append(line)
            continue
        part = next((p for p in _PARTS if line == _PART_MARKER.format(p[: -len(".py")])), None)
        if part is None:
            return None
        text = _part_text(parts[part])
        if text is None:
            return None
        placed.append(part)
        program.append(text)
    source = "".join(program)
    if tuple(placed) != _PARTS or any(
        marker in source for marker in (_PART_MARKER_PREFIX, _BODY_MARKER, _END_MARKER)
    ):
        return None
    return source


def _read_program_source() -> str | None:
    """The program as the package holds it now, or ``None`` if it cannot be assembled.

    Every file is read in this one pass, when this module is imported, and never again:
    no render reads a file the package holds by then.
    """
    try:
        package = resources.files("kiro_crew")
        facade = package.joinpath("sandbox_launcher_program.py").read_text(encoding="utf-8")
        parts_dir = package.joinpath("sandbox_launcher_parts")
        parts = {part: parts_dir.joinpath(part).read_text(encoding="utf-8") for part in _PARTS}
    except (OSError, ValueError):
        return None
    return _assemble(facade, parts)


#: The launcher program's source, read when this module is imported (see above).
_PROGRAM_SOURCE = _read_program_source()


def launcher_program_source() -> str:
    """The launcher program's source text, as it was when this module was imported.

    Checked here rather than at import, so a package without a readable program, or a
    program that lost its placeholder, refuses every Linux spawn instead of making
    ``import kiro_crew.sandbox`` raise on every host.
    """
    if _PROGRAM_SOURCE is None:
        raise RuntimeError("the launcher program could not be read when its renderer loaded")
    if _PROGRAM_SOURCE.count(PLAN_PLACEHOLDER) != 1:
        raise RuntimeError("the launcher program must carry exactly one plan placeholder")
    return _PROGRAM_SOURCE


def render_namespace_launcher(plan: ConfinementPlan) -> str:
    """The launcher program for *plan*: its source with the plan's data substituted.

    The data is embedded as a Python literal, which is why
    :func:`~kiro_crew.sandbox_plan.namespace_payload` carries no booleans or nulls.
    """
    payload = json.dumps(namespace_payload(plan))
    return launcher_program_source().replace(PLAN_PLACEHOLDER, _PLAN_LINE.format(payload), 1)
