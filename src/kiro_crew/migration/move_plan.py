"""Generic move-plan core.

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
import unicodedata
import uuid

from kiro_crew.migration import protocol as P
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def safe_requirement_identity(identity: str) -> str:
    """Make a requirement identity safe to print to a terminal.

    A requirement's identity is the thing it names on the host -- a cron job's
    ``command``, a task run's remote -- so it is operator-authored text echoed
    verbatim. Two hazards, and neither covers the other:

    * A credential or credential-bearing URL inline in that text would land in
      shell scrollback and history, which outlive the process. The shared
      credential + exfiltration-URL chain scrubs those.
    * A terminal control sequence (an OSC title-set, a CSI cursor move) is not a
      credential, so redaction leaves it untouched, yet printing it lets the
      value drive the terminal instead of being read by it. C0/C1 controls are
      REPLACED rather than removed, because removal would splice two separated
      separated fragments into one token.

    This lives beside ``render_plan`` rather than in each CLI verb because the
    renderer is the shared terminal boundary: a per-verb copy is what let the
    task-run verb keep printing raw identities after the cron verb was fixed.
    """
    safe, _ = redact_exfiltration_urls(identity)
    safe, _ = redact_credentials(safe)
    return "".join(
        ch if ch == "\t" or (ch.isprintable() and unicodedata.category(ch) != "Cf") else "\ufffd"
        for ch in safe
    )


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
            lines.append(f"    - {r.kind}: {safe_requirement_identity(r.identity)}")
    else:
        lines.append("  target requirements: none")
    for line in extra or []:
        lines.append(f"  {line}")
    lines.append(
        "\nThis is the migration PLAN only. The transmit/quiesce/tombstone steps "
        "run over the crew tunnel and are wired in a later change."
    )
    return "\n".join(lines)
