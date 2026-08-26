"""Detect stored config values that still hold a superseded dataclass default.

Why this module exists
----------------------
``config.json`` is written as a FULL materialization of the schema: every field
lands on disk, including ones the operator never set. The loader then resolves
each field as ``data.get(key, DEFAULT)``, so a stored value always beats the
dataclass default. The consequence (issue #5244) is that changing a shipped
default only reaches installs created after the change -- every pre-existing
install keeps whatever value was materialized the last time it wrote config, and
nothing tells anyone.

Why this REPORTS and never rewrites
-----------------------------------
An earlier revision corrected the stored value. That cannot be done safely for a
key that also has a documented escape hatch, and at least one does:
``test_a_real_false_still_turns_it_off`` in the gateway env suite pins that an
explicitly stored ``forward_declared_env: false`` is honoured, and calls it "the
escape hatch for a server that must not share a backend". On disk that escape
hatch and a stale materialized default are the SAME BYTES, so no rewrite can tell
them apart -- correcting one necessarily overrides the other.

Distinguishing them needs per-key provenance (which keys the operator actually
set), which is a materially larger change to the config layer and its own piece
of work. Until that exists, the honest scope is to make the drift VISIBLE and
leave the change to the operator: a warning on the gateway's own log and a
``doctor`` section naming the key, the stored value, the current default, and the
release that changed it.

Scope discipline
-----------------
Nothing here writes to configuration: detection is pure, and the doctor renderer
below only reads. Reading `config.json` lives HERE rather than in the CLI so the
config package stays the one place that knows what the stored document means; the
caller's job is to decide when to show it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SupersededDefault:
    """One shipped default change that already-materialized installs never saw.

    ``dotted_key`` addresses a scalar field as ``<section>.<field>`` (the only
    shape the current entries need). ``old_default`` and ``new_default`` are the
    literal values before and after the change; an install is reported as drifted
    only when its stored value equals ``old_default``. ``changed_in`` names the
    PR the change shipped in, so the report can say when the divergence started.
    """

    dotted_key: str
    old_default: object
    new_default: object
    changed_in: str


# The explicit, versioned registry of superseded defaults. APPEND-ONLY: a future
# default change that existing installs should be told about adds an entry here.
#
# A forgotten entry means that one field's drift goes unreported, which is the
# known cost of an explicit list. It is accepted because the alternative -- deriving
# "was this value chosen or merely materialized" automatically -- is exactly the
# provenance the config layer does not have.
SUPERSEDED_DEFAULTS: tuple[SupersededDefault, ...] = (
    # #4566 changed mcp_gateway.forward_declared_env from False to True because
    # the False default was costing env-declaring servers their pooling. It
    # shipped with no migration, so an install materialized while the default was
    # still False keeps resolving False and never received the fix (issue #5244).
    SupersededDefault(
        dotted_key="mcp_gateway.forward_declared_env",
        old_default=False,
        new_default=True,
        changed_in="#4566",
    ),
    # #4388 changed session.autocompact_pct from 90.0 to 70.0 because 90.0 was
    # also the maximum its own validator accepted, so the shipped default was
    # the most expensive value an operator could hold: credits scale with
    # context and steepen near the ceiling, and compacting AT the ceiling pays
    # that rate repeatedly before acting. It shipped deliberately without a
    # migration -- on disk, "chose 90" and "90 was the default when this file
    # was written" are the same bytes -- so an install materialized before it
    # still compacts at 90 and nothing told anyone (issue #4389).
    SupersededDefault(
        dotted_key="session.autocompact_pct",
        old_default=90.0,
        new_default=70.0,
        changed_in="#4388",
    ),
)


def _split_dotted(dotted_key: str) -> tuple[str, str]:
    """Split ``"<section>.<field>"`` into its two parts.

    Only the two-level shape is supported, which is all the current entries need;
    a malformed key raises so a bad registry entry fails loudly in tests rather
    than silently reporting nothing in production.
    """
    section, _, field = dotted_key.partition(".")
    if not section or not field or "." in field:
        raise ValueError("superseded-default key must be '<section>.<field>': " + repr(dotted_key))
    return section, field


def superseded_default_drift(base_data: dict) -> list[SupersededDefault]:
    """Return the registered entries whose STORED value is the superseded default.

    *base_data* must be the stored base document (``config.json`` alone), never the
    view produced by merging ``config.local.json`` over it. The overlay is a
    separate user-owned file applied at read time; a value it supplies is the
    operator's live choice and says nothing about what the base has materialized,
    so reporting on the merged view would both miss real drift in the base and
    describe a value the base does not hold.

    An entry is reported only when the stored value equals ``old_default`` with the
    same type -- ``bool`` is an ``int`` subclass, so requiring the type as well
    keeps a stored ``0`` from being read as ``False``. An absent section, an absent
    key, or any other value is not drift: those already resolve to the current
    dataclass default at parse time, which is the desired outcome.

    Pure: reads *base_data* and returns a list. Nothing is mutated or written.
    """
    drifted: list[SupersededDefault] = []
    for entry in SUPERSEDED_DEFAULTS:
        section, field = _split_dotted(entry.dotted_key)
        section_data = base_data.get(section)
        if not isinstance(section_data, dict) or field not in section_data:
            continue
        stored = section_data[field]
        if type(stored) is type(entry.old_default) and stored == entry.old_default:
            drifted.append(entry)
    return drifted


def drift_summary(entry: SupersededDefault) -> str:
    """One line describing *entry*'s drift, shared by the log and ``doctor``.

    Kept in one place so the two surfaces cannot drift into describing the same
    condition differently, and worded as a statement of fact plus the operator's
    options -- this mechanism does not know whether the stored value was chosen
    deliberately, and must not imply the value is wrong.
    """
    return (
        f"{entry.dotted_key} is stored as {entry.old_default!r}, which was the default "
        f"before {entry.changed_in} changed it to {entry.new_default!r}. An install that "
        f"predates that change keeps the old value because a stored value beats the "
        f"default. If {entry.old_default!r} was not a deliberate choice, removing the key "
        f"or setting it to {entry.new_default!r} adopts the current default."
    )


def render_doctor_section(issues: list[str]) -> None:
    """Print the ``Stored Defaults`` section of ``kirocrew doctor``.

    Reads ``config.json`` DIRECTLY rather than the resolved config, and does not
    merge ``config.local.json``: the question is what the base file has
    materialized, and the resolved view cannot answer it -- a stored value and the
    same value arriving from the current default are indistinguishable once parsed.

    Drift is informational and does NOT become an issue. This cannot tell a stale
    materialized default from a deliberate opt-out (on disk they are identical), so
    presenting it as something to fix would be telling operators to undo their own
    choices. It prints what is stored, what the current default is, and which
    release changed it, and leaves the decision with them. An unreadable or
    non-object config IS an issue: that is unambiguously wrong.

    ``config_path`` is imported lazily because ``config.loader`` imports this
    module for the load-path warning, so a module-level import would be a cycle.
    """
    from kiro_crew.config.loader import config_path

    print("\nStored Defaults")
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("  drift:       ✅ no config file yet (current defaults apply)")
        return
    except (OSError, json.JSONDecodeError) as e:
        print(f"  drift:       ⚠️  could not read {path}: {e}")
        issues.append("stored defaults unreadable")
        return
    if not isinstance(raw, dict):
        print(f"  drift:       ⚠️  {path} is not a JSON object")
        issues.append("stored defaults unreadable")
        return

    drifted = superseded_default_drift(raw)
    if not drifted:
        print("  drift:       ✅ no stored value holds a superseded default")
        return
    for entry in drifted:
        print(f"  drift:       ℹ️  {drift_summary(entry)}")
