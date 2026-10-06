"""Render the Linux namespace launcher a sandboxed agent spawn runs first.

The program itself is :mod:`kiro_crew.sandbox_launcher_program`, a stdlib-only module:
it forks, unshares the user and mount namespaces, stages the private windows, seals the
read-only dirs, bind-masks the sensitive dirs and files, applies the write carve-outs,
scrubs the environment and execs the agent command. :func:`render_namespace_launcher`
returns that module's source with exactly ONE substitution, the plan's data, so the file
the child runs is the module as it stands in the package. ``kiro_crew.sandbox`` plans the
spawn, writes the text to ``<config_dir>/run`` and invokes it (``namespace_argv``).

The program's source is read ONCE, when this module is imported, together with the plan
code it is rendered from. A package replaced on disk under a running gateway (an in-place
upgrade) therefore cannot pair the new program with the plan data the old code computes:
every launcher a process renders is the program it booted with.
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


def _read_program_source() -> str | None:
    """The program's source as the package holds it now, or ``None`` if it cannot be read."""
    try:
        return (
            resources.files("kiro_crew")
            .joinpath("sandbox_launcher_program.py")
            .read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


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
