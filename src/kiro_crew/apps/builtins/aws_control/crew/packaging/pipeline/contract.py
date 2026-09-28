"""The bundle's frozen contract: version numbers, file names, the read ceiling, the failure type.

The lowest layer of the pipeline. A published bundle, a curation plan and a report each carry
one of these versions or names, so a change here is a format change rather than a refactor.
"""

from __future__ import annotations

# The frozen layout the image copies in and the container reader validates.
BUNDLE_VERSION = 1
PLAN_VERSION = 1

#: Identifies a report THIS tool wrote. Its only job is origin: the report path is derived
#: from --out, in a directory the build does not own, so replacing an existing file there
#: needs proof rather than a matching name. Same role ``PLAN_VERSION`` plays for the plan.
REPORT_VERSION = 1
PLAN_FILENAME = "curation-plan.json"

#: Every top-level name ``build_bundle`` writes inside its staging directory. A
#: staging path holding anything else is refused rather than deleted -- see the
#: check in ``build_bundle``. Kept beside ``PLAN_FILENAME`` because the plan is one
#: of them (it is carried across the swap).
_STAGING_OWNED_TOP_LEVEL: frozenset[str] = frozenset(
    {"agent.json", "mcp.json", "manifest.json", "skills", PLAN_FILENAME}
)

#: The only directory this build creates and may legitimately leave EMPTY.
#:
#: The empty-directory check exempted all of ``_STAGING_OWNED_TOP_LEVEL``, and four of those
#: five entries are FILE names -- so an operator's own empty directory called ``agent.json`` or
#: ``manifest.json`` was exempted and then removed by the recursive delete. The two sets overlap
#: because both describe what this build writes at the top level; what differs is that only one
#: of them can have nothing inside it.
_BUILD_WRITES_EMPTY: frozenset[str] = frozenset({"skills"})


_MAX_PROMPT_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Failure mode: refusal only. Ported from ``crew_export/errors.py``.
# ---------------------------------------------------------------------------
class ExportRefused(RuntimeError):
    """The export cannot proceed and no bundle was written.

    A warning the operator can scroll past is not a control, so every guard
    aborts rather than degrading -- the alternative is shipping a bundle wrong in
    the one direction that matters.
    """
