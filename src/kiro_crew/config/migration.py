"""The write-back migrations a config load can apply to ``config.json``.

Owns what a migration changes: the ids a load records as pending, the one-shot
``connections_ui`` marker name, the legacy ``skills.lazy_load`` cohort test, the
transform that re-applies each pending
migration to the document read inside the write lock, the once-per-process
report of stored superseded defaults, and the in-memory half of an adoption.
When and how the result is written -- the backup, the locked atomic write and
their failure handling -- stays with the loader, which passes the adoption
ledger writer this transform calls. A name this module reads is patched here,
not on the loader. This module imports neither the loader nor schema/validation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import MISSING, asdict
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.sections import DEFAULT_KIRO_TEMPLATE, KiroCrewAgentConfig, WorkspaceConfig
from kiro_crew.config.superseded_defaults import (
    LEGACY_LAZY_LOAD_ADOPTION,
    adopted_superseded_if_readable,
    auto_adoptable,
    drift_summary,
    drop_drifted_keys,
    stored_value_or_none,
    superseded_default_drift,
)

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger("kiro_crew.config.loader")


#: The write-back migrations a load can find pending, as recorded by
#: :func:`~kiro_crew.config.loader.persist_write_back` and re-checked against the
#: on-disk document by :func:`apply_document_migrations`.
MIGRATE_WORKSPACES = "workspaces"
MIGRATE_AGENTS = "agents"
MIGRATE_DEFAULT_AGENT = "default_agent"
MIGRATE_CONNECTIONS_UI = "connections_ui"
#: Un-materialize the stored values that still hold a superseded default an entry
#: marked ``auto_adopt``. Unlike the three above, this one carries a payload: the
#: keys the load decided on travel separately in ``adopt_keys``, because the set is
#: per-install rather than a fixed schema shape.
MIGRATE_SUPERSEDED_DEFAULTS = "superseded_defaults"

#: Sidecar marker recording that the one-shot ``connections_ui`` launch
#: migration ran. Pre-launch builds materialized ``connections_ui: false`` into
#: every config they saved (the key was the opt-in gate then, so a stored false
#: was default noise, never a choice); post-launch the same bytes are the
#: deliberate opt-out. The marker is the boundary between those two readings:
#: a stored false found BEFORE it exists is stripped once, and any false found
#: AFTER it exists is honoured forever. It lives beside ``config.json`` rather
#: than inside it for the same reason as the superseded-defaults ack file — a
#: full ``to_dict()`` rewrite carries only schema fields and would drop it.
CONNECTIONS_UI_MIGRATION_MARKER = "connections_ui_migrated.json"

#: Remove a ``skills.lazy_load: false`` that a 0.6.x-or-earlier build materialized.
#: Those builds wrote every default into ``config.json``, and their default was
#: ``false`` -- the full skills listing. Since 0.7.0 ``false`` selects the short skill
#: entry and the default is ``true``, so on an upgraded install the bytes nobody chose
#: select the narrowest mode. Nobody on 0.6.x could have chosen the short entry,
#: because it did not exist yet; that is what makes a rewrite sound here where the
#: generic one is not.
MIGRATE_SKILLS_LAZY_LOAD = "skills_lazy_load"
LAZY_LOAD_KEY = LEGACY_LAZY_LOAD_ADOPTION[0]

#: Carry a "Default for new sessions" pick stored in the alias shape into
#: ``agent.default_agent``. A document from a build whose picker star enrolled
#: the chosen template as a crewmate alias (shared folder and store, no model)
#: and pointed the top-level ``default_agent`` at it holds the user's choice
#: there and nowhere else; the resolver reads ``agent.default_agent`` alone, so
#: without this seed the picker row reads "kirocrew is the default" on the first
#: load of such a document and every agent-less session switches template.
#: One-shot by state: the field is UNSET in that shape and in no other -- the star
#: writes it directly, the stock template included, so any stored value, even
#: ``kirocrew``, is a choice already made and the predicate is false.
MIGRATE_STAR_DEFAULT_TEMPLATE = "star_default_template"

#: The newest release line whose writes are rewritten. ``CONNECTIONS_UI_MIGRATION_MARKER``
#: first shipped in 0.7.0 (0.7.0-insider.1) and every clean load of a later build
#: writes it, so "stamped by this line or older AND no marker" proves that no 0.7+
#: build has ever loaded the document -- and therefore that nobody chose ``false``
#: under its current meaning.
_LEGACY_LAZY_LOAD_LINE = (0, 6)

# ``major.minor.patch`` plus at most a short suffix (``-insider.3``, ``.10``, ``rc1``).
# Anything else is a stamp this cannot read, which is never proof.
_VERSION_STAMP = re.compile(
    r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:[A-Za-z.+\-][0-9A-Za-z.+\-]{0,48})?"
)


def _legacy_writer_stamp(data: dict) -> str | None:
    """The ``meta.lastTouchedVersion`` of *data* when it names 0.6.x or older.

    ``None`` for every other case -- a newer build, and also an absent, non-object or
    unparsable ``meta``: an unknown writer is not a provable one. The stamp is
    returned so the notice can name the build.
    """
    meta = data.get("meta")
    stamp = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
    if not isinstance(stamp, str):
        return None
    match = _VERSION_STAMP.fullmatch(stamp)
    if match is None or (int(match[1]), int(match[2])) > _LEGACY_LAZY_LOAD_LINE:
        return None
    return stamp


def _stores_lazy_load_false(data: dict) -> bool:
    skills = data.get("skills")
    return isinstance(skills, dict) and skills.get("lazy_load") is False


def legacy_lazy_load_rewrite_due(base_data: dict, *, connections_marker: Path) -> str | None:
    """Whether this load should remove a legacy ``skills.lazy_load: false``.

    *base_data* is ``config.json`` alone, before the overlay merge: the overlay is
    the operator's live choice and the stale materialization only ever landed in the
    base. Returns the writer's version stamp when every condition holds, else
    ``None``:

    * the base stores exactly ``false`` (an explicit ``true`` is never touched);
    * ``meta.lastTouchedVersion`` names 0.6.x or older -- read BEFORE a stamping write
      by this build replaces it, which is why the decision is taken on the load and
      why ``refresh_config_meta_stamp`` holds its refresh while this is due;
    * *connections_marker* does not exist, so no 0.7+ build has loaded this home;
    * the adoption ledger is readable and does not already name the key, which keeps
      it one-shot even if the marker is later deleted. An unreadable ledger is
      unknown, never "not yet", so it declines.

    Every declining branch leaves the value exactly as stored.
    """
    if not _stores_lazy_load_false(base_data):
        return None
    stamp = _legacy_writer_stamp(base_data)
    if stamp is None or connections_marker.exists():
        return None
    adopted = adopted_superseded_if_readable()
    if adopted is None or LAZY_LOAD_KEY in adopted:
        return None
    return stamp


def star_default_template_due(
    stored_template: object, default_alias: object, rows: object
) -> str | None:
    """The template a pre-rule star pick chose, when a document still carries it
    only as the roster's default alias; ``None`` when there is nothing to carry.

    All five must hold, on the document as given: ``agent.default_agent`` is unset
    (any stored value, the stock template included, is a choice already made in
    the field: resetting the star to ``kirocrew`` stores that name, and a later
    load must not put the alias's template back); the top-level ``default_agent`` names an alias other than the
    reserved ``default``; that alias is a template-only binding -- the shared
    ``default`` folder and store and no model pin; the alias is spelled as the
    template it binds (the star enrolled the template under its own name, so a
    crew named otherwise is one the user built and then promoted, and keeps its
    own three); and that template is not the stock one, which would be a no-op.
    Pure over plain values, so the in-memory half (the merged config) and the
    on-disk half (the base document inside the lock) apply one rule.
    """
    if isinstance(stored_template, str) and stored_template:
        return None
    if not isinstance(default_alias, str) or not default_alias or default_alias == "default":
        return None
    row = rows.get(default_alias) if isinstance(rows, dict) else None
    if not isinstance(row, dict):
        return None
    kiro = row.get("kiro_agent")
    if not isinstance(kiro, str) or not kiro or kiro == DEFAULT_KIRO_TEMPLATE:
        return None
    if kiro != default_alias:
        return None
    if (row.get("workspace") or "default") != "default":
        return None
    if (row.get("memory_store") or "default") != "default":
        return None
    if row.get("model"):
        return None
    return kiro


def apply_document_migrations(
    data: dict,
    pending: frozenset[str],
    *,
    overlay_kiro_agent: str | None,
    default_kiro_agent: str,
    adopt_keys: frozenset[str] = frozenset(),
    recorded_adoptions: list[str] | None = None,
    record_adoptions: Callable[[dict[str, object]], object],
    dispatch: Callable[[str], str] = lambda name: name,
) -> bool:
    """Apply the pending write-back migrations to a raw config document in place.

    *data* is ``config.json`` as read **inside the write lock**, not the merged
    snapshot the calling load parsed. That is the whole point: the migration is
    expressed as a delta against the document that is actually on disk right now,
    so a config write that landed after this load's read survives instead of being
    replaced by a re-serialization of the older snapshot.

    *pending* names the migrations the load decided on, so this never widens what
    the migration writes. The load's decisions are taken against the MERGED
    base+overlay view; the overlay is user-owned and never written back, so a
    migration the merged view did not ask for must not be invented here.

    The seeded agent's kiro agent resolves the same three-way precedence the
    loader itself applies, because it has to be the MERGED effective value (that
    is what the default crew dispatched before the migration) computed against a
    CURRENT base: *overlay_kiro_agent* first, since ``config.local.json`` wins the
    deep-merge and the base can say nothing about it; then the base document's own
    ``agent.default_agent`` as read here, so a ``config set`` that landed after
    the load's read is honored rather than reverted; then *default_kiro_agent*,
    the value the load resolved, which is the only one carrying the dataclass
    default. Taking any single one of the three is wrong in a different direction.

    Every entry is re-checked against *data* before it is applied, which makes the
    function idempotent and makes a concurrent writer that already migrated a
    no-op rather than a second rewrite. Returns True when anything changed; the
    caller skips the write entirely when it returns False.

    *record_adoptions* writes the adoption ledger; the loader passes the one it
    exposes, so the ledger write is the same seam its write-back path reaches.
    """
    changed = False

    # Flat workspace strings -> {"dir": ...}. Per entry, and only for entries the
    # document still holds as a string: a workspace added or rewritten by another
    # writer since this load's read is left exactly as that writer left it.
    if MIGRATE_WORKSPACES in pending:
        raw_workspaces = data.get("workspaces")
        if isinstance(raw_workspaces, dict):
            for name, value in list(raw_workspaces.items()):
                if isinstance(value, str):
                    raw_workspaces[name] = asdict(WorkspaceConfig(dir=value))
                    changed = True

    # Seed the default agent when the document still has none.
    if MIGRATE_AGENTS in pending:
        stored_agents = data.get("agents")
        if not isinstance(stored_agents, dict) or not stored_agents:
            stored_agent_section = data.get("agent")
            stored_kiro = (
                stored_agent_section.get("default_agent")
                if isinstance(stored_agent_section, dict)
                else None
            )
            base_kiro = stored_kiro if isinstance(stored_kiro, str) and stored_kiro else None
            data["agents"] = {
                "default": asdict(
                    KiroCrewAgentConfig(
                        kiro_agent=overlay_kiro_agent or base_kiro or default_kiro_agent,
                        workspace="default",
                        memory_store="default",
                    )
                )
            }
            changed = True

    # Strip the pre-launch materialized ``connections_ui: false``. Re-checked
    # against *data*: only an exact stored ``false`` is touched, so a concurrent
    # writer that already removed the key, or set it ``true``, is left alone.
    # The one-shot boundary (a deliberate post-launch ``false`` must survive
    # every later load) is enforced by the caller via the marker file — this
    # delta is only ever pending on a load that found no marker.
    if MIGRATE_CONNECTIONS_UI in pending:
        if data.get("connections_ui") is False:
            del data["connections_ui"]
            changed = True

    # Point default_agent at an agent that exists. Resolved against the
    # document's OWN agents (after any seeding above), so a concurrent writer's
    # newly added agent is a valid target rather than something we overwrite.
    if MIGRATE_DEFAULT_AGENT in pending:
        stored_agents = data.get("agents")
        known = stored_agents if isinstance(stored_agents, dict) else {}
        stored_default = data.get("default_agent")
        if not isinstance(stored_default, str) or not stored_default or stored_default not in known:
            if "default" in known:
                data["default_agent"] = "default"
            elif known:
                data["default_agent"] = next(iter(known))
            else:
                data["default_agent"] = "default"
            changed = data["default_agent"] != stored_default or changed

    # Carry a pre-rule star pick into ``agent.default_agent``. Re-detected on
    # *data*: a document another writer already seeded, or whose star row was
    # edited since this load's read, answers None here and is left alone. The
    # stored value is the row's ``kiro_agent`` resolved to the name kiro-cli
    # declares (*dispatch*), the same mapping the crewmate path applies at
    # dispatch time, since the template path reads this field verbatim.
    if MIGRATE_STAR_DEFAULT_TEMPLATE in pending:
        stored_section = data.get("agent")
        stored_template = (
            stored_section.get("default_agent") if isinstance(stored_section, dict) else None
        )
        carried = star_default_template_due(
            stored_template, data.get("default_agent"), data.get("agents")
        )
        if carried is not None:
            section = data.get("agent")
            if not isinstance(section, dict):
                section = {}
                data["agent"] = section
            section["default_agent"] = dispatch(carried)
            changed = True

    # Un-materialize the auto-adopting superseded defaults this load found. Four
    # properties are load-bearing and all four live here rather than at the call
    # site, because this is the only code that runs inside the config write lock:
    #
    # * RE-DETECTED against *data*, so a value another writer changed since this
    #   load's read is left alone -- the same rule every migration above follows,
    #   and the reason a live ``config set`` is never clobbered;
    # * the ledger is written BEFORE the removal is reported as done. A removal
    #   whose record did not land would repeat on the next load, and repeating is
    #   the one failure that can override a value the operator restored. A failing
    #   record therefore propagates and aborts the whole migration write;
    # * every key it records is reported back through *recorded_adoptions*, because
    #   writing the ledger first leaves a known residual: if the config write then
    #   fails, the key is marked adopted while the stale value is still stored, and
    #   the one-shot filter never revisits it. Nothing rolls the ledger back -- see
    #   ``record_adoptions`` for why that residual is the one chosen -- so the caller
    #   uses the list only to decide which keys the IN-MEMORY half may apply: those
    #   whose removal it saw land, and no others;
    # * ``drop_drifted_keys`` REMOVES the key rather than writing the new number, so
    #   the field resolves through ``data.get(key, DEFAULT)`` until the next full
    #   rewrite of the document re-materializes it.
    #
    # Lock order is config-then-ack, matching ``record_acks`` -- the only two sites
    # that nest these locks, and they nest them the same way.
    #
    # The legacy ``skills.lazy_load: false`` removal rides the same four rules. Its
    # re-detection is "still an exact stored false, still stamped by 0.6.x or older":
    # ``kirocrew config set`` and every settings save re-stamp the document, so a
    # value set since this load's read is never undone. Both go to the ledger in ONE
    # record, so a failed record cannot leave one half marked adopted with nothing
    # removed.
    to_record: dict[str, object] = {}
    if MIGRATE_SUPERSEDED_DEFAULTS in pending and adopt_keys:
        for entry in auto_adoptable(data):
            if entry.dotted_key in adopt_keys:
                to_record[entry.dotted_key] = stored_value_or_none(data, entry.dotted_key)
    if (
        MIGRATE_SKILLS_LAZY_LOAD in pending
        and _stores_lazy_load_false(data)
        and _legacy_writer_stamp(data) is not None
    ):
        to_record[LAZY_LOAD_KEY] = False
    if to_record:
        record_adoptions(to_record)
        if recorded_adoptions is not None:
            recorded_adoptions.extend(to_record)
        if drop_drifted_keys(data, list(to_record)):
            changed = True

    return changed


def _overlay_supplies(local_data: dict, dotted_key: str) -> bool:
    """True when ``config.local.json`` itself carries *dotted_key*.

    The overlay wins the deep-merge, so where it names a field the base cannot be
    the effective value. Adopting such a key in memory would replace the operator's
    live overlay choice with a default -- the one thing an automatic adoption must
    never do.
    """
    section, _, field = dotted_key.partition(".")
    section_data = local_data.get(section)
    return isinstance(section_data, dict) and field in section_data


def _adopt_in_memory(cfg: KiroCrewConfig, dotted_key: str, old_default: object) -> None:
    """Move one parsed field from *old_default* to the LIVE dataclass default.

    The dataclass default is read rather than the registry's ``new_default`` on
    purpose. Both sides of a registry row are history, so on a key whose default
    moved twice the matching row's ``new_default`` is an intermediate value, while
    the field's own default is what the removal on disk will resolve to. Reading it
    here keeps memory and disk agreeing on one number.

    The guard is exact-value, not just key presence: a field whose parsed value
    differs from *old_default* was clamped or coerced on the way in, and replacing
    that would discard the loader's own correction rather than a stale default.
    """
    section, _, field = dotted_key.partition(".")
    target = getattr(cfg, section, None)
    if target is None:
        return
    fields_map = getattr(type(target), "__dataclass_fields__", {})
    spec = fields_map.get(field)
    if spec is None or getattr(target, field, None) != old_default:
        return
    live_default = spec.default
    if live_default is MISSING:
        return
    setattr(target, field, live_default)


# Keys already warned about in this process. The gateway loads config repeatedly
# and a superseded default is per-install information, not per-load, so it is
# said once; ``doctor`` is the surface that renders it again on demand.
_REPORTED_SUPERSEDED_KEYS: set[str] = set()


def _report_superseded_defaults(base_data: dict, *, skip: set[str] | None = None) -> None:
    """Warn once when stored base values still hold a superseded default.

    *base_data* is the ``config.json`` document as read, BEFORE the
    ``config.local.json`` overlay is merged over it. Reporting on the base is the
    point: the overlay is a separate user-owned file whose value is the operator's
    live choice, so it neither proves nor disproves what the base has materialized.

    Reads only. This deliberately does NOT correct the value -- for a key that also
    has a documented escape hatch, a stored old default and a deliberate opt-out
    are the same bytes on disk, so a rewrite cannot correct one without overriding
    the other. Telling the operator is the part that can be done without guessing.

    ONE line naming every drifted key, not one line per key. The registry is
    append-only, so a per-key line means the terminal noise on a long-lived install
    grows with every default the project ever changes -- and it lands on every
    short-lived ``kirocrew`` invocation, where the once-per-process guard below
    buys nothing because there the process IS the invocation. The per-key detail
    belongs on the surface the operator asked for: ``kirocrew config defaults``,
    and ``doctor``. It is also emitted at debug here, so a gateway run with
    ``-vv`` still carries the full text in its own log. The one exception is the
    note of a row marked ``meaning_moved``: its stored value now selects a
    different behaviour from the one an operator who chose it got, so the line
    carries that note for someone who would otherwise ``--keep`` it unread.

    Keys already named in this process are not repeated, so a gateway that loads
    config many times says it once. An acknowledged key is not reported at all --
    ``superseded_default_drift`` filters it -- which is what makes this line
    answerable instead of permanent.

    *skip* names the keys this load is ADOPTING. They are excluded because the line
    tells the operator to run a command, and pointing them at a command for a key
    that is being fixed in the same load is worse than saying nothing: by the time
    they read it the key is already gone from the file.
    """
    skipped = skip or set()
    drifted = [
        e
        for e in superseded_default_drift(base_data)
        if e.dotted_key not in _REPORTED_SUPERSEDED_KEYS and e.dotted_key not in skipped
    ]
    if not drifted:
        return
    for entry in drifted:
        _REPORTED_SUPERSEDED_KEYS.add(entry.dotted_key)
        logger.debug("Superseded default in stored config: %s", drift_summary(entry))
    # Only a moved MEANING earns line space: an operator who never opens 'config
    # defaults' must still read what the stored value selects now before choosing
    # '--keep'. Every other note stays on the per-key surfaces.
    notes = "".join(f" {e.dotted_key}: {e.note}." for e in drifted if e.meaning_moved and e.note)
    logger.warning(
        "%d stored config value(s) still hold a superseded default: %s. "
        "Run 'kirocrew config defaults' to see each one, '--adopt' to take the "
        "current defaults, or '--keep' to affirm yours and stop this notice.%s",
        len(drifted),
        ", ".join(e.dotted_key for e in drifted),
        notes,
    )
