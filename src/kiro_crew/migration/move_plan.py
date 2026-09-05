"""Generic move-plan core (issue #7577).

Turns any unit into the ``MigrationBundle`` the coordinator's transmit step
sends. It touches ONLY the generic ``MigrationUnitAdapter`` seam, so one
implementation serves every unit kind and every surface: `kirocrew cron move`,
`kirocrew session move`, `kirocrew taskrun move`, and the dashboard actions all
call this.

It deliberately does NOT quiesce, transmit or tombstone — those are the
coordinator's ordered steps. This only BUILDS what would be sent, which is why
it is also the dry-run/preview surface.
"""

from __future__ import annotations

import time
import uuid

from kiro_crew.migration import protocol as P


async def plan_unit_move(
    adapter,
    unit_id: str,
    *,
    target: P.CrewRef,
    source: P.CrewRef | None = None,
    handoff_id: str | None = None,
    clock=time.time,
) -> P.MigrationBundle:
    """Build the ``MigrationBundle`` for moving ``unit_id`` to ``target``.

    ``adapter`` is any ``MigrationUnitAdapter``. Raises ``KeyError`` when the
    unit does not exist on the source (surfaced by the adapter). The payload is
    already allow-listed by the adapter's ``serialize``; ``requirements`` are the
    target-side checks preflight will run.
    """
    payload = await adapter.serialize(unit_id)  # KeyError if unknown
    requirements = list(await adapter.requirements(unit_id))
    return P.MigrationBundle(
        bundle_kind=adapter.bundle_kind,
        bundle_version=adapter.bundle_version,
        handoff_id=handoff_id or uuid.uuid4().hex,
        created_ts=clock(),
        source_crew=source or P.CrewRef(crew_id="local", label="local"),
        payload=payload,
        requirements=requirements,
    )


def render_plan(
    bundle: P.MigrationBundle, target_label: str, *, unit_id: str, extra: list[str] | None = None
) -> str:
    """Render a move plan for a terminal. Shared by every CLI move verb."""
    lines = [
        f"Migration plan for {bundle.bundle_kind} {unit_id} → crew {target_label!r}:",
        f"  handoff_id: {bundle.handoff_id}",
        f"  bundle:     {bundle.bundle_kind} v{bundle.bundle_version}",
        f"  ships:      {len(bundle.payload)} allow-listed fields",
    ]
    if bundle.requirements:
        lines.append("  target must satisfy (blocking):")
        for r in bundle.requirements:
            lines.append(f"    - {r.kind}: {r.identity}")
    else:
        lines.append("  target requirements: none")
    for line in extra or []:
        lines.append(f"  {line}")
    lines.append(
        "\nThis is the migration PLAN only. The transmit/quiesce/tombstone steps "
        "run over the crew tunnel and are wired in a later change."
    )
    return "\n".join(lines)
